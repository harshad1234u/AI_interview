"""Planning API end to end: HTTP -> service -> agent -> gateway (fake providers) -> PostgreSQL.

Covers trust (approved facts only), fallback, state checks, things changing during the model call,
rollback/recovery, privacy of logs and prompts, and real concurrency on committed data.
"""

import json
import logging
import threading
import uuid
from collections.abc import Iterator

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.ai.gateway import ErrorKind, RawCompletion
from app.ai.providers import get_model_gateway
from app.core.config import Settings
from app.db.session import SessionLocal, engine
from app.main import app, create_app
from app.models import Candidate, CandidateFact, InterviewSession, ProviderCall, Resume
from app.models.interview_session import InterviewStatus as S
from app.models.resume import FactCategory as FC, FactSource, FactStatus, ResumeStatus
from app.services.interview_state_machine import SessionEvent as E, apply_event, create_session
from tests.auth_helpers import make_candidate, owner_client
from tests.fakes import FakeProvider, make_gateway

NAME, EMAIL = "Asha Rao", "asha.plan@example.com"
SOURCE_LINE = "TrustLens - phishing detection browser extension (team of 3)"
SETTINGS = {"target_role": "SOFTWARE_DEVELOPER_INTERN", "experience_level": "STUDENT",
            "mode": "MIXED", "difficulty": "MEDIUM", "duration_minutes": 15}
VALID = {"topics": [
    {"competency": "web_development", "topic": "React state", "fact_refs": ["F2"],
     "question_templates": ["How do you manage state in React?"],
     "expected_concepts": ["hooks", "props"]},
    {"competency": "project_understanding", "topic": "TrustLens architecture", "fact_refs": ["F1"],
     "question_templates": ["Explain the architecture of TrustLens."], "expected_concepts": []},
    {"competency": "behavioral", "topic": "Teamwork", "fact_refs": [],
     "question_templates": ["Describe a time you worked in a team."], "expected_concepts": []},
    {"competency": "hr", "topic": "Motivation", "fact_refs": [],
     "question_templates": ["Why this internship?"], "expected_concepts": []},
]}
INVENTED = {"topics": [{**VALID["topics"][1], "fact_refs": [], "topic": "Your Kubernetes platform"}]}


# ---------- helpers ----------


def add_facts(db: Session, candidate_id: object, *facts: tuple[FC, str, FactStatus]) -> list:
    resume = Resume(candidate_id=candidate_id, parsed_json={}, upload_status=ResumeStatus.PROCESSED)
    resume.facts = [CandidateFact(category=c, fact=f, extracted_fact=f, source=FactSource.RESUME,
                                  source_section="Projects", source_text=SOURCE_LINE, status=s)
                    for c, f, s in facts]
    db.add(resume)
    db.commit()
    return [f.id for f in resume.facts]


def plan(client: TestClient, session_id: object, **overrides: object):
    return client.post(f"/api/interview-sessions/{session_id}/plan",
                       json={**SETTINGS, **overrides})


def consented(client: TestClient, session_id: object) -> object:
    """Grant the session's AI consent (Phase 7: it replaced the request's use_ai flag)."""
    res = client.put(f"/api/interview-sessions/{session_id}/ai-consent", json={"granted": True})
    assert res.status_code == 200
    return session_id


def session_row(db: Session, session_id: object) -> tuple:
    return tuple(db.execute(text(
        "SELECT status, plan, plan_provider_call_id FROM interview_sessions WHERE id = :i"
    ), {"i": session_id}).one())


class HookProvider(FakeProvider):
    """Runs `hook` when the model is called: simulates changes made during the model call."""

    def __init__(self, hook, *script: object) -> None:
        super().__init__("groq", *script)
        self.hook = hook

    async def complete(self, request, json_schema, max_output_tokens):
        self.hook()
        return await super().complete(request, json_schema, max_output_tokens)


