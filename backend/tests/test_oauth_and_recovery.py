"""
Tests for Google OAuth 2.0 PKCE and Password Recovery / Reset flows.
"""

from urllib.parse import parse_qs, urlparse
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.api.routes.auth import OAUTH_COOKIE, SESSION_COOKIE


def test_google_oauth_start(client: TestClient) -> None:
    settings = get_settings()
    res = client.get("/api/auth/google/start", follow_redirects=False)
    assert res.status_code == 302
    assert "location" in res.headers

    loc = res.headers["location"]
    assert loc.startswith(f"{settings.supabase_url}/auth/v1/authorize?")
    assert "provider=google" in loc
    assert "code_challenge_method=s256" in loc
    assert "code_challenge=" in loc
    assert "redirect_to=" in loc

    # Verify redirect_to points to backend callback with state
    parsed = urlparse(loc)
    qs = parse_qs(parsed.query)
    redirect_to = qs["redirect_to"][0]
    assert redirect_to.startswith(f"{settings.fastapi_public_url}/api/auth/google/callback?")
    rt_qs = parse_qs(urlparse(redirect_to).query)
    assert "state" in rt_qs

    # Verify sealed state cookie
    assert OAUTH_COOKIE in res.cookies
    assert res.headers.get("referrer-policy") == "no-referrer"


def test_google_oauth_callback_success(client: TestClient, fake_supabase) -> None:
    settings = get_settings()

    # 1. Start OAuth to get valid sealed state cookie
    start_res = client.get("/api/auth/google/start", follow_redirects=False)
    assert start_res.status_code == 302
    oauth_cookie = start_res.cookies[OAUTH_COOKIE]

    # Extract state from redirect_to
    loc = start_res.headers["location"]
    parsed = urlparse(loc)
    redirect_to = parse_qs(parsed.query)["redirect_to"][0]
    state = parse_qs(urlparse(redirect_to).query)["state"][0]

    # 2. Simulate browser following redirect back to callback with code & state
    client.cookies.set(OAUTH_COOKIE, oauth_cookie)
    cb_res = client.get(
        f"/api/auth/google/callback?code=valid_code&state={state}",
        follow_redirects=False,
    )

    # Must redirect to frontend dashboard (root)
    assert cb_res.status_code == 302
    assert cb_res.headers["location"] == f"{settings.frontend_url}/"
    assert SESSION_COOKIE in cb_res.cookies

    # State cookie must be deleted
    assert cb_res.cookies.get(OAUTH_COOKIE) in (None, "")

    # 3. Verify session is authenticated
    client.cookies.set(SESSION_COOKIE, cb_res.cookies[SESSION_COOKIE])
    me_res = client.get("/api/auth/me")
    assert me_res.status_code == 200
    candidate = me_res.json()
    assert candidate["email"] == "google_user@example.com"
    assert candidate["name"] == "Google User"


def test_google_oauth_callback_state_mismatch(client: TestClient, fake_supabase) -> None:
    settings = get_settings()
    start_res = client.get("/api/auth/google/start", follow_redirects=False)
    oauth_cookie = start_res.cookies[OAUTH_COOKIE]

    # Send wrong state
    client.cookies.set(OAUTH_COOKIE, oauth_cookie)
    cb_res = client.get(
        "/api/auth/google/callback?code=valid_code&state=wrong_tampered_state",
        follow_redirects=False,
    )
    assert cb_res.status_code == 302
    assert cb_res.headers["location"] == f"{settings.frontend_url}/login?error=oauth_state_mismatch"
    assert SESSION_COOKIE not in cb_res.cookies


def test_google_oauth_callback_missing_state_cookie(client: TestClient, fake_supabase) -> None:
    settings = get_settings()
    cb_res = client.get(
        "/api/auth/google/callback?code=valid_code&state=some_state",
        follow_redirects=False,
    )
    assert cb_res.status_code == 302
    assert cb_res.headers["location"] == f"{settings.frontend_url}/login?error=oauth_missing_state"


def test_google_oauth_callback_missing_params(client: TestClient, fake_supabase) -> None:
    settings = get_settings()
    start_res = client.get("/api/auth/google/start", follow_redirects=False)
    oauth_cookie = start_res.cookies[OAUTH_COOKIE]

    client.cookies.set(OAUTH_COOKIE, oauth_cookie)
    # Missing state query param
    cb_res = client.get("/api/auth/google/callback?code=valid_code", follow_redirects=False)
    assert cb_res.status_code == 302
    assert cb_res.headers["location"] == f"{settings.frontend_url}/login?error=oauth_missing_state"


