"""Test for AUDIT-007: Expired auth sessions cleanup."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import security
from app.db.session import SessionLocal
from app.models import AuthSession, Candidate
from app.services.auth import cleanup_expired_sessions


def test_cleanup_expired_sessions(db_session: Session):
    db = db_session
    now = datetime.now(UTC)
    c1 = Candidate(email=f"c1_{uuid.uuid4().hex[:8]}@example.com", name="Candidate 1")
    c2 = Candidate(email=f"c2_{uuid.uuid4().hex[:8]}@example.com", name="Candidate 2")
    db.add_all([c1, c2])
    db.flush()

    # Expired session for candidate 1 (expired 2 hours ago)
    s1 = AuthSession(
        token_hash=security.token_hash(security.new_session_token()),
        candidate_id=c1.id,
        created_at=now - timedelta(days=8),
        expires_at=now - timedelta(hours=2),
    )
    # Active session for candidate 1 (expires in 6 days)
    s2 = AuthSession(
        token_hash=security.token_hash(security.new_session_token()),
        candidate_id=c1.id,
        created_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=6),
    )
    # Expired session for candidate 2 (expired 1 day ago)
    s3 = AuthSession(
        token_hash=security.token_hash(security.new_session_token()),
        candidate_id=c2.id,
        created_at=now - timedelta(days=9),
        expires_at=now - timedelta(days=1),
    )
    db.add_all([s1, s2, s3])
    db.flush()

    # Run cleanup
    cleaned = cleanup_expired_sessions(db)
    db.flush()

    assert cleaned >= 2

    # Verify active session remains
    active_remaining = db.scalars(
        select(AuthSession).where(AuthSession.candidate_id == c1.id, AuthSession.expires_at > now)
    ).all()
    assert len(active_remaining) >= 1

    # Verify expired sessions were deleted
    expired_remaining = db.scalars(
        select(AuthSession).where(
            AuthSession.candidate_id.in_([c1.id, c2.id]),
            AuthSession.expires_at <= now,
        )
    ).all()
    assert len(expired_remaining) == 0