@pytest.fixture
def use_gateway() -> Iterator:
    def _use(gateway) -> None:
        app.dependency_overrides[get_model_gateway] = lambda: gateway

    yield _use
    app.dependency_overrides.pop(get_model_gateway, None)


@pytest.fixture
def candidate(client: TestClient, db_session: Session) -> str:
    return make_candidate(client, db_session, EMAIL, NAME)


@pytest.fixture
def facts(db_session: Session, candidate: str) -> list:
    return add_facts(db_session, candidate,
                     (FC.PROJECT, "TrustLens", FactStatus.APPROVED),
                     (FC.SKILL, "React", FactStatus.APPROVED),
                     (FC.SKILL, "Kubernetes", FactStatus.REJECTED))


@pytest.fixture
def sid(client: TestClient, candidate: str, facts: list) -> str:
    """A session in INTERVIEW_PLANNING (profile confirmed), with a pending fact arriving after."""
    session_id = client.post("/api/interview-sessions", json={}).json()["id"]
    res = client.post(f"/api/interview-sessions/{session_id}/actions",
                      json={"action": "CONFIRM_PROFILE"})
    assert res.json()["status"] == "INTERVIEW_PLANNING"
    return session_id


# ---------- happy paths ----------


def test_rules_plan_moves_the_session_to_ready(client: TestClient, sid: str,
                                                db_session: Session) -> None:
    res = plan(client, sid)  # no AI consent given (the default): no model
    assert res.status_code == 200
    body = res.json()
    assert (body["status"], body["model_calls"]) == ("READY", [])
    p = body["plan"]
    assert (p["source"], p["rules_reason"], p["omitted_groups"]) == ("RULES", "NOT_REQUESTED", [])
    assert p["settings"] == SETTINGS
    assert 1 <= len(p["topics"]) <= 5
    status, stored, call = session_row(db_session, sid)
    assert (status, stored, call) == ("READY", p, None)
    assert client.get(f"/api/interview-sessions/{sid}/plan").json() == {
        "session_id": sid, "status": "READY", "plan": p}
    assert client.get(f"/api/interview-sessions/{sid}").json()["status"] == "READY"


def test_ai_plan_is_validated_stored_and_linked_to_its_call(
    client: TestClient, sid: str, db_session: Session, use_gateway
) -> None:
    provider = FakeProvider("groq", VALID)
    use_gateway(make_gateway(provider))
    res = plan(client, consented(client, sid))
    assert res.status_code == 200
    p, [call] = res.json()["plan"], res.json()["model_calls"]
    assert (p["source"], p["rules_reason"]) == ("AI", None)
    assert [t["basis"] for t in p["topics"]] == ["CANDIDATE_FACT", "CANDIDATE_FACT", "GENERIC",
                                                 "GENERIC"]
    assert [f["fact"] for f in p["topics"][1]["facts"]] == ["TrustLens"]
    assert (call["provider"], call["model"], call["fallback_used"]) == ("groq", "fake-model-1", False)
    stored_call = db_session.scalar(select(ProviderCall).where(
        ProviderCall.request_id == call["request_id"]))
    assert stored_call.capability == "INTERVIEW_PLANNING" and stored_call.succeeded
    assert session_row(db_session, sid)[2] == stored_call.id


def test_only_approved_facts_reach_the_model_and_nothing_personal(
    client: TestClient, sid: str, candidate: str, db_session: Session, use_gateway
) -> None:
    add_facts(db_session, candidate, (FC.PROJECT, "SecretPendingProject", FactStatus.PENDING_REVIEW))
    provider = FakeProvider("groq", VALID)
    use_gateway(make_gateway(provider))
    assert plan(client, consented(client, sid)).status_code == 200
    [request] = provider.calls
    sent = request.system_instruction + request.user_content
    assert "TrustLens" in request.user_content and "React" in request.user_content
    for private in ("SecretPendingProject", "Kubernetes", NAME, EMAIL, SOURCE_LINE, candidate, sid):
        assert private not in sent


