import logging
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes.candidates import candidate_write
from app.core.config import Settings
from app.main import create_app
from app.models import AuthSession, Candidate
from tests.auth_helpers import api_client

EMAIL = "harshad@example.com"
PASSWORD = "correct horse 1"
REGISTER = "/api/auth/register"


def create(client: TestClient, email: str = EMAIL, name: str = "Harshad") -> dict:
    """Provisions a candidate and sets session cookie for candidate profile testing."""
    from app.services.auth import start_session
    from app.db.session import get_db, SessionLocal
    from app.api.deps import SESSION_COOKIE
    clean_email = email.strip().lower()
    clean_name = " ".join(name.strip().split()) or "Harshad"
    cand_id = uuid.uuid4()
    auth_id = uuid.uuid4()
    override = client.app.dependency_overrides.get(get_db)
    if override:
        db = next(override())
        cand = Candidate(id=cand_id, email=clean_email, name=clean_name, auth_user_id=auth_id)
        db.add(cand)
        db.flush()
        token = start_session(db, cand.id, cand.auth_user_id)
        db.flush()
    else:
        with SessionLocal() as db:
            cand = Candidate(id=cand_id, email=clean_email, name=clean_name, auth_user_id=auth_id)
            db.add(cand)
            db.commit()
            token = start_session(db, cand.id, cand.auth_user_id)
            db.commit()
    client.cookies.set(SESSION_COOKIE, token)
    return {
        "id": str(cand.id),
        "email": cand.email,
        "name": cand.name,
        "created_at": cand.created_at.isoformat() if cand.created_at else "2026-11-04T00:00:00Z",
    }


# ---------- create (registration) ----------


def test_create_returns_201_and_normalizes(client: TestClient) -> None:
    res = client.post(REGISTER, json={"email": "  Harshad@Example.COM ",
                                      "name": "  Harshad \t  Ambi ", "password": PASSWORD})
    assert res.status_code == 201
    body = res.json()
    assert body["email"] == EMAIL
    assert body["verification_required"] is True
    assert body["message"]


@pytest.mark.parametrize(
    "payload",
    [
        {"email": EMAIL},
        {"name": "Harshad"},
        {"email": EMAIL, "name": ""},
        {"email": EMAIL, "name": "   \t "},
        {"email": EMAIL, "name": "x" * 201},
        {"email": EMAIL, "name": None},
        {"email": "not-an-email", "name": "Harshad"},
        {"email": "a@b", "name": "Harshad"},
        {"email": "a@@example.com", "name": "Harshad"},
        {"email": "", "name": "Harshad"},
        {"email": "a" * 250 + "@example.com", "name": "Harshad"},
    ],
    ids=[
        "missing-name", "missing-email", "empty-name", "blank-name", "long-name", "null-name",
        "no-at", "no-dot-domain", "double-at", "empty-email", "long-email",
    ],
)
def test_create_rejects_invalid_input(client: TestClient, payload: dict) -> None:
    assert client.post(REGISTER, json={**payload, "password": PASSWORD}).status_code == 422


def test_create_accepts_max_length_name(client: TestClient) -> None:
    res = client.post(REGISTER, json={"email": "maxname@example.com", "name": "x" * 200, "password": PASSWORD})
    assert res.status_code == 201
    assert res.json()["verification_required"] is True


@pytest.mark.parametrize(
    "extra", [{"id": str(uuid.uuid4())}, {"created_at": "2020-01-01T00:00:00Z"}]
)
def test_create_rejects_server_controlled_fields(client: TestClient, extra: dict) -> None:
    res = client.post(REGISTER, json={"email": EMAIL, "name": "H", "password": PASSWORD, **extra})
    assert res.status_code == 422


@pytest.mark.parametrize("dupe", [EMAIL, "HARSHAD@EXAMPLE.COM"])
def test_create_duplicate_email_is_409(client: TestClient, fake_supabase, dupe: str) -> None:
    fake_supabase.register_test_user(EMAIL)
    res = client.post(REGISTER, json={"email": dupe, "name": "Other", "password": PASSWORD})
    assert res.status_code == 409
    assert res.json() == {"detail": "An account with this email already exists."}


# ---------- read ----------


def test_read_existing(client: TestClient) -> None:
    created = create(client)
    res = client.get(f"/api/candidates/{created['id']}")
    assert res.status_code == 200
    assert res.json() == created


def test_read_nonexistent_is_404(client: TestClient) -> None:
    create(client)
    res = client.get(f"/api/candidates/{uuid.uuid4()}")
    assert res.status_code == 404
    assert res.json() == {"detail": "Candidate not found"}


def test_read_malformed_id_is_422(client: TestClient) -> None:
    create(client)
    assert client.get("/api/candidates/not-a-uuid").status_code == 422


def test_no_list_or_unauthenticated_create_endpoint(client: TestClient) -> None:
    # POST /api/candidates was replaced by registration: nothing creates a candidate here.
    assert client.get("/api/candidates").status_code == 404
    assert client.post("/api/candidates", json={"email": EMAIL, "name": "H"}).status_code == 404


# ---------- update ----------


def test_update_name_only_leaves_email(client: TestClient) -> None:
    c = create(client)
    res = client.patch(f"/api/candidates/{c['id']}", json={"name": "  New   Name "})
    assert res.status_code == 200
    assert res.json() == {**c, "name": "New Name"}


