"""Interview turns end to end: HTTP -> interviewer -> agent -> gateway (fake providers) -> PostgreSQL.

Covers the deterministic and AI paths, the deadline and late answers, D5, consent during a call,
facts changing during a call, replays, real concurrency on committed data, recovery and privacy.
"""

import contextlib
import json
import logging
import threading
import uuid
from collections.abc import Iterator

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.ai.gateway import ErrorKind, RawCompletion
from app.ai.providers import get_model_gateway
from app.core.config import Settings
from app.db.session import SessionLocal, engine
from app.main import app, create_app
from app.models import Candidate, InterviewQuestion, ProviderCall
from app.models.resume import FactCategory as FC, FactStatus
from app.services.interview_planner import TECHNICAL_QUESTIONS, approved_facts, fallback_plan
from app.services.interview_state_machine import SessionEvent as E, apply_event, create_session
from app.services.interviewer import (
    FOLLOW_UP_TEMPLATE,
    GENERIC_FOLLOW_UP,
    LATE_ANSWER,
    REPHRASE_TEMPLATE,
    TurnContext,
    decide,
    load_history,
    resolve,
    stored_plan,
    valid_positions,
)
from app.schemas.interview_plan import Competency, PlanSettings, RulesReason
from tests.auth_helpers import owner_client
from tests.fakes import FakeProvider, make_gateway
from tests.test_interview_plan_api import (  # noqa: F401  (fixtures)
    EMAIL,
    NAME,
    SETTINGS,
    HookProvider,
    add_facts,
    candidate,
    consented,
    facts,
    sid,
    use_gateway,
)

TOO_LONG = {"detail": "Answer is too long: the limit is 10,000 characters."}
ANSWER = "I usually reason about the trade-offs first, then the implementation."
SECRET_ANSWER = "My private answer text 7f3a about trade-offs"
# Fallback MIXED plan for TrustLens + React: 1 Programming Fundamentals, 2 Data Structures,
# 3 Project: TrustLens, 4 Teamwork, 5 Motivation.
TOPIC_1 = TECHNICAL_QUESTIONS[Competency.PROGRAMMING_FUNDAMENTALS]


# ---------- helpers ----------


def nxt(client: TestClient, session_id: object, **body: object):
    return client.post(f"/api/interview-sessions/{session_id}/questions/next", json=body)


def ans(client: TestClient, session_id: object, question_id: object, kind: str = "ANSWER",
        said: str | None = ANSWER):
    body = {"question_id": str(question_id), "kind": kind}
    if said is not None:
        body["text"] = said
    return client.post(f"/api/interview-sessions/{session_id}/answers", json=body)


def act(client: TestClient, session_id: object, action: str):
    return client.post(f"/api/interview-sessions/{session_id}/actions", json={"action": action})


def run_for(db: Session, session_id: object, seconds: float) -> None:
    """Pretend the interview clock has run `seconds` longer (database time can't be pinned)."""
    db.execute(text("UPDATE interview_sessions SET active_since = active_since - "
                    "make_interval(secs => :s) WHERE id = :i"), {"s": seconds, "i": session_id})
    db.commit()


def state(db: Session, session_id: object) -> tuple:
    return tuple(db.execute(text(
        "SELECT status, finish_reason, active_seconds FROM interview_sessions WHERE id = :i"),
        {"i": session_id}).one())


def answers(db: Session, session_id: object) -> list[tuple]:
    return [tuple(r) for r in db.execute(text(
        "SELECT seq, answer_kind, answer_text, evaluation_status FROM interview_questions "
        "WHERE session_id = :i ORDER BY seq"), {"i": session_id})]


def calls(db: Session) -> int:
    return db.scalar(text(
        "SELECT count(*) FROM provider_calls WHERE capability = 'QUESTION_GENERATION'"))


@pytest.fixture
def started(client: TestClient, sid: str) -> str:
    """A planned (RULES, MIXED 15 min) interview that has just started: ASKING."""
    assert client.post(f"/api/interview-sessions/{sid}/plan", json=SETTINGS).status_code == 200
    assert act(client, sid, "START_INTERVIEW").json()["status"] == "ASKING"
    return sid


def first_question(client: TestClient, session_id: str) -> dict:
    res = nxt(client, session_id)
    assert res.status_code == 200
    return res.json()["question"]


# ---------- deterministic interview end to end (A1, K1, E1-E3, G9) ----------


def test_a_full_interview_without_consent_uses_no_model(
    client: TestClient, started: str, db_session: Session, use_gateway
) -> None:
    provider = FakeProvider("groq", {"question": "unused", "anchor": "unused"})
    use_gateway(make_gateway(provider))  # configured, but there is no consent
    first = nxt(client, started).json()
    assert first["status"] == "WAITING_FOR_ANSWER" and first["model_calls"] == []
    q = first["question"]
    assert (q["seq"], q["kind"], q["topic_position"], q["text"], q["source"]) == (
        1, "TOPIC", 1, TOPIC_1, "RULES")
    assert (q["topic"], q["competency"], q["question_type"], q["basis"]) == (
        "Programming Fundamentals", "programming_fundamentals", "TECHNICAL", "GENERIC")
    assert q["answer"] is None and "anchor" not in q and "provider_call_id" not in q
    assert first["progress"]["topics_total"] == 5
    assert 890 < first["progress"]["time_remaining_seconds"] <= 900
    seen = []
    for _ in range(30):
        q = nxt(client, started).json()["question"]
        if q is None:
            break
        seen.append((q["kind"], q["topic_position"], q["text"]))
        res = ans(client, started, q["id"], said=ANSWER if len(seen) % 2 else "No idea really.")
        assert res.status_code == 200 and res.json()["status"] == "NEXT_DECISION"  # E3
        assert res.json()["question"]["answer"]["evaluation"] == {"status": "NOT_EVALUATED"}
    final = nxt(client, started).json()
    assert (final["status"], final["finish_reason"], final["question"]) == (
        "REPORT_GENERATION", "PLAN_COMPLETED", None)
    # Term template (first vocabulary hit), the generic follow-up once (O1), no term reused (P2).
    assert seen == [
        ("TOPIC", 1, TOPIC_1),
        ("FOLLOW_UP", 1, FOLLOW_UP_TEMPLATE.format(term="implementation")),
        ("FOLLOW_UP", 1, GENERIC_FOLLOW_UP),
        ("TOPIC", 2, TECHNICAL_QUESTIONS[Competency.DATA_STRUCTURES]),
        ("TOPIC", 3, "Explain the architecture of TrustLens."),
        ("FOLLOW_UP", 3, FOLLOW_UP_TEMPLATE.format(term="trade-offs")),
        ("TOPIC", 4, "Describe a time you worked in a team. What was your role?"),
        ("TOPIC", 5, "What interests you about a Software Developer Intern role?"),
    ]
    assert provider.calls == [] and calls(db_session) == 0  # K1, G9
    history = client.get(f"/api/interview-sessions/{started}/questions").json()
    assert len(history["questions"]) == len(seen) and history["finish_reason"] == \
        "PLAN_COMPLETED"
    assert {a[3] for a in answers(db_session, started)} == {"NOT_EVALUATED"}  # E1
    for key in ("score", "evidence", "missing", "rubric", "rating"):  # E2
        assert key not in json.dumps(history)


