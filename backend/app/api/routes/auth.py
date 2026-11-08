"""FastAPI Authentication Router (BFF Pattern with Supabase Auth).

Provides:
- Email/Password Registration (decoupled provisioning post-verification)
- Email/Password Login (opaque session cookie, encrypted refresh token)
- Email Verification Callback
- Google OAuth 2.0 PKCE Authorization & Callback
- Session Logout & Revocation
- Password Recovery Request & Password Reset
- Session Identity (/api/auth/me)
"""

import hashlib
import html
import hmac
import logging
import re
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import (
    SESSION_COOKIE,
    clear_session_cookie,
    current_candidate,
    set_session_cookie,
)
from app.core.config import Settings, get_settings
from app.core.security import (
    DecryptionError,
    generate_oauth_state,
    generate_pkce_challenge,
    generate_pkce_verifier,
    seal_oauth_state,
    unseal_oauth_state,
)
from app.db.session import get_db
from app.models import Candidate
from app.schemas.candidate import (
    CandidateResponse,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    RegisterResponse,
    ResendConfirmationRequest,
    ResetPasswordRequest,
)
from app.services.auth import (
    end_session,
    provision_or_get_candidate,
    rotate_session_refresh_token,
    session_candidate,
    should_refresh_session,
    start_session,
)
from app.services.limits import (
    FORGOT_PASSWORD,
    LOGIN,
    REGISTER,
    RESEND_CONFIRMATION,
    Limit,
    LimitReached,
    consume,
    network_key,
)
from app.services.supabase_auth_service import (
    EmailNotConfirmedError,
    InvalidCredentialsError,
    InvalidGrantError,
    SupabaseAuthError,
    SupabaseAuthService,
    UserAlreadyExistsError,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

OAUTH_COOKIE = "oauth_session_flow"
BAD_CREDENTIALS = "Email or password is incorrect."
TOKEN_HASH_PATTERN = re.compile(r"^[a-zA-Z0-9_.\-@]{1,256}$")
VERIFY_TYPES = frozenset({"signup", "email"})
THROTTLED = {
    REGISTER.scope: "Too many accounts were created from your network. Please try again in {when}.",
    LOGIN.scope: "Too many sign-in attempts from your network. Please try again in {when}.",
    FORGOT_PASSWORD.scope: "Too many password recovery attempts from your network. Please try again in {when}.",
    RESEND_CONFIRMATION.scope: "Too many confirmation emails requested from your network. Please try again in {when}.",
}


def _throttle(db: Session, request: Request, limit: Limit) -> None:
    """Sliding-window network throttle before external calls."""
    try:
        consume(db, limit, network_key(request.client.host if request.client else None))
    except LimitReached as exc:
        db.rollback()
        minutes = max(1, -(-exc.retry_after // 60))
        when = "1 minute" if minutes == 1 else f"{minutes} minutes"
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            THROTTLED[limit.scope].format(when=when),
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    db.commit()


def get_supabase_service() -> SupabaseAuthService:
    return SupabaseAuthService()


def generate_verify_csrf_token(token_hash: str, type_val: str, key: bytes) -> str:
    """Cryptographic HMAC signature binding verification token and action type to prevent CSRF."""
    msg = f"verify:{token_hash}:{type_val}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def check_verify_csrf_token(
    token_hash: str, type_val: str, csrf_token: str, settings: Settings
) -> bool:
    """Verifies the HMAC signature against active or fallback session encryption key."""
    expected = generate_verify_csrf_token(token_hash, type_val, settings.session_key_bytes)
    if hmac.compare_digest(expected, csrf_token):
        return True
    if settings.session_fallback_key_bytes:
        fallback = generate_verify_csrf_token(token_hash, type_val, settings.session_fallback_key_bytes)
        if hmac.compare_digest(fallback, csrf_token):
            return True
    return False


# ---------------------------------------------------------------------------
# 1. Registration & Email Verification
# ---------------------------------------------------------------------------

@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    supabase_auth: SupabaseAuthService = Depends(get_supabase_service),
) -> RegisterResponse:
    """
    Registers a new user in Supabase Auth.
    Candidate record is NOT created until email verification or first post-verification login.
    No session cookie is issued.
    """
    _throttle(db, request, REGISTER)
    try:
        redirect_to = f"{settings.fastapi_public_url}/api/auth/verify"
        await supabase_auth.signup(
            email=str(body.email),
            password=body.password,
            name=str(body.name),
            redirect_to=redirect_to,
        )
    except UserAlreadyExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.message) from None
    except SupabaseAuthError as exc:
        logger.error("Supabase signup error: %s", exc)
        raise HTTPException(exc.status_code, exc.message) from None

    return RegisterResponse(
        email=str(body.email),
        verification_required=True,
        message="Verification email sent. Please check your inbox to activate your account.",
    )


