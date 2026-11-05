"""Interview sessions: API, persistence, guards, rollback and real concurrency."""

import logging
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import SessionLocal
from app.main import create_app
from app.models import Candidate, CandidateFact, InterviewSession, Resume
from app.models.interview_session import PRE_PLAN_STATUSES, InterviewStatus as S
from app.models.resume import FactCategory, FactSource, FactStatus, ResumeStatus
from app.schemas.interview_plan import PlanSettings, RulesReason
from app.services.interview_planner import fallback_plan
from tests.auth_helpers import api_client, make_candidate
from app.services.interview_state_machine import (
    ActiveSessionExists,
    InvalidTransition,
    SessionEvent as E,
    apply_event,
    create_session,
)

SYSTEM_EVENTS = ["PROFILE_PROCESSING_STARTED", "PROFILE_READY", "PLAN_READY", "QUESTION_ASKED",
                 "ANSWER_SUBMITTED", "ANSWER_PROCESSED", "ANSWER_EVALUATED", "FOLLOW_UP",
                 "NEXT_TOPIC", "REPHRASE", "FINISH", "REPORT_READY"]


# ---------- helpers ----------

# Since Phase 6, PLAN_READY must carry a validated plan (and the database requires one past
# planning). Any valid plan will do for lifecycle tests.
PLAN = fallback_plan(
    PlanSettings(target_role="SOFTWARE_DEVELOPER_INTERN", experience_level="STUDENT",
                 mode="TECHNICAL", difficulty="MEDIUM", duration_minutes=15),
    [], RulesReason.NOT_REQUESTED,
).model_dump(mode="json")


def plan_for(event: E) -> dict | None:
    return PLAN if event == E.PLAN_READY else None


def new_candidate(db: Session, email: str = "other@example.com") -> tuple[TestClient, str]:
    """Another candidate, with their own signed-in client."""
    other = api_client()
    return other, make_candidate(other, db, email)


def create(client: TestClient):
    """For the client's signed-in candidate: the body names no candidate (Phase 12)."""
    return client.post("/api/interview-sessions", json={})


def act(client: TestClient, session_id: str, action: object):
    return client.post(f"/api/interview-sessions/{session_id}/actions", json={"action": action})


def drive(db: Session, session_id: str, *events: E) -> None:
    """Apply system events the way later phases' orchestrator will."""
    for event in events:
        apply_event(db, uuid.UUID(session_id), event, plan=plan_for(event),
                    finish_reason="PLAN_COMPLETED" if event == E.FINISH else None)
        db.commit()


def add_fact(db: Session, candidate_id: object, status: FactStatus = FactStatus.PENDING_REVIEW):
    resume = Resume(candidate_id=candidate_id, parsed_json={}, upload_status=ResumeStatus.PROCESSED)
    resume.facts = [CandidateFact(category=FactCategory.SKILL, fact="Python",
                                  extracted_fact="Python", source=FactSource.RESUME,
                                  source_section="Skills", source_text="Python", status=status)]
    db.add(resume)
    db.commit()
    return resume.id, resume.facts[0].id


def row(db: Session, session_id: str) -> tuple[str, datetime]:
    """The persisted state, bypassing anything cached in the ORM session."""
    return tuple(db.execute(
        text("SELECT status, updated_at FROM interview_sessions WHERE id = :i"), {"i": session_id}
    ).one())


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


@pytest.fixture
def candidate_id(client: TestClient, db_session: Session) -> str:
    return make_candidate(client, db_session)


@pytest.fixture
def session(client: TestClient, candidate_id: str) -> dict:
    res = create(client)
    assert res.status_code == 201
    return res.json()


# ---------- create / read / list ----------


def test_create_starts_in_profile_review_and_is_persisted(
    client: TestClient, candidate_id: str, db_session: Session
) -> None:
    res = create(client)
    assert res.status_code == 201
    body = res.json()
    assert set(body) == {"id", "candidate_id", "status", "created_at", "updated_at",
                         "finish_reason", "retry_of_id", "settings", "plan_source"}
    assert (body["candidate_id"], body["status"]) == (candidate_id, "PROFILE_REVIEW")
    # Nothing is planned or retried yet: the history fields are empty.
    assert (body["finish_reason"], body["retry_of_id"], body["settings"],
            body["plan_source"]) == (None, None, None, None)
    assert body["created_at"] == body["updated_at"]
    assert row(db_session, body["id"])[0] == "PROFILE_REVIEW"
    assert client.get(f"/api/interview-sessions/{body['id']}").json() == body


