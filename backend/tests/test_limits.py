"""Usage limits (abuse and cost controls): window math, the atomic counter, every limited route,
the deterministic fallback when an AI quota is reached, and real concurrency."""

import re
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

import app.api.routes.auth as auth_routes
import app.api.routes.interview_sessions as session_routes
import app.api.routes.resumes as resume_routes
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import app
from app.models import Candidate, Resume, UsageCounter
from app.services import auth as auth_service
from app.services.evaluator import QUOTA_USED
from app.services.limits import (
    DAY,
    LOGIN,
    REGISTER,
    Limit,
    LimitReached,
    consume,
    network_key,
    window_start,
)
from tests.auth_helpers import api_client, verify_email_flow
from tests.fakes import FakeProvider, make_gateway
from tests.test_evaluation_api import TECH, finished  # noqa: F401  (fixture)
from tests.test_interview_plan_api import (  # noqa: F401  (fixtures)
    SETTINGS,
    VALID,
    candidate,
    consented,
    facts,
    plan,
    sid,
    use_gateway,
)
from tests.test_interview_turn_api import ans, first_question, started  # noqa: F401
from tests.test_resumes import RESUME_PDF, upload

NOON = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
TINY = Limit("test_scope", 3, timedelta(hours=1))


def count(db: Session, scope: str) -> int | None:
    return db.scalar(select(func.sum(UsageCounter.count)).where(UsageCounter.scope == scope))


# ---------- windows and the counter ----------


@pytest.mark.parametrize("now, window, start", [
    (datetime(2026, 10, 3, 12, 34, 56, tzinfo=UTC), timedelta(hours=1),
     datetime(2026, 10, 3, 12, 0, tzinfo=UTC)),
    (datetime(2026, 10, 3, 12, 34, 56, tzinfo=UTC), timedelta(minutes=15),
     datetime(2026, 10, 3, 12, 30, tzinfo=UTC)),
    (datetime(2026, 10, 3, 23, 59, 59, tzinfo=UTC), DAY, datetime(2026, 10, 3, tzinfo=UTC)),
    (datetime(2026, 10, 4, 0, 0, 0, tzinfo=UTC), DAY, datetime(2026, 10, 4, tzinfo=UTC)),
])
def test_windows_are_aligned_in_utc(now: datetime, window: timedelta, start: datetime) -> None:
    assert window_start(now, window) == start


def test_the_limit_is_exact_and_refusals_are_not_counted(db_session: Session) -> None:
    for _ in range(TINY.max):
        consume(db_session, TINY, "k", NOON + timedelta(minutes=10))
    for _ in range(3):  # refused again and again: still exactly the limit
        with pytest.raises(LimitReached) as exc:
            consume(db_session, TINY, "k", NOON + timedelta(minutes=10))
        assert (exc.value.scope, exc.value.retry_after) == ("test_scope", 50 * 60)
    assert count(db_session, "test_scope") == TINY.max


def test_keys_and_scopes_are_independent(db_session: Session) -> None:
    for _ in range(TINY.max):
        consume(db_session, TINY, "a", NOON)
    consume(db_session, TINY, "b", NOON)
    consume(db_session, Limit("other_scope", 1, timedelta(hours=1)), "a", NOON)
    with pytest.raises(LimitReached):
        consume(db_session, TINY, "a", NOON)


def test_a_new_window_starts_from_zero_and_ended_windows_are_deleted(db_session: Session) -> None:
    for _ in range(TINY.max):
        consume(db_session, TINY, "k", NOON)
    consume(db_session, TINY, "k", NOON + timedelta(hours=1))  # the next window
    rows = db_session.execute(select(UsageCounter.window_start, UsageCounter.count)
                              .where(UsageCounter.scope == "test_scope")).all()
    assert rows == [(NOON + timedelta(hours=1), 1)]  # the ended window was deleted


def test_a_zero_limit_refuses_everything(db_session: Session) -> None:
    with pytest.raises(LimitReached):
        consume(db_session, Limit("zero", 0, DAY), "k", NOON)
    assert count(db_session, "zero") is None


def test_network_keys() -> None:
    v6 = network_key("2001:db8:1:2:aaaa::1")
    assert v6 == network_key("2001:db8:1:2:bbbb::9")  # one /64
    assert v6 != network_key("2001:db8:1:3::1")
    assert network_key("203.0.113.7") == network_key("::ffff:203.0.113.7")
    assert network_key("203.0.113.7") != network_key("203.0.113.8")
    assert network_key("testclient") == network_key(None)  # not an address: one shared key
    key = network_key("203.0.113.7")
    assert len(key) == 64 and "203" not in key  # never the address itself


# ---------- registration and login (per network, before any password work) ----------


def register(client: TestClient, email: str):
    return client.post("/api/auth/register",
                       json={"name": "Asha", "email": email, "password": "correct horse"})