def test_snapshots_never_become_facts_and_go_with_the_candidate(
    client: TestClient, sid: str, candidate: str, db_session: Session
) -> None:
    def facts_now() -> list:
        return list(db_session.execute(select(CandidateFact.fact, CandidateFact.status)
                                       .order_by(CandidateFact.fact)))
    before = facts_now()
    assert plan(client, sid, mode="PROJECT_DEEP_DIVE").status_code == 200
    assert facts_now() == before  # no fact added, approved or changed by planning
    assert client.delete(f"/api/candidates/{candidate}").status_code == 204
    assert db_session.scalar(select(func.count()).select_from(InterviewSession)
                             .where(InterviewSession.id == sid)) == 0


def test_mixed_without_an_approved_project_plans_the_other_groups(
    client: TestClient, db_session: Session
) -> None:
    cid = make_candidate(client, db_session, "np@example.com", "N P")
    sid = client.post("/api/interview-sessions", json={}).json()["id"]
    client.post(f"/api/interview-sessions/{sid}/actions", json={"action": "CONFIRM_PROFILE"})
    p = plan(client, sid).json()["plan"]
    assert p["omitted_groups"] == ["PROJECT"]
    assert {t["question_type"] for t in p["topics"]} == {"TECHNICAL", "BEHAVIORAL", "HR"}
    assert all(t["facts"] == [] and t["basis"] == "GENERIC" for t in p["topics"])


# ---------- AI failure never blocks planning, never stores invalid output ----------


def test_ai_not_configured_uses_the_rules_plan(client: TestClient, sid: str) -> None:
    body = plan(client, consented(client, sid)).json()  # conftest: AI_PROVIDERS is empty
    assert (body["plan"]["source"], body["plan"]["rules_reason"]) == ("RULES", "NOT_CONFIGURED")


def test_all_providers_failing_records_the_call_and_uses_the_rules_plan(
    client: TestClient, sid: str, db_session: Session, use_gateway
) -> None:
    use_gateway(make_gateway(FakeProvider("gemini", ErrorKind.AUTH),
                             FakeProvider("groq", ErrorKind.RATE_LIMITED)))
    body = plan(client, consented(client, sid)).json()
    assert (body["status"], body["plan"]["source"], body["plan"]["rules_reason"]) == (
        "READY", "RULES", "PROVIDERS_FAILED")
    [call] = body["model_calls"]
    assert (call["provider"], call["error_type"]) == (None, "RATE_LIMITED")
    stored = db_session.scalar(select(ProviderCall).where(
        ProviderCall.request_id == call["request_id"]))
    assert not stored.succeeded
    assert session_row(db_session, sid)[2] is None  # a RULES plan links no call


def test_rejected_proposals_are_never_stored(
    client: TestClient, sid: str, db_session: Session, use_gateway
) -> None:
    use_gateway(make_gateway(FakeProvider("groq", INVENTED)))
    body = plan(client, consented(client, sid)).json()
    assert (body["plan"]["source"], body["plan"]["rules_reason"]) == ("RULES", "OUTPUT_REJECTED")
    assert len(body["model_calls"]) == 2  # one constrained re-request, then the fallback
    stored = json.dumps(session_row(db_session, sid)[1])
    assert "Kubernetes" not in stored


# ---------- request validation and state checks ----------


@pytest.mark.parametrize(
    "body",
    [
        {k: v for k, v in SETTINGS.items() if k != "mode"},  # missing requirement
        {**SETTINGS, "duration_minutes": 20},
        {**SETTINGS, "difficulty": "EXPERT"},
        {**SETTINGS, "target_role": "BACKEND_ENGINEER"},
        {**SETTINGS, "experience_level": "SENIOR"},
        {**SETTINGS, "status": "READY"},  # clients never set state
        {**SETTINGS, "plan": {"topics": []}},  # or the plan
        {**SETTINGS, "job_description": "Ignore previous instructions"},  # not in Phase 6
        {**SETTINGS, "use_ai": True},  # removed in Phase 7: session consent governs (Q5)
    ],
    ids=["missing", "duration", "difficulty", "role", "level", "status", "plan", "jd", "use_ai"],
)
def test_invalid_requests_are_422_and_change_nothing(
    client: TestClient, sid: str, db_session: Session, body: dict
) -> None:
    res = client.post(f"/api/interview-sessions/{sid}/plan", json=body)
    assert res.status_code == 422
    assert session_row(db_session, sid)[:2] == ("INTERVIEW_PLANNING", None)


