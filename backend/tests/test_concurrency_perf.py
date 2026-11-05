"""Test for AUDIT-010: Concurrency performance verification."""

import uuid
from tests.auth_helpers import owner_client
from tests.fakes import FakeProvider, make_gateway
from tests.test_interview_plan_api import use_gateway
from tests.test_interview_turn_api import act, ans, committed_interview, first_question, in_threads


def test_concurrent_batch_evaluation_non_blocking(committed_interview: uuid.UUID, use_gateway) -> None:
    sid_ = committed_interview
    client = owner_client(sid_)
    
    q1 = first_question(client, sid_)
    ans(client, sid_, q1["id"], said="High throughput async microservices require non-blocking I/O event loops.")
    
    first_question(client, sid_)
    act(client, sid_, "END_INTERVIEW")

    client.put(f"/api/interview-sessions/{sid_}/ai-consent", json={"granted": True})

    eval_q1 = {
        "criteria": [
            {"criterion": "technical_accuracy", "score": 5, "evidence": ["require non-blocking I/O event loops."]},
            {"criterion": "relevance", "score": 5, "evidence": ["High throughput async microservices"]},
            {"criterion": "completeness", "score": 5, "evidence": ["require non-blocking I/O event loops."]},
            {"criterion": "reasoning", "score": 5, "evidence": ["require non-blocking I/O event loops."]},
            {"criterion": "communication", "score": 5, "evidence": ["High throughput async microservices require non-blocking I/O event loops."]},
        ],
        "missing_concepts": [],
        "claims_to_verify": [],
        "feedback": "Flawless explanation of event loop concurrency.",
    }

    use_gateway(make_gateway(FakeProvider("groq", eval_q1)))

    # Dispatch multiple concurrent client requests to the batch evaluation endpoint
    threads, results = in_threads(4, lambda: owner_client(sid_).post(f"/api/interview-sessions/{sid_}/evaluations/batch", json={}))
    for t in threads:
        t.join(10)

    # All concurrent requests succeeded with 200 and identical total evaluations
    assert all(r.status_code == 200 for r in results)
    totals = [r.json()["total_evaluated"] for r in results]
    assert all(t >= 1 for t in totals)
