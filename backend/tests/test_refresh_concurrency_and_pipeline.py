"""Tests for refresh-token concurrency protection (CAS fencing) and the active Supabase refresh pipeline.

Verifies:
- Atomic CAS lock acquisition with unique fencing UUID (refresh_lock_id)
- Rejection of concurrent lock acquisitions
- Rejection of stale workers (CAS lock ID mismatch / rowcount == 0)
- Rejection of commits to revoked or expired sessions
- Safe handling of concurrent logout during in-flight refresh
- Full Supabase refresh pipeline roundtrip (decrypt -> upstream refresh -> encrypt -> CAS persist)
- Fail-closed permanent revocation on InvalidGrant
- Safe lock release on transient network failures
- POST /api/auth/refresh endpoint behavior
- Transparent refresh integration in GET /api/auth/me
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import SESSION_COOKIE
from app.core import security
from app.core.config import get_settings
from app.models import AuthSession, Candidate
from app.services import auth
from tests.fake_supabase import FakeSupabaseAuthService


@pytest.fixture
def test_candidate(db_session: Session) -> Candidate:
    c = Candidate(
        id=uuid.uuid4(),
        email=f"candidate_{uuid.uuid4().hex[:8]}@example.com",
        name="Refresh Test Candidate",
        auth_user_id=uuid.uuid4(),
    )
    db_session.add(c)
    db_session.flush()
    return c


@pytest.fixture
def active_session_with_refresh(db_session: Session, test_candidate: Candidate) -> tuple[str, str, str]:
    """Returns (raw_session_token, token_hash, plaintext_refresh_token)."""
    rt_plaintext = f"sb-test-rt-{uuid.uuid4().hex}"
    raw_token = auth.start_session(
        db_session,
        candidate_id=test_candidate.id,
        auth_user_id=test_candidate.auth_user_id,
        refresh_token=rt_plaintext,
    )
    db_session.flush()
    th = security.token_hash(raw_token)
    return raw_token, th, rt_plaintext


# ===========================================================================
# 1. CAS Lock Acquisition & Concurrency Fencing
# ===========================================================================

def test_cas_lock_acquisition_success(db_session: Session, active_session_with_refresh: tuple[str, str, str]) -> None:
    _, th, _ = active_session_with_refresh
    lock_id = auth.acquire_refresh_lock(db_session, th)
    assert lock_id is not None
    assert isinstance(lock_id, uuid.UUID)

    sess = db_session.scalars(select(AuthSession).where(AuthSession.token_hash == th)).one()
    assert sess.refresh_lock_id == lock_id
    assert sess.refresh_locked_at is not None


def test_cas_lock_acquisition_rejects_concurrent_worker(db_session: Session, active_session_with_refresh: tuple[str, str, str]) -> None:
    _, th, _ = active_session_with_refresh
    lock_id_1 = auth.acquire_refresh_lock(db_session, th)
    assert lock_id_1 is not None

    # Immediate second attempt while lock is active (< 30s) must be refused
    lock_id_2 = auth.acquire_refresh_lock(db_session, th)
    assert lock_id_2 is None


def test_cas_lock_can_be_reacquired_after_timeout(db_session: Session, active_session_with_refresh: tuple[str, str, str]) -> None:
    _, th, _ = active_session_with_refresh
    lock_id_1 = auth.acquire_refresh_lock(db_session, th)
    assert lock_id_1 is not None

    # Simulate timeout (> 30s ago)
    now = auth._now()
    sess = db_session.scalars(select(AuthSession).where(AuthSession.token_hash == th)).one()
    sess.refresh_locked_at = now - timedelta(seconds=35)
    db_session.flush()

    # Fresh worker acquires new lock
    lock_id_2 = auth.acquire_refresh_lock(db_session, th)
    assert lock_id_2 is not None
    assert lock_id_2 != lock_id_1

    sess = db_session.scalars(select(AuthSession).where(AuthSession.token_hash == th)).one()
    assert sess.refresh_lock_id == lock_id_2


# ===========================================================================
# 2. CAS Token Persistence & Stale Worker Rejection
# ===========================================================================

def test_cas_persistence_success(db_session: Session, active_session_with_refresh: tuple[str, str, str], test_candidate: Candidate) -> None:
    _, th, _ = active_session_with_refresh
    lock_id = auth.acquire_refresh_lock(db_session, th)
    assert lock_id is not None

    settings = get_settings()
    new_rt = "new-rotated-token-123"
    new_encrypted = security.encrypt_refresh_token(
        refresh_token=new_rt,
        token_hash=th,
        auth_user_id=str(test_candidate.auth_user_id),
        key=settings.session_key_bytes,
        key_id=settings.session_key_id,
    )
    new_expiry = auth._now() + timedelta(hours=1)

    persisted = auth.persist_rotated_refresh_token(
        db_session,
        token_hash=th,
        expected_lock_id=lock_id,
        new_encrypted_refresh_token=new_encrypted,
        access_token_expires_at=new_expiry,
    )
    assert persisted is True

    sess = db_session.scalars(select(AuthSession).where(AuthSession.token_hash == th)).one()
    assert sess.refresh_lock_id is None
    assert sess.refresh_locked_at is None
    assert sess.encrypted_refresh_token == new_encrypted
    assert sess.access_token_expires_at == new_expiry


def test_cas_persistence_rejects_stale_worker(db_session: Session, active_session_with_refresh: tuple[str, str, str]) -> None:
    _, th, _ = active_session_with_refresh
    stale_lock_id = auth.acquire_refresh_lock(db_session, th)
    assert stale_lock_id is not None

    # Simulate timeout and takeover by a new worker
    now = auth._now()
    sess = db_session.scalars(select(AuthSession).where(AuthSession.token_hash == th)).one()
    sess.refresh_locked_at = now - timedelta(seconds=35)
    db_session.flush()

    new_lock_id = auth.acquire_refresh_lock(db_session, th)
    assert new_lock_id is not None
    assert new_lock_id != stale_lock_id

    # Stale worker attempts to persist using its old lock ID -> must be rejected (0 rows updated)
    persisted = auth.persist_rotated_refresh_token(
        db_session,
        token_hash=th,
        expected_lock_id=stale_lock_id,
        new_encrypted_refresh_token="stale-ciphertext",
    )
    assert persisted is False

    # Database still holds the new worker's lock
    sess = db_session.scalars(select(AuthSession).where(AuthSession.token_hash == th)).one()
    assert sess.refresh_lock_id == new_lock_id
    assert sess.encrypted_refresh_token != "stale-ciphertext"


def test_cas_persistence_rejects_concurrently_revoked_session(
    db_session: Session, active_session_with_refresh: tuple[str, str, str]
) -> None:
    raw_token, th, _ = active_session_with_refresh
    lock_id = auth.acquire_refresh_lock(db_session, th)
    assert lock_id is not None

    # User concurrently logs out
    auth.end_session(db_session, raw_token)
    sess = db_session.scalars(select(AuthSession).where(AuthSession.token_hash == th)).one()
    assert sess.revoked_at is not None

    # Worker returning from upstream attempts to persist -> rejected
    persisted = auth.persist_rotated_refresh_token(
        db_session,
        token_hash=th,
        expected_lock_id=lock_id,
        new_encrypted_refresh_token="rotated-ciphertext",
    )
    assert persisted is False
    # Session must remain revoked
    assert sess.revoked_at is not None


def test_cas_persistence_rejects_expired_session(
    db_session: Session, active_session_with_refresh: tuple[str, str, str]
) -> None:
    _, th, _ = active_session_with_refresh
    lock_id = auth.acquire_refresh_lock(db_session, th)
    assert lock_id is not None

    # Fast forward session to past absolute_expires_at
    sess = db_session.scalars(select(AuthSession).where(AuthSession.token_hash == th)).one()
    sess.absolute_expires_at = auth._now() - timedelta(seconds=1)
    db_session.flush()

    persisted = auth.persist_rotated_refresh_token(
        db_session,
        token_hash=th,
        expected_lock_id=lock_id,
        new_encrypted_refresh_token="rotated-ciphertext",
    )
    assert persisted is False


# ===========================================================================
# 3. Full Supabase Refresh Pipeline Integration
# ===========================================================================

@pytest.mark.anyio
async def test_rotate_session_refresh_token_full_pipeline(
    db_session: Session,
    active_session_with_refresh: tuple[str, str, str],
    fake_supabase: FakeSupabaseAuthService,
    test_candidate: Candidate,
) -> None:
    raw_token, th, initial_rt = active_session_with_refresh

    success = await auth.rotate_session_refresh_token(db_session, raw_token, fake_supabase)
    assert success is True

    # Inspect rotated token in DB
    sess = db_session.scalars(select(AuthSession).where(AuthSession.token_hash == th)).one()
    assert sess.refresh_lock_id is None
    assert sess.refresh_locked_at is None
    assert sess.access_token_expires_at is not None

    # Verify decrypted token matches FakeSupabase rotation pattern
    settings = get_settings()
    decrypted_rt, _ = security.decrypt_refresh_token(
        sess.encrypted_refresh_token,
        th,
        str(test_candidate.auth_user_id),
        primary_key=settings.session_key_bytes,
    )
    assert decrypted_rt == f"rotated_{initial_rt}"


@pytest.mark.anyio
async def test_rotate_session_refresh_token_fatal_invalid_grant_revokes_session(
    db_session: Session,
    test_candidate: Candidate,
    fake_supabase: FakeSupabaseAuthService,
) -> None:
    # Start session with invalid refresh token
    raw_token = auth.start_session(
        db_session,
        candidate_id=test_candidate.id,
        auth_user_id=test_candidate.auth_user_id,
        refresh_token="invalid_refresh_token",
    )
    th = security.token_hash(raw_token)

    success = await auth.rotate_session_refresh_token(db_session, raw_token, fake_supabase)
    assert success is False

    # Fail-closed: session must be marked permanently revoked
    sess = db_session.scalars(select(AuthSession).where(AuthSession.token_hash == th)).one()
    assert sess.revoked_at is not None
    assert sess.refresh_lock_id is None
    assert sess.refresh_locked_at is None
    assert auth.session_candidate(db_session, raw_token) is None


@pytest.mark.anyio
async def test_rotate_session_refresh_token_transient_failure_releases_lock(
    db_session: Session,
    active_session_with_refresh: tuple[str, str, str],
    monkeypatch: pytest.MonkeyPatch,
    test_candidate: Candidate,
) -> None:
    raw_token, th, _ = active_session_with_refresh

    # Simulate network timeout
    class FailingSupabase:
        async def refresh_session(self, rt: str):
            raise httpx.ConnectTimeout("Connection to Supabase timed out")

    success = await auth.rotate_session_refresh_token(db_session, raw_token, FailingSupabase())
    assert success is False

    # Lock must be released, but session must NOT be revoked
    sess = db_session.scalars(select(AuthSession).where(AuthSession.token_hash == th)).one()
    assert sess.refresh_lock_id is None
    assert sess.refresh_locked_at is None
    assert sess.revoked_at is None

    # Candidate can still use their active session
    assert auth.session_candidate(db_session, raw_token) is not None


# ===========================================================================
# 4. HTTP API Endpoints: POST /api/auth/refresh and GET /api/auth/me
# ===========================================================================

def test_api_refresh_endpoint_unauthenticated(client: TestClient) -> None:
    res = client.post("/api/auth/refresh")
    assert res.status_code == 401


def test_api_refresh_endpoint_authenticated(
    client: TestClient,
    active_session_with_refresh: tuple[str, str, str],
) -> None:
    raw_token, _, _ = active_session_with_refresh
    client.cookies.set(SESSION_COOKIE, raw_token)

    res = client.post("/api/auth/refresh")
    assert res.status_code == 200
    assert "refreshed" in res.json()["message"].lower() or "active" in res.json()["message"].lower()


def test_api_auth_me_transparent_rotation(
    client: TestClient,
    db_session: Session,
    active_session_with_refresh: tuple[str, str, str],
    test_candidate: Candidate,
) -> None:
    raw_token, th, initial_rt = active_session_with_refresh
    client.cookies.set(SESSION_COOKIE, raw_token)

    # Set access token expired
    sess = db_session.scalars(select(AuthSession).where(AuthSession.token_hash == th)).one()
    sess.access_token_expires_at = auth._now() - timedelta(minutes=1)
    db_session.flush()

    res = client.get("/api/auth/me")
    assert res.status_code == 200
    assert res.json()["id"] == str(test_candidate.id)

    # Verify that Supabase token was rotated in DB
    db_session.expire_all()
    sess_after = db_session.scalars(select(AuthSession).where(AuthSession.token_hash == th)).one()
    settings = get_settings()
    decrypted, _ = security.decrypt_refresh_token(
        sess_after.encrypted_refresh_token,
        th,
        str(test_candidate.auth_user_id),
        primary_key=settings.session_key_bytes,
    )
    assert decrypted == f"rotated_{initial_rt}"