def test_registration_is_limited_per_network(client: TestClient, db_session: Session,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_routes, "REGISTER", Limit(REGISTER.scope, 2, REGISTER.window))
    assert [register(client, f"r{i}@example.com").status_code for i in range(2)] == [201, 201]
    res = register(client, "r2@example.com")
    assert res.status_code == 429
    assert res.json()["detail"].startswith("Too many accounts were created from your network.")
    assert 1 <= int(res.headers["Retry-After"]) <= 3600
    assert db_session.scalar(select(func.count()).select_from(Candidate)
                             .where(Candidate.email == "r2@example.com")) == 0


def test_login_is_limited_per_network_before_any_password_is_checked(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fake_supabase import FakeSupabaseAuthService

    monkeypatch.setattr(auth_routes, "LOGIN", Limit(LOGIN.scope, 3, LOGIN.window))
    assert register(client, "known@example.com").status_code == 201
    verify_res = verify_email_flow(client, "verify_known@example.com")
    assert verify_res.status_code == 303

    checks = []
    real_login = FakeSupabaseAuthService.login

    async def mock_login(self, email: str, password: str):
        checks.append(1)
        return await real_login(self, email, password)

    monkeypatch.setattr(FakeSupabaseAuthService, "login", mock_login)
    attempt = lambda email: client.post(  # noqa: E731
        "/api/auth/login", json={"email": email, "password": "wrong password"})
    assert [attempt("known@example.com").status_code for _ in range(3)] == [401, 401, 401]
    assert len(checks) == 3
    known, unknown = attempt("known@example.com"), attempt("nobody@example.com")
    # The same answer whether or not the email exists, and no password was checked for either.
    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json())
    assert known.status_code == 429 and "Retry-After" in known.headers
    assert re.search(r"Please try again in (1 minute|([2-9]|1[0-5]) minutes)\.$", known.json()["detail"])
    assert len(checks) == 3


def test_a_correct_password_is_throttled_too(client: TestClient,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_routes, "LOGIN", Limit(LOGIN.scope, 1, LOGIN.window))
    assert register(client, "ok@example.com").status_code == 201
    verify_res = verify_email_flow(client, "verify_ok@example.com")
    assert verify_res.status_code == 303
    good = {"email": "ok@example.com", "password": "correct horse"}
    assert client.post("/api/auth/login", json=good).status_code == 200
    assert client.post("/api/auth/login", json=good).status_code == 429


# ---------- resume uploads and AI extraction (per candidate) ----------


@pytest.fixture
def signed_in(client: TestClient) -> str:
    res = register(client, "resumes@example.com")
    assert res.status_code == 201
    verify_res = verify_email_flow(client, "verify_resumes@example.com")
    assert verify_res.status_code == 303
    me_res = client.get("/api/auth/me")
    assert me_res.status_code == 200
    return me_res.json()["id"]


def test_uploads_are_limited_per_candidate_per_day(
    client: TestClient, signed_in: str, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resume_routes, "RESUME_UPLOAD", Limit("resume_upload", 2, DAY))
    assert [upload(client).status_code for _ in range(2)] == [201, 201]
    # Deleting a resume gives nothing back.
    first = client.get(f"/api/candidates/{signed_in}/resumes").json()[0]["id"]
    assert client.delete(f"/api/resumes/{first}").status_code == 204
    res = upload(client)
    assert res.status_code == 429 and "Retry-After" in res.headers
    assert "resume uploads" in res.json()["detail"]
    assert db_session.scalar(select(func.count()).select_from(Resume)) == 1


def test_an_invalid_file_is_refused_before_it_is_counted(
    client: TestClient, signed_in: str, db_session: Session
) -> None:
    assert upload(client, b"not a pdf").status_code == 415
    assert count(db_session, "resume_upload") is None


def extraction(client: TestClient, resume_id: str):
    return client.post(f"/api/resumes/{resume_id}/ai-extraction")


def test_extraction_repeats_are_each_charged_and_then_refused_without_a_call(
    client: TestClient, signed_in: str, use_gateway, db_session: Session,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resume_routes, "AI_EXTRACTION", Limit("ai_extraction", 2, DAY))
    provider = FakeProvider("groq", {"facts": []})
    use_gateway(make_gateway(provider, retries=0))
    resume_id = upload(client).json()["id"]
    assert [extraction(client, resume_id).status_code for _ in range(2)] == [200, 200]
    res = extraction(client, resume_id)  # the same resume again
    assert res.status_code == 429 and "AI extractions" in res.json()["detail"]
    assert len(provider.calls) == 2
    assert (count(db_session, "ai_extraction"), count(db_session, "ai_global")) == (2, 2)


def test_a_provider_failure_still_counts(client: TestClient, signed_in: str, use_gateway,
                                         db_session: Session) -> None:
    from app.ai.gateway import ErrorKind
    use_gateway(make_gateway(FakeProvider("groq", ErrorKind.SERVER_ERROR), retries=0))
    resume_id = upload(client).json()["id"]
    assert extraction(client, resume_id).status_code == 503
    assert count(db_session, "ai_extraction") == 1  # the cost was incurred


@pytest.fixture
def budget_of():
    def _set(n: int) -> None:
        settings = get_settings().model_copy(update={"ai_daily_request_limit": n})
        app.dependency_overrides[get_settings] = lambda: settings
    yield _set
    app.dependency_overrides.pop(get_settings, None)