@pytest.mark.parametrize(
    "payload",
    # A client-supplied candidate_id is refused, never trusted (Phase 12).
    [{"candidate_id": "not-a-uuid"}, {"candidate_id": None}, {"candidate_id": str(uuid.uuid4())},
     {"status": "COMPLETED"},  # clients never set a status
     {"id": str(uuid.uuid4())}],
    ids=["malformed-candidate", "null-candidate", "client-candidate-id", "status-field",
         "id-field"],
)
def test_create_rejects_invalid_bodies(client: TestClient, candidate_id: str,
                                       payload: dict) -> None:
    res = client.post("/api/interview-sessions", json=payload)
    assert res.status_code == 422
    assert isinstance(res.json()["detail"], list)  # FastAPI's standard validation format


def test_create_needs_a_signed_in_candidate(client: TestClient) -> None:
    res = create(client)
    assert (res.status_code, res.json()) == (401, {"detail": "Please log in."})


def test_unknown_or_malformed_session_ids(client: TestClient, candidate_id: str) -> None:
    missing = str(uuid.uuid4())
    for res in (client.get(f"/api/interview-sessions/{missing}"), act(client, missing, "PAUSE"),
                client.delete(f"/api/interview-sessions/{missing}")):
        assert (res.status_code, res.json()) == (404, {"detail": "Interview session not found"})
    assert client.get("/api/interview-sessions/not-a-uuid").status_code == 422
    assert act(client, "not-a-uuid", "PAUSE").status_code == 422


def test_list_is_scoped_to_the_candidate_and_newest_first(
    client: TestClient, candidate_id: str, session: dict, db_session: Session
) -> None:
    other_client, _ = new_candidate(db_session)
    other_session = create(other_client).json()
    # Finish the first session so a second may be created.
    assert act(client, session["id"], "CONFIRM_PROFILE").status_code == 200
    drive(db_session, session["id"], E.PLAN_READY)
    act(client, session["id"], "START_INTERVIEW")
    drive(db_session, session["id"], E.QUESTION_ASKED)
    act(client, session["id"], "END_INTERVIEW")
    drive(db_session, session["id"], E.REPORT_READY)
    second = create(client).json()

    listed = client.get(f"/api/candidates/{candidate_id}/interview-sessions").json()
    assert {s["id"] for s in listed} == {second["id"], session["id"]}
    # Newest first, ties broken by id. (Here both share one test transaction, so one now().)
    expected = sorted(listed, key=lambda s: s["id"])
    expected.sort(key=lambda s: s["created_at"], reverse=True)
    assert listed == expected
    assert other_session["id"] not in {s["id"] for s in listed}
    assert client.get(f"/api/candidates/{uuid.uuid4()}/interview-sessions").status_code == 404


# ---------- actions ----------


def test_confirm_without_any_resume_or_facts(
    client: TestClient, session: dict, db_session: Session
) -> None:
    res = act(client, session["id"], "CONFIRM_PROFILE")
    assert res.status_code == 200
    assert res.json()["status"] == "INTERVIEW_PLANNING"
    assert ts(res.json()["updated_at"]) > ts(session["updated_at"])
    assert row(db_session, session["id"]) == (
        "INTERVIEW_PLANNING", ts(res.json()["updated_at"])
    )


def test_confirm_is_blocked_while_facts_are_pending_and_never_touches_them(
    client: TestClient, candidate_id: str, session: dict, db_session: Session
) -> None:
    add_fact(db_session, uuid.UUID(candidate_id), FactStatus.REJECTED)  # reviewed: no block
    resume_id, fact_id = add_fact(db_session, uuid.UUID(candidate_id))
    before = row(db_session, session["id"])

    res = act(client, session["id"], "CONFIRM_PROFILE")
    assert res.status_code == 409
    assert res.json() == {"detail": "Review your extracted profile facts before confirming: "
                                    "1 still pending."}
    assert row(db_session, session["id"]) == before
    assert db_session.scalar(text("SELECT status FROM candidate_facts WHERE id = :i"),
                             {"i": fact_id}) == "PENDING_REVIEW"

    # The candidate reviews the fact; the guard reads the current state and lets them through.
    assert client.patch(f"/api/resumes/{resume_id}/facts/{fact_id}",
                        json={"status": "APPROVED"}).status_code == 200
    assert act(client, session["id"], "CONFIRM_PROFILE").json()["status"] == "INTERVIEW_PLANNING"


