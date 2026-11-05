"""Test for AUDIT-004: Technical accuracy evaluation criterion."""

import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.answer_evaluation import Criterion, Rubric
from app.services.evaluator import RUBRICS
from tests.auth_helpers import owner_client
from tests.fakes import FakeProvider, make_gateway
from tests.test_evaluation_api import evaluate
from tests.test_interview_plan_api import use_gateway
from tests.test_interview_turn_api import act, ans, committed_interview, first_question


def test_technical_accuracy_in_rubric():
    assert Criterion.TECHNICAL_ACCURACY in RUBRICS[Rubric.TECHNICAL_PROJECT]
    assert RUBRICS[Rubric.TECHNICAL_PROJECT][0] == Criterion.TECHNICAL_ACCURACY


def test_evaluate_technical_accuracy(committed_interview: uuid.UUID, use_gateway) -> None:
    sid_ = committed_interview
    client = owner_client(sid_)
    q = first_question(client, sid_)
    
    # Accurate technical answer
    ans(client, sid_, q["id"], said="In Python dictionaries use hash tables which give average O(1) time complexity for lookups.")
    first_question(client, sid_)
    act(client, sid_, "END_INTERVIEW")

    client.put(f"/api/interview-sessions/{sid_}/ai-consent", json={"granted": True})

    eval_proposal = {
        "criteria": [
            {"criterion": "technical_accuracy", "score": 5, "evidence": ["dictionaries use hash tables which give average O(1) time complexity"]},
            {"criterion": "relevance", "score": 5, "evidence": ["dictionaries use hash tables"]},
            {"criterion": "completeness", "score": 4, "evidence": ["average O(1) time complexity for lookups."]},
            {"criterion": "reasoning", "score": 4, "evidence": ["average O(1) time complexity"]},
            {"criterion": "communication", "score": 5, "evidence": ["dictionaries use hash tables which give average O(1) time complexity"]},
        ],
        "missing_concepts": [],
        "claims_to_verify": [],
        "feedback": "Great explanation of hash table lookup complexity in Python.",
    }

    use_gateway(make_gateway(FakeProvider("groq", eval_proposal)))

    res = evaluate(client, sid_, q["id"])
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "EVALUATED"
    
    criteria = {c["criterion"]: c["score"] for c in data["evaluation"]["criteria"]}
    assert "technical_accuracy" in criteria
    assert criteria["technical_accuracy"] == 5