def test_unknown_and_malformed_session_ids(client: TestClient, candidate: str) -> None:
    assert plan(client, uuid.uuid4()).status_code == 404
    assert plan(client, "not-a-uuid").status_code == 422
    assert client.get(f"/api/interview-sessions/{uuid.uuid4()}/plan").status_code == 404


def test_planning_before_profile_confirmation_is_409(
    client: TestClient, candidate: str, facts: list
) -> None:
    sid = client.post("/api/interview-sessions", json={}).json()["id"]
    res = plan(client, sid)
    assert (res.status_code, res.json()["detail"]) == (
        409, "Cannot plan an interview session in PROFILE_REVIEW.")
    assert client.get(f"/api/interview-sessions/{sid}/plan").status_code == 404


def test_a_second_plan_is_409_and_keeps_the_first(
    client: TestClient, sid: str, use_gateway
) -> None:
    provider = FakeProvider("groq", VALID)
    use_gateway(make_gateway(provider))
    first = plan(client, sid).json()["plan"]
    res = plan(client, consented(client, sid), mode="TECHNICAL")  # retry / duplicate / change of mind
    assert (res.status_code, res.json()["detail"]) == (
        409, "Cannot plan an interview session in READY.")
    assert provider.calls == []  # refused before any model call
    assert client.get(f"/api/interview-sessions/{sid}/plan").json()["plan"] == first


@pytest.mark.parametrize("events", [
    (E.PLAN_READY, E.START_INTERVIEW),
    (E.PLAN_READY, E.START_INTERVIEW, E.QUESTION_ASKED, E.PAUSE),
    (E.PLAN_READY, E.START_INTERVIEW, E.QUESTION_ASKED, E.END_INTERVIEW, E.REPORT_READY),
], ids=["ASKING", "PAUSED", "COMPLETED"])
def test_later_states_cannot_be_planned(
    client: TestClient, sid: str, db_session: Session, events: tuple
) -> None:
    from tests.test_interview_sessions import PLAN, plan_for
    for event in events:
        apply_event(db_session, uuid.UUID(sid), event, plan=plan_for(event))
        db_session.commit()
    assert plan(client, sid).status_code == 409
    assert session_row(db_session, sid)[1] == PLAN  # untouched


def test_project_deep_dive_without_approved_project_asks_first(
    client: TestClient, db_session: Session, use_gateway
) -> None:
    cid = make_candidate(client, db_session, "pd@example.com", "P D")
    add_facts(db_session, cid, (FC.PROJECT, "Unreviewed", FactStatus.REJECTED))
    sid = client.post("/api/interview-sessions", json={}).json()["id"]
    client.post(f"/api/interview-sessions/{sid}/actions", json={"action": "CONFIRM_PROFILE"})
    provider = FakeProvider("groq", VALID)
    use_gateway(make_gateway(provider))
    res = plan(client, consented(client, sid), mode="PROJECT_DEEP_DIVE")
    assert res.status_code == 409 and "approved project" in res.json()["detail"]
    assert provider.calls == []  # no credits spent on a plan that can't be grounded
    assert session_row(db_session, sid)[:2] == ("INTERVIEW_PLANNING", None)


# ---------- things that change while the model is running ----------


