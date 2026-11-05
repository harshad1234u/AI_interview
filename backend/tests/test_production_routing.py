import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_production_environment_registers_all_functional_routers() -> None:
    """AUDIT-001 verification: ensure all core routers are registered when ENVIRONMENT=production."""
    prod_settings = Settings(
        environment="production",
        database_url="postgresql+psycopg://postgres:postgres@localhost:5432/ai_interview_coach",
        auth_cookie_secure=True,
    )
    prod_app = create_app(prod_settings)
    client = TestClient(prod_app)

    # 1. Health check is available
    res_health = client.get("/api/health")
    assert res_health.status_code in (200, 503)

    # 2. Auth routes are registered (POST with empty body returns 422, not 404)
    res_auth = client.post("/api/auth/login", json={}, headers={"Origin": "http://localhost:3000"})
    assert res_auth.status_code != 404

    # 3. Candidates routes are registered (protected -> 401, not 404)
    res_candidate = client.get("/api/candidates/me", headers={"Origin": "http://localhost:3000"})
    assert res_candidate.status_code == 401

    # 4. Resumes routes are registered (protected -> 401, not 404)
    res_resumes = client.post("/api/resumes", headers={"Origin": "http://localhost:3000"})
    assert res_resumes.status_code == 401

    # 5. Manual facts routes are registered (protected -> 401, not 404)
    res_facts = client.get(
        "/api/candidates/00000000-0000-0000-0000-000000000000/manual-facts",
        headers={"Origin": "http://localhost:3000"},
    )
    assert res_facts.status_code == 401

    # 6. Interview sessions routes are registered (protected -> 401, not 404)
    res_sessions = client.post(
        "/api/interview-sessions", json={}, headers={"Origin": "http://localhost:3000"}
    )
    assert res_sessions.status_code == 401

    # 7. Documentation routes are disabled in production for security
    assert prod_app.docs_url is None
    assert prod_app.openapi_url is None
    res_docs = client.get("/docs")
    assert res_docs.status_code == 404
    res_openapi = client.get("/openapi.json")
    assert res_openapi.status_code == 404


def test_development_environment_enables_docs() -> None:
    dev_settings = Settings(
        environment="development",
        database_url="postgresql+psycopg://postgres:postgres@localhost:5432/ai_interview_coach",
    )
    dev_app = create_app(dev_settings)
    assert dev_app.docs_url == "/docs"
    assert dev_app.openapi_url == "/openapi.json"
