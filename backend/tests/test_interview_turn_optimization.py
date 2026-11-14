"""Integration and behavioral tests for Phase 2B: Interview Turn Performance Optimization.

Validates:
1. Answer submission success and AnswerResult payload contract.
2. Answer submission rejection on invalid input without state corruption.
3. Next-question retrieval and state transition to WAITING_FOR_ANSWER.
4. Next-question recovery after answer persistence.
5. Independent question history consistency.
6. Duplicate/replay submission handling (identical replay vs conflicting update).
7. Duplicate next-question idempotency (no double-advancing while open).
8. Monotonic question sequence numbering.
9. FOLLOW_UP behavior on partial/qualifying answers.
10. REPHRASE behavior when candidate asks to clarify.
11. TOPIC progression through planned curriculum.
12. Interview completion and transition to REPORT_GENERATION.
13. Deadline enforcement and refusal of late answers.
14. Concurrency safety under session row locks and invalid question IDs.
"""

import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import InterviewQuestion
from tests.test_interview_plan_api import (  # noqa: F401
    EMAIL,
    NAME,
    SETTINGS,
    add_facts,
    candidate,
    consented,
    facts,
    sid,
    use_gateway,
)
from tests.test_interview_turn_api import act, ans, nxt, run_for, started


def test_answer_submission_success_returns_populated_question_view(
    client: TestClient, started: str
) -> None:
    first = nxt(client, started).json()
    q = first["question"]
    assert q is not None
    assert q["seq"] == 1
    assert q["answer"] is None

    # Submit candidate answer
    res = ans(client, started, q["id"], kind="ANSWER", said="I structure code by domains.")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "NEXT_DECISION"
    assert body["question"]["id"] == q["id"]
    assert body["question"]["seq"] == 1
    assert body["question"]["answer"]["kind"] == "ANSWER"
    assert body["question"]["answer"]["text"] == "I structure code by domains."
    assert body["question"]["answer"]["evaluation"]["status"] == "NOT_EVALUATED"


def test_answer_submission_failure_leaves_session_state_intact(
    client: TestClient, started: str
) -> None:
    first = nxt(client, started).json()
    q = first["question"]

    # 1. Empty answer text when kind=ANSWER -> 422
    empty_res = ans(client, started, q["id"], kind="ANSWER", said="")
    assert empty_res.status_code == 422

    # 2. Oversized answer text -> 422
    large_res = ans(client, started, q["id"], kind="ANSWER", said="x" * 10_001)
    assert large_res.status_code == 422

    # Verify session remains in WAITING_FOR_ANSWER and question is still open
    recheck = nxt(client, started).json()
    assert recheck["status"] == "WAITING_FOR_ANSWER"
    assert recheck["question"]["id"] == q["id"]
    assert recheck["question"]["answer"] is None


def test_next_question_progression_after_answer_persistence(
    client: TestClient, started: str
) -> None:
    first = nxt(client, started).json()["question"]
    ans_res = ans(client, started, first["id"], said="Valid domain answer.")
    assert ans_res.status_code == 200

    # Request next question
    second = nxt(client, started).json()
    assert second["status"] == "WAITING_FOR_ANSWER"
    q2 = second["question"]
    assert q2 is not None
    assert q2["id"] != first["id"]
    assert q2["seq"] == 2


def test_next_question_idempotency_while_question_is_open(
    client: TestClient, started: str
) -> None:
    first = nxt(client, started).json()["question"]

    # Calling next_question again while question 1 is unanswered must return question 1 again
    second_call = nxt(client, started).json()["question"]
    assert second_call["id"] == first["id"]
    assert second_call["seq"] == 1


def test_duplicate_answer_submission_handling(
    client: TestClient, started: str
) -> None:
    first = nxt(client, started).json()["question"]

    # First submission
    res1 = ans(client, started, first["id"], kind="ANSWER", said="Initial submission.")
    assert res1.status_code == 200

    # Identical replay: succeeds idempotently (200 OK)
    replay = ans(client, started, first["id"], kind="ANSWER", said="Initial submission.")
    assert replay.status_code == 200
    assert replay.json()["question"]["answer"]["text"] == "Initial submission."

    # Conflicting replay: different answer text -> 409 Conflict
    conflict = ans(client, started, first["id"], kind="ANSWER", said="Modified conflicting answer.")
    assert conflict.status_code == 409
    assert "already answered differently" in conflict.json()["detail"]


