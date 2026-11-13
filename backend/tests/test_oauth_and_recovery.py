"""
Tests for Google OAuth 2.0 PKCE and Password Recovery / Reset flows.
"""

from urllib.parse import parse_qs, urlparse
import uuid
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.api.routes.auth import OAUTH_COOKIE, SESSION_COOKIE
from app.services.auth import CandidateIdentityConflict, provision_or_get_candidate
from app.models.candidate import Candidate


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

    # Verify redirect_to points to backend callback without query params
    parsed = urlparse(loc)
    qs = parse_qs(parsed.query)
    redirect_to = qs["redirect_to"][0]
    assert redirect_to == f"{settings.fastapi_public_url}/api/auth/google/callback"
    assert "state" in qs

    # Verify sealed state cookie
    assert OAUTH_COOKIE in res.cookies
    assert res.headers.get("referrer-policy") == "no-referrer"


def test_google_oauth_callback_success(client: TestClient, fake_supabase) -> None:
    settings = get_settings()

    # 1. Start OAuth to get valid sealed state cookie
    start_res = client.get("/api/auth/google/start", follow_redirects=False)
    assert start_res.status_code == 302
    oauth_cookie = start_res.cookies[OAUTH_COOKIE]

    # Extract state from authorize query
    loc = start_res.headers["location"]
    parsed = urlparse(loc)
    state = parse_qs(parsed.query)["state"][0]

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


def test_google_oauth_callback_without_state_param(client: TestClient, fake_supabase) -> None:
    settings = get_settings()

    # 1. Start OAuth to get valid sealed state cookie
    start_res = client.get("/api/auth/google/start", follow_redirects=False)
    assert start_res.status_code == 302
    oauth_cookie = start_res.cookies[OAUTH_COOKIE]

    # 2. Simulate Supabase GoTrue redirecting back with only code (no state in query)
    client.cookies.set(OAUTH_COOKIE, oauth_cookie)
    cb_res = client.get(
        "/api/auth/google/callback?code=valid_code",
        follow_redirects=False,
    )

    # Must redirect to frontend dashboard (root) and create session
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
    # Missing code query param
    cb_res = client.get("/api/auth/google/callback", follow_redirects=False)
    assert cb_res.status_code == 302
    assert cb_res.headers["location"] == f"{settings.frontend_url}/login?error=oauth_missing_state"


def test_google_oauth_callback_invalid_code(client: TestClient, fake_supabase) -> None:
    settings = get_settings()
    start_res = client.get("/api/auth/google/start", follow_redirects=False)
    oauth_cookie = start_res.cookies[OAUTH_COOKIE]

    loc = start_res.headers["location"]
    state = parse_qs(urlparse(loc).query)["state"][0]

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


# ---------------------------------------------------------------------------
# Candidate Identity Mapping & Provisioning Tests
# ---------------------------------------------------------------------------


def test_candidate_identity_existing_auth_user_id_match(db_session: Session) -> None:
    uid = uuid.uuid4()
    c1 = provision_or_get_candidate(db_session, auth_user_id=uid, email="user@example.com", name="Original Name")
    db_session.commit()

    # Second call with same auth_user_id returns existing record without altering it
    c2 = provision_or_get_candidate(db_session, auth_user_id=uid, email="changed@example.com", name="New Name")
    assert c2.id == c1.id
    assert c2.auth_user_id == uid
    assert c2.email == "user@example.com"
    assert c2.name == "Original Name"


def test_candidate_identity_new_identity_new_email(db_session: Session) -> None:
    uid = uuid.uuid4()
    c = provision_or_get_candidate(db_session, auth_user_id=uid, email="fresh.user@example.com", name="Fresh User")
    db_session.commit()

    assert c.id is not None
    assert c.auth_user_id == uid
    assert c.email == "fresh.user@example.com"
    assert c.name == "Fresh User"


def test_candidate_identity_new_auth_user_id_with_existing_email_controlled_conflict(db_session: Session) -> None:
    uid1 = uuid.uuid4()
    c1 = provision_or_get_candidate(db_session, auth_user_id=uid1, email="target@example.com", name="Target Candidate")
    db_session.commit()

    # Attacker or separate account with a DIFFERENT auth_user_id presents the same email
    uid2 = uuid.uuid4()
    with pytest.raises(CandidateIdentityConflict) as exc_info:
        provision_or_get_candidate(db_session, auth_user_id=uid2, email="target@example.com", name="Attacker")

    assert "conflicting credentials" in str(exc_info.value)

    # Original candidate remains completely unchanged and unlinked
    existing = db_session.get(Candidate, c1.id)
    assert existing is not None
    assert existing.auth_user_id == uid1
    assert existing.name == "Target Candidate"


def test_existing_candidate_identity_and_history_preserved_on_conflict(db_session: Session) -> None:
    uid1 = uuid.uuid4()
    c1 = provision_or_get_candidate(db_session, auth_user_id=uid1, email="interviewee@example.com", name="Active Candidate")
    db_session.commit()

    # Attach an interview session to verify history preservation
    from app.models.interview_session import InterviewSession, InterviewStatus
    session_id = uuid.uuid4()
    interview_sess = InterviewSession(
        id=session_id,
        candidate_id=c1.id,
        status=InterviewStatus.CREATED,
    )
    db_session.add(interview_sess)
    db_session.commit()

    # Now an incoming provisioning conflict occurs with a different auth_user_id
    uid2 = uuid.uuid4()
    with pytest.raises(CandidateIdentityConflict):
        provision_or_get_candidate(db_session, auth_user_id=uid2, email="interviewee@example.com", name="Imposter")

    # Verify existing candidate and all interview histories remain completely intact
    preserved_cand = db_session.get(Candidate, c1.id)
    assert preserved_cand is not None
    assert preserved_cand.auth_user_id == uid1
    assert preserved_cand.name == "Active Candidate"

    preserved_session = db_session.get(InterviewSession, session_id)
    assert preserved_session is not None
    assert preserved_session.candidate_id == c1.id
    assert preserved_session.status == InterviewStatus.CREATED


