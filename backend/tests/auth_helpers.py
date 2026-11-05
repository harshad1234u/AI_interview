"""Signing test clients in. Import from tests, never from conftest (it reconfigures the env)."""

import re
from functools import lru_cache

from fastapi.testclient import TestClient
from requests import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import SESSION_COOKIE
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import app
from app.models import Candidate, InterviewSession
from app.services.auth import start_session

# State-changing requests must carry an allowed Origin (Phase 12 CSRF check), as browsers do.
ORIGIN = get_settings().cors_origin_list[0]


def api_client(token: str | None = None) -> TestClient:
    """A client like the frontend's: allowed Origin, and signed in when given a session token."""
    c = TestClient(app, headers={"Origin": ORIGIN})
    if token:
        c.cookies.set(SESSION_COOKIE, token)
    return c


def sign_in(client: TestClient, db: Session, candidate_id: object) -> None:
    """Signs the client in as this candidate, with a real session row (no password hashing)."""
    client.cookies.set(SESSION_COOKIE, start_session(db, candidate_id))


def make_candidate(client: TestClient, db: Session, email: str = "asha@example.com",
                   name: str = "Asha Rao") -> str:
    """Adds a candidate (inside the test's rolled-back transaction) and signs the client in."""
    candidate = Candidate(email=email, name=name)
    db.add(candidate)
    db.flush()
    sign_in(client, db, candidate.id)
    return str(candidate.id)


def committed_client(candidate_id: object) -> TestClient:
    """For tests on real, committed connections: a client signed in with a committed session.
    The session row goes when the test deletes its candidate (ON DELETE CASCADE)."""
    with SessionLocal() as db:
        token = start_session(db, candidate_id)
        db.commit()
    return api_client(token)


@lru_cache
def _owner_token(session_id: object) -> str:
    with SessionLocal() as db:
        candidate_id = db.scalar(
            select(InterviewSession.candidate_id).where(InterviewSession.id == session_id))
        token = start_session(db, candidate_id)
        db.commit()
    return token


def owner_client(session_id: object) -> TestClient:
    """For tests on committed rows: a client signed in as the session's owner. The token is
    looked up once per session, so calls inside racing threads touch no database rows."""
    return api_client(_owner_token(session_id))


def verify_email_flow(client: TestClient, token_hash: str, type_val: str = "signup") -> Response:
    """Performs the complete two-step email confirmation flow (GET confirmation page + POST confirm)."""
    get_res = client.get(f"/api/auth/verify?token_hash={token_hash}&type={type_val}", follow_redirects=False)
    assert get_res.status_code == 200, f"Expected 200 from verify page, got {get_res.status_code}"
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_res.text)
    assert m is not None, "csrf_token not found in verify page HTML"
    csrf_token = m.group(1)
    return client.post(
        "/api/auth/verify",
        data={"token_hash": token_hash, "type": type_val, "csrf_token": csrf_token},
        follow_redirects=False,
    )