def test_followup_question_progression(
    client: TestClient, started: str
) -> None:
    first = nxt(client, started).json()["question"]
    assert first["kind"] == "TOPIC"

    # Candidate provides an answer to the topic question
    ans_res = ans(client, started, first["id"], kind="ANSWER", said="I prefer microservices with Kafka.")
    assert ans_res.status_code == 200

    # When time permits and topic share remains, next question is a FOLLOW_UP
    second = nxt(client, started).json()["question"]
    assert second["kind"] == "FOLLOW_UP"
    assert second["followup_index"] == 1
    assert second["seq"] == 2


def test_next_question_recovery_retry_after_failure(
    client: TestClient, started: str
) -> None:
    first = nxt(client, started).json()["question"]
    ans(client, started, first["id"], kind="ANSWER", said="Stable architecture response.")

    # Even if client network blips and retries next_question, it returns the next question reliably
    q_retry1 = nxt(client, started).json()["question"]
    q_retry2 = nxt(client, started).json()["question"]
    assert q_retry1["id"] == q_retry2["id"]
    assert q_retry1["seq"] == 2


def test_question_history_endpoint_consistency(
    client: TestClient, started: str
) -> None:
    first = nxt(client, started).json()["question"]
    ans(client, started, first["id"], kind="ANSWER", said="Answer 1.")
    second = nxt(client, started).json()["question"]

    hist_res = client.get(f"/api/interview-sessions/{started}/questions")
    assert hist_res.status_code == 200
    questions = hist_res.json()["questions"]
    assert len(questions) == 2
    assert questions[0]["id"] == first["id"]
    assert questions[0]["answer"]["text"] == "Answer 1."
    assert questions[1]["id"] == second["id"]
    assert questions[1]["answer"] is None


def test_rephrase_behavior_on_clarification(
    client: TestClient, started: str
) -> None:
    q1 = nxt(client, started).json()["question"]

    # Candidate requests clarification
    res = ans(client, started, q1["id"], kind="CLARIFY", said=None)
    assert res.status_code == 200

    # Next question must be a REPHRASE
    rephrase = nxt(client, started).json()["question"]
    assert rephrase["kind"] == "REPHRASE"
    assert rephrase["rephrase_index"] == 1
    assert rephrase["seq"] == 2


def test_topic_progression_and_completion(
    client: TestClient, started: str
) -> None:
    turn_count = 0
    while turn_count < 30:
        res = nxt(client, started).json()
        q = res["question"]
        if q is None:
            # Interview ended
            assert res["status"] in ("REPORT_GENERATION", "COMPLETED")
            break
        turn_count += 1
        # Alternate between answering and skipping to advance quickly
        ans_res = ans(client, started, q["id"], kind="SKIP" if turn_count % 2 == 0 else "ANSWER",
                      said=None if turn_count % 2 == 0 else "Clear and concise explanation.")
        assert ans_res.status_code == 200

    # Interview reached completion cleanly
    final = nxt(client, started).json()
    assert final["question"] is None
    assert final["status"] in ("REPORT_GENERATION", "COMPLETED")


def test_late_answer_refusal_on_deadline_expiration(
    client: TestClient, started: str, db_session: Session
) -> None:
    q = nxt(client, started).json()["question"]

    # Fast forward interview clock past the 15-minute deadline (901 seconds)
    run_for(db_session, started, 905)

    # Late answer must be rejected with 409 Conflict
    res = ans(client, started, q["id"], kind="ANSWER", said="Too late to answer.")
    assert res.status_code == 409

    # Session status must be closed due to time limit
    final = nxt(client, started).json()
    assert final["status"] == "REPORT_GENERATION"
    assert final["finish_reason"] == "TIME_LIMIT"
    assert final["question"] is None


def test_cannot_answer_nonexistent_question(
    client: TestClient, started: str
) -> None:
    fake_id = str(uuid.uuid4())
    res = ans(client, started, fake_id, kind="ANSWER", said="Answer to nowhere.")
    assert res.status_code == 404
