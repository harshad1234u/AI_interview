"""Test for AUDIT-005: Speech metrics persistence and inclusion in report."""

import uuid
from fastapi.testclient import TestClient

from tests.auth_helpers import owner_client
from tests.test_interview_turn_api import act, committed_interview, first_question
from tests.test_report import post_report


def test_speech_metrics_persisted_and_reported(committed_interview: uuid.UUID) -> None:
    sid_ = committed_interview
    client = owner_client(sid_)
    
    q1 = first_question(client, sid_)
    # Answer with speech duration and WPM metrics
    res_ans = client.post(
        f"/api/interview-sessions/{sid_}/answers",
        json={
            "question_id": q1["id"],
            "kind": "ANSWER",
            "text": "I used Python with async FastAPI to build high performance microservices.",
            "speech_duration_ms": 12000,
            "speech_wpm": 135,
        },
    )
    assert res_ans.status_code == 200
    answer_data = res_ans.json()
    assert answer_data["question"]["speech_duration_ms"] == 12000
    assert answer_data["question"]["speech_wpm"] == 135

    # End interview to generate report
    first_question(client, sid_)
    act(client, sid_, "END_INTERVIEW")

    # Evaluate answer (observations only)
    client.post(f"/api/interview-sessions/{sid_}/questions/{q1['id']}/evaluation", json={})

    # Generate report
    res_report = post_report(client, sid_)
    assert res_report.status_code == 200
    report = res_report.json()["report"]

    speech_metrics = report["communication_observations"]["speech_metrics"]
    assert speech_metrics["available"] is True
    assert speech_metrics["total_spoken_seconds"] == 12
    assert speech_metrics["average_wpm"] == 135
    assert speech_metrics["spoken_answers_count"] == 1
    assert "about 135 words per minute" in speech_metrics["note"]