def test_cited_fact_rejected_during_the_call_is_409(
    client: TestClient, sid: str, facts: list, db_session: Session, use_gateway
) -> None:
    def candidate_rejects_trustlens() -> None:
        db_session.execute(text("UPDATE candidate_facts SET status = 'REJECTED' WHERE id = :i"),
                           {"i": facts[0]})
        db_session.commit()

    use_gateway(make_gateway(HookProvider(candidate_rejects_trustlens, VALID)))
    res = plan(client, consented(client, sid))
    assert res.status_code == 409 and "profile changed" in res.json()["detail"]
    assert session_row(db_session, sid)[:2] == ("INTERVIEW_PLANNING", None)
    assert db_session.scalar(select(func.count()).select_from(ProviderCall)
                             .where(ProviderCall.capability == "INTERVIEW_PLANNING")) == 1


def test_cited_fact_edited_during_the_call_is_409(
    client: TestClient, sid: str, facts: list, db_session: Session, use_gateway
) -> None:
    def candidate_edits_react() -> None:
        db_session.execute(text("UPDATE candidate_facts SET fact = 'React Native' WHERE id = :i"),
                           {"i": facts[1]})
        db_session.commit()

    use_gateway(make_gateway(HookProvider(candidate_edits_react, VALID)))
    assert plan(client, consented(client, sid)).status_code == 409
    assert session_row(db_session, sid)[:2] == ("INTERVIEW_PLANNING", None)
    # Recovery: simply plan again (the model now sees the edited fact).
    assert plan(client, sid).status_code == 200


def test_session_deleted_during_the_call_is_404_and_the_call_is_still_recorded(
    client: TestClient, sid: str, db_session: Session, use_gateway
) -> None:
    def candidate_discards_session() -> None:
        db_session.execute(text("DELETE FROM interview_sessions WHERE id = :i"), {"i": sid})
        db_session.commit()

    use_gateway(make_gateway(HookProvider(candidate_discards_session, VALID)))
    assert plan(client, consented(client, sid)).status_code == 404
    assert db_session.scalar(select(func.count()).select_from(ProviderCall)
                             .where(ProviderCall.capability == "INTERVIEW_PLANNING")) == 1


def test_session_planned_by_another_request_during_the_call_keeps_that_plan(
    client: TestClient, sid: str, db_session: Session, use_gateway
) -> None:
    from tests.test_interview_sessions import PLAN

    def other_request_wins() -> None:
        apply_event(db_session, uuid.UUID(sid), E.PLAN_READY, plan=PLAN)
        db_session.commit()

    use_gateway(make_gateway(HookProvider(other_request_wins, VALID)))
    res = plan(client, consented(client, sid))
    assert (res.status_code, res.json()["detail"]) == (
        409, "Cannot plan an interview session in READY.")
    assert session_row(db_session, sid)[:2] == ("READY", PLAN)


# ---------- rollback and recovery ----------