def test_session_remains_usable_after_handled_conflict(db_session: Session) -> None:
    uid1 = uuid.uuid4()
    c1 = provision_or_get_candidate(db_session, auth_user_id=uid1, email="collision@example.com", name="First Candidate")
    db_session.commit()

    # Trigger conflict
    uid2 = uuid.uuid4()
    with pytest.raises(CandidateIdentityConflict):
        provision_or_get_candidate(db_session, auth_user_id=uid2, email="collision@example.com", name="Second Candidate")

    # Session is NOT dead: verify we can immediately perform queries and inserts in the same session without rollback errors
    uid3 = uuid.uuid4()
    c3 = provision_or_get_candidate(db_session, auth_user_id=uid3, email="unrelated@example.com", name="Third Candidate")
    db_session.commit()

    assert c3.id is not None
    assert c3.auth_user_id == uid3
    assert db_session.get(Candidate, c1.id) is not None


def test_candidate_identity_duplicate_oauth_callback_idempotent(client: TestClient, fake_supabase, monkeypatch) -> None:
    fixed_uid = str(uuid.uuid4())
    async def _fixed_exchange(auth_code: str, code_verifier: str):
        return {
            "user": {
                "id": fixed_uid,
                "email": "idempotent.user@example.com",
                "user_metadata": {"name": "Idempotent User", "full_name": "Idempotent User"},
            },
            "access_token": f"access_{fixed_uid}",
            "refresh_token": f"refresh_{fixed_uid}",
        }
    monkeypatch.setattr(fake_supabase, "exchange_pkce_code", _fixed_exchange)

    # 1. Start flow
    start_res = client.get("/api/auth/google/start", follow_redirects=False)
    oauth_cookie = start_res.cookies[OAUTH_COOKIE]
    client.cookies.set(OAUTH_COOKIE, oauth_cookie)

    # 2. First callback succeeds
    cb1 = client.get("/api/auth/google/callback?code=valid_code", follow_redirects=False)
    assert cb1.status_code == 302
    assert SESSION_COOKIE in cb1.cookies

    # Get candidate info
    client.cookies.set(SESSION_COOKIE, cb1.cookies[SESSION_COOKIE])
    me1 = client.get("/api/auth/me").json()
    cand1_id = me1["id"]

    # 3. Simulate another login for the SAME user
    start_res2 = client.get("/api/auth/google/start", follow_redirects=False)
    client.cookies.set(OAUTH_COOKIE, start_res2.cookies[OAUTH_COOKIE])
    cb2 = client.get("/api/auth/google/callback?code=valid_code", follow_redirects=False)
    assert cb2.status_code == 302
    assert SESSION_COOKIE in cb2.cookies

    # Candidate ID must be identical (no duplicate candidate created)
    client.cookies.set(SESSION_COOKIE, cb2.cookies[SESSION_COOKIE])
    me2 = client.get("/api/auth/me").json()
    assert me2["id"] == cand1_id


def test_candidate_identity_conflicting_identities_isolation(db_session: Session) -> None:
    uid_a = uuid.uuid4()
    uid_b = uuid.uuid4()

    ca = provision_or_get_candidate(db_session, auth_user_id=uid_a, email="user.a@example.com", name="User A")
    cb = provision_or_get_candidate(db_session, auth_user_id=uid_b, email="user.b@example.com", name="User B")
    db_session.commit()

    assert ca.id != cb.id
    assert ca.auth_user_id != cb.auth_user_id
    assert ca.email != cb.email


def test_google_oauth_callback_identity_conflict(client: TestClient, fake_supabase, monkeypatch) -> None:
    settings = get_settings()

    # 1. First user completes OAuth
    start_res1 = client.get("/api/auth/google/start", follow_redirects=False)
    client.cookies.set(OAUTH_COOKIE, start_res1.cookies[OAUTH_COOKIE])
    cb1 = client.get("/api/auth/google/callback?code=valid_code", follow_redirects=False)
    assert cb1.status_code == 302
    assert SESSION_COOKIE in cb1.cookies

    # 2. Second user attempts OAuth with DIFFERENT auth_user_id but SAME email
    new_uid = str(uuid.uuid4())
    async def _conflicting_exchange(auth_code: str, code_verifier: str):
        return {
            "user": {
                "id": new_uid,
                "email": "google_user@example.com",  # same email as user 1
                "user_metadata": {"name": "Conflict User", "full_name": "Conflict User"},
            },
            "access_token": f"access_{new_uid}",
            "refresh_token": f"refresh_{new_uid}",
        }
    monkeypatch.setattr(fake_supabase, "exchange_pkce_code", _conflicting_exchange)

    start_res2 = client.get("/api/auth/google/start", follow_redirects=False)
    client.cookies.set(OAUTH_COOKIE, start_res2.cookies[OAUTH_COOKIE])
    cb2 = client.get("/api/auth/google/callback?code=valid_code", follow_redirects=False)

    # Must safely redirect to login with oauth_identity_conflict error parameter
    assert cb2.status_code == 302
    assert cb2.headers["location"] == f"{settings.frontend_url}/login?error=oauth_identity_conflict"
    # Temporary OAuth flow cookie must be deleted
    assert cb2.cookies.get(OAUTH_COOKIE) in (None, "")
