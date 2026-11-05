"""Phase 12: authentication, sessions, lockout, CSRF, and candidate isolation (IDOR)."""

import base64
import hashlib
import logging
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.ai.providers import get_model_gateway
from app.api.deps import SESSION_COOKIE, current_candidate
from app.api.routes import (
    auth as auth_routes,
    candidates,
    health,
    interview_sessions,
    manual_facts,
    resumes,
)
from app.core import security
from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.main import app, create_app
from app.models import AuthSession, Candidate
from app.models.resume import FactCategory as FC, FactStatus
from app.services import auth
from tests.auth_helpers import api_client, make_candidate
from tests.test_evaluation_api import evaluate
from tests.test_interview_plan_api import SETTINGS, add_facts
from tests.test_interview_turn_api import act, ans, nxt
from tests.test_report import post_report

PASSWORD = "correct horse 1"
EMAIL = "asha@example.com"
BAD_CREDENTIALS = {"detail": "Email or password is incorrect."}
LOCKED = {"detail": "Too many failed attempts. Please try again in 15 minutes."}
NOT_AUTHENTICATED = {"detail": "Please log in."}


def register(client: TestClient, email: str = EMAIL, password: str = PASSWORD,
             name: str = "Asha Rao"):
    return client.post("/api/auth/register",
                       json={"email": email, "name": name, "password": password})


