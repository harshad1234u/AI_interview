"""Supabase Auth (GoTrue) REST client service.

Communicates with Supabase GoTrue over HTTPS for identity operations:
- Email/Password signup & verification
- Email/Password authentication
- Google OAuth PKCE token exchange
- On-demand refresh token rotation
- User-scoped and administrative global session revocation
- Password recovery request and reset
"""

import logging
from typing import Any, Dict, Optional

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class SupabaseAuthError(Exception):
    """Base exception for Supabase Auth HTTP failures."""
    def __init__(self, message: str, status_code: int = 500, error_code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code


class EmailNotConfirmedError(SupabaseAuthError):
    """Raised when login is attempted before email verification."""
    pass


class InvalidCredentialsError(SupabaseAuthError):
    """Raised when email or password is invalid."""
    pass


class UserAlreadyExistsError(SupabaseAuthError):
    """Raised when registering an email that already exists in Supabase Auth."""
    pass


class InvalidGrantError(SupabaseAuthError):
    """Raised when a refresh token or authorization code is revoked, expired, or invalid."""
    pass


class SupabaseAuthService:
    def __init__(self):
        settings = get_settings()
        self.base_url = settings.supabase_url.rstrip("/")
        self.anon_key = settings.supabase_anon_key
        self.service_role_key = (
            settings.supabase_service_role_key.get_secret_value()
            if settings.supabase_service_role_key
            else None
        )
        self.timeout = httpx.Timeout(10.0, connect=5.0)

    def _anon_headers(self) -> Dict[str, str]:
        headers = {
            "apikey": self.anon_key,
            "Content-Type": "application/json",
        }
        if self.anon_key:
            headers["Authorization"] = f"Bearer {self.anon_key}"
        return headers

    def _service_headers(self) -> Dict[str, str]:
        key = self.service_role_key or self.anon_key
        return {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    async def signup(
        self,
        email: str,
        password: str,
        name: str,
        redirect_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Creates a Supabase Auth identity. Sends confirmation email."""
        url = f"{self.base_url}/auth/v1/signup"
        payload: Dict[str, Any] = {
            "email": email.strip().lower(),
            "password": password,
            "data": {"name": name.strip()},
        }
        params: Dict[str, str] = {}
        if redirect_to:
            payload["redirect_to"] = redirect_to
            params["redirect_to"] = redirect_to
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, params=params, headers=self._anon_headers())
            data = resp.json() if resp.content else {}
            
            if resp.status_code == 200:
                # Supabase returns user object. If identities is empty, user already exists.
                user = data.get("user") or data
                identities = user.get("identities", [])
                if identities is not None and len(identities) == 0:
                    raise UserAlreadyExistsError(
                        "An account with this email already exists.",
                        status_code=409,
                        error_code="user_already_exists"
                    )
                return data
            
            error_msg = data.get("msg") or data.get("error_description") or data.get("message") or resp.text
            error_code = data.get("error_code")
            
            if resp.status_code == 400 and ("already registered" in error_msg.lower() or "already exists" in error_msg.lower()):
                raise UserAlreadyExistsError(error_msg, status_code=409, error_code="user_already_exists")
            
            raise SupabaseAuthError(f"Signup failed: {error_msg}", status_code=resp.status_code, error_code=error_code)

    async def login(self, email: str, password: str) -> Dict[str, Any]:
        """Authenticates with password. Returns tokens and user object."""
        url = f"{self.base_url}/auth/v1/token?grant_type=password"
        payload = {
            "email": email.strip().lower(),
            "password": password,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, headers=self._anon_headers())
            data = resp.json() if resp.content else {}
            
            if resp.status_code == 200:
                return data
            
            error_msg = data.get("error_description") or data.get("msg") or data.get("message") or resp.text
            error_code = data.get("error_code")
            
            if "email not confirmed" in error_msg.lower():
                raise EmailNotConfirmedError(
                    "Email verification required. Please check your inbox.",
                    status_code=403,
                    error_code="email_not_confirmed"
                )
            if resp.status_code in (400, 401) and ("invalid login credentials" in error_msg.lower() or "invalid_grant" in error_msg.lower()):
                raise InvalidCredentialsError(
                    "Invalid email or password.",
                    status_code=401,
                    error_code="invalid_credentials"
                )
            
            raise SupabaseAuthError(f"Login failed: {error_msg}", status_code=resp.status_code, error_code=error_code)

    async def verify_email_token(self, token_hash: str, type_val: str = "signup") -> Dict[str, Any]:
        """Verifies email confirmation token. Returns tokens and user object."""
        url = f"{self.base_url}/auth/v1/verify"
        payload = {
            "token_hash": token_hash,
            "type": type_val,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, headers=self._anon_headers())
            data = resp.json() if resp.content else {}
            
            if resp.status_code == 200:
                return data
            
            error_msg = data.get("msg") or data.get("error_description") or resp.text
            error_code = data.get("error_code")
            raise SupabaseAuthError(f"Email verification failed: {error_msg}", status_code=resp.status_code, error_code=error_code)

    async def exchange_pkce_code(self, auth_code: str, code_verifier: str) -> Dict[str, Any]:
        """Exchanges single-use PKCE auth_code with code_verifier for tokens."""
        url = f"{self.base_url}/auth/v1/token?grant_type=pkce"
        payload = {
            "auth_code": auth_code,
            "code_verifier": code_verifier,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, headers=self._anon_headers())
            data = resp.json() if resp.content else {}
            
            if resp.status_code == 200:
                return data
            
            error_msg = data.get("error_description") or data.get("msg") or resp.text
            error_code = data.get("error_code")
            if resp.status_code in (400, 401):
                raise InvalidGrantError(f"PKCE exchange failed: {error_msg}", status_code=resp.status_code, error_code=error_code)
            raise SupabaseAuthError(f"PKCE exchange failed: {error_msg}", status_code=resp.status_code, error_code=error_code)

    async def refresh_session(self, refresh_token: str) -> Dict[str, Any]:
        """Rotates refresh token and returns fresh tokens."""
        url = f"{self.base_url}/auth/v1/token?grant_type=refresh_token"
        payload = {"refresh_token": refresh_token}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, headers=self._anon_headers())
            data = resp.json() if resp.content else {}
            
            if resp.status_code == 200:
                return data
            
            error_msg = data.get("error_description") or data.get("msg") or resp.text
            error_code = data.get("error_code")
            if resp.status_code in (400, 401):
                raise InvalidGrantError(f"Refresh token invalid: {error_msg}", status_code=401, error_code=error_code)
            raise SupabaseAuthError(f"Token refresh failed: {error_msg}", status_code=resp.status_code, error_code=error_code)

    async def logout(self, access_token: Optional[str] = None, auth_user_id: Optional[str] = None) -> bool:
        """Revokes sessions at Supabase. Supports Admin API and user-scoped logout."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
            # 1. Try Admin API if service role key and auth_user_id exist
            if self.service_role_key and auth_user_id:
                try:
                    admin_url = f"{self.base_url}/auth/v1/admin/users/{auth_user_id}/logout"
                    resp = await client.post(admin_url, headers=self._service_headers())
                    if resp.status_code in (200, 204):
                        return True
                except Exception as exc:
                    logger.warning("Admin logout request failed: %s", exc)

            # 2. Try user-scoped logout if access_token exists
            if access_token:
                try:
                    user_url = f"{self.base_url}/auth/v1/logout?scope=global"
                    headers = {
                        "apikey": self.anon_key,
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    }
                    resp = await client.post(user_url, headers=headers)
                    if resp.status_code in (200, 204):
                        return True
                except Exception as exc:
                    logger.warning("User-scoped logout request failed: %s", exc)

            return False

    async def resend_confirmation(self, email: str, redirect_to: Optional[str] = None) -> bool:
        """Resends signup confirmation email via Supabase GoTrue."""
        url = f"{self.base_url}/auth/v1/resend"
        payload: Dict[str, Any] = {
            "type": "signup",
            "email": email.strip().lower(),
        }
        if redirect_to:
            payload["options"] = {"email_redirect_to": redirect_to}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, headers=self._anon_headers())
            return resp.status_code in (200, 204)

    async def recover_password(self, email: str, redirect_to: str) -> bool:
        """Triggers password recovery email from Supabase."""
        url = f"{self.base_url}/auth/v1/recover"
        payload = {
            "email": email.strip().lower(),
            "redirect_to": redirect_to,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, headers=self._anon_headers())
            return resp.status_code in (200, 204)

    async def update_password(self, access_token: str, new_password: str) -> Dict[str, Any]:
        """Updates user password using an active access token."""
        url = f"{self.base_url}/auth/v1/user"
        headers = {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        payload = {"password": new_password}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.put(url, json=payload, headers=headers)
            data = resp.json() if resp.content else {}
            if resp.status_code == 200:
                return data
            error_msg = data.get("msg") or data.get("error_description") or resp.text
            raise SupabaseAuthError(f"Password update failed: {error_msg}", status_code=resp.status_code)
