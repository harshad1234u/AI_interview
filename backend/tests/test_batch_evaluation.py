"""Test for AUDIT-003 and AUDIT-010: Batch evaluation and concurrency."""

import uuid
from fastapi.testclient import TestClient

from app.models.interview_session import InterviewStatus
from tests.auth_helpers import owner_client
from tests.fakes import FakeProvider, make_gateway
from tests.test_interview_plan_api import use_gateway
from tests.test_interview_turn_api import act, ans, committed_interview, first_question


def test_batch_evaluation_flow(committed_interview: uuid.UUID, use_gateway) -> None:
    sid_ = committed_interview
    client = owner_client(sid_)
    
    # Question 1: technical
    q1 = first_question(client, sid_)
    ans(client, sid_, q1["id"], said="A hash map uses a hash function to map keys to bucket indices.")
    
    # Question 2: follow up or next
    q2 = first_question(client, sid_)
    ans(client, sid_, q2["id"], said="Collisions can be resolved using chaining with linked lists or open addressing.")
    
    first_question(client, sid_)
    act(client, sid_, "END_INTERVIEW")

    client.put(f"/api/interview-sessions/{sid_}/ai-consent", json={"granted": True})

    eval_q1 = {
        "criteria": [
            {"criterion": "technical_accuracy", "score": 4, "evidence": ["A hash map uses a hash function to map keys to bucket indices."]},
            {"criterion": "relevance", "score": 4, "evidence": ["A hash map uses a hash function"]},
            {"criterion": "completeness", "score": 4, "evidence": ["map keys to bucket indices."]},
            {"criterion": "reasoning", "score": 4, "evidence": ["map keys to bucket indices."]},
            {"criterion": "communication", "score": 4, "evidence": ["A hash map uses a hash function to map keys"]},
        ],
        "missing_concepts": [],
        "claims_to_verify": [],
        "feedback": "Clear explanation of hash maps.",
    }
    eval_q2 = {
        "criteria": [
            {"criterion": "technical_accuracy", "score": 5, "evidence": ["chaining with linked lists or open addressing."]},
            {"criterion": "relevance", "score": 4, "evidence": ["Collisions can be resolved"]},
            {"criterion": "completeness", "score": 4, "evidence": ["chaining with linked lists or open addressing."]},
            {"criterion": "reasoning", "score": 4, "evidence": ["chaining with linked lists or open addressing."]},
            {"criterion": "communication", "score": 5, "evidence": ["Collisions can be resolved using chaining"]},
        ],
        "missing_concepts": [],
        "claims_to_verify": [],
        "feedback": "Accurate coverage of collision resolution techniques.",
    }

    use_gateway(make_gateway(FakeProvider("groq", eval_q1, eval_q2)))

    # Call batch evaluation endpoint
    res = client.post(f"/api/interview-sessions/{sid_}/evaluations/batch", json={})
    assert res.status_code == 200
    batch = res.json()
    assert batch["session_id"] == str(sid_)
    assert batch["total_evaluated"] >= 2
    assert len(batch["evaluations"]) >= 2
    
    # Verify all evaluations succeeded
    statuses = [e["status"] for e in batch["evaluations"]]
    assert all(s == "EVALUATED" for s in statuses)

    # Calling batch again is idempotent and fast (returns stored results without model calls)
    res_again = client.post(f"/api/interview-sessions/{sid_}/evaluations/batch", json={})
    assert res_again.status_code == 200
    assert res_again.json()["total_evaluated"] == batch["total_evaluated"]