def login(client: TestClient, email: str = EMAIL, password: str = PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def me(client: TestClient):
    return client.get("/api/auth/me")


def token_of(client: TestClient) -> str:
    return client.cookies[SESSION_COOKIE]


# ---------- unit: passwords ----------


def test_password_hash_verifies_only_the_right_password() -> None:
    stored = security.hash_password(PASSWORD)
    assert stored.startswith("scrypt$16384$8$5$")
    assert PASSWORD not in stored
    assert security.verify_password(PASSWORD, stored)
    for wrong in ("correct horse 2", "", PASSWORD.upper(), PASSWORD + " ", "ünïcødé"):
        assert not security.verify_password(wrong, stored)


def test_every_hash_has_its_own_salt() -> None:
    first, second = security.hash_password(PASSWORD), security.hash_password(PASSWORD)
    assert first != second
    assert first.split("$")[4] != second.split("$")[4]
    assert security.verify_password(PASSWORD, first) and security.verify_password(PASSWORD, second)


def test_non_ascii_passwords_round_trip() -> None:
    password = "pässwörd ✓ 密码"
    assert security.verify_password(password, security.hash_password(password))


@pytest.mark.parametrize("stored", [
    "", "scrypt", "plaintext-password", "bcrypt$16384$8$5$c2FsdA==$a2V5",
    "scrypt$16384$8$5$!!notbase64$a2V5", "scrypt$x$8$5$c2FsdA==$a2V5",
    "scrypt$16384$8$5$c2FsdA==", "scrypt$3$8$5$c2FsdA==$a2V5",  # n must be a power of two
    "scrypt$1073741824$8$5$c2FsdA==$a2V5",  # beyond maxmem
])
def test_a_malformed_stored_hash_verifies_nothing_and_never_raises(stored: str) -> None:
    assert security.verify_password(PASSWORD, stored) is False


def test_a_stored_hash_keeps_its_own_parameters() -> None:
    """Raising the cost later must not lock out existing passwords."""
    salt = b"0123456789abcdef"
    key = hashlib.scrypt(PASSWORD.encode(), salt=salt, n=2**4, r=8, p=1, dklen=32)
    stored = f"scrypt$16$8$1${base64.b64encode(salt).decode()}${base64.b64encode(key).decode()}"
    assert security.verify_password(PASSWORD, stored)
    assert not security.verify_password("nope", stored)


# ---------- unit: tokens ----------


def test_session_tokens_are_random_and_stored_only_as_sha256() -> None:
    tokens = {security.new_session_token() for _ in range(200)}
    assert len(tokens) == 200
    token = tokens.pop()
    assert len(token) >= 43  # 32 random bytes, URL-safe base64
    assert security.token_hash(token) == hashlib.sha256(token.encode()).hexdigest()
    assert security.token_hash(token) != token and len(security.token_hash(token)) == 64


def test_start_session_stores_the_hash_and_drops_expired_sessions(db_session: Session) -> None:
    candidate = Candidate(email=EMAIL, name="A")
    db_session.add(candidate)
    db_session.flush()
    old = auth.start_session(db_session, candidate.id)
    db_session.execute(update(AuthSession).values(
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        created_at=datetime.now(UTC) - timedelta(days=8)))
    assert auth.session_candidate(db_session, old) is None  # expired: unauthenticated
    new = auth.start_session(db_session, candidate.id)
    hashes = db_session.scalars(select(AuthSession.token_hash)).all()
    assert hashes == [security.token_hash(new)]  # the expired one was cleaned up
    assert auth.session_candidate(db_session, new).id == candidate.id
    session = db_session.scalar(select(AuthSession))
    assert session.expires_at - session.created_at == auth.SESSION_TTL


# ---------- unit: lockout ----------


@pytest.fixture
def with_password(db_session: Session) -> Candidate:
    candidate = Candidate(email=EMAIL, name="A", password_hash=security.hash_password(PASSWORD))
    db_session.add(candidate)
    db_session.flush()
    return candidate


def test_failures_count_up_to_a_lock_and_success_resets(
    db_session: Session, with_password: Candidate
) -> None:
    for expected in range(1, auth.MAX_FAILED_LOGINS):
        with pytest.raises(auth.InvalidCredentials):
            auth.authenticate(db_session, EMAIL, "wrong")
        assert (with_password.failed_logins, with_password.locked_until) == (expected, None)
    assert auth.authenticate(db_session, EMAIL, PASSWORD) is with_password
    assert (with_password.failed_logins, with_password.locked_until) == (0, None)

    before = datetime.now(UTC)
    for _ in range(auth.MAX_FAILED_LOGINS):
        with pytest.raises(auth.InvalidCredentials):
            auth.authenticate(db_session, EMAIL, "wrong")
    assert with_password.failed_logins == 0  # the lock replaces the count
    assert before + auth.LOCKOUT <= with_password.locked_until <= datetime.now(UTC) + auth.LOCKOUT
    with pytest.raises(auth.AccountLocked):  # even with the right password
        auth.authenticate(db_session, EMAIL, PASSWORD)


def test_an_expired_lock_lets_the_right_password_in(
    db_session: Session, with_password: Candidate
) -> None:
    with_password.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    with_password.failed_logins = 3
    assert auth.authenticate(db_session, EMAIL, PASSWORD) is with_password
    assert (with_password.failed_logins, with_password.locked_until) == (0, None)


def test_unknown_email_and_no_password_are_plain_invalid_credentials(db_session: Session) -> None:
    db_session.add(Candidate(email="legacy@example.com", name="L"))  # A3: pre-auth profile
    db_session.flush()
    for email in ("nobody@example.com", "legacy@example.com"):
        with pytest.raises(auth.InvalidCredentials):
            auth.authenticate(db_session, email, PASSWORD)


# ---------- API: register / login / logout / me ----------


def test_register_returns_201_verification_required_and_never_returns_secrets(
    client: TestClient, db_session: Session
) -> None:
    res = register(client, email="  Asha@Example.COM ")
    assert res.status_code == 201
    body = res.json()
    assert body["email"] == EMAIL
    assert body["verification_required"] is True
    assert body["message"]
    assert PASSWORD not in res.text
    assert SESSION_COOKIE not in client.cookies  # no session cookie issued before verification


def test_session_cookie_flags_on_login(client: TestClient, fake_supabase) -> None:
    fake_supabase.register_test_user(EMAIL, PASSWORD, confirmed=True)
    res = login(client, email=EMAIL, password=PASSWORD)
    assert res.status_code == 200
    cookie = res.headers["set-cookie"]
    parts = {p.strip().split("=")[0].lower() for p in cookie.split(";")}
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert {"httponly", "path", "max-age", "samesite"} <= parts
    assert "samesite=lax" in cookie.lower() and "Max-Age=604800" in cookie and "Path=/" in cookie
    assert "secure" not in parts  # plain-HTTP development default


def test_cookie_is_secure_when_configured(client: TestClient, fake_supabase) -> None:
    fake_supabase.register_test_user(EMAIL, PASSWORD, confirmed=True)
    secure = get_settings().model_copy(update={"auth_cookie_secure": True})
    app.dependency_overrides[get_settings] = lambda: secure
    try:
        assert "secure" in login(client, email=EMAIL, password=PASSWORD).headers["set-cookie"].lower().split("; ")
        assert "secure" in client.post("/api/auth/logout").headers["set-cookie"].lower().split("; ")
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_cookie_domain_and_samesite_configuration(client: TestClient, fake_supabase) -> None:
    fake_supabase.register_test_user(EMAIL, PASSWORD, confirmed=True)
    custom_cfg = get_settings().model_copy(update={
        "cookie_domain": ".example.com",
        "cookie_samesite": "none",
        "auth_cookie_secure": True,
    })
    app.dependency_overrides[get_settings] = lambda: custom_cfg
    try:
        set_cookie = login(client, email=EMAIL, password=PASSWORD).headers["set-cookie"].lower()
        assert "domain=.example.com" in set_cookie
        assert "samesite=none" in set_cookie
        assert "secure" in set_cookie

        logout_cookie = client.post("/api/auth/logout").headers["set-cookie"].lower()
        assert "domain=.example.com" in logout_cookie
        assert "samesite=none" in logout_cookie
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.parametrize("password", ["Zq9!xY", "x" * 129, "", None, 12345678])
def test_register_rejects_bad_passwords_without_echoing_them(
    client: TestClient, db_session: Session, password: object
) -> None:
    res = client.post("/api/auth/register",
                      json={"email": EMAIL, "name": "A", "password": password})
    assert res.status_code == 422
    if isinstance(password, str) and password:
        assert password not in res.text
    assert db_session.scalar(select(Candidate)) is None


def test_validation_errors_on_auth_routes_never_echo_the_body(client: TestClient) -> None:
    secret = "my secret password"
    for res in (client.post("/api/auth/register", json={"email": EMAIL, "password": secret}),
                client.post("/api/auth/login", json={"email": "not-an-email", "password": secret})):
        assert res.status_code == 422 and secret not in res.text
        assert all("input" not in e for e in res.json()["detail"])


def test_a_password_that_is_not_valid_unicode_is_422(client: TestClient) -> None:
    body = '{"email": "a@example.com", "name": "A", "password": "\\ud800abcdefgh"}'
    res = client.post("/api/auth/register", content=body,
                      headers={"Content-Type": "application/json"})
    assert res.status_code == 422


def test_duplicate_registration_is_409_and_keeps_the_first(
    client: TestClient, fake_supabase
) -> None:
    register(client, email=EMAIL)
    other = api_client()
    res = register(other, email="ASHA@example.com", password="another password")
    assert res.status_code == 409
    assert res.json() == {"detail": "An account with this email already exists."}
    assert SESSION_COOKIE not in other.cookies


def test_login_and_logout(client: TestClient, db_session: Session, fake_supabase) -> None:
    fake_supabase.register_test_user(EMAIL, PASSWORD, confirmed=True)
    browser = api_client()
    res = login(browser, email="ASHA@EXAMPLE.COM", password=PASSWORD)
    assert res.status_code == 200
    candidate_data = res.json()
    assert candidate_data["email"] == EMAIL
    assert "httponly" in res.headers["set-cookie"].lower()
    assert SESSION_COOKIE in browser.cookies
    assert me(browser).status_code == 200

    res = browser.post("/api/auth/logout")
    assert (res.status_code, res.content) == (204, b"")
    assert f'{SESSION_COOKIE}=""' in res.headers["set-cookie"]
    assert me(browser).status_code == 401
    assert browser.post("/api/auth/logout").status_code == 204  # idempotent


def test_a_logged_out_token_stays_dead_even_if_replayed(client: TestClient, db_session: Session) -> None:
    make_candidate(client, db_session, EMAIL, "Asha Rao")
    token = token_of(client)
    client.post("/api/auth/logout")
    replay = api_client(token)
    assert (me(replay).status_code, me(replay).json()) == (401, NOT_AUTHENTICATED)


@pytest.mark.parametrize("case", ["wrong-password", "unknown-email", "no-password-set"])
def test_failed_logins_are_indistinguishable(
    client: TestClient, db_session: Session, case: str
) -> None:
    register(api_client())
    db_session.add(Candidate(email="legacy@example.com", name="L"))  # A3
    db_session.flush()
    email, password = {"wrong-password": (EMAIL, "wrong password"),
                       "unknown-email": ("nobody@example.com", PASSWORD),
                       "no-password-set": ("legacy@example.com", PASSWORD)}[case]
    res = login(client, email=email, password=password)
    assert (res.status_code, res.json()) == (401, BAD_CREDENTIALS)
    assert "set-cookie" not in res.headers


def test_legacy_candidates_cannot_log_in_without_supabase_auth(
    client: TestClient, db_session: Session
) -> None:
    db_session.add(Candidate(email="legacy@example.com", name="Legacy"))
    db_session.flush()
    # No Supabase Auth account -> 401
    assert login(client, email="legacy@example.com", password=PASSWORD).status_code == 401


def test_unconfirmed_email_cannot_log_in(client: TestClient, fake_supabase) -> None:
    fake_supabase.register_test_user("unconfirmed@example.com", PASSWORD, confirmed=False)
    res = login(client, email="unconfirmed@example.com", password=PASSWORD)
    assert res.status_code == 403
    assert res.json()["detail"]["verification_required"] is True
    assert SESSION_COOKIE not in client.cookies


def test_me_without_a_valid_session_is_401(client: TestClient, db_session: Session) -> None:
    assert (me(client).status_code, me(client).json()) == (401, NOT_AUTHENTICATED)
    assert me(api_client("not-a-real-token")).status_code == 401
    make_candidate(client, db_session, EMAIL, "Asha Rao")
    db_session.execute(update(AuthSession).values(
        created_at=datetime.now(UTC) - timedelta(days=8),
        expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert me(client).status_code == 401  # expired: as good as none


def test_me_reflects_profile_changes(client: TestClient, db_session: Session) -> None:
    cid = make_candidate(client, db_session, EMAIL, "Asha Rao")
    res = client.patch(f"/api/candidates/{cid}", json={"name": "New Name"})
    assert res.status_code == 200
    assert me(client).json()["name"] == "New Name"


def test_auth_never_logs_secrets_or_personal_data(
    client: TestClient, db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    make_candidate(client, db_session, EMAIL, "Asha Rao")
    token = token_of(client)
    login(api_client(), password="wrong password")
    client.post("/api/auth/logout")
    client.post("/api/auth/register", json={"email": EMAIL, "name": "A", "password": "short"})
    for secret in (PASSWORD, "wrong password", token, security.token_hash(token)):
        assert secret not in caplog.text


def test_auth_routes_cannot_reach_a_model() -> None:
    auth_routes = [r for _, p, r in _routes() if p.startswith("/api/auth/")]
    assert len(auth_routes) == 11
    assert not any(_depends_on(r.dependant, get_model_gateway) for r in auth_routes)


def test_auth_database_unavailable_is_503(dead_database_client: TestClient) -> None:
    for res in (register(dead_database_client), login(dead_database_client),
                me(dead_database_client), dead_database_client.post("/api/auth/logout")):
        assert res.status_code == 503
        assert res.json() == {"detail": "Service temporarily unavailable. Please try again."}


# ---------- CSRF: Origin check (A5) ----------


@pytest.mark.parametrize("origin", [None, "https://evil.example", "null", "http://localhost:3001",
                                    "http://localhost:3000.evil.example"])
def test_state_changing_requests_need_an_allowed_origin(
    client: TestClient, db_session: Session, origin: str | None
) -> None:
    cid = make_candidate(client, db_session, EMAIL, "Asha Rao")
    before = db_session.scalar(select(Candidate.name))
    headers = {"Origin": origin} if origin else {}
    hostile = api_client(token_of(client))
    if origin is None:
        del hostile.headers["Origin"]
    for res in (hostile.patch(f"/api/candidates/{cid}", json={"name": "Hacked"}, headers=headers),
                hostile.delete(f"/api/candidates/{cid}", headers=headers),
                hostile.post("/api/auth/logout", headers=headers),
                hostile.put(f"/api/interview-sessions/{uuid.uuid4()}/ai-consent",
                            json={"granted": True}, headers=headers),
                hostile.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD},
                             headers=headers)):
        assert (res.status_code, res.json()) == (403, {"detail": "Request origin not allowed."})
    assert db_session.scalar(select(Candidate.name)) == before
    assert me(client).status_code == 200  # the logout was refused too


def test_safe_methods_need_no_origin(client: TestClient, db_session: Session) -> None:
    make_candidate(client, db_session, EMAIL, "Asha Rao")
    plain = api_client(token_of(client))
    del plain.headers["Origin"]
    assert me(plain).status_code == 200
    assert plain.get("/api/health").status_code == 200
    assert plain.head("/api/health").status_code in (200, 405)


def test_cors_preflight_and_credentials(client: TestClient) -> None:
    plain = TestClient(app)
    ok = plain.options("/api/auth/login", headers={
        "Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert ok.headers["access-control-allow-credentials"] == "true"
    bad = plain.options("/api/auth/login", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert bad.status_code == 400 and "access-control-allow-origin" not in bad.headers
    res = register(client)
    assert res.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert res.headers["access-control-allow-credentials"] == "true"


# ---------- 401 sweep: every route needs a session unless it is deliberately public ----------

PUBLIC = {
    ("GET", "/api/health"),
    ("POST", "/api/auth/register"),
    ("GET", "/api/auth/verify"),
    ("POST", "/api/auth/verify"),
    ("POST", "/api/auth/login"),
    ("GET", "/api/auth/google/start"),
    ("GET", "/api/auth/google/callback"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/forgot-password"),
    ("POST", "/api/auth/reset-password"),
}


ROUTERS = [health.router, auth_routes.router, candidates.router, resumes.router,
           manual_facts.router, interview_sessions.router]


def _routes() -> list[tuple[str, str, APIRoute]]:
    return [(method, "/api" + r.path, r) for router in ROUTERS for r in router.routes
            if isinstance(r, APIRoute) for method in sorted(r.methods - {"HEAD"})]


def _served(application) -> set[tuple[str, str]]:
    return {(m.upper(), path) for path, ops in application.openapi()["paths"].items()
            for m in ops}


def test_the_sweep_covers_every_served_route() -> None:
    """A router added to the app but not to ROUTERS fails here, so the sweep cannot miss it."""
    assert _served(app) == {(m, p) for m, p, _ in _routes()}


def _depends_on(dependant, target: Callable) -> bool:
    return any(d.call is target or _depends_on(d, target) for d in dependant.dependencies)


def test_every_non_public_route_depends_on_the_signed_in_candidate() -> None:
    """Structural: a new route added without authentication fails here."""
    routes = _routes()
    assert {(m, p) for m, p, _ in routes} >= PUBLIC
    unprotected = {(m, p) for m, p, r in routes if not _depends_on(r.dependant, current_candidate)}
    assert unprotected == PUBLIC


@pytest.mark.parametrize("token", [None, "forged-token"], ids=["no-cookie", "unknown-session"])
def test_every_protected_route_rejects_unauthenticated_requests(
    client: TestClient, db_session: Session, token: str | None
) -> None:
    """Behavioural: called with real-looking ids and bodies, each one answers 401."""
    if token:
        client.cookies.set(SESSION_COOKIE, token)
    checked = 0
    for method, path, _ in _routes():
        if (method, path) in PUBLIC:
            continue
        url = path
        for param in ("candidate_id", "resume_id", "session_id", "fact_id", "question_id"):
            url = url.replace("{" + param + "}", str(uuid.uuid4()))
        res = client.request(method, url, json={})
        assert (res.status_code, res.json()) == (401, NOT_AUTHENTICATED), (method, path)
        checked += 1
    assert checked >= 25
    assert db_session.scalar(select(text("count(*)")).select_from(Candidate)) == 0


def test_the_upload_rejects_an_unauthenticated_file(client: TestClient) -> None:
    res = client.post("/api/resumes", files={"file": ("r.pdf", b"%PDF-1.4 x", "application/pdf")})
    assert res.status_code == 401


def test_production_serves_application_routes() -> None:
    # AUDIT-001: Production serves health AND application routers (docs/openapi remain disabled)
    prod_app = create_app(Settings(_env_file=None, database_url="postgresql+psycopg://u:p@h/x",
                                   environment="production"))
    served = _served(prod_app)
    assert ("GET", "/api/health") in served
    assert ("POST", "/api/auth/login") in served
    assert ("POST", "/api/auth/register") in served
    assert ("GET", "/docs") not in served
    assert ("GET", "/openapi.json") not in served


# ---------- IDOR matrix: two candidates, every resource ----------


def build_world(client: TestClient, db: Session, email: str) -> dict:
    """A signed-in candidate with a reviewed resume, a COMPLETED interview with an evaluated answer
    and its report, and a second session just created (PROFILE_REVIEW)."""
    cid = make_candidate(client, db, email, email.split("@")[0])
    fact_ids = add_facts(db, cid, (FC.PROJECT, "TrustLens", FactStatus.APPROVED),
                         (FC.SKILL, "React", FactStatus.APPROVED))
    resume_id = db.scalar(text("SELECT resume_id FROM candidate_facts WHERE id = :f"),
                          {"f": fact_ids[0]})
    done = client.post("/api/interview-sessions", json={}).json()["id"]
    assert act(client, done, "CONFIRM_PROFILE").status_code == 200
    assert client.post(f"/api/interview-sessions/{done}/plan", json=SETTINGS).status_code == 200
    assert act(client, done, "START_INTERVIEW").status_code == 200
    question = nxt(client, done).json()["question"]
    assert ans(client, done, question["id"], said="I would explain the design.").status_code == 200
    nxt(client, done)
    assert act(client, done, "END_INTERVIEW").json()["status"] == "REPORT_GENERATION"
    assert evaluate(client, done, question["id"]).status_code == 200
    assert post_report(client, done).json()["status"] == "COMPLETED"
    live = client.post("/api/interview-sessions", json={}).json()["id"]
    manual = client.post(f"/api/candidates/{cid}/manual-facts",  # Phase 13
                         json={"category": "SKILL", "fact": "Go"}).json()["id"]
    return {"candidate": cid, "resume": str(resume_id), "fact": str(fact_ids[0]), "done": done,
            "question": question["id"], "live": live, "manual": manual}


def intruder_requests(w: dict, mine: dict) -> list[tuple[str, str, dict | None]]:
    """Every way to reach world w's data. `mine` = the intruder's own world, for requests that
    put a foreign child id under the intruder's own resource."""
    s, done, live = "/api/interview-sessions", w["done"], w["live"]
    return [
        ("GET", f"/api/candidates/{w['candidate']}", None),
        ("PATCH", f"/api/candidates/{w['candidate']}", {"email": "stolen@example.com"}),
        ("DELETE", f"/api/candidates/{w['candidate']}", None),
        ("GET", f"/api/candidates/{w['candidate']}/resumes", None),
        ("GET", f"/api/candidates/{w['candidate']}/interview-sessions", None),
        ("GET", f"/api/resumes/{w['resume']}", None),
        ("GET", f"/api/resumes/{w['resume']}/facts", None),
        ("PATCH", f"/api/resumes/{w['resume']}/facts/{w['fact']}", {"status": "REJECTED"}),
        ("PATCH", f"/api/resumes/{mine['resume']}/facts/{w['fact']}", {"status": "REJECTED"}),
        ("POST", f"/api/resumes/{w['resume']}/ai-extraction", None),
        ("POST", f"/api/resumes/{w['resume']}/facts/review-pending", {"status": "APPROVED"}),
        ("GET", f"/api/candidates/{w['candidate']}/manual-facts", None),
        ("POST", f"/api/candidates/{w['candidate']}/manual-facts",
         {"category": "SKILL", "fact": "Injected"}),
        ("PATCH", f"/api/manual-facts/{w['manual']}", {"fact": "Hijacked"}),
        ("DELETE", f"/api/manual-facts/{w['manual']}", None),
        ("DELETE", f"/api/resumes/{w['resume']}", None),
        ("GET", f"{s}/{done}", None),
        ("GET", f"{s}/{done}/plan", None),
        ("GET", f"{s}/{done}/questions", None),
        ("GET", f"{s}/{done}/evaluations", None),
        ("GET", f"{s}/{done}/report", None),
        ("GET", f"{s}/{done}/ai-consent", None),
        ("POST", f"{s}/{done}/report", {}),
        ("POST", f"{s}/{done}/retry", {}),
        ("POST", f"{s}/{done}/questions/{w['question']}/evaluation", {}),
        ("POST", f"{s}/{mine['done']}/questions/{w['question']}/evaluation", {}),
        ("PUT", f"{s}/{live}/questions/{w['question']}/expressions", {"status": "UNAVAILABLE"}),
        ("PUT", f"{s}/{live}/ai-consent", {"granted": True}),
        ("POST", f"{s}/{live}/actions", {"action": "CONFIRM_PROFILE"}),
        ("POST", f"{s}/{live}/plan", SETTINGS),
        ("POST", f"{s}/{live}/questions/next", {}),
        ("POST", f"{s}/{live}/answers",
         {"question_id": w["question"], "kind": "ANSWER", "text": "hijack"}),
        ("POST", f"{s}/{mine['live']}/answers",
         {"question_id": w["question"], "kind": "ANSWER", "text": "hijack"}),
        ("DELETE", f"{s}/{live}", None),
        ("DELETE", f"{s}/{done}", None),
    ]


def snapshot(db: Session) -> dict:
    tables = ["candidates", "auth_sessions", "resumes", "candidate_facts", "interview_sessions",
              "interview_consent_events", "interview_questions", "answer_evaluations",
              "answer_evaluation_criteria", "provider_calls"]
    return {t: db.scalars(text(f"SELECT row_to_json(x)::text FROM {t} x ORDER BY 1")).all()
            for t in tables}


@pytest.fixture
def worlds(client: TestClient, db_session: Session) -> tuple:
    b_client = api_client()
    a = build_world(client, db_session, "alice@example.com")
    b = build_world(b_client, db_session, "bob@example.com")
    return client, a, b_client, b


def test_cross_candidate_requests_are_404_and_change_nothing(
    worlds: tuple, db_session: Session
) -> None:
    a_client, a, b_client, b = worlds
    before = snapshot(db_session)
    count = 0
    for intruder, victim, own in ((b_client, a, b), (a_client, b, a)):
        for method, url, body in intruder_requests(victim, own):
            res = intruder.request(method, url, json=body)
            assert res.status_code == 404, (method, url, res.status_code, res.text)
            # Exactly what a missing resource says: nothing confirms that it exists.
            assert res.json()["detail"] in {"Candidate not found", "Resume not found",
                                            "Fact not found", "Interview session not found",
                                            "Question not found"}, (method, url)
            count += 1
    assert count == 70
    assert snapshot(db_session) == before


def test_a_foreign_id_answers_like_a_missing_one(worlds: tuple) -> None:
    a_client, a, _, b = worlds
    for template in ("/api/candidates/{}", "/api/resumes/{}", "/api/interview-sessions/{}"):
        kind = template.split("/")[2]
        foreign = {"candidates": b["candidate"], "resumes": b["resume"],
                   "interview-sessions": b["done"]}[kind]
        assert a_client.get(template.format(foreign)).json() == a_client.get(
            template.format(uuid.uuid4())).json()


def test_anonymous_requests_on_real_ids_are_401_and_change_nothing(
    worlds: tuple, db_session: Session
) -> None:
    _, a, _, b = worlds
    before = snapshot(db_session)
    anonymous = api_client()
    for method, url, body in intruder_requests(a, b):
        assert anonymous.request(method, url, json=body).status_code == 401, (method, url)
    assert snapshot(db_session) == before


@pytest.mark.parametrize("owner", ["a", "b"])
def test_each_candidate_still_reaches_their_own_resources(worlds: tuple, owner: str) -> None:
    a_client, a, b_client, b = worlds
    client, w = (a_client, a) if owner == "a" else (b_client, b)
    s = "/api/interview-sessions"
    expected = [
        ("GET", f"/api/candidates/{w['candidate']}", None, 200),
        ("GET", f"/api/candidates/{w['candidate']}/resumes", None, 200),
        ("GET", f"/api/candidates/{w['candidate']}/interview-sessions", None, 200),
        ("GET", f"/api/resumes/{w['resume']}", None, 200),
        ("GET", f"/api/resumes/{w['resume']}/facts", None, 200),
        ("GET", f"{s}/{w['done']}", None, 200),
        ("GET", f"{s}/{w['done']}/plan", None, 200),
        ("GET", f"{s}/{w['done']}/questions", None, 200),
        ("GET", f"{s}/{w['done']}/evaluations", None, 200),
        ("GET", f"{s}/{w['done']}/report", None, 200),
        ("GET", f"{s}/{w['done']}/ai-consent", None, 200),
        ("POST", f"{s}/{w['done']}/report", {}, 200),  # the same report again
        ("POST", f"{s}/{w['done']}/questions/{w['question']}/evaluation", {}, 200),  # stored
        ("POST", f"{s}/{w['done']}/retry", {}, 409),  # reached: blocked by the live session
        ("PATCH", f"/api/candidates/{w['candidate']}", {"name": "Renamed"}, 200),
        ("PATCH", f"/api/resumes/{w['resume']}/facts/{w['fact']}", {"status": "APPROVED"}, 200),
        ("POST", f"/api/resumes/{w['resume']}/ai-extraction", None, 503),  # no AI configured
        ("GET", f"/api/candidates/{w['candidate']}/manual-facts", None, 200),
        ("POST", f"/api/candidates/{w['candidate']}/manual-facts",
         {"category": "SKILL", "fact": "Rust"}, 201),
        ("PATCH", f"/api/manual-facts/{w['manual']}", {"fact": "Go 1.23"}, 200),
        ("DELETE", f"/api/manual-facts/{w['manual']}", None, 204),
        ("PUT", f"{s}/{w['live']}/ai-consent", {"granted": False}, 200),
        ("POST", f"{s}/{w['live']}/questions/next", {}, 409),  # not started yet
        ("POST", f"{s}/{w['live']}/answers",
         {"question_id": w["question"], "kind": "SKIP"}, 404),  # the question is of `done`
        ("POST", f"{s}/{w['live']}/actions", {"action": "CONFIRM_PROFILE"}, 200),
        ("POST", f"{s}/{w['live']}/plan", SETTINGS, 200),
        ("DELETE", f"{s}/{w['live']}", None, 204),
        ("DELETE", f"/api/resumes/{w['resume']}", None, 204),
        ("DELETE", f"{s}/{w['done']}", None, 204),
        ("DELETE", f"/api/candidates/{w['candidate']}", None, 204),
    ]
    for method, url, body, status in expected:
        res = client.request(method, url, json=body)
        assert res.status_code == status, (method, url, res.status_code, res.text)


def test_deleting_one_candidate_leaves_the_other_untouched(
    worlds: tuple, db_session: Session
) -> None:
    a_client, a, b_client, b = worlds
    before_b = {t: [r for r in rows if b["candidate"] in r or b["done"] in r or b["live"] in r]
                for t, rows in snapshot(db_session).items()}
    assert a_client.delete(f"/api/candidates/{a['candidate']}").status_code == 204
    after = snapshot(db_session)
    assert all(a["candidate"] not in r for rows in after.values() for r in rows)
    for t, rows in before_b.items():
        assert set(rows) <= set(after[t]), t
    assert me(b_client).json()["id"] == b["candidate"]


# ---------- real concurrency: separate, committed connections ----------


@pytest.fixture
def committed_email() -> Iterator[str]:
    email = f"{uuid.uuid4().hex}@example.com"
    yield email
    with SessionLocal() as db:
        db.execute(text("DELETE FROM candidates WHERE email = :e"), {"e": email})
        db.commit()


def run_parallel(*fns: Callable[[], object]) -> list:
    """Starts every call at once (a barrier) on its own thread; returns their results."""
    n = len(fns)
    barrier, results, errors = threading.Barrier(n), [], []

    def target(fn: Callable[[], object]) -> None:
        try:
            barrier.wait(10)
            results.append(fn())
        except Exception as exc:  # reported to the test thread
            errors.append(exc)

    threads = [threading.Thread(target=target, args=(fn,), daemon=True) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors and len(results) == n
    return results


def _extract_csrf(html_text: str) -> str:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html_text)
    assert m is not None, "csrf_token not found in confirmation page HTML"
    return m.group(1)


def test_verify_email_callback_flow(client: TestClient, fake_supabase) -> None:
    # 1. Register candidate and verify backend supplies verification redirect URL
    reg_res = client.post("/api/auth/register", json={"email": "callback@example.com", "name": "Callback User", "password": PASSWORD})
    assert reg_res.status_code == 201
    assert fake_supabase.last_redirect_to == f"{get_settings().fastapi_public_url}/api/auth/verify"

    # 2. Simulate template-constructed URL: {{ .RedirectTo }}?token_hash={{ .TokenHash }}&type=signup
    token_hash = "verify_callback@example.com"
    constructed_url = f"/api/auth/verify?token_hash={token_hash}&type=signup"

    # 3. Step 1: GET confirmation page (must NOT consume the token)
    get_res = client.get(constructed_url, follow_redirects=False)
    assert get_res.status_code == 200
    assert "text/html" in get_res.headers["content-type"]
    assert "Confirm Your Email" in get_res.text
    assert token_hash in get_res.text
    assert SESSION_COOKIE not in get_res.cookies
    assert token_hash in fake_supabase.verification_tokens  # Token remains unconsumed!

    csrf_token = _extract_csrf(get_res.text)

    # 4. Step 2: POST to complete verification (explicit candidate confirmation)
    post_res = client.post(
        "/api/auth/verify",
        data={"token_hash": token_hash, "type": "signup", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert post_res.status_code == 303
    assert post_res.headers["location"].endswith("/interview")
    assert post_res.headers["referrer-policy"] == "no-referrer"
    assert SESSION_COOKIE in post_res.cookies
    assert token_hash not in fake_supabase.verification_tokens  # Token now consumed!

    # 5. Verify candidate can access /me with this cookie
    browser = api_client(post_res.cookies[SESSION_COOKIE])
    me_res = browser.get("/api/auth/me")
    assert me_res.status_code == 200
    assert me_res.json()["email"] == "callback@example.com"


def test_verify_email_scanner_prefetch_does_not_consume_token(client: TestClient, fake_supabase) -> None:
    client.post("/api/auth/register", json={"email": "scanner@example.com", "name": "Scanner Test", "password": PASSWORD})
    token_hash = "verify_scanner@example.com"
    constructed_url = f"/api/auth/verify?token_hash={token_hash}&type=signup"

    # Automated security scanner crawls/prefetches the link multiple times
    for _ in range(5):
        scan_res = client.get(constructed_url, follow_redirects=False)
        assert scan_res.status_code == 200
        assert "text/html" in scan_res.headers["content-type"]
        assert SESSION_COOKIE not in scan_res.cookies
        # Crucial: Token MUST remain valid and unconsumed despite repeated scanner GETs
        assert token_hash in fake_supabase.verification_tokens

    # Real human candidate opens link and submits the form
    page_res = client.get(constructed_url, follow_redirects=False)
    csrf_token = _extract_csrf(page_res.text)

    confirm_res = client.post(
        "/api/auth/verify",
        data={"token_hash": token_hash, "type": "signup", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert confirm_res.status_code == 303
    assert confirm_res.headers["location"].endswith("/interview")
    assert SESSION_COOKIE in confirm_res.cookies
    assert token_hash not in fake_supabase.verification_tokens


def test_verify_email_single_use_token_reuse_fails_safely(client: TestClient, fake_supabase) -> None:
    client.post("/api/auth/register", json={"email": "singleuse@example.com", "name": "Single Use", "password": PASSWORD})
    token_hash = "verify_singleuse@example.com"

    # GET confirmation page
    get_res = client.get(f"/api/auth/verify?token_hash={token_hash}&type=signup", follow_redirects=False)
    assert get_res.status_code == 200
    csrf_token = _extract_csrf(get_res.text)

    # First POST verification succeeds
    res1 = client.post(
        "/api/auth/verify",
        data={"token_hash": token_hash, "type": "signup", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert res1.status_code == 303
    assert res1.headers["location"].endswith("/interview")
    assert SESSION_COOKIE in res1.cookies

    # Second POST verification with the same token fails safely (single-use enforced)
    res2 = client.post(
        "/api/auth/verify",
        data={"token_hash": token_hash, "type": "signup", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert res2.status_code == 303
    assert "error=verification_expired" in res2.headers["location"]
    assert SESSION_COOKIE not in res2.cookies


def test_verify_email_invalid_and_expired_tokens_redirect_safely(client: TestClient, fake_supabase) -> None:
    # Attempting to confirm an invalid/expired token via POST
    settings = get_settings()
    from app.api.routes.auth import generate_verify_csrf_token
    bogus_csrf = generate_verify_csrf_token("invalid_or_expired_hash", "signup", settings.session_key_bytes)

    res = client.post(
        "/api/auth/verify",
        data={"token_hash": "invalid_or_expired_hash", "type": "signup", "csrf_token": bogus_csrf},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"].endswith("/login?error=verification_expired")
    assert SESSION_COOKIE not in res.cookies


def test_verify_email_missing_token_hash_redirects_safely(client: TestClient, fake_supabase) -> None:
    # Missing token_hash on GET
    res1 = client.get("/api/auth/verify", follow_redirects=False)
    assert res1.status_code == 302
    assert res1.headers["location"].endswith("/login?error=verification_expired")
    assert SESSION_COOKIE not in res1.cookies

    # Empty string token_hash on GET
    res2 = client.get("/api/auth/verify?token_hash=", follow_redirects=False)
    assert res2.status_code == 302
    assert res2.headers["location"].endswith("/login?error=verification_expired")
    assert SESSION_COOKIE not in res2.cookies

    # Whitespace token_hash on GET
    res3 = client.get("/api/auth/verify?token_hash=%20%20", follow_redirects=False)
    assert res3.status_code == 302
    assert res3.headers["location"].endswith("/login?error=verification_expired")
    assert SESSION_COOKIE not in res3.cookies

    # Missing token_hash on POST
    res4 = client.post("/api/auth/verify", data={"csrf_token": "any"}, follow_redirects=False)
    assert res4.status_code == 303
    assert res4.headers["location"].endswith("/login?error=verification_expired")
    assert SESSION_COOKIE not in res4.cookies


def test_verify_email_csrf_protection(client: TestClient, fake_supabase) -> None:
    client.post("/api/auth/register", json={"email": "csrf@example.com", "name": "CSRF User", "password": PASSWORD})
    token_hash = "verify_csrf@example.com"

    # POST without CSRF token
    res1 = client.post("/api/auth/verify", data={"token_hash": token_hash, "type": "signup"}, follow_redirects=False)
    assert res1.status_code == 303
    assert "error=verification_expired" in res1.headers["location"]
    assert SESSION_COOKIE not in res1.cookies
    assert token_hash in fake_supabase.verification_tokens  # Token remains untouched!

    # POST with forged CSRF token
    res2 = client.post(
        "/api/auth/verify",
        data={"token_hash": token_hash, "type": "signup", "csrf_token": "forged_token_value"},
        follow_redirects=False,
    )
    assert res2.status_code == 303
    assert "error=verification_expired" in res2.headers["location"]
    assert SESSION_COOKIE not in res2.cookies
    assert token_hash in fake_supabase.verification_tokens  # Token still untouched!


def test_verify_email_invalid_type_redirects_safely(client: TestClient, fake_supabase) -> None:
    # GET with invalid type (only signup and email allowed)
    res1 = client.get("/api/auth/verify?token_hash=somehash&type=recovery", follow_redirects=False)
    assert res1.status_code == 302
    assert res1.headers["location"].endswith("/login?error=verification_expired")

    # POST with invalid type
    settings = get_settings()
    from app.api.routes.auth import generate_verify_csrf_token
    csrf = generate_verify_csrf_token("somehash", "invalid_type", settings.session_key_bytes)
    res2 = client.post(
        "/api/auth/verify",
        data={"token_hash": "somehash", "type": "invalid_type", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert res2.status_code == 303
    assert res2.headers["location"].endswith("/login?error=verification_expired")


def test_verify_email_url_construction_for_production(client: TestClient, fake_supabase) -> None:
    # Verify production URL settings construct the exact expected backend link
    prod_settings = get_settings().model_copy(update={
        "fastapi_public_url": "https://api.YOUR_DOMAIN",
        "frontend_url": "https://www.YOUR_DOMAIN",
    })
    app.dependency_overrides[get_settings] = lambda: prod_settings
    try:
        reg_res = client.post("/api/auth/register", json={"email": "produrl@example.com", "name": "Prod URL", "password": PASSWORD})
        assert reg_res.status_code == 201
        assert fake_supabase.last_redirect_to == "https://api.YOUR_DOMAIN/api/auth/verify"

        # Simulate clicking email link with TokenHash
        token_hash = "verify_produrl@example.com"
        # Supabase email template: {{ .RedirectTo }}?token_hash={{ .TokenHash }}&type=signup
        email_link = f"{fake_supabase.last_redirect_to}?token_hash={token_hash}&type=signup"
        assert email_link == "https://api.YOUR_DOMAIN/api/auth/verify?token_hash=verify_produrl@example.com&type=signup"

        # Step 1: GET email link
        get_res = client.get(f"/api/auth/verify?token_hash={token_hash}&type=signup", follow_redirects=False)
        assert get_res.status_code == 200
        csrf_token = _extract_csrf(get_res.text)

        # Step 2: POST confirmation
        post_res = client.post(
            "/api/auth/verify",
            data={"token_hash": token_hash, "type": "signup", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert post_res.status_code == 303
        assert post_res.headers["location"] == "https://www.YOUR_DOMAIN/interview"
        assert SESSION_COOKIE in post_res.cookies
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_verify_email_json_client_api_response(client: TestClient, fake_supabase) -> None:
    client.post("/api/auth/register", json={"email": "jsonclient@example.com", "name": "JSON User", "password": PASSWORD})
    token_hash = "verify_jsonclient@example.com"
    settings = get_settings()
    from app.api.routes.auth import generate_verify_csrf_token
    csrf = generate_verify_csrf_token(token_hash, "signup", settings.session_key_bytes)

    res = client.post(
        "/api/auth/verify",
        json={"token_hash": token_hash, "type": "signup", "csrf_token": csrf},
        headers={"Accept": "application/json"},
    )
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "redirect_url": f"{settings.frontend_url}/interview"}
    assert SESSION_COOKIE in res.cookies


def test_verify_email_html_injection_prevention(client: TestClient) -> None:
    malicious_hashes = [
        "<script>alert(1)</script>",
        '"><script>alert(1)</script>',
        '" onfocus="alert(1)" autofocus="',
        "' onmouseover='alert(1)",
        "<img src=x onerror=alert(1)>",
        "javascript:alert(1)",
        'payload"withquotes',
        "payload'withsinglequote",
    ]
    for bad_hash in malicious_hashes:
        # Step 1: Malformed / malicious token hashes are rejected without rendering HTML
        res = client.get(f"/api/auth/verify?token_hash={quote(bad_hash)}&type=signup", follow_redirects=False)
        assert res.status_code == 302
        assert res.headers["location"].endswith("/login?error=verification_expired")
        assert "text/html" not in res.headers.get("content-type", "")

    # Malicious type is also rejected
    res_type = client.get('/api/auth/verify?token_hash=validhash123&type="><script>', follow_redirects=False)
    assert res_type.status_code == 302
    assert res_type.headers["location"].endswith("/login?error=verification_expired")


def test_verify_email_malformed_token_hash_rejected_safely(client: TestClient) -> None:
    malformed_hashes = [
        "token with spaces",
        "token\nnewline",
        "token\0null",
        "a" * 257,  # exceeds 256 character length limit
        "token#hash",
        "token?query",
    ]
    for bad_hash in malformed_hashes:
        # GET rejects malformed token_hash
        get_res = client.get(f"/api/auth/verify?token_hash={quote(bad_hash)}&type=signup", follow_redirects=False)
        assert get_res.status_code == 302
        assert get_res.headers["location"].endswith("/login?error=verification_expired")

        # POST rejects malformed token_hash
        post_res = client.post(
            "/api/auth/verify",
            data={"token_hash": bad_hash, "type": "signup", "csrf_token": "any"},
            follow_redirects=False,
        )
        assert post_res.status_code == 303
        assert post_res.headers["location"].endswith("/login?error=verification_expired")


def test_verify_email_logs_never_leak_token_hash_or_credentials(
    client: TestClient, fake_supabase, caplog: pytest.LogCaptureFixture
) -> None:
    from app.main import SensitiveDataRedactionFilter

    caplog.set_level(logging.DEBUG)
    caplog.handler.addFilter(SensitiveDataRedactionFilter())
    secret_token = "SECRET_VERIFY_TOKEN_UNIQUE_987654321"

    # 1. GET confirmation page with secret token
    get_res = client.get(f"/api/auth/verify?token_hash={secret_token}&type=signup", follow_redirects=False)
    assert get_res.status_code == 200

    # 2. Extract CSRF and submit POST (fails in fake_supabase because unregistered)
    csrf_token = _extract_csrf(get_res.text)
    post_res = client.post(
        "/api/auth/verify",
        data={"token_hash": secret_token, "type": "signup", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert post_res.status_code == 303

    # Assert raw secret token NEVER appears anywhere in captured application logs
    assert secret_token not in caplog.text
    # Assert complete verification URL with raw token NEVER appears in captured logs
    assert f"token_hash={secret_token}" not in caplog.text


def test_sensitive_data_redaction_filter() -> None:
    from app.main import SensitiveDataRedactionFilter

    redactor = SensitiveDataRedactionFilter()

    # Test msg redaction
    rec1 = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Handling GET /api/auth/verify?token_hash=secret_tok_123&type=signup HTTP/1.1",
        args=(),
        exc_info=None,
    )
    redactor.filter(rec1)
    assert "secret_tok_123" not in rec1.msg
    assert "token_hash=[REDACTED]" in rec1.msg

    # Test tuple args redaction
    rec2 = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1", "GET", "/api/auth/verify?token_hash=secret_hash_456&access_token=jwt789", "1.1", 200),
        exc_info=None,
    )
    redactor.filter(rec2)
    assert "secret_hash_456" not in str(rec2.args)
    assert "jwt789" not in str(rec2.args)
    assert "token_hash=[REDACTED]" in str(rec2.args)
    assert "access_token=[REDACTED]" in str(rec2.args)

    # Test dict args redaction
    rec3 = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Payload: %(token)s",
        args=({"token": "token=secret_val_999"},),
        exc_info=None,
    )
    redactor.filter(rec3)
    assert "secret_val_999" not in str(rec3.args)
    assert "token=[REDACTED]" in str(rec3.args)

    # Test URL-encoded parameters and %2B tokens
    rec4 = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="URL: https://api.domain/verify?token_hash%3Dencoded_secret%26type%3Dsignup and token_hash=%2Bplus_secret",
        args=(),
        exc_info=None,
    )
    redactor.filter(rec4)
    assert "encoded_secret" not in rec4.msg
    assert "plus_secret" not in rec4.msg
    assert "token_hash%3D[REDACTED]" in rec4.msg
    assert "token_hash=[REDACTED]" in rec4.msg

    # Test Bearer token and JSON credentials redaction
    rec5 = logging.LogRecord(
        name="httpx",
        level=logging.DEBUG,
        pathname="",
        lineno=0,
        msg='Header: Bearer eyJhbGciOiJIUzI1Ni... Body: {"access_token": "secret_jwt", "password": "plain_password"}',
        args=(),
        exc_info=None,
    )
    redactor.filter(rec5)
    assert "eyJhbGciOiJIUzI1Ni..." not in rec5.msg
    assert "secret_jwt" not in rec5.msg
    assert "plain_password" not in rec5.msg
    assert "Bearer [REDACTED]" in rec5.msg
    assert '"access_token": "[REDACTED]"' in rec5.msg
    assert '"password": "[REDACTED]"' in rec5.msg

    # Test exception traceback redaction
    try:
        raise ValueError("Failed connecting to https://api.supabase.co/verify?token_hash=traceback_secret_hash")
    except Exception as exc:
        import sys
        rec6 = logging.LogRecord(
            name="app.routes.auth",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="Exception during verify",
            args=(),
            exc_info=sys.exc_info(),
        )
        redactor.filter(rec6)
        assert rec6.exc_info is None  # Sanitized to prevent unredacted formatException
        assert "traceback_secret_hash" not in rec6.exc_text
        assert "token_hash=[REDACTED]" in rec6.exc_text

    # Test non-string record.msg (e.g. logger.error(exc))
    exc_obj = RuntimeError("Error with token=leaked_via_exception_object")
    rec7 = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname="",
        lineno=0,
        msg=exc_obj,
        args=(),
        exc_info=None,
    )
    redactor.filter(rec7)
    assert "leaked_via_exception_object" not in rec7.msg
    assert "token=[REDACTED]" in rec7.msg

    # Test format specifier preservation
    rec8 = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Summary: error_code=%s, count=%d",
        args=("NOT_FOUND", 42),
        exc_info=None,
    )
    redactor.filter(rec8)
    assert rec8.msg == "Summary: error_code=%s, count=%d"
    assert rec8.args == ("NOT_FOUND", 42)


def test_supabase_valid_tokens_accepted_by_format_pattern() -> None:
    from app.api.routes.auth import TOKEN_HASH_PATTERN

    # 1. Standard SHA-256 hex string (GoTrue default token_hash)
    sha256_hash = "c7ae6fc5d2e3f4a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7"
    assert TOKEN_HASH_PATTERN.match(sha256_hash) is not None

    # 2. Base64URL-encoded token hash
    base64url_hash = "c7ae6fc5-d2e3_f4a1b0c9d8e7f6a5b4c3d2e1f0a9"
    assert TOKEN_HASH_PATTERN.match(base64url_hash) is not None

    # 3. High-entropy alphanumeric token
    alphanumeric_hash = "pk_test_ABC123xyz890_.-@"
    assert TOKEN_HASH_PATTERN.match(alphanumeric_hash) is not None

    # 4. Reject dangerous characters that could breakout of HTML or injection
    assert TOKEN_HASH_PATTERN.match('token"onfocus="alert(1)') is None
    assert TOKEN_HASH_PATTERN.match("token<script>alert(1)</script>") is None
    assert TOKEN_HASH_PATTERN.match("token' OR '1'='1") is None
    assert TOKEN_HASH_PATTERN.match("token\nheader:inject") is None


def test_google_oauth_pkce_flow(client: TestClient, fake_supabase) -> None:
    # 1. Start OAuth
    start_res = client.get("/api/auth/google/start", follow_redirects=False)
    assert start_res.status_code == 302
    assert "provider=google" in start_res.headers["location"]
    assert "oauth_session_flow" in start_res.cookies

    # 2. Extract state from location
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(start_res.headers["location"])
    query_params = parse_qs(parsed.query)
    redirect_to = query_params["redirect_to"][0]
    state = parse_qs(urlparse(redirect_to).query)["state"][0]

    # 3. Simulate callback
    client.cookies.set("oauth_session_flow", start_res.cookies["oauth_session_flow"])
    callback_res = client.get(f"/api/auth/google/callback?code=valid_code&state={state}", follow_redirects=False)
    assert callback_res.status_code == 302
    assert callback_res.headers["location"].endswith("/interview")
    assert SESSION_COOKIE in callback_res.cookies

    # 4. Verify candidate session
    browser = api_client(callback_res.cookies[SESSION_COOKIE])
    assert browser.get("/api/auth/me").status_code == 200


def test_password_recovery_and_reset_flow(client: TestClient, fake_supabase) -> None:
    # Forgot password always returns 200 MessageResponse
    forgot_res = client.post("/api/auth/forgot-password", json={"email": "nobody@example.com"})
    assert forgot_res.status_code == 200
    assert "password reset link has been sent" in forgot_res.json()["message"]

    # Reset password
    reset_res = client.post("/api/auth/reset-password", json={"access_token": "valid_token", "new_password": "new_password_123"})
    assert reset_res.status_code == 200
    assert "Password updated successfully" in reset_res.json()["message"]


def test_simultaneous_registrations_with_one_email(committed_email: str) -> None:
    attempt = lambda: register(api_client(), email=committed_email).status_code  # noqa: E731
    results = run_parallel(attempt, attempt)
    assert sorted(results) == [201, 409]


@pytest.fixture
def committed_pair(committed_email: str) -> Iterator[tuple[str, str]]:
    other = f"{uuid.uuid4().hex}@example.com"
    stored = security.hash_password(PASSWORD)
    with SessionLocal() as db:
        db.add_all([Candidate(email=committed_email, name="Target", password_hash=stored),
                    Candidate(email=other, name="Bystander", password_hash=stored)])
        db.commit()
    yield committed_email, other
    with SessionLocal() as db:
        db.execute(text("DELETE FROM candidates WHERE email = :e"), {"e": other})
        db.commit()


def test_login_rate_limiting_throttles_excessive_attempts(client: TestClient, db_session: Session) -> None:
    for _ in range(20):
        res = login(client, email="ratelimit@example.com", password="wrong password")
        assert res.status_code == 401
    # 21st attempt hits network rate limit
    res = login(client, email="ratelimit@example.com", password="wrong password")
    assert res.status_code == 429
    assert "Too many sign-in attempts" in res.json()["detail"]


def test_logout_racing_requests_is_all_or_nothing(committed_pair: tuple) -> None:
    target, _ = committed_pair
    with SessionLocal() as db:
        cid = db.scalar(text("SELECT id FROM candidates WHERE email = :e"), {"e": target})
        token = auth.start_session(db, cid)
        db.commit()
    browser = api_client(token)

    def read() -> int:
        return me(api_client(token)).status_code

    def logout() -> int:
        return api_client(token).post("/api/auth/logout").status_code

    results = run_parallel(logout, *[read] * 8)
    # Every request is either fully signed in or fully refused; never a 500.
    assert results.count(204) == 1 and set(results) <= {200, 204, 401}
    assert me(api_client(token)).status_code == 401
    with SessionLocal() as db:
        revoked_at = db.scalar(text("SELECT revoked_at FROM auth_sessions WHERE token_hash = :h"),
                               {"h": security.token_hash(token)})
        assert revoked_at is not None