def test_open_question_is_returned_again_without_a_new_row(
    client: TestClient, started: str, db_session: Session
) -> None:
    q = first_question(client, started)
    again = nxt(client, started).json()
    assert again["question"]["id"] == q["id"] and again["model_calls"] == []
    assert len(answers(db_session, started)) == 1


def test_finished_interview_returns_no_question(
    client: TestClient, started: str
) -> None:
    first_question(client, started)
    assert act(client, started, "END_INTERVIEW").json()["status"] == "REPORT_GENERATION"
    res = nxt(client, started).json()
    assert (res["question"], res["finish_reason"], res["status"]) == (
        None, "CANDIDATE_ENDED", "REPORT_GENERATION")  # D11: never shown as COMPLETED


# ---------- request validation and state checks (A2, A3) ----------


def test_not_started_paused_and_unknown(
    client: TestClient, sid: str, started: str
) -> None:
    q = first_question(client, started)
    assert act(client, started, "PAUSE").status_code == 200
    res = nxt(client, started)
    assert (res.status_code, res.json()) == (409, {"detail": "Resume the interview first."})
    res = ans(client, started, q["id"])
    assert (res.status_code, res.json()["detail"]) == (
        409, "Cannot answer a question in an interview session in PAUSED.")
    assert nxt(client, uuid.uuid4()).status_code == 404
    assert ans(client, uuid.uuid4(), q["id"]).status_code == 404
    assert ans(client, started, uuid.uuid4()).json() == {"detail": "Question not found"}
    assert client.get(f"/api/interview-sessions/{uuid.uuid4()}/questions").status_code == 404


def test_before_the_interview_starts(client: TestClient, candidate: str, facts: list) -> None:
    session_id = client.post("/api/interview-sessions", json={}).json()["id"]
    for _ in range(2):  # PROFILE_REVIEW, then READY
        res = nxt(client, session_id)
        assert (res.status_code, res.json()) == (409, {"detail": "The interview has not started."})
        act(client, session_id, "CONFIRM_PROFILE")
        client.post(f"/api/interview-sessions/{session_id}/plan", json=SETTINGS)
    history = client.get(f"/api/interview-sessions/{session_id}/questions").json()
    assert (history["questions"], history["progress"]["deadline_reached"]) == ([], False)


@pytest.mark.parametrize("body", [{"action": "FINISH"}, {"finish_reason": "TIME_LIMIT"},
                                  {"topic_position": 5}, {"text": "Ask me anything"}])
def test_next_question_takes_no_input(client: TestClient, started: str, body: dict) -> None:
    assert nxt(client, started, **body).status_code == 422


@pytest.mark.parametrize("extra", [{"status": "COMPLETED"}, {"score": 10},
                                   {"evaluation_status": "SCORED"}, {"kind": "FINISH"},
                                   {"finish_reason": "TIME_LIMIT"}, {"question_id": "x"}])
def test_answer_bodies_are_validated(
    client: TestClient, started: str, db_session: Session, extra: dict
) -> None:
    q = first_question(client, started)
    body = {"question_id": q["id"], "kind": "ANSWER", "text": "hello", **extra}
    res = client.post(f"/api/interview-sessions/{started}/answers", json=body)
    assert res.status_code == 422
    assert answers(db_session, started) == [(1, None, None, None)]


def test_an_answer_needs_text_other_kinds_do_not(
    client: TestClient, started: str, db_session: Session
) -> None:
    q = first_question(client, started)
    res = ans(client, started, q["id"], said="   ")
    assert (res.status_code, res.json()) == (422, {"detail": "An answer needs text."})
    assert ans(client, started, q["id"], kind="SKIP", said=None).status_code == 200
    assert answers(db_session, started) == [(1, "SKIP", None, "NOT_EVALUATED")]


# ---------- D5 oversized answers (N9) ----------


def test_answer_length_limit(client: TestClient, started: str, db_session: Session) -> None:
    q = first_question(client, started)
    before = state(db_session, started)
    res = ans(client, started, q["id"], said="é" * 10_001)
    assert (res.status_code, res.json()) == (422, TOO_LONG)
    assert answers(db_session, started) == [(1, None, None, None)]
    assert state(db_session, started) == before  # no transition, no clock change
    padded = "  \n" + "é" * 10_000 + "\t "
    res = ans(client, started, q["id"], said=padded)
    assert res.status_code == 200
    assert answers(db_session, started) == [(1, "ANSWER", "é" * 10_000, "NOT_EVALUATED")]


def test_oversized_answer_after_the_deadline_writes_nothing(
    client: TestClient, started: str, db_session: Session
) -> None:
    q = first_question(client, started)
    run_for(db_session, started, 1000)
    res = ans(client, started, q["id"], said="x" * 10_001)
    assert (res.status_code, res.json()) == (422, TOO_LONG)
    assert state(db_session, started)[:2] == ("WAITING_FOR_ANSWER", None)  # no closure written
    res = ans(client, started, q["id"], said="x" * 10_000)  # the next valid write closes it
    assert (res.status_code, res.json()) == (409, {"detail": LATE_ANSWER})
    assert state(db_session, started)[:2] == ("REPORT_GENERATION", "TIME_LIMIT")


# ---------- deadline and late answers (T2, T4, T6, T7, T9) ----------