def test_the_global_budget_refuses_extraction(client: TestClient, signed_in: str, use_gateway,
                                              budget_of, db_session: Session) -> None:
    budget_of(0)
    provider = FakeProvider("groq", {"facts": []})
    use_gateway(make_gateway(provider, retries=0))
    resume_id = upload(client).json()["id"]
    res = extraction(client, resume_id)
    assert res.status_code == 429 and "daily limit on this server" in res.json()["detail"]
    assert provider.calls == []
    # Refused as a whole: the candidate's own limit was not charged either.
    assert count(db_session, "ai_extraction") is None


# ---------- planning, wording and evaluation: quota reached = the deterministic path ----------


def test_planning_quota_survives_deleting_and_recreating_sessions(
    client: TestClient, sid: str, use_gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_routes, "AI_PLANNING", Limit("ai_planning", 1, DAY))
    provider = FakeProvider("groq", VALID)
    use_gateway(make_gateway(provider, retries=0))
    consented(client, sid)
    assert plan(client, sid).json()["plan"]["source"] == "AI"
    assert client.delete(f"/api/interview-sessions/{sid}").status_code == 204

    again = client.post("/api/interview-sessions", json={}).json()["id"]
    client.post(f"/api/interview-sessions/{again}/actions", json={"action": "CONFIRM_PROFILE"})
    consented(client, again)
    body = plan(client, again).json()
    assert (body["status"], body["plan"]["source"], body["plan"]["rules_reason"],
            body["model_calls"]) == ("READY", "RULES", "QUOTA_REACHED", [])
    assert len(provider.calls) == 1


def test_the_global_budget_applies_to_every_candidate(
    client: TestClient, sid: str, use_gateway, budget_of
) -> None:
    budget_of(0)
    provider = FakeProvider("groq", VALID)
    use_gateway(make_gateway(provider, retries=0))
    consented(client, sid)
    assert plan(client, sid).json()["plan"]["rules_reason"] == "QUOTA_REACHED"
    assert provider.calls == []


def test_wording_quota_uses_the_template(
    client: TestClient, started: str, use_gateway, db_session: Session,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_routes, "AI_WORDING", Limit("ai_wording", 0, DAY))
    provider = FakeProvider("groq", {"question": "Unused?", "anchor": "unused"})
    use_gateway(make_gateway(provider, retries=0))
    consented(client, started)
    q = first_question(client, started)
    assert ans(client, started, q["id"]).status_code == 200
    f = first_question(client, started)
    assert (f["kind"], f["source"], f["rules_reason"]) == ("FOLLOW_UP", "RULES", "QUOTA_REACHED")
    assert provider.calls == []
    stored = db_session.scalar(text("SELECT rules_reason FROM interview_questions WHERE id = :i"),
                               {"i": f["id"]})
    assert stored == "QUOTA_REACHED"


def test_evaluation_quota_gives_observations_with_a_notice(
    client: TestClient, started: str, finished: dict, use_gateway,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_routes, "AI_EVALUATION", Limit("ai_evaluation", 0, DAY))
    provider = FakeProvider("groq", {})
    use_gateway(make_gateway(provider, retries=0))
    consented(client, started)
    res = client.post(f"/api/interview-sessions/{started}/questions/"
                      f"{finished['tech']['id']}/evaluation", json={}).json()
    assert (res["status"], res["evaluation"]["rules_reason"], res["notice"]) == (
        "OBSERVATIONS_ONLY", "QUOTA_REACHED", QUOTA_USED)
    assert provider.calls == []
    assert TECH not in QUOTA_USED


# ---------- real concurrency ----------


def test_a_parallel_burst_admits_exactly_the_limit() -> None:
    key = uuid.uuid4().hex
    limit = Limit("burst_test", 5, DAY)
    results: list[str] = []
    barrier = threading.Barrier(12)

    def attempt() -> None:
        with SessionLocal() as db:
            barrier.wait()
            try:
                consume(db, limit, key)
                db.commit()
                results.append("ok")
            except LimitReached:
                db.rollback()
                results.append("refused")

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results) == ["ok"] * 5 + ["refused"] * 7
    with SessionLocal() as db:
        assert db.scalar(select(UsageCounter.count).where(UsageCounter.key == key)) == 5


def test_limits_hold_across_the_app_restarting() -> None:
    """The count lives in PostgreSQL: a new client (or process) sees the same usage."""
    key = uuid.uuid4().hex
    limit = Limit("restart_test", 1, DAY)
    with SessionLocal() as db:
        consume(db, limit, key)
        db.commit()
    with SessionLocal() as db, pytest.raises(LimitReached):
        consume(db, limit, key)


def test_no_client_sees_counters_or_keys(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(auth_routes, "REGISTER", Limit(REGISTER.scope, 0, REGISTER.window))
    res = api_client().post("/api/auth/register",
                            json={"name": "A", "email": "x@example.com", "password": "abcdefgh"})
    assert res.status_code == 429
    assert set(res.json()) == {"detail"} and "register_ip" not in res.text