def test_another_candidates_pending_facts_do_not_block(
    client: TestClient, session: dict, db_session: Session
) -> None:
    _, other = new_candidate(db_session)
    add_fact(db_session, uuid.UUID(other))
    assert act(client, session["id"], "CONFIRM_PROFILE").status_code == 200


def test_client_lifecycle_through_the_api(
    client: TestClient, session: dict, db_session: Session
) -> None:
    sid = session["id"]
    assert act(client, sid, "CONFIRM_PROFILE").json()["status"] == "INTERVIEW_PLANNING"
    drive(db_session, sid, E.PLAN_READY)
    assert act(client, sid, "START_INTERVIEW").json()["status"] == "ASKING"
    drive(db_session, sid, E.QUESTION_ASKED)
    assert act(client, sid, "PAUSE").json()["status"] == "PAUSED"
    assert act(client, sid, "RESUME").json()["status"] == "WAITING_FOR_ANSWER"
    drive(db_session, sid, E.ANSWER_SUBMITTED, E.ANSWER_PROCESSED, E.ANSWER_EVALUATED,
          E.FOLLOW_UP, E.QUESTION_ASKED)
    assert act(client, sid, "PAUSE").json()["status"] == "PAUSED"
    assert act(client, sid, "END_INTERVIEW").json()["status"] == "REPORT_GENERATION"
    # Once the interview has ended, a late PAUSE is rejected and reports the real state.
    res = act(client, sid, "PAUSE")
    assert (res.status_code, res.json()) == (
        409, {"detail": "Cannot PAUSE an interview session in REPORT_GENERATION."}
    )
    drive(db_session, sid, E.REPORT_READY)
    for action in ("CONFIRM_PROFILE", "START_INTERVIEW", "PAUSE", "RESUME", "END_INTERVIEW"):
        assert act(client, sid, action).status_code == 409  # COMPLETED is terminal
    assert client.get(f"/api/interview-sessions/{sid}").json()["status"] == "COMPLETED"


@pytest.mark.parametrize(
    ("action", "status"),
    [("PAUSE", "PROFILE_REVIEW"), ("RESUME", "PROFILE_REVIEW"), ("END_INTERVIEW", "PROFILE_REVIEW"),
     ("START_INTERVIEW", "PROFILE_REVIEW")],
)
def test_invalid_transition_is_409_and_changes_nothing(
    client: TestClient, session: dict, db_session: Session, action: str, status: str
) -> None:
    before = row(db_session, session["id"])
    res = act(client, session["id"], action)
    assert (res.status_code, res.json()) == (
        409, {"detail": f"Cannot {action} an interview session in {status}."}
    )
    assert row(db_session, session["id"]) == before  # status AND updated_at untouched


@pytest.mark.parametrize(
    "action", [*SYSTEM_EVENTS, "pause", "SKIP", "", None, 7, ["PAUSE"], {"name": "PAUSE"}]
)
def test_system_events_and_unknown_actions_are_rejected_by_validation(
    client: TestClient, session: dict, db_session: Session, action: object
) -> None:
    before = row(db_session, session["id"])
    res = act(client, session["id"], action)
    assert res.status_code == 422
    assert isinstance(res.json()["detail"], list)
    assert row(db_session, session["id"]) == before


def test_action_body_cannot_carry_a_target_status(client: TestClient, session: dict) -> None:
    res = client.post(f"/api/interview-sessions/{session['id']}/actions",
                      json={"action": "CONFIRM_PROFILE", "status": "COMPLETED"})
    assert res.status_code == 422
    assert client.get(f"/api/interview-sessions/{session['id']}").json()["status"] == "PROFILE_REVIEW"


def test_retried_action_reports_the_current_state(client: TestClient, session: dict) -> None:
    assert act(client, session["id"], "CONFIRM_PROFILE").status_code == 200
    retry = act(client, session["id"], "CONFIRM_PROFILE")  # e.g. after a client timeout
    assert (retry.status_code, retry.json()) == (
        409, {"detail": "Cannot CONFIRM_PROFILE an interview session in INTERVIEW_PLANNING."}
    )