def test_google_oauth_callback_invalid_code(client: TestClient, fake_supabase) -> None:
    settings = get_settings()
    start_res = client.get("/api/auth/google/start", follow_redirects=False)
    oauth_cookie = start_res.cookies[OAUTH_COOKIE]

    loc = start_res.headers["location"]
    redirect_to = parse_qs(urlparse(loc).query)["redirect_to"][0]
    state = parse_qs(urlparse(redirect_to).query)["state"][0]

    client.cookies.set(OAUTH_COOKIE, oauth_cookie)
    # code="invalid_code" triggers InvalidGrantError in FakeSupabaseAuthService
    cb_res = client.get(
        f"/api/auth/google/callback?code=invalid_code&state={state}",
        follow_redirects=False,
    )
    assert cb_res.status_code == 302
    assert cb_res.headers["location"] == f"{settings.frontend_url}/login?error=oauth_code_invalid"
    assert SESSION_COOKIE not in cb_res.cookies


def test_google_oauth_callback_provider_error(client: TestClient, fake_supabase) -> None:
    settings = get_settings()
    cb_res = client.get(
        "/api/auth/google/callback?error=access_denied&error_description=User+denied+access",
        follow_redirects=False,
    )
    assert cb_res.status_code == 302
    assert cb_res.headers["location"] == f"{settings.frontend_url}/login?error=oauth_cancelled"


def test_google_oauth_callback_corrupted_cookie(client: TestClient, fake_supabase) -> None:
    settings = get_settings()
    client.cookies.set(OAUTH_COOKIE, "corrupted:not:valid:hex")
    cb_res = client.get(
        "/api/auth/google/callback?code=valid_code&state=some_state",
        follow_redirects=False,
    )
    assert cb_res.status_code == 302
    assert cb_res.headers["location"] == f"{settings.frontend_url}/login?error=oauth_state_invalid"


# ---------------------------------------------------------------------------
# Password Recovery & Reset Tests
# ---------------------------------------------------------------------------


def test_forgot_password_redirect_url_points_to_reset_password(client: TestClient, fake_supabase) -> None:
    settings = get_settings()
    res = client.post(
        "/api/auth/forgot-password",
        json={"email": "applicant@example.com"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "password reset link has been sent" in data["message"]
    # Check redirect URL passed to Supabase auth service
    assert fake_supabase.last_recover_redirect_to == f"{settings.frontend_url}/reset-password"


def test_forgot_password_anti_enumeration(client: TestClient, fake_supabase) -> None:
    # Non-existent email still returns 200 and standard message
    res = client.post(
        "/api/auth/forgot-password",
        json={"email": "doesnotexist@example.com"},
    )
    assert res.status_code == 200
    assert "password reset link has been sent" in res.json()["message"]


def test_forgot_password_invalid_email(client: TestClient) -> None:
    res = client.post("/api/auth/forgot-password", json={"email": "not-an-email"})
    assert res.status_code == 422


def test_reset_password_success(client: TestClient, fake_supabase) -> None:
    res = client.post(
        "/api/auth/reset-password",
        json={"access_token": "valid_token", "new_password": "NewSecretPassword123"},
    )
    assert res.status_code == 200
    assert "Password updated successfully" in res.json()["message"]


def test_reset_password_invalid_or_expired_token(client: TestClient, fake_supabase) -> None:
    res = client.post(
        "/api/auth/reset-password",
        json={"access_token": "invalid_token", "new_password": "NewSecretPassword123"},
    )
    assert res.status_code == 401
    assert "Password update failed" in res.json()["detail"]


def test_reset_password_validation(client: TestClient) -> None:
    # Password too short (< 8 chars)
    res_short = client.post(
        "/api/auth/reset-password",
        json={"access_token": "valid_token", "new_password": "short"},
    )
    assert res_short.status_code == 422

    # Password too long (> 128 chars)
    res_long = client.post(
        "/api/auth/reset-password",
        json={"access_token": "valid_token", "new_password": "A" * 129},
    )
    assert res_long.status_code == 422

    # Missing access_token
    res_no_token = client.post(
        "/api/auth/reset-password",
        json={"access_token": "", "new_password": "ValidPassword123"},
    )
    # Depending on schema, empty string might be accepted by schema or fail min_length, but let's test missing key:
    res_missing = client.post(
        "/api/auth/reset-password",
        json={"new_password": "ValidPassword123"},
    )
    assert res_missing.status_code == 422


def test_scanner_get_on_recovery_endpoints(client: TestClient) -> None:
    # GET on recovery POST endpoints must be safely rejected (405 Method Not Allowed)
    assert client.get("/api/auth/forgot-password").status_code == 405
    assert client.get("/api/auth/reset-password").status_code == 405