def test_late_answer_is_refused_and_never_stored(
    client: TestClient, started: str, db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:  # T2
    caplog.set_level(logging.DEBUG)
    q1 = first_question(client, started)
    assert ans(client, started, q1["id"], said="An early answer.").status_code == 200
    q2 = nxt(client, started).json()["question"]
    run_for(db_session, started, 901)
    res = ans(client, started, q2["id"], said=SECRET_ANSWER)
    assert (res.status_code, res.json()) == (409, {"detail": LATE_ANSWER})
    status, reason, seconds = state(db_session, started)
    assert (status, reason) == ("REPORT_GENERATION", "TIME_LIMIT") and seconds >= 900
    assert answers(db_session, started) == [(1, "ANSWER", "An early answer.", "NOT_EVALUATED"),
                                            (2, None, None, None)]
    assert "7f3a" not in caplog.text
    # And again: same refusal, nothing changes.
    assert ans(client, started, q2["id"], said=SECRET_ANSWER).json() == {"detail": LATE_ANSWER}
    assert state(db_session, started) == (status, reason, seconds)


def test_replay_of_an_answer_given_before_the_deadline(
    client: TestClient, started: str, db_session: Session
) -> None:  # T9
    q = first_question(client, started)
    assert ans(client, started, q["id"]).status_code == 200
    run_for(db_session, started, 1000)
    res = ans(client, started, q["id"])
    assert res.status_code == 200 and res.json()["status"] == "NEXT_DECISION"
    assert state(db_session, started)[:2] == ("NEXT_DECISION", None)  # nothing written
    res = ans(client, started, q["id"], said="A different answer")
    assert (res.status_code, res.json()["detail"]) == (
        409, "This question was already answered differently.")


def test_expiry_from_asking_and_from_next_decision(
    client: TestClient, started: str, sid: str, db_session: Session
) -> None:  # T4
    run_for(db_session, started, 850)  # ASKING with 50 s left: no question may start
    res = nxt(client, started).json()
    assert (res["status"], res["finish_reason"], res["question"]) == (
        "REPORT_GENERATION", "TIME_LIMIT", None)
    assert res["progress"]["time_remaining_seconds"] <= 50
    assert answers(db_session, started) == []  # zero questions


def test_expiry_from_next_decision(client: TestClient, started: str, db_session: Session) -> None:
    q = first_question(client, started)
    ans(client, started, q["id"])
    run_for(db_session, started, 845)  # NEXT_DECISION inside the reserve
    res = nxt(client, started).json()
    assert (res["status"], res["finish_reason"], res["question"]) == (
        "REPORT_GENERATION", "TIME_LIMIT", None)


def test_reads_report_the_deadline_without_changing_state(
    client: TestClient, started: str, db_session: Session
) -> None:  # L9
    first_question(client, started)
    run_for(db_session, started, 950)
    history = client.get(f"/api/interview-sessions/{started}/questions").json()
    assert history["progress"] == {"topics_total": 5, "time_remaining_seconds": 0,
                                   "deadline_reached": True}
    assert state(db_session, started)[0] == "WAITING_FOR_ANSWER"
    assert client.get(f"/api/interview-sessions/{started}").json()["status"] == \
        "WAITING_FOR_ANSWER"


def test_actions_after_the_deadline(client: TestClient, started: str, db_session: Session) -> None:
    first_question(client, started)  # T7
    run_for(db_session, started, 901)
    for action in ("PAUSE", "END_INTERVIEW"):
        res = act(client, started, action)
        assert (res.status_code, res.json()) == (
            409, {"detail": f"Cannot {action} an interview session in REPORT_GENERATION."})
    assert state(db_session, started)[:2] == ("REPORT_GENERATION", "TIME_LIMIT")


def test_a_pause_stops_the_clock(client: TestClient, started: str, db_session: Session) -> None:
    q = first_question(client, started)  # T6
    run_for(db_session, started, 800)
    assert act(client, started, "PAUSE").status_code == 200
    db_session.execute(text("SELECT pg_sleep(0.2)"))
    assert act(client, started, "RESUME").status_code == 200
    seconds = state(db_session, started)[2]
    assert 800 <= seconds < 801  # the pause added nothing
    assert ans(client, started, q["id"]).status_code == 200  # still in time


def test_end_before_expiry_is_candidate_ended(
    client: TestClient, started: str, db_session: Session
) -> None:  # T8 (sequential halves)
    q = first_question(client, started)
    assert act(client, started, "END_INTERVIEW").status_code == 200
    run_for_finished = state(db_session, started)
    assert run_for_finished[:2] == ("REPORT_GENERATION", "CANDIDATE_ENDED")
    res = ans(client, started, q["id"])
    assert (res.status_code, res.json()["detail"]) == (
        409, "Cannot answer a question in an interview session in REPORT_GENERATION.")
    assert state(db_session, started)[:2] == ("REPORT_GENERATION", "CANDIDATE_ENDED")


def test_restart_gives_the_same_decision(
    client: TestClient, started: str, db_session: Session
) -> None:  # T10: everything a decision needs is in the database
    q = first_question(client, started)
    ans(client, started, q["id"])

    def decision():
        with Session(db_session.connection()) as db:  # a fresh ORM session, same data
            session = db.execute(text("SELECT candidate_id, plan FROM interview_sessions "
                                      "WHERE id = :i"), {"i": started}).one()
            plan = stored_plan(type("S", (), {"plan": session.plan})())
            ctx = TurnContext(plan, load_history(db, uuid.UUID(started)), 42.0,
                              valid_positions(db, session.candidate_id, plan))
            return resolve(ctx, decide(ctx), reason=RulesReason.NOT_REQUESTED)

    assert decision() == decision()


# ---------- facts withdrawn (D8, A-facts) ----------


def test_a_withdrawn_project_topic_is_skipped(
    client: TestClient, started: str, facts: list, db_session: Session
) -> None:
    db_session.execute(text("UPDATE candidate_facts SET status = 'REJECTED' WHERE id = :i"),
                       {"i": facts[0]})
    db_session.commit()
    positions = []
    for _ in range(30):
        q = nxt(client, started).json()["question"]
        if q is None:
            break
        positions.append(q["topic_position"])
        ans(client, started, q["id"], kind="SKIP", said=None)
    assert sorted(set(positions)) == [1, 2, 4, 5]  # TrustLens (3) skipped


# ---------- every technical fallback topic is asked (Decision B) ----------

TECHNICAL = {"mode": "TECHNICAL"}


def run_interview(client: TestClient, session_id: str, kind: str) -> list[tuple]:
    """Answers every question with `kind` until the interview ends; returns the TOPIC questions."""
    topics = []
    for _ in range(40):
        q = nxt(client, session_id).json()["question"]
        if q is None:
            return topics
        if q["kind"] == "TOPIC":
            topics.append((q["topic_position"], q["text"], q["source"], q["rules_reason"]))
        assert ans(client, session_id, q["id"], kind=kind,
                   said=ANSWER if kind == "ANSWER" else None).status_code == 200
    pytest.fail("interview did not end")


@pytest.mark.parametrize("kind", ["ANSWER", "SKIP"])
@pytest.mark.parametrize("consent", ["off", "on, providers failing", "on, no provider"])
def test_every_technical_topic_is_asked(
    client: TestClient, sid: str, db_session: Session, use_gateway, consent: str, kind: str
) -> None:
    expected_reason = {"off": "NOT_REQUESTED", "on, providers failing": "PROVIDERS_FAILED",
                       "on, no provider": "NOT_CONFIGURED"}[consent]
    if consent != "off":
        consented(client, sid)
    use_gateway(make_gateway(FakeProvider("groq", ErrorKind.SERVER_ERROR), retries=0)
                if consent == "on, providers failing" else None)
    res = client.post(f"/api/interview-sessions/{sid}/plan", json={**SETTINGS, **TECHNICAL})
    assert res.json()["plan"]["rules_reason"] == expected_reason
    act(client, sid, "START_INTERVIEW")
    topics = run_interview(client, sid, kind)
    assert topics == [(i, TECHNICAL_QUESTIONS[c], "RULES", expected_reason) for i, c in enumerate(
        (Competency.PROGRAMMING_FUNDAMENTALS, Competency.DATA_STRUCTURES, Competency.DATABASE,
         Competency.WEB_DEVELOPMENT), 1)]
    assert state(db_session, sid)[:2] == ("REPORT_GENERATION", "PLAN_COMPLETED")
    rules = {r[0] for r in db_session.execute(text(
        "SELECT source FROM interview_questions WHERE session_id = :i"), {"i": sid})}
    assert rules == {"RULES"}  # deterministic throughout, whatever the consent


# ---------- no eligible first topic (Decision A) ----------

DEEP_DIVE = {"mode": "PROJECT_DEEP_DIVE"}  # every topic cites a project: all can lose their fact


def reject(db: Session, *fact_ids: object) -> None:
    db.execute(text("UPDATE candidate_facts SET status = 'REJECTED' WHERE id = ANY(:i)"),
               {"i": list(fact_ids)})
    db.commit()


@pytest.fixture
def deep_dive(client: TestClient, sid: str, candidate: str, facts: list,
              db_session: Session) -> tuple[str, list]:
    """A started project deep dive on TrustLens and Campus Connect. Yields (session, fact ids)."""
    [campus] = add_facts(db_session, candidate, (FC.PROJECT, "Campus Connect", FactStatus.APPROVED))
    res = client.post(f"/api/interview-sessions/{sid}/plan", json={**SETTINGS, **DEEP_DIVE})
    assert [t["topic"] for t in res.json()["plan"]["topics"]] == [
        "Project: Campus Connect", "Project: TrustLens"]
    assert act(client, sid, "START_INTERVIEW").json()["status"] == "ASKING"
    return sid, [campus, facts[0]]


def test_all_topics_eligible_asks_the_first(client: TestClient, deep_dive) -> None:
    session_id, _ = deep_dive
    q = first_question(client, session_id)
    assert (q["kind"], q["topic_position"], q["text"]) == (
        "TOPIC", 1, "Explain the architecture of Campus Connect.")


def test_an_ineligible_first_topic_is_passed_over(
    client: TestClient, deep_dive, db_session: Session
) -> None:
    session_id, (campus, _) = deep_dive
    reject(db_session, campus)
    q = first_question(client, session_id)
    assert (q["kind"], q["topic_position"], q["topic"]) == ("TOPIC", 2, "Project: TrustLens")


def test_no_eligible_first_topic_ends_the_interview(
    client: TestClient, deep_dive, db_session: Session, use_gateway,
    caplog: pytest.LogCaptureFixture
) -> None:
    session_id, fact_ids = deep_dive
    consented(client, session_id)
    provider = FakeProvider("groq", {"question": "unused?", "anchor": "unused"})
    use_gateway(make_gateway(provider))  # consent and a model: still decided by code alone
    reject(db_session, *fact_ids)
    caplog.set_level(logging.INFO)
    for _ in range(3):  # the first request ends it; repeats just report it
        res = nxt(client, session_id)
        assert res.status_code == 200
        body = res.json()
        assert (body["status"], body["finish_reason"], body["question"], body["model_calls"]) == (
            "REPORT_GENERATION", "NO_ELIGIBLE_TOPIC", None, [])
    assert caplog.text.count("-> REPORT_GENERATION on FINISH (NO_ELIGIBLE_TOPIC)") == 1
    assert provider.calls == [] and calls(db_session) == 0
    assert answers(db_session, session_id) == []  # no question row
    status, reason, seconds = state(db_session, session_id)
    assert (status, reason) == ("REPORT_GENERATION", "NO_ELIGIBLE_TOPIC") and seconds < 60
    history = client.get(f"/api/interview-sessions/{session_id}/questions").json()
    assert (history["finish_reason"], history["questions"]) == ("NO_ELIGIBLE_TOPIC", [])
    for action in ("PAUSE", "END_INTERVIEW"):  # the candidate cannot change the finish
        assert act(client, session_id, action).status_code == 409
    assert state(db_session, session_id)[:2] == ("REPORT_GENERATION", "NO_ELIGIBLE_TOPIC")


def test_no_eligible_topic_after_a_question_is_a_completed_plan(
    client: TestClient, deep_dive, db_session: Session
) -> None:
    session_id, fact_ids = deep_dive
    q = first_question(client, session_id)
    reject(db_session, *fact_ids)  # withdrawn after the interview really started
    ans(client, session_id, q["id"])
    res = nxt(client, session_id).json()
    assert (res["status"], res["finish_reason"], res["question"]) == (
        "REPORT_GENERATION", "PLAN_COMPLETED", None)


def test_time_limit_still_wins_over_no_eligible_topic(
    client: TestClient, deep_dive, db_session: Session
) -> None:
    session_id, fact_ids = deep_dive
    reject(db_session, *fact_ids)
    run_for(db_session, session_id, 850)  # inside the reserve
    res = nxt(client, session_id).json()
    assert (res["status"], res["finish_reason"]) == ("REPORT_GENERATION", "TIME_LIMIT")


def test_reserve_reached_while_deciding_from_asking_expires(
    client: TestClient, started: str, monkeypatch: pytest.MonkeyPatch
) -> None:  # the §6.3 edge from ASKING, not a "profile changed" 409
    import app.services.interviewer as interviewer

    monkeypatch.setattr(interviewer, "decide",
                        lambda ctx: interviewer.Intent(interviewer.Action.FINISH, "TIME_LIMIT"))
    res = nxt(client, started)
    assert res.status_code == 200
    assert (res.json()["status"], res.json()["finish_reason"]) == ("REPORT_GENERATION", "TIME_LIMIT")


# ---------- the AI path (A1 AI, K2-K4, N11a, R-series at the API) ----------


@pytest.fixture
def ai_waiting(client: TestClient, started: str, use_gateway) -> Iterator:
    """Consent granted; topic 1 asked. Yields (session id, question, set_provider)."""
    consented(client, started)
    q = first_question(client, started)

    def provider(*script, hook=None):
        p = HookProvider(hook, *script) if hook else FakeProvider("groq", *script)
        use_gateway(make_gateway(p, retries=0))
        return p

    yield started, q, provider


def test_ai_follow_up_is_validated_stored_and_linked(
    client: TestClient, ai_waiting, db_session: Session
) -> None:
    session_id, q, provider = ai_waiting
    p = provider({"question": "Which trade-offs come up most often for you?",
                  "anchor": "trade-offs"})
    assert ans(client, session_id, q["id"]).status_code == 200
    res = nxt(client, session_id).json()
    f = res["question"]
    assert (f["kind"], f["source"], f["rules_reason"], f["followup_index"], f["topic_position"]) \
        == ("FOLLOW_UP", "AI", None, 1, 1)
    assert f["text"] == "Which trade-offs come up most often for you?" and "anchor" not in f
    [call] = res["model_calls"]
    row = db_session.execute(text("SELECT anchor, provider_call_id FROM interview_questions "
                                  "WHERE id = :i"), {"i": f["id"]}).one()
    stored = db_session.scalar(select(ProviderCall.id).where(
        ProviderCall.request_id == call["request_id"]))
    assert tuple(row) == ("trade-offs", stored)
    [request] = p.calls
    assert ANSWER in request.user_content and ANSWER not in request.system_instruction
    # The model is told which anchors the validator accepts: planned terms found in the answer.
    data = json.loads(request.user_content.splitlines()[1])
    assert data["anchor_options"] == ["implementation", "trade-offs"]  # vocabulary order


def test_rejected_ai_wording_falls_back_to_the_term_template(
    client: TestClient, ai_waiting, db_session: Session
) -> None:  # N4b, R2
    session_id, q, provider = ai_waiting
    provider({"question": "You mentioned trade-offs. Why did you choose Kafka for it?",
              "anchor": "trade-offs"})
    ans(client, session_id, q["id"], said="We used Kafka and weighed the trade-offs.")
    res = nxt(client, session_id).json()
    assert len(res["model_calls"]) == 2  # one constrained re-request
    f = res["question"]
    assert (f["source"], f["rules_reason"], f["text"]) == (
        "RULES", "OUTPUT_REJECTED", FOLLOW_UP_TEMPLATE.format(term="trade-offs"))
    assert "Kafka" not in json.dumps(client.get(
        f"/api/interview-sessions/{session_id}/questions").json()["questions"][1])
    assert db_session.scalar(text("SELECT count(*) FROM candidate_facts WHERE fact = 'Kafka'")) == 0


def test_a_duplicate_rephrase_moves_on_after_one_call(
    client: TestClient, ai_waiting, db_session: Session
) -> None:  # R7
    session_id, q, provider = ai_waiting
    provider({"question": q["text"].upper()})
    ans(client, session_id, q["id"], kind="CLARIFY", said="What do you mean? asha@example.com")
    res = nxt(client, session_id).json()
    assert len(res["model_calls"]) == 1
    assert (res["question"]["kind"], res["question"]["topic_position"]) == ("TOPIC", 2)
    assert db_session.scalar(text("SELECT count(*) FROM interview_questions WHERE "
                                  "session_id = :i AND kind = 'REPHRASE'"), {"i": session_id}) == 0


def test_rephrases_with_a_failing_model(
    client: TestClient, ai_waiting, db_session: Session
) -> None:  # R12(a), R12(c), L12
    session_id, q, provider = ai_waiting
    p = provider(ErrorKind.TIMEOUT, ErrorKind.INVALID_OUTPUT, ErrorKind.TIMEOUT)
    clarification = "Could you explain? My email is asha@example.com"
    ans(client, session_id, q["id"], kind="CLARIFY", said=clarification)
    res = nxt(client, session_id).json()
    r = res["question"]
    assert (r["kind"], r["rephrase_index"], r["source"], r["rules_reason"]) == (
        "REPHRASE", 1, "RULES", "PROVIDERS_FAILED")
    assert r["text"] == REPHRASE_TEMPLATE.format(topic="Programming Fundamentals",
                                                 question=TOPIC_1)
    assert len(res["model_calls"]) == 2
    ans(client, session_id, r["id"], kind="CLARIFY", said=clarification)
    res = nxt(client, session_id).json()
    assert (res["question"]["kind"], res["question"]["topic_position"]) == ("TOPIC", 2)
    assert db_session.scalar(text("SELECT max(rephrase_index) FROM interview_questions "
                                  "WHERE session_id = :i"), {"i": session_id}) == 1
    for request in p.calls:  # clarification text is never sent
        assert "asha@example.com" not in request.user_content + request.system_instruction


def test_withdrawing_between_turns_means_no_call(
    client: TestClient, ai_waiting
) -> None:  # K3
    session_id, q, provider = ai_waiting
    p = provider({"question": "unused?", "anchor": "trade-offs"})
    client.put(f"/api/interview-sessions/{session_id}/ai-consent", json={"granted": False})
    ans(client, session_id, q["id"])
    res = nxt(client, session_id).json()
    assert (res["question"]["rules_reason"], res["model_calls"], p.calls) == (
        "NOT_REQUESTED", [], [])


def test_cited_fact_rejected_during_the_call(
    client: TestClient, started: str, facts: list, db_session: Session, use_gateway
) -> None:  # A: facts withdrawn during a call
    consented(client, started)
    for _ in range(2):  # topics 1 and 2: skip to the TrustLens topic
        q = first_question(client, started)
        ans(client, started, q["id"], kind="SKIP", said=None)
    q = first_question(client, started)
    assert q["topic"] == "Project: TrustLens"

    def candidate_rejects_trustlens() -> None:
        db_session.execute(text("UPDATE candidate_facts SET status = 'REJECTED' WHERE id = :i"),
                           {"i": facts[0]})
        db_session.commit()

    use_gateway(make_gateway(HookProvider(candidate_rejects_trustlens, {
        "question": "What were the main architecture choices in TrustLens?",
        "anchor": "architecture"})))
    ans(client, started, q["id"], said="The architecture was a browser extension.")
    res = nxt(client, started)
    assert (res.status_code, res.json()) == (
        409, {"detail": "Your approved profile changed. Please try again."})
    assert calls(db_session) == 1  # the call is still recorded
    retry = nxt(client, started).json()["question"]  # the retry skips the topic
    assert (retry["kind"], retry["topic_position"]) == ("TOPIC", 4)


def test_session_deleted_during_the_call(
    client: TestClient, ai_waiting, db_session: Session
) -> None:  # C4
    session_id, q, provider = ai_waiting

    def discard() -> None:
        db_session.execute(text("DELETE FROM interview_sessions WHERE id = :i"), {"i": session_id})
        db_session.commit()

    ans(client, session_id, q["id"])
    provider({"question": "Which trade-offs?", "anchor": "trade-offs"}, hook=discard)
    assert nxt(client, session_id).status_code == 404
    assert calls(db_session) == 1


def test_expiry_during_the_call_discards_the_wording(
    client: TestClient, ai_waiting, db_session: Session
) -> None:  # T5
    session_id, q, provider = ai_waiting
    ans(client, session_id, q["id"])
    provider({"question": "Which trade-offs?", "anchor": "trade-offs"},
             hook=lambda: run_for(db_session, session_id, 860))  # into the reserve
    res = nxt(client, session_id).json()
    assert (res["status"], res["finish_reason"], res["question"]) == (
        "REPORT_GENERATION", "TIME_LIMIT", None)
    assert len(res["model_calls"]) == 1 and calls(db_session) == 1


# ---------- deletion, privacy, failures (A-cascade, P1, A-prod, A-503, A-rollback) ----------


def test_deleting_removes_questions_answers_and_consent(
    client: TestClient, ai_waiting, candidate: str, db_session: Session
) -> None:
    session_id, q, provider = ai_waiting
    provider({"question": "Which trade-offs come up most?", "anchor": "trade-offs"})
    ans(client, session_id, q["id"])
    nxt(client, session_id)
    assert client.delete(f"/api/interview-sessions/{session_id}").status_code == 204
    for table in ("interview_questions", "interview_consent_events"):
        assert db_session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    assert calls(db_session) == 1  # operational metadata, linked to no one


def test_logs_and_call_records_hold_no_content(
    client: TestClient, ai_waiting, db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:  # P1
    caplog.set_level(logging.DEBUG)
    session_id, q, provider = ai_waiting
    provider({"question": "You mentioned Kafka 7f3a?", "anchor": "Kafka"},
             {"question": "Which trade-offs come up most?", "anchor": "trade-offs"})
    ans(client, session_id, q["id"], said=SECRET_ANSWER)
    f = nxt(client, session_id).json()["question"]
    assert f["source"] == "AI"
    ans(client, session_id, f["id"], kind="CLARIFY", said="clarify 7f3a please")
    nxt(client, session_id)
    logged = caplog.text
    for private in ("7f3a", "trade-offs", "Programming Fundamentals", "Which trade-offs",
                    TOPIC_1[:30], NAME, EMAIL, "Kafka"):
        assert private not in logged
    assert session_id in logged and "anchor_not_in_plan_vocabulary" in logged
    rows = db_session.execute(text("SELECT * FROM provider_calls")).mappings().all()
    assert "7f3a" not in json.dumps([dict(r) for r in rows], default=str)


def test_turn_routes_served_in_production() -> None:
    # AUDIT-001: routers ARE registered in production, so unauthenticated requests return 401 (not 404)
    prod = TestClient(create_app(Settings(_env_file=None, database_url="postgresql+psycopg://u:p@h/x",
                                          environment="production")),
                      headers={"Origin": "http://localhost:3000"})
    sid_ = uuid.uuid4()
    assert prod.post(f"/api/interview-sessions/{sid_}/questions/next", json={}).status_code == 401
    assert prod.post(f"/api/interview-sessions/{sid_}/answers", json={}).status_code == 401
    assert prod.get(f"/api/interview-sessions/{sid_}/questions").status_code == 401
    assert prod.put(f"/api/interview-sessions/{sid_}/ai-consent",
                    json={"granted": True}).status_code == 401


def test_database_unavailable_is_503(dead_database_client: TestClient) -> None:
    sid_ = uuid.uuid4()
    for res in (nxt(dead_database_client, sid_), ans(dead_database_client, sid_, uuid.uuid4()),
                dead_database_client.get(f"/api/interview-sessions/{sid_}/questions"),
                dead_database_client.put(f"/api/interview-sessions/{sid_}/ai-consent",
                                         json={"granted": True})):
        assert res.status_code == 503


def test_failed_commit_keeps_nothing_partial(
    client: TestClient, started: str, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    q = first_question(client, started)
    before = state(db_session, started)

    def broken_commit() -> None:
        raise OperationalError("COMMIT", {}, Exception("connection lost"))

    monkeypatch.setattr(db_session, "commit", broken_commit)
    assert ans(client, started, q["id"]).status_code == 503
    monkeypatch.undo()
    assert state(db_session, started) == before
    assert answers(db_session, started) == [(1, None, None, None)]
    assert ans(client, started, q["id"]).status_code == 200  # and the retry works


# ---------- real concurrency: separate, committed connections (C1-C3, N10, N11a, R12b) ----------


class GatedProvider:
    """Answers only once released, so requests can be held inside the model call."""

    name, model = "groq", "gated-interviewer"

    def __init__(self, output: dict) -> None:
        self.output = output
        self.arrived = threading.Semaphore(0)
        self.release = threading.Event()
        self.calls = 0

    async def complete(self, request, json_schema, max_output_tokens) -> RawCompletion:
        self.calls += 1
        self.arrived.release()
        while not self.release.is_set():
            await anyio.sleep(0.01)
        return RawCompletion(text=json.dumps(self.output), model=self.model, finish_reason="stop")


@contextlib.contextmanager
def _committed(**overrides: object) -> Iterator[tuple[uuid.UUID, list]]:
    """A started interview (RULES plan, consent granted) on TrustLens, committed for real; cleaned
    up after. Yields (session id, project fact ids)."""
    with SessionLocal() as db:
        cand = Candidate(email=f"{uuid.uuid4().hex}@example.com", name="Concurrency Test")
        db.add(cand)
        db.commit()
        fact_ids = add_facts(db, cand.id, (FC.PROJECT, "TrustLens", FactStatus.APPROVED))
        session = create_session(db, cand.id)
        db.commit()
        apply_event(db, session.id, E.CONFIRM_PROFILE)
        plan = fallback_plan(PlanSettings(**{**SETTINGS, **overrides}),
                             approved_facts(db, cand.id), RulesReason.NOT_REQUESTED)
        apply_event(db, session.id, E.PLAN_READY, plan=plan.model_dump(mode="json"))
        apply_event(db, session.id, E.START_INTERVIEW)
        db.commit()
        ids = (cand.id, session.id)
    consented(owner_client(ids[1]), ids[1])
    try:
        yield ids[1], fact_ids
    finally:
        app.dependency_overrides.pop(get_model_gateway, None)
        with SessionLocal() as db:
            db.execute(text("DELETE FROM candidates WHERE id = :i"), {"i": ids[0]})
            db.execute(text("DELETE FROM provider_calls"))
            db.commit()


@pytest.fixture
def committed_interview() -> Iterator[uuid.UUID]:
    with _committed() as (session_id, _):
        yield session_id


@pytest.fixture
def committed_deep_dive() -> Iterator[tuple[uuid.UUID, list]]:
    with _committed(**DEEP_DIVE) as ids:
        yield ids


def in_threads(n: int, fn) -> tuple[list[threading.Thread], list]:
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(fn()), daemon=True)
               for _ in range(n)]
    for thread in threads:
        thread.start()
    return threads, results


def assert_nothing_held(session_id: uuid.UUID) -> None:
    assert engine.pool.checkedout() == 0  # no pooled connection held during the model call
    with engine.begin() as conn:  # and no row lock
        conn.execute(text("SELECT id FROM interview_sessions WHERE id = :i FOR UPDATE NOWAIT"),
                     {"i": session_id})


def test_concurrent_next_requests_persist_one_rephrase(committed_interview: uuid.UUID) -> None:
    sid_ = committed_interview  # C1, R12(b)
    client = owner_client(sid_)
    q = first_question(client, sid_)
    ans(client, sid_, q["id"], kind="CLARIFY", said="Sorry?")
    provider = GatedProvider({"question": "What is one basic idea in programming that you "
                                          "rely on, and where have you seen it applied?"})
    app.dependency_overrides[get_model_gateway] = lambda: make_gateway(provider, timeout=30)
    threads, results = in_threads(2, lambda: nxt(owner_client(sid_), sid_))
    try:
        for _ in threads:
            assert provider.arrived.acquire(timeout=10)  # both inside the model call
        assert_nothing_held(sid_)
        provider.release.set()
        for thread in threads:
            thread.join(30)
    finally:
        provider.release.set()
    assert [r.status_code for r in results] == [200, 200]
    ids = {r.json()["question"]["id"] for r in results}
    assert len(ids) == 1  # both return the one persisted rephrase
    with SessionLocal() as db:
        kinds = list(db.scalars(select(InterviewQuestion.kind).where(
            InterviewQuestion.session_id == sid_).order_by(InterviewQuestion.seq)))
        assert kinds == ["TOPIC", "REPHRASE"]
        assert db.scalar(select(ProviderCall.id).where(
            ProviderCall.model == "gated-interviewer").limit(1)) is not None
        assert db.scalar(text("SELECT count(*) FROM provider_calls "
                              "WHERE model = 'gated-interviewer'")) == 2  # both recorded


def test_withdrawal_during_an_interviewer_call(committed_interview: uuid.UUID) -> None:
    sid_ = committed_interview  # K4, N11(a)
    client = owner_client(sid_)
    q = first_question(client, sid_)
    ans(client, sid_, q["id"])
    provider = GatedProvider({"question": "Which trade-offs come up most?", "anchor": "trade-offs"})
    app.dependency_overrides[get_model_gateway] = lambda: make_gateway(provider, timeout=30)
    threads, results = in_threads(1, lambda: nxt(owner_client(sid_), sid_))
    try:
        assert provider.arrived.acquire(timeout=10)
        assert_nothing_held(sid_)
        res = client.put(f"/api/interview-sessions/{sid_}/ai-consent", json={"granted": False})
        assert res.status_code == 200
        provider.release.set()
        threads[0].join(30)
    finally:
        provider.release.set()
    [res] = results
    f = res.json()["question"]
    assert (f["source"], f["rules_reason"], f["text"]) == (
        "RULES", "CONSENT_WITHDRAWN", FOLLOW_UP_TEMPLATE.format(term="implementation"))
    assert len(res.json()["model_calls"]) == 1
    with SessionLocal() as db:
        assert db.scalar(text("SELECT count(*) FROM provider_calls "
                              "WHERE model = 'gated-interviewer'")) == 1
        assert list(db.scalars(text("SELECT granted FROM interview_consent_events "
                                    "WHERE session_id = :i ORDER BY id"), {"i": sid_})) == [
            True, False]


def test_concurrent_late_answers_close_once(
    committed_interview: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:  # T3, N10
    sid_ = committed_interview
    client = owner_client(sid_)
    q1 = first_question(client, sid_)
    ans(client, sid_, q1["id"], said="On time.")
    q2 = first_question(client, sid_)
    with SessionLocal() as db:
        run_for(db, sid_, 1000)
    caplog.set_level(logging.INFO)
    threads, results = in_threads(3, lambda: ans(owner_client(sid_), sid_, q2["id"], said="Late!"))
    for thread in threads:
        thread.join(30)
    assert [(r.status_code, r.json()) for r in results] == [(409, {"detail": LATE_ANSWER})] * 3
    assert caplog.text.count("-> REPORT_GENERATION on TIME_EXPIRED") == 1  # one transition
    with SessionLocal() as db:
        assert state(db, sid_)[:2] == ("REPORT_GENERATION", "TIME_LIMIT")
        assert answers(db, sid_) == [(1, "ANSWER", "On time.", "NOT_EVALUATED"),
                                     (2, None, None, None)]


def test_racing_identical_and_different_answers(committed_interview: uuid.UUID) -> None:
    sid_ = committed_interview  # C2
    client = owner_client(sid_)
    q = first_question(client, sid_)
    threads, results = in_threads(3, lambda: ans(owner_client(sid_), sid_, q["id"], said="Same."))
    for thread in threads:
        thread.join(30)
    assert [r.status_code for r in results] == [200, 200, 200]  # one store, two replays
    res = ans(client, sid_, q["id"], said="Different.")
    assert res.status_code == 409
    with SessionLocal() as db:
        assert answers(db, sid_) == [(1, "ANSWER", "Same.", "NOT_EVALUATED")]
        assert state(db, sid_)[0] == "NEXT_DECISION"


def test_an_answer_waits_for_a_pause_and_is_then_refused(committed_interview: uuid.UUID) -> None:
    sid_ = committed_interview  # C3
    client = owner_client(sid_)
    q = first_question(client, sid_)
    first = SessionLocal()
    try:
        apply_event(first, sid_, E.PAUSE)  # holds the row lock, not committed yet
        threads, results = in_threads(1, lambda: ans(owner_client(sid_), sid_, q["id"]))
        threads[0].join(0.5)
        assert threads[0].is_alive()  # blocked behind the lock
        first.commit()
        threads[0].join(30)
    finally:
        first.close()
    assert (results[0].status_code, results[0].json()["detail"]) == (
        409, "Cannot answer a question in an interview session in PAUSED.")


def test_end_and_a_late_answer_race_to_one_finish_reason(committed_interview: uuid.UUID) -> None:
    sid_ = committed_interview  # T8
    client = owner_client(sid_)
    q = first_question(client, sid_)
    with SessionLocal() as db:
        run_for(db, sid_, 1000)
    threads, results = in_threads(1, lambda: act(owner_client(sid_), sid_, "END_INTERVIEW"))
    late = ans(client, sid_, q["id"])
    threads[0].join(30)
    assert late.status_code == 409 and results[0].status_code == 409
    with SessionLocal() as db:
        assert state(db, sid_)[:2] == ("REPORT_GENERATION", "TIME_LIMIT")


def test_concurrent_requests_end_an_ineligible_interview_once(
    committed_deep_dive: tuple, caplog: pytest.LogCaptureFixture
) -> None:  # Decision A
    sid_, fact_ids = committed_deep_dive
    with SessionLocal() as db:
        reject(db, *fact_ids)
    caplog.set_level(logging.INFO)
    threads, results = in_threads(4, lambda: nxt(owner_client(sid_), sid_))
    for thread in threads:
        thread.join(30)
    assert [(r.status_code, r.json()["status"], r.json()["finish_reason"], r.json()["question"])
            for r in results] == [(200, "REPORT_GENERATION", "NO_ELIGIBLE_TOPIC", None)] * 4
    assert caplog.text.count("-> REPORT_GENERATION on FINISH (NO_ELIGIBLE_TOPIC)") == 1
    with SessionLocal() as db:
        assert state(db, sid_)[:2] == ("REPORT_GENERATION", "NO_ELIGIBLE_TOPIC")
        assert answers(db, sid_) == []


def test_a_withdrawal_racing_the_first_question_is_seen_after_the_lock(
    committed_deep_dive: tuple
) -> None:  # Decision A: facts are read under the session lock, never before it
    sid_, fact_ids = committed_deep_dive
    withdrawal = SessionLocal()
    try:
        withdrawal.execute(text("SELECT id FROM interview_sessions WHERE id = :i FOR UPDATE"),
                           {"i": sid_})
        withdrawal.execute(text("UPDATE candidate_facts SET status = 'REJECTED' "
                                "WHERE id = ANY(:i)"), {"i": fact_ids})
        threads, results = in_threads(2, lambda: nxt(owner_client(sid_), sid_))
        for thread in threads:
            thread.join(0.5)
            assert thread.is_alive()  # both wait behind the lock
        withdrawal.commit()
        for thread in threads:
            thread.join(30)
    finally:
        withdrawal.close()
    assert [(r.status_code, r.json()["finish_reason"], r.json()["question"]) for r in results] == [
        (200, "NO_ELIGIBLE_TOPIC", None)] * 2
    with SessionLocal() as db:
        assert answers(db, sid_) == []


# ---------- question text limit (640, approved 2026-10-02) ----------


def test_the_longest_rephrase_is_stored_and_read_back_whole(
    client: TestClient, sid: str, db_session: Session
) -> None:
    from app.models.resume import FACT_MAX_LENGTH
    from app.schemas.interview_plan import InterviewPlan, PlannedTopic

    title = ("Indexing " * 40)[:FACT_MAX_LENGTH + 49] + "!"  # the longest valid topic title
    template = ("Why would an index " * 20)[:FACT_MAX_LENGTH + 99] + "?"  # and plan question
    plan = InterviewPlan(
        source="RULES", rules_reason="NOT_REQUESTED", settings=PlanSettings(**SETTINGS),
        competencies=["database"], omitted_groups=[],
        topics=[PlannedTopic(position=1, competency="database", question_type="TECHNICAL",
                             difficulty="MEDIUM", basis="GENERIC", topic=title,
                             question_templates=[template], expected_concepts=["indexes"],
                             facts=[])])
    apply_event(db_session, uuid.UUID(sid), E.PLAN_READY, plan=plan.model_dump(mode="json"))
    db_session.commit()
    act(client, sid, "START_INTERVIEW")
    q = first_question(client, sid)
    assert q["text"] == template and len(template) == 300
    ans(client, sid, q["id"], kind="CLARIFY", said=None)
    r = nxt(client, sid).json()["question"]
    framed = REPHRASE_TEMPLATE.format(topic=title, question=template)
    assert (r["kind"], r["text"], len(r["text"])) == ("REPHRASE", framed, 604)
    stored = db_session.scalar(text("SELECT text FROM interview_questions WHERE id = :i"),
                               {"i": r["id"]})
    history = client.get(f"/api/interview-sessions/{sid}/questions").json()["questions"]
    assert stored == history[1]["text"] == framed  # no truncation anywhere


# ---------- topic question metadata (item 6) ----------


def test_topic_questions_carry_the_plans_provenance(
    client: TestClient, sid: str, db_session: Session, use_gateway
) -> None:
    from tests.test_interview_plan_api import VALID

    consented(client, sid)
    provider = FakeProvider("groq", VALID)
    use_gateway(make_gateway(provider))
    assert client.post(f"/api/interview-sessions/{sid}/plan", json=SETTINGS).json()[
        "plan"]["source"] == "AI"
    act(client, sid, "START_INTERVIEW")
    res = nxt(client, sid).json()
    q = res["question"]
    assert (q["kind"], q["text"], q["source"], q["rules_reason"], res["model_calls"]) == (
        "TOPIC", "How do you manage state in React?", "AI", None, [])  # written at planning
    assert len(provider.calls) == 1  # the planner's call only: a topic question needs none
    row = db_session.execute(text("SELECT anchor, provider_call_id FROM interview_questions "
                                  "WHERE id = :i"), {"i": q["id"]}).one()
    assert tuple(row) == (None, None)  # not linked to any interviewer call


def test_consent_granted_after_planning_does_not_relabel_topic_questions(
    client: TestClient, started: str, use_gateway
) -> None:
    consented(client, started)
    use_gateway(make_gateway(FakeProvider("groq", {"question": "unused?", "anchor": "unused"})))
    q = first_question(client, started)
    assert (q["kind"], q["source"], q["rules_reason"]) == ("TOPIC", "RULES", "NOT_REQUESTED")