# ---------- one active session per candidate ----------


def test_second_active_session_is_409_with_the_candidates_own_session_id(
    client: TestClient, candidate_id: str, session: dict, db_session: Session
) -> None:
    other_client, _ = new_candidate(db_session)
    other_session = create(other_client).json()

    res = create(client)  # also what a retried create gets
    assert res.status_code == 409
    assert res.json() == {"detail": "This candidate already has an unfinished interview session: "
                                    f"{session['id']}. Resume it, or delete it to start again."}
    assert other_session["id"] not in res.json()["detail"]
    assert len(client.get(f"/api/candidates/{candidate_id}/interview-sessions").json()) == 1
    # The transaction is still usable after the duplicate: later requests work normally.
    assert client.get(f"/api/interview-sessions/{session['id']}").status_code == 200


def test_completed_session_does_not_block_a_new_one(
    client: TestClient, candidate_id: str, session: dict, db_session: Session
) -> None:
    act(client, session["id"], "CONFIRM_PROFILE")
    drive(db_session, session["id"], E.PLAN_READY)
    act(client, session["id"], "START_INTERVIEW")
    drive(db_session, session["id"], E.QUESTION_ASKED, E.ANSWER_SUBMITTED, E.ANSWER_PROCESSED,
          E.ANSWER_EVALUATED, E.FINISH, E.REPORT_READY)
    res = create(client)
    assert (res.status_code, res.json()["status"]) == (201, "PROFILE_REVIEW")


# ---------- delete / restart / candidate deletion ----------


def test_delete_then_restart(client: TestClient, candidate_id: str, session: dict) -> None:
    assert client.delete(f"/api/interview-sessions/{session['id']}").status_code == 204
    assert client.get(f"/api/interview-sessions/{session['id']}").status_code == 404
    assert client.delete(f"/api/interview-sessions/{session['id']}").status_code == 404
    restarted = create(client)  # PRD §59: restart = a new attempt
    assert restarted.status_code == 201 and restarted.json()["id"] != session["id"]


def test_deleting_the_candidate_deletes_their_sessions(
    client: TestClient, candidate_id: str, session: dict, db_session: Session
) -> None:
    assert client.delete(f"/api/candidates/{candidate_id}").status_code == 204
    # Their sign-in went too (Phase 12), so the session cannot even be asked for.
    assert client.get(f"/api/interview-sessions/{session['id']}").status_code == 401
    assert db_session.scalar(text("SELECT count(*) FROM interview_sessions")) == 0


# ---------- database constraints ----------


def _insert(db: Session, candidate_id: object, status: str) -> None:
    # Past planning a session must have a plan (Phase 6 constraint): give it a minimal one.
    plan = None if status in PRE_PLAN_STATUSES else "{}"
    with db.begin_nested():
        db.execute(text("INSERT INTO interview_sessions (candidate_id, status, plan) "
                        "VALUES (:c, :s, CAST(:p AS jsonb))"),
                   {"c": candidate_id, "s": status, "p": plan})


def test_database_enforces_status_candidate_and_one_active_session(
    client: TestClient, db_session: Session
) -> None:
    cid = make_candidate(client, db_session)
    for status, constraint in [
        ("TRUSTED", "ck_interview_sessions_status"),
        ("FOLLOW_UP", "ck_interview_sessions_status"),  # a decision, never a stored state
        ("profile_review", "ck_interview_sessions_status"),
    ]:
        with pytest.raises(IntegrityError) as exc:
            _insert(db_session, cid, status)
        assert exc.value.orig.diag.constraint_name == constraint
    with pytest.raises(IntegrityError) as exc:
        _insert(db_session, uuid.uuid4(), "PROFILE_REVIEW")
    assert exc.value.orig.diag.constraint_name == "fk_interview_sessions_candidate_id_candidates"

    _insert(db_session, cid, "COMPLETED")
    _insert(db_session, cid, "COMPLETED")  # any number of finished sessions
    _insert(db_session, cid, "PAUSED")
    with pytest.raises(IntegrityError) as exc:
        _insert(db_session, cid, "READY")
    assert exc.value.orig.diag.constraint_name == "uq_interview_sessions_one_active_per_candidate"