@router.post("/resend-confirmation", response_model=MessageResponse, status_code=status.HTTP_200_OK)
async def resend_confirmation(
    body: ResendConfirmationRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    supabase_auth: SupabaseAuthService = Depends(get_supabase_service),
) -> MessageResponse:
    """
    Safely resends account confirmation email with rate limiting.
    Always returns 200 OK to prevent account enumeration.
    """
    _throttle(db, request, RESEND_CONFIRMATION)
    redirect_to = f"{settings.fastapi_public_url}/api/auth/verify"
    try:
        await supabase_auth.resend_confirmation(email=str(body.email), redirect_to=redirect_to)
    except Exception as exc:
        logger.warning("Resend confirmation request failed: %s", exc)

    return MessageResponse(
        message="If an unconfirmed account exists for this email, a new confirmation link has been sent."
    )


@router.get("/verify", response_class=HTMLResponse)
async def verify_email_page(
    token_hash: str | None = Query(None, description="Supabase verification token hash"),
    type: str = Query("signup", description="Verification type"),
    settings: Settings = Depends(get_settings),
) -> Response:
    """
    Step 1 of email verification: GET-prefetch resistant confirmation page.
    Renders a lightweight, self-contained confirmation page without consuming the Supabase token.
    Provides resistance against standard automated email security scanner GET prefetching.

    Security boundary:
    - Protects against scanners that issue background GET requests without executing form submission.
    - Scanners capable of executing JavaScript and submitting HTML forms may still interact with this page.
    """
    def _err_redirect() -> Response:
        resp = RedirectResponse(
            url=f"{settings.frontend_url}/login?error=verification_expired",
            status_code=status.HTTP_302_FOUND,
        )
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    if not token_hash or not token_hash.strip():
        logger.warning("Email verification GET called without token_hash")
        return _err_redirect()

    clean_token_hash = token_hash.strip()
    clean_type = type.strip() if type else "signup"

    if clean_type not in VERIFY_TYPES:
        logger.warning("Email verification GET called with invalid type")
        return _err_redirect()

    if not TOKEN_HASH_PATTERN.match(clean_token_hash):
        logger.warning("Email verification GET called with invalid token_hash format")
        return _err_redirect()

    csrf_token = generate_verify_csrf_token(clean_token_hash, clean_type, settings.session_key_bytes)
    escaped_hash = html.escape(clean_token_hash, quote=True)
    escaped_type = html.escape(clean_type, quote=True)
    escaped_csrf = html.escape(csrf_token, quote=True)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="referrer" content="no-referrer">
  <meta name="robots" content="noindex, nofollow, noarchive">
  <title>Confirm Your Email — AI Interview Coach</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background-color: #0b0f19;
      color: #f1f5f9;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 1.5rem;
    }}
    .card {{
      background-color: #151d2f;
      border: 1px solid #23304a;
      border-radius: 1rem;
      max-width: 28rem;
      width: 100%;
      padding: 2.25rem;
      box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.4), 0 10px 10px -5px rgba(0, 0, 0, 0.3);
      text-align: center;
    }}
    .icon-wrap {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 3.5rem;
      height: 3.5rem;
      border-radius: 50%;
      background: rgba(99, 102, 241, 0.15);
      color: #818cf8;
      margin-bottom: 1.25rem;
    }}
    .icon-wrap svg {{
      width: 1.75rem;
      height: 1.75rem;
    }}
    h1 {{
      font-size: 1.5rem;
      font-weight: 700;
      margin-bottom: 0.75rem;
      color: #ffffff;
      letter-spacing: -0.025em;
    }}
    p {{
      font-size: 0.95rem;
      line-height: 1.5;
      color: #94a3b8;
      margin-bottom: 1.75rem;
    }}
    .btn {{
      display: inline-block;
      width: 100%;
      padding: 0.875rem 1.25rem;
      font-size: 1rem;
      font-weight: 600;
      color: #ffffff;
      background: linear-gradient(135deg, #4f46e5 0%, #6366f1 100%);
      border: none;
      border-radius: 0.5rem;
      cursor: pointer;
      transition: opacity 0.15s ease, transform 0.1s ease;
      text-decoration: none;
    }}
    .btn:hover {{
      opacity: 0.92;
    }}
    .btn:active {{
      transform: scale(0.99);
    }}
    .btn:disabled {{
      opacity: 0.6;
      cursor: not-allowed;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon-wrap">
      <svg fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"></path>
      </svg>
    </div>
    <h1>Confirm Your Email</h1>
    <p>Click below to verify your email address and activate your AI Interview Coach account.</p>
    <form method="POST" action="/api/auth/verify" id="verify-form">
      <input type="hidden" name="token_hash" value="{escaped_hash}">
      <input type="hidden" name="type" value="{escaped_type}">
      <input type="hidden" name="csrf_token" value="{escaped_csrf}">
      <button type="submit" class="btn" id="confirm-btn">Confirm Email</button>
    </form>
  </div>
  <script>
    document.getElementById("verify-form").addEventListener("submit", function() {{
      var btn = document.getElementById("confirm-btn");
      btn.disabled = true;
      btn.innerText = "Confirming...";
    }});
  </script>
</body>
</html>"""
    resp = HTMLResponse(content=html_content, status_code=status.HTTP_200_OK)
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


@router.post("/verify")
async def verify_email_confirm(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    supabase_auth: SupabaseAuthService = Depends(get_supabase_service),
) -> Response:
    """
    Step 2 of hardened email verification.
    Triggered exclusively by explicit candidate action (POST).
    Consumes the Supabase verification token, provisions candidate, sets HttpOnly session cookie,
    and cleanly redirects to the application dashboard.
    """
    def _err_redirect() -> Response:
        if "application/json" in request.headers.get("accept", "") and "text/html" not in request.headers.get("accept", ""):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"detail": "Verification failed: invalid, expired, or missing token."},
            )
        resp = RedirectResponse(
            url=f"{settings.frontend_url}/login?error=verification_expired",
            status_code=status.HTTP_303_SEE_OTHER,
        )
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    token_hash = None
    type_val = "signup"
    csrf_token = None

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body_data = await request.json()
            if isinstance(body_data, dict):
                token_hash = body_data.get("token_hash")
                type_val = body_data.get("type", "signup")
                csrf_token = body_data.get("csrf_token")
        except Exception:
            pass
    else:
        try:
            form_data = await request.form()
            token_hash = form_data.get("token_hash")
            type_val = form_data.get("type", "signup")
            csrf_token = form_data.get("csrf_token")
        except Exception:
            pass

    if not token_hash:
        token_hash = request.query_params.get("token_hash")
    if not csrf_token:
        csrf_token = request.query_params.get("csrf_token")
    if request.query_params.get("type"):
        type_val = request.query_params.get("type")

    if not token_hash or not str(token_hash).strip():
        logger.warning("Email verification POST called without token_hash")
        return _err_redirect()

    clean_token_hash = str(token_hash).strip()
    clean_type = str(type_val).strip() if type_val else "signup"

    if clean_type not in VERIFY_TYPES:
        logger.warning("Email verification POST called with invalid type")
        return _err_redirect()

    if not TOKEN_HASH_PATTERN.match(clean_token_hash):
        logger.warning("Email verification POST called with invalid token_hash format")
        return _err_redirect()

    if not csrf_token or not check_verify_csrf_token(clean_token_hash, clean_type, str(csrf_token), settings):
        logger.warning("Email verification POST CSRF validation failed")
        return _err_redirect()

    try:
        data = await supabase_auth.verify_email_token(token_hash=clean_token_hash, type_val=clean_type)
        user = data.get("user") or {}
        user_id_str = user.get("id")
        if not user_id_str:
            logger.warning("Verification payload missing user id")
            return _err_redirect()

        user_id = uuid.UUID(str(user_id_str))
        email = user["email"]
        user_meta = user.get("user_metadata") or {}
        name = user_meta.get("name") or user_meta.get("full_name") or "Candidate"

        candidate = provision_or_get_candidate(db, auth_user_id=user_id, email=email, name=name)
        raw_token = start_session(
            db,
            candidate_id=candidate.id,
            auth_user_id=candidate.auth_user_id,
            refresh_token=data.get("refresh_token"),
        )
        db.commit()

        target_url = f"{settings.frontend_url}/interview"
        if "application/json" in request.headers.get("accept", "") and "text/html" not in request.headers.get("accept", ""):
            api_resp = JSONResponse(
                content={"status": "ok", "redirect_url": target_url},
                status_code=status.HTTP_200_OK,
            )
            set_session_cookie(api_resp, raw_token, settings)
            api_resp.headers["Referrer-Policy"] = "no-referrer"
            return api_resp

        redirect_resp = RedirectResponse(
            url=target_url,
            status_code=status.HTTP_303_SEE_OTHER,
        )
        set_session_cookie(redirect_resp, raw_token, settings)
        redirect_resp.headers["Referrer-Policy"] = "no-referrer"
        redirect_resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return redirect_resp
    except (SupabaseAuthError, ValueError, Exception) as exc:
        err_code = getattr(exc, "error_code", None) or type(exc).__name__
        st_code = getattr(exc, "status_code", 400)
        logger.warning("Email verification failed (error_code=%s, status_code=%s)", err_code, st_code)
        return _err_redirect()


# ---------------------------------------------------------------------------
# 2. Email & Password Login
# ---------------------------------------------------------------------------

@router.post("/login", response_model=CandidateResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    supabase_auth: SupabaseAuthService = Depends(get_supabase_service),
) -> Candidate:
    """
    Authenticates candidate via Supabase Auth.
    Idempotently provisions candidate record if needed.
    Issues opaque HttpOnly coach_session cookie with AES-256-GCM encrypted refresh token in DB.
    """
    _throttle(db, request, LOGIN)
    try:
        data = await supabase_auth.login(email=str(body.email), password=body.password)
    except EmailNotConfirmedError as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"detail": exc.message, "verification_required": True},
        ) from None
    except InvalidCredentialsError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, BAD_CREDENTIALS) from None
    except SupabaseAuthError as exc:
        logger.error("Supabase login error: %s", exc)
        raise HTTPException(exc.status_code, exc.message) from None

    user = data.get("user") or {}
    user_id = uuid.UUID(user["id"])
    email = user["email"]
    user_meta = user.get("user_metadata") or {}
    name = user_meta.get("name") or user_meta.get("full_name") or "Candidate"

    candidate = provision_or_get_candidate(db, auth_user_id=user_id, email=email, name=name)
    raw_token = start_session(
        db,
        candidate_id=candidate.id,
        auth_user_id=candidate.auth_user_id,
        refresh_token=data.get("refresh_token"),
    )
    db.commit()

    set_session_cookie(response, raw_token, settings)
    return candidate


# ---------------------------------------------------------------------------
# 3. Google OAuth 2.0 PKCE Flow
# ---------------------------------------------------------------------------

@router.get("/google/start", status_code=status.HTTP_302_FOUND)
def google_oauth_start(
    settings: Settings = Depends(get_settings),
) -> Response:
    """
    Initiates Google OAuth 2.0 PKCE flow.
    Generates code_verifier, code_challenge, and state.
    Seals them into an HttpOnly cookie and redirects browser to Supabase authorize endpoint.
    """
    verifier = generate_pkce_verifier()
    challenge = generate_pkce_challenge(verifier)
    state = generate_oauth_state()
    sealed_cookie_val = seal_oauth_state(state, verifier, settings.session_key_bytes)

    redirect_to = f"{settings.fastapi_public_url}/api/auth/google/callback?state={state}"
    auth_url = (
        f"{settings.supabase_url}/auth/v1/authorize?"
        f"provider=google&code_challenge={challenge}&code_challenge_method=s256&"
        f"redirect_to={quote(redirect_to)}"
    )

    redirect_resp = RedirectResponse(url=auth_url, status_code=status.HTTP_302_FOUND)
    oauth_samesite = "none" if settings.cookie_samesite == "none" else "lax"
    cookie_kwargs: dict[str, object] = {
        "key": OAUTH_COOKIE,
        "value": sealed_cookie_val,
        "path": "/api/auth/google/callback",
        "httponly": True,
        "samesite": oauth_samesite,
        "secure": settings.auth_cookie_secure,
        "max_age": 300,  # 5 minutes
    }
    if settings.cookie_domain:
        cookie_kwargs["domain"] = settings.cookie_domain
    redirect_resp.set_cookie(**cookie_kwargs)
    redirect_resp.headers["Referrer-Policy"] = "no-referrer"
    return redirect_resp


@router.get("/google/callback", status_code=status.HTTP_302_FOUND)
async def google_oauth_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    supabase_auth: SupabaseAuthService = Depends(get_supabase_service),
) -> Response:
    """
    Handles Supabase Google OAuth callback.
    Validates state against sealed cookie, exchanges PKCE code with Supabase,
    provisions candidate record, establishes session, and redirects to application.
    """
    def _error_redirect(err_param: str) -> Response:
        resp = RedirectResponse(
            url=f"{settings.frontend_url}/login?error={err_param}",
            status_code=status.HTTP_302_FOUND,
        )
        del_kwargs: dict[str, object] = {"key": OAUTH_COOKIE, "path": "/api/auth/google/callback"}
        if settings.cookie_domain:
            del_kwargs["domain"] = settings.cookie_domain
        resp.delete_cookie(**del_kwargs)
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    if error:
        logger.info("Google OAuth provider returned error: %s", error)
        return _error_redirect("oauth_cancelled")

    sealed_cookie = request.cookies.get(OAUTH_COOKIE)
    if not sealed_cookie or not code or not state:
        logger.warning("Missing OAuth callback parameters or state cookie")
        return _error_redirect("oauth_missing_state")

    try:
        expected_state, code_verifier = unseal_oauth_state(sealed_cookie, settings.session_key_bytes)
    except DecryptionError as exc:
        logger.warning("Failed to unseal OAuth state cookie: %s", exc)
        return _error_redirect("oauth_state_invalid")

    if not hmac.compare_digest(state, expected_state):
        logger.warning("OAuth state mismatch in callback")
        return _error_redirect("oauth_state_mismatch")

    try:
        data = await supabase_auth.exchange_pkce_code(auth_code=code, code_verifier=code_verifier)
    except InvalidGrantError as exc:
        logger.warning("PKCE code exchange rejected: %s", exc)
        return _error_redirect("oauth_code_invalid")
    except SupabaseAuthError as exc:
        logger.error("Supabase PKCE error: %s", exc)
        return _error_redirect("oauth_exchange_failed")

    user = data.get("user") or {}
    user_id = uuid.UUID(user["id"])
    email = user["email"]
    user_meta = user.get("user_metadata") or {}
    name = user_meta.get("full_name") or user_meta.get("name") or "Candidate"

    candidate = provision_or_get_candidate(db, auth_user_id=user_id, email=email, name=name)
    raw_token = start_session(
        db,
        candidate_id=candidate.id,
        auth_user_id=candidate.auth_user_id,
        refresh_token=data.get("refresh_token"),
    )
    db.commit()

    redirect_resp = RedirectResponse(
        url=f"{settings.frontend_url}/interview",
        status_code=status.HTTP_302_FOUND,
    )
    set_session_cookie(redirect_resp, raw_token, settings)
    del_kwargs: dict[str, object] = {"key": OAUTH_COOKIE, "path": "/api/auth/google/callback"}
    if settings.cookie_domain:
        del_kwargs["domain"] = settings.cookie_domain
    redirect_resp.delete_cookie(**del_kwargs)
    redirect_resp.headers["Referrer-Policy"] = "no-referrer"
    return redirect_resp


# ---------------------------------------------------------------------------
# 4. Session Termination (Logout)
# ---------------------------------------------------------------------------

@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    supabase_auth: SupabaseAuthService = Depends(get_supabase_service),
) -> None:
    """
    Deletes server-side session.
    Commits local revocation first. Triggers remote Supabase logout if possible.
    Clears the coach_session cookie.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        session = end_session(db, token)
        db.commit()
        if session and session.auth_user_id:
            try:
                await supabase_auth.logout(auth_user_id=str(session.auth_user_id))
            except Exception as exc:
                logger.warning("Remote logout failed (non-fatal): %s", exc)

    clear_session_cookie(response, settings)


# ---------------------------------------------------------------------------
# 5. Password Recovery & Reset
# ---------------------------------------------------------------------------

@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    supabase_auth: SupabaseAuthService = Depends(get_supabase_service),
) -> MessageResponse:
    """Triggers a Supabase password recovery email with a secure reset link."""
    _throttle(db, request, FORGOT_PASSWORD)
    redirect_url = f"{settings.frontend_url}/login?tab=reset-password"
    try:
        await supabase_auth.recover_password(str(body.email), redirect_to=redirect_url)
    except Exception as exc:
        logger.warning("Password recovery error: %s", exc)

    # Constant message prevents account enumeration
    return MessageResponse(
        message="If an account exists for this email, a password reset link has been sent."
    )


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(
    body: ResetPasswordRequest,
    supabase_auth: SupabaseAuthService = Depends(get_supabase_service),
) -> MessageResponse:
    """Updates candidate password using a verified recovery access token."""
    try:
        await supabase_auth.update_password(body.access_token, body.new_password)
    except SupabaseAuthError as exc:
        raise HTTPException(exc.status_code, exc.message) from None

    return MessageResponse(
        message="Password updated successfully. Please log in with your new password."
    )


# ---------------------------------------------------------------------------
# 6. Current User Identity & Session Refresh
# ---------------------------------------------------------------------------

@router.post("/refresh", response_model=MessageResponse)
async def refresh_session_endpoint(
    request: Request,
    response: Response,
    candidate: Candidate = Depends(current_candidate),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    supabase_auth: SupabaseAuthService = Depends(get_supabase_service),
) -> MessageResponse:
    """
    Actively rotates the encrypted Supabase refresh token for the authenticated session.
    Guarantees concurrency safety using CAS fencing tokens (refresh_lock_id).
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please log in.")

    success = await rotate_session_refresh_token(db, token, supabase_auth)
    if not success:
        # Check if session was revoked
        cand = session_candidate(db, token)
        if cand is None:
            clear_session_cookie(response, settings)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired or revoked.")
        return MessageResponse(message="Session refresh in-flight or not required.")

    db.commit()
    return MessageResponse(message="Session refreshed successfully.")


@router.get("/me", response_model=CandidateResponse)
async def me(
    request: Request,
    candidate: Candidate = Depends(current_candidate),
    db: Session = Depends(get_db),
    supabase_auth: SupabaseAuthService = Depends(get_supabase_service),
) -> Candidate:
    """Returns candidate identity for the current authenticated BFF session and transparently rotates expiring refresh tokens."""
    token = request.cookies.get(SESSION_COOKIE)
    if token and should_refresh_session(db, token):
        try:
            refreshed = await rotate_session_refresh_token(db, token, supabase_auth)
            if refreshed:
                db.commit()
        except Exception as exc:
            logger.warning("Background session refresh in /me failed (non-fatal): %s", exc)
    return candidate
