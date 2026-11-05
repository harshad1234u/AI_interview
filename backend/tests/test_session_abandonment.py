"""Test for AUDIT-008: Single-session abandonment and immediate new session creation."""

import uuid
from fastapi.testclient import TestClient

from app.models.interview_session import FinishReason, InterviewStatus
from tests.auth_helpers import owner_client
from tests.test_interview_turn_api import committed_interview


def test_abandon_active_session_endpoint(committed_interview: uuid.UUID) -> None:
    sid_ = committed_interview
    client = owner_client(sid_)

    # Active session is currently in ASKING / WAITING_FOR_ANSWER
    res = client.post(f"/api/interview-sessions/{sid_}/abandon")
    assert res.status_code == 200
    session_data = res.json()
    assert session_data["status"] == "COMPLETED"
    assert session_data["finish_reason"] == "CANDIDATE_ENDED"

    # Candidate can now create a new session without 409 conflict
    res_new = client.post("/api/interview-sessions", json={})
    assert res_new.status_code == 201
    assert res_new.json()["status"] == "PROFILE_REVIEW"


def test_create_session_with_abandon_active_flag(committed_interview: uuid.UUID) -> None:
    sid_ = committed_interview
    client = owner_client(sid_)

    # Trying to create a new session without abandon_active fails with 409
    res_conflict = client.post("/api/interview-sessions", json={})
    assert res_conflict.status_code == 409
    assert "unfinished interview session" in res_conflict.json()["detail"]

    # Creating with abandon_active=True succeeds atomically in one step
    res_success = client.post("/api/interview-sessions", json={"abandon_active": True})
    assert res_success.status_code == 201
    new_session = res_success.json()
    assert new_session["status"] == "PROFILE_REVIEW"
    assert new_session["id"] != str(sid_)

    # The previous session is marked COMPLETED
    res_old = client.get(f"/api/interview-sessions/{sid_}")
    assert res_old.status_code == 200
    assert res_old.json()["status"] == "COMPLETED"
    assert res_old.json()["finish_reason"] == "CANDIDATE_ENDED"