# ---------- failures ----------


def test_failed_commit_leaves_state_and_updated_at_unchanged(
    client: TestClient, session: dict, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = row(db_session, session["id"])

    def broken_commit() -> None:
        raise OperationalError("COMMIT", {}, Exception("connection lost"))

    monkeypatch.setattr(db_session, "commit", broken_commit)
    res = act(client, session["id"], "CONFIRM_PROFILE")
    assert res.status_code == 503
    monkeypatch.undo()
    assert row(db_session, session["id"]) == before
    # And the next attempt works normally.
    assert act(client, session["id"], "CONFIRM_PROFILE").json()["status"] == "INTERVIEW_PLANNING"


def test_database_unavailable_is_503(dead_database_client: TestClient) -> None:
    sid = uuid.uuid4()
    for res in (create(dead_database_client),
                act(dead_database_client, str(sid), "PAUSE"),
                dead_database_client.get(f"/api/interview-sessions/{sid}")):
        assert res.status_code == 503
        assert res.json() == {"detail": "Service temporarily unavailable. Please try again."}


def test_session_routes_served_in_production() -> None:
    # AUDIT-001: routers ARE registered in production, so unauthenticated requests return 401 (not 404)
    prod = TestClient(create_app(
        Settings(_env_file=None, database_url="postgresql+psycopg://u:p@h/x", environment="production")
    ), headers={"Origin": "http://localhost:3000"})
    sid = uuid.uuid4()
    assert prod.post("/api/interview-sessions", json={"candidate_id": str(sid)}).status_code == 401
    assert prod.get(f"/api/interview-sessions/{sid}").status_code == 401
    assert prod.get(f"/api/candidates/{sid}/interview-sessions").status_code == 401
    assert prod.post(f"/api/interview-sessions/{sid}/actions",
                     json={"action": "PAUSE"}).status_code == 401
    assert prod.delete(f"/api/interview-sessions/{sid}").status_code == 401


def test_logs_carry_ids_and_states_only(
    client: TestClient, candidate_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    sid = create(client).json()["id"]
    act(client, sid, "CONFIRM_PROFILE")
    assert f"Interview session {sid}: PROFILE_REVIEW -> INTERVIEW_PLANNING" in caplog.text
    assert "Asha" not in caplog.text and "asha@example.com" not in caplog.text


# ---------- real concurrency: separate, committed connections ----------


@pytest.fixture
def committed() -> Iterator[dict]:
    """A candidate + session committed for real (not inside the per-test rollback), cleaned up."""
    with SessionLocal() as db:
        candidate = Candidate(email=f"{uuid.uuid4().hex}@example.com", name="Concurrency Test")
        db.add(candidate)
        db.commit()
        session = create_session(db, candidate.id)
        db.commit()
        ids = {"candidate": candidate.id, "session": session.id}
    try:
        yield ids
    finally:
        with SessionLocal() as db:
            db.execute(text("DELETE FROM candidates WHERE id = :i"), {"i": ids["candidate"]})
            db.commit()


def run_in_thread(fn) -> tuple[threading.Thread, dict]:
    out: dict = {}

    def target() -> None:
        try:
            out["value"] = fn()
        except Exception as exc:  # reported to the test thread
            out["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, out


def advance(session_id: uuid.UUID, *events: E) -> None:
    with SessionLocal() as db:
        for event in events:
            apply_event(db, session_id, event, plan=plan_for(event))
        db.commit()


def test_conflicting_transitions_serialise_on_the_row_lock(committed: dict) -> None:
    sid = committed["session"]
    advance(sid, E.CONFIRM_PROFILE, E.PLAN_READY, E.START_INTERVIEW, E.QUESTION_ASKED)
    first, second = SessionLocal(), SessionLocal()
    try:
        # The second request read the session (WAITING_FOR_ANSWER) before the first one acted:
        # its in-memory copy goes stale, which must not matter.
        stale = second.get(InterviewSession, sid)
        assert stale.status == S.WAITING_FOR_ANSWER

        apply_event(first, sid, E.END_INTERVIEW)  # holds the row lock, not committed yet

        def pause() -> InterviewSession:
            try:
                return apply_event(second, sid, E.PAUSE)
            finally:
                second.rollback()

        thread, out = run_in_thread(pause)
        thread.join(0.5)
        assert thread.is_alive()  # blocked behind the first transaction's lock
        first.commit()
        thread.join(10)
        assert not thread.is_alive()
        # Re-checked against the committed state after the wait: PAUSE is no longer valid.
        assert isinstance(out.get("error"), InvalidTransition)
        assert out["error"].status == S.REPORT_GENERATION
    finally:
        first.close()
        second.close()
    with SessionLocal() as db:
        assert db.get(InterviewSession, sid).status == S.REPORT_GENERATION


def test_duplicate_transition_only_succeeds_once(committed: dict) -> None:
    sid = committed["session"]
    first, second = SessionLocal(), SessionLocal()
    try:
        apply_event(first, sid, E.CONFIRM_PROFILE)

        def confirm() -> InterviewSession:
            try:
                result = apply_event(second, sid, E.CONFIRM_PROFILE)
                second.commit()
                return result
            except Exception:
                second.rollback()
                raise

        thread, out = run_in_thread(confirm)
        thread.join(0.5)
        assert thread.is_alive()
        first.commit()
        thread.join(10)
        assert isinstance(out.get("error"), InvalidTransition)
        assert out["error"].status == S.INTERVIEW_PLANNING
    finally:
        first.close()
        second.close()


def test_sequential_conflicting_transitions_both_apply_in_order_with_monotonic_updated_at(
    committed: dict,
) -> None:
    sid = committed["session"]
    advance(sid, E.CONFIRM_PROFILE, E.PLAN_READY, E.START_INTERVIEW, E.QUESTION_ASKED)
    first, second = SessionLocal(), SessionLocal()
    try:
        second.execute(text("SELECT 1"))  # second's transaction starts before first's
        apply_event(first, sid, E.PAUSE)

        def end() -> InterviewSession:
            result = apply_event(second, sid, E.END_INTERVIEW)  # valid from PAUSED as well
            second.commit()
            return result

        thread, out = run_in_thread(end)
        thread.join(0.5)
        assert thread.is_alive()
        first.commit()
        paused_at = first.get(InterviewSession, sid).updated_at
        thread.join(10)
        assert "error" not in out
        assert out["value"].status == S.REPORT_GENERATION
        # Stamped when it actually ran, not when its transaction began.
        assert out["value"].updated_at > paused_at
    finally:
        first.close()
        second.close()


def test_concurrent_creates_make_exactly_one_active_session(committed: dict) -> None:
    cid = committed["candidate"]
    with SessionLocal() as db:  # free the candidate for a fresh pair of creates
        db.execute(text("DELETE FROM interview_sessions WHERE candidate_id = :c"), {"c": cid})
        db.commit()
    first, second = SessionLocal(), SessionLocal()
    try:
        created = create_session(first, cid)  # inserted, not committed

        def create_again() -> InterviewSession:
            try:
                return create_session(second, cid)
            finally:
                assert second.execute(text("SELECT 1")).scalar() == 1  # still usable
                second.rollback()

        thread, out = run_in_thread(create_again)
        thread.join(0.5)
        assert thread.is_alive()  # waits on the unique index for the first insert's outcome
        first.commit()
        thread.join(10)
        assert isinstance(out.get("error"), ActiveSessionExists)
        assert out["error"].session_id == created.id
    finally:
        first.close()
        second.close()
    with SessionLocal() as db:
        assert db.scalar(text("SELECT count(*) FROM interview_sessions WHERE candidate_id = :c"),
                         {"c": cid}) == 1


def test_pending_facts_guard_reads_the_latest_committed_facts(committed: dict) -> None:
    sid, cid = committed["session"], committed["candidate"]
    with SessionLocal() as db:
        _, fact_id = add_fact(db, cid)
    first = SessionLocal()
    try:
        # first's transaction is already open and has seen the pending fact...
        assert first.scalar(select(CandidateFact.status).where(CandidateFact.id == fact_id)) == \
            FactStatus.PENDING_REVIEW
        with SessionLocal() as other:  # ...when the candidate approves it elsewhere
            other.get(CandidateFact, fact_id).status = FactStatus.APPROVED
            other.commit()
        assert apply_event(first, sid, E.CONFIRM_PROFILE).status == S.INTERVIEW_PLANNING
        first.commit()
    finally:
        first.close()