def test_failed_commit_stores_nothing_and_planning_can_be_retried(
    client: TestClient, sid: str, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_commit, calls = db_session.commit, []

    def commit_fails_on_save() -> None:
        calls.append(1)
        if len(calls) == 2:  # 1st: end of the read phase; 2nd: saving plan + READY
            raise OperationalError("COMMIT", {}, Exception("connection lost"))
        real_commit()

    monkeypatch.setattr(db_session, "commit", commit_fails_on_save)
    assert plan(client, sid).status_code == 503
    monkeypatch.undo()
    # Same state as a crash between CONFIRM_PROFILE and the plan: nothing half-written.
    assert session_row(db_session, sid) == ("INTERVIEW_PLANNING", None, None)
    assert plan(client, sid).json()["status"] == "READY"


def test_plan_ready_cannot_be_applied_without_a_plan(sid: str, db_session: Session) -> None:
    with pytest.raises(ValueError, match="PLAN_READY needs the validated plan"):
        apply_event(db_session, uuid.UUID(sid), E.PLAN_READY)
    with pytest.raises(ValueError):
        apply_event(db_session, uuid.UUID(sid), E.CONFIRM_PROFILE, plan={"version": 1})
    assert session_row(db_session, sid)[:2] == ("INTERVIEW_PLANNING", None)


def test_database_unavailable_is_503(dead_database_client: TestClient) -> None:
    sid = uuid.uuid4()
    for res in (dead_database_client.post(f"/api/interview-sessions/{sid}/plan", json=SETTINGS),
                dead_database_client.get(f"/api/interview-sessions/{sid}/plan")):
        assert res.status_code == 503


def test_plan_routes_served_in_production() -> None:
    # AUDIT-001: routers ARE registered in production, so unauthenticated requests return 401 (not 404)
    prod = TestClient(create_app(
        Settings(_env_file=None, database_url="postgresql+psycopg://u:p@h/x", environment="production")
    ), headers={"Origin": "http://localhost:3000"})
    sid = uuid.uuid4()
    assert prod.post(f"/api/interview-sessions/{sid}/plan", json=SETTINGS).status_code == 401
    assert prod.get(f"/api/interview-sessions/{sid}/plan").status_code == 401


def test_logs_carry_ids_and_codes_only(
    client: TestClient, sid: str, use_gateway, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    use_gateway(make_gateway(FakeProvider("groq", INVENTED, VALID)))  # one rejection, then ok
    assert plan(client, consented(client, sid)).status_code == 200
    assert "project_topic_needs_one_project" in caplog.text and sid in caplog.text
    for private in ("TrustLens", "React", "Kubernetes", "Teamwork", "Explain the architecture",
                    NAME, EMAIL, SOURCE_LINE):
        assert private not in caplog.text


# ---------- real concurrency: separate, committed connections ----------


class GatedProvider:
    """Answers only once released, so two requests can be held inside the model call at once."""

    name, model = "groq", "gated-model"

    def __init__(self, output: dict) -> None:
        self.output = output
        self.arrived = threading.Semaphore(0)
        self.release = threading.Event()

    async def complete(self, request, json_schema, max_output_tokens) -> RawCompletion:
        self.arrived.release()
        while not self.release.is_set():
            await anyio.sleep(0.01)
        return RawCompletion(text=json.dumps(self.output), model=self.model, finish_reason="stop")


@pytest.fixture
def committed_planning_session() -> Iterator[uuid.UUID]:
    with SessionLocal() as db:
        cand = Candidate(email=f"{uuid.uuid4().hex}@example.com", name="Concurrency Test")
        db.add(cand)
        db.commit()
        add_facts(db, cand.id, (FC.PROJECT, "TrustLens", FactStatus.APPROVED))
        session = create_session(db, cand.id)
        db.commit()
        apply_event(db, session.id, E.CONFIRM_PROFILE)
        db.commit()
        ids = (cand.id, session.id)
    try:
        yield ids[1]
    finally:
        with SessionLocal() as db:
            db.execute(text("DELETE FROM candidates WHERE id = :i"), {"i": ids[0]})
            db.execute(text("DELETE FROM provider_calls WHERE model = 'gated-model'"))
            db.commit()


def test_concurrent_plans_persist_exactly_once_without_holding_locks(
    committed_planning_session: uuid.UUID,
) -> None:
    sid = committed_planning_session
    provider = GatedProvider({"topics": [
        {"competency": "project_understanding", "topic": "TrustLens architecture",
         "fact_refs": ["F1"], "question_templates": ["Explain the architecture of TrustLens."],
         "expected_concepts": []}]})
    app.dependency_overrides[get_model_gateway] = lambda: make_gateway(provider, timeout=30)
    results: list[int] = []

    def request() -> None:
        res = owner_client(sid).post(f"/api/interview-sessions/{sid}/plan",
                                   json={**SETTINGS, "mode": "PROJECT_DEEP_DIVE"})
        results.append(res.status_code)

    threads = [threading.Thread(target=request, daemon=True) for _ in range(2)]
    consented(owner_client(sid), sid)
    try:
        for thread in threads:
            thread.start()
        for _ in threads:  # both requests are now inside the model call
            assert provider.arrived.acquire(timeout=10)
        # Neither holds a pooled connection, nor the session row lock, while the model runs.
        assert engine.pool.checkedout() == 0
        with engine.begin() as conn:
            conn.execute(text("SELECT id FROM interview_sessions WHERE id = :i FOR UPDATE NOWAIT"),
                         {"i": sid})
        provider.release.set()
        for thread in threads:
            thread.join(30)
    finally:
        provider.release.set()
        app.dependency_overrides.pop(get_model_gateway, None)
    assert sorted(results) == [200, 409]  # exactly one plan persisted
    with SessionLocal() as db:
        status, stored, call_id = session_row(db, sid)
        calls = list(db.scalars(select(ProviderCall.id).where(ProviderCall.model == "gated-model")))
    assert status == S.READY and stored["source"] == "AI"
    assert len(calls) == 2 and call_id in calls  # both calls recorded: not exactly-once execution


# ---------- selectable interview roles (Phase 1) ----------


@pytest.mark.parametrize("target_role", [
    "CYBERSECURITY_ANALYST",
    "DATA_SCIENTIST",
    "DEVOPS_ENGINEER",
    "PYTHON_DEVELOPER",
    "HR_BEHAVIORAL",
    "SOFTWARE_ENGINEER",
])
def test_valid_role_is_accepted_and_preserved(
    client: TestClient, sid: str, db_session: Session, target_role: str
) -> None:
    res = plan(client, sid, target_role=target_role)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "READY"
    assert body["plan"]["settings"]["target_role"] == target_role
    status, stored, _ = session_row(db_session, sid)
    assert status == "READY"
    assert stored["settings"]["target_role"] == target_role


def test_legacy_client_omitting_target_role_defaults_to_intern(
    client: TestClient, sid: str, db_session: Session
) -> None:
    settings_without_role = {k: v for k, v in SETTINGS.items() if k != "target_role"}
    res = client.post(f"/api/interview-sessions/{sid}/plan", json=settings_without_role)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "READY"
    assert body["plan"]["settings"]["target_role"] == "SOFTWARE_DEVELOPER_INTERN"


def test_invalid_role_is_rejected_with_422(
    client: TestClient, sid: str, db_session: Session
) -> None:
    res = plan(client, sid, target_role="ASTRONAUT_CAPTAIN")
    assert res.status_code == 422
    assert session_row(db_session, sid)[:2] == ("INTERVIEW_PLANNING", None)


def test_ai_planning_receives_selected_role(
    client: TestClient, sid: str, db_session: Session, use_gateway
) -> None:
    sec_valid = {"topics": [
        {"competency": "security_fundamentals", "topic": "Threat modeling", "fact_refs": [],
         "question_templates": ["How do you approach threat modeling?"],
         "expected_concepts": ["STRIDE", "assets"]},
        {"competency": "project_understanding", "topic": "TrustLens architecture", "fact_refs": ["F1"],
         "question_templates": ["Explain the architecture of TrustLens."], "expected_concepts": []},
        {"competency": "behavioral", "topic": "Teamwork", "fact_refs": [],
         "question_templates": ["Describe a time you worked in a team."], "expected_concepts": []},
        {"competency": "hr", "topic": "Motivation", "fact_refs": [],
         "question_templates": ["Why this role?"], "expected_concepts": []},
    ]}
    provider = FakeProvider("groq", sec_valid)
    use_gateway(make_gateway(provider))
    res = plan(client, consented(client, sid), target_role="CYBERSECURITY_ANALYST")
    assert res.status_code == 200
    [request] = provider.calls
    assert "Cybersecurity Analyst" in request.system_instruction
    assert "security_fundamentals" in request.system_instruction
    assert res.json()["plan"]["settings"]["target_role"] == "CYBERSECURITY_ANALYST"
    assert res.json()["plan"]["source"] == "AI"