def test_update_email_only_normalizes_and_leaves_name(client: TestClient) -> None:
    c = create(client)
    res = client.patch(f"/api/candidates/{c['id']}", json={"email": "New@Example.com"})
    assert res.status_code == 200
    assert res.json() == {**c, "email": "new@example.com"}
    assert client.get(f"/api/candidates/{c['id']}").json()["email"] == "new@example.com"


def test_update_both_fields(client: TestClient) -> None:
    c = create(client)
    res = client.patch(
        f"/api/candidates/{c['id']}", json={"email": "b@example.com", "name": "B"}
    )
    assert res.json() == {**c, "email": "b@example.com", "name": "B"}


def test_update_to_own_email_is_ok(client: TestClient) -> None:
    c = create(client)
    assert client.patch(f"/api/candidates/{c['id']}", json={"email": EMAIL}).status_code == 200


def test_update_to_taken_email_is_409_and_rolls_back(client: TestClient) -> None:
    create(client, email="taken@example.com")
    c = create(client)
    res = client.patch(
        f"/api/candidates/{c['id']}", json={"email": "Taken@example.com", "name": "Changed"}
    )
    assert res.status_code == 409
    assert client.get(f"/api/candidates/{c['id']}").json() == c


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": None},
        {"email": None},
        {"name": "   "},
        {"email": "bad"},
        {"id": str(uuid.uuid4())},
        {"created_at": "2020-01-01T00:00:00Z"},
        {"name": "Ok", "id": str(uuid.uuid4())},
    ],
    ids=["empty", "null-name", "null-email", "blank-name", "bad-email", "id", "created_at", "mixed"],
)
def test_update_rejects_invalid_patch(client: TestClient, payload: dict) -> None:
    c = create(client)
    assert client.patch(f"/api/candidates/{c['id']}", json=payload).status_code == 422
    assert client.get(f"/api/candidates/{c['id']}").json() == c


def test_update_nonexistent_is_404(client: TestClient) -> None:
    create(client)
    res = client.patch(f"/api/candidates/{uuid.uuid4()}", json={"name": "X"})
    assert res.status_code == 404


# ---------- delete ----------


def test_delete_removes_candidate(client: TestClient, db_session: Session) -> None:
    c = create(client)
    res = client.delete(f"/api/candidates/{c['id']}")
    assert res.status_code == 204
    assert res.content == b""
    assert db_session.scalar(select(Candidate).where(Candidate.email == EMAIL)) is None
    # Its sessions went with it (ON DELETE CASCADE), and the cookie is cleared: signed out.
    assert db_session.scalar(select(AuthSession)) is None
    assert 'coach_session=""' in res.headers["set-cookie"]
    assert client.get(f"/api/candidates/{c['id']}").status_code == 401
    assert client.delete(f"/api/candidates/{c['id']}").status_code == 401


def test_delete_frees_email_for_reuse(client: TestClient) -> None:
    c = create(client)
    client.delete(f"/api/candidates/{c['id']}")
    create(client)


def test_delete_nonexistent_is_404(client: TestClient) -> None:
    create(client)
    assert client.delete(f"/api/candidates/{uuid.uuid4()}").status_code == 404


# ---------- database unavailable / privacy ----------


def test_database_unavailable_is_controlled_503(
    dead_database_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for res in (
        dead_database_client.post(REGISTER, json={"email": EMAIL, "name": "H",
                                                  "password": PASSWORD}),
        dead_database_client.get(f"/api/candidates/{uuid.uuid4()}"),
    ):
        assert res.status_code == 503
        assert res.json() == {"detail": "Service temporarily unavailable. Please try again."}
        for leak in ("secretpw", "127.0.0.1", "nobody", "psycopg"):
            assert leak not in res.text
    assert "secretpw" not in caplog.text
    assert EMAIL not in caplog.text


def test_duplicate_does_not_log_personal_data(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    create(client)
    client.post(REGISTER, json={"email": EMAIL, "name": "Harshad", "password": PASSWORD})
    assert EMAIL not in caplog.text
    assert PASSWORD not in caplog.text
    assert "Harshad" not in caplog.text


def test_unexpected_constraint_violation_is_500_without_personal_data(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # Bypass schema validation to hit a DB CHECK the API normally prevents.
    caplog.set_level(logging.DEBUG)
    with pytest.raises(HTTPException) as exc, candidate_write(db_session):
        db_session.add(Candidate(email="Upper@Example.com", name="Secret Person"))
    assert exc.value.status_code == 500
    assert exc.value.detail == "Could not save candidate"
    assert "ck_candidates_email_lowercase" in caplog.text
    assert "Upper@Example.com" not in caplog.text
    assert "Secret Person" not in caplog.text


def test_candidate_routes_served_in_production() -> None:
    # AUDIT-001: routers ARE registered in production, so unauthenticated requests return 401 (not 404)
    prod = create_app(
        Settings(_env_file=None, database_url="postgresql+psycopg://u:p@h/x", environment="production")
    )
    c = TestClient(prod, headers={"Origin": "http://localhost:3000"})
    assert c.get(f"/api/candidates/{uuid.uuid4()}").status_code == 401
    assert c.get("/api/auth/me").status_code == 401
    # Control: the same request is routed outside production (and needs a session there).
    assert api_client().get("/api/candidates/not-a-uuid").status_code == 401
