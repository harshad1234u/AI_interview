"""In-memory fake SupabaseAuthService for deterministic offline tests."""

import uuid
from typing import Any, Dict, Optional

from app.services.supabase_auth_service import (
    EmailNotConfirmedError,
    InvalidCredentialsError,
    InvalidGrantError,
    SupabaseAuthError,
    UserAlreadyExistsError,
)


class FakeSupabaseAuthService:
    def __init__(self):
        # email -> dict(id, email, password, name, confirmed)
        self.users: Dict[str, Dict[str, Any]] = {}
        # token_hash -> email
        self.verification_tokens: Dict[str, str] = {}
        self.pkce_codes: Dict[str, Dict[str, Any]] = {}
        self.last_redirect_to: Optional[str] = None

    def register_test_user(
        self,
        email: str,
        password: str = "correct horse 1",
        name: str = "Test User",
        confirmed: bool = True,
        user_id: Optional[uuid.UUID] = None,
    ) -> Dict[str, Any]:
        uid = str(user_id or uuid.uuid4())
        u = {
            "id": uid,
            "email": email.strip().lower(),
            "password": password,
            "name": name.strip(),
            "confirmed": confirmed,
        }
        self.users[u["email"]] = u
        return u

    async def signup(
        self,
        email: str,
        password: str,
        name: str,
        redirect_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.last_redirect_to = redirect_to
        clean_email = email.strip().lower()
        if clean_email in self.users:
            raise UserAlreadyExistsError(
                "An account with this email already exists.",
                status_code=409,
                error_code="user_already_exists",
            )
        uid = str(uuid.uuid4())
        self.users[clean_email] = {
            "id": uid,
            "email": clean_email,
            "password": password,
            "name": name.strip(),
            "confirmed": False,
        }
        # generate a token for verification
        tok = f"verify_{clean_email}"
        self.verification_tokens[tok] = clean_email
        return {
            "user": {
                "id": uid,
                "email": clean_email,
                "identities": [{"id": uid}],
                "user_metadata": {"name": name.strip()},
            }
        }

    async def verify_email_token(self, token_hash: str, type_val: str = "signup") -> Dict[str, Any]:
        email = self.verification_tokens.pop(token_hash, None)
        if not email or email not in self.users:
            raise SupabaseAuthError("Email verification failed: invalid or expired token", status_code=400)
        user = self.users[email]
        user["confirmed"] = True
        return {
            "user": {
                "id": user["id"],
                "email": user["email"],
                "user_metadata": {"name": user["name"]},
            },
            "access_token": f"access_{user['id']}",
            "refresh_token": f"refresh_{user['id']}",
        }

    async def login(self, email: str, password: str) -> Dict[str, Any]:
        clean_email = email.strip().lower()
        user = self.users.get(clean_email)
        if not user or user["password"] != password:
            raise InvalidCredentialsError("Invalid email or password.", status_code=401)
        if not user["confirmed"]:
            raise EmailNotConfirmedError(
                "Email verification required. Please check your inbox.",
                status_code=403,
                error_code="email_not_confirmed",
            )
        return {
            "user": {
                "id": user["id"],
                "email": user["email"],
                "user_metadata": {"name": user["name"]},
            },
            "access_token": f"access_{user['id']}",
            "refresh_token": f"refresh_{user['id']}",
        }

    async def exchange_pkce_code(self, auth_code: str, code_verifier: str) -> Dict[str, Any]:
        if auth_code == "invalid_code":
            raise InvalidGrantError("PKCE exchange failed: code expired", status_code=400)
        uid = str(uuid.uuid4())
        return {
            "user": {
                "id": uid,
                "email": "google_user@example.com",
                "user_metadata": {"name": "Google User", "full_name": "Google User"},
            },
            "access_token": f"access_{uid}",
            "refresh_token": f"refresh_{uid}",
        }

    async def refresh_session(self, refresh_token: str) -> Dict[str, Any]:
        if refresh_token == "invalid_refresh_token":
            raise InvalidGrantError("Refresh token invalid", status_code=401)
        return {
            "access_token": "new_access_token",
            "refresh_token": f"rotated_{refresh_token}",
        }

    async def logout(self, access_token: Optional[str] = None, auth_user_id: Optional[str] = None) -> bool:
        return True

    async def recover_password(self, email: str, redirect_to: str) -> bool:
        return True

    async def update_password(self, access_token: str, new_password: str) -> Dict[str, Any]:
        if access_token == "invalid_token":
            raise SupabaseAuthError("Password update failed: invalid token", status_code=401)
        return {"user": {"id": str(uuid.uuid4())}}
