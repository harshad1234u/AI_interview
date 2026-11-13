"""Authentication service, candidate provisioning, and server-side session management (BFF Pattern).

All external user identities are verified via Supabase Auth (GoTrue).
All candidate profiles are indexed by internal UUID (`candidates.id`) and mapped
to external identities via `candidates.auth_user_id`.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import (
    DUMMY_HASH,
    DecryptionError,
    decrypt_refresh_token,
    encrypt_refresh_token,
    hash_password,
    new_session_token,
    token_hash,
    verify_password,
)
from app.models import AuthSession, Candidate

logger = logging.getLogger(__name__)

SESSION_SLIDING_TTL = timedelta(days=7)
SESSION_ABSOLUTE_TTL = timedelta(days=30)
SESSION_TTL = SESSION_SLIDING_TTL
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)


class InvalidCredentials(Exception):
    """Unknown email or invalid credentials."""
    pass


class AccountLocked(Exception):
    """Account locked due to consecutive failed attempts."""
    pass


class CandidateIdentityConflict(Exception):
    """Raised when candidate provisioning encounters conflicting credentials or identity mismatch."""
    def __init__(self, message: str = "An account with conflicting credentials already exists."):
        super().__init__(message)
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def provision_or_get_candidate(
    db: Session,
    auth_user_id: uuid.UUID,
    email: str,
    name: str,
) -> Candidate:
    """
    Idempotently retrieves an existing candidate by auth_user_id or provisions a new one.
    Strictly decouples identity linking from email string matching.
    """
    candidate = db.scalars(
        select(Candidate).where(Candidate.auth_user_id == auth_user_id)
    ).one_or_none()
    
    if candidate is not None:
        return candidate

    # Provision fresh candidate record
    clean_email = email.strip().lower()
    clean_name = name.strip() or "Candidate"
    
    candidate = Candidate(
        id=uuid.uuid4(),
        auth_user_id=auth_user_id,
        email=clean_email,
        name=clean_name,
        created_at=_now(),
    )
    db.add(candidate)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        # Distinguish concurrent duplicate auth_user_id from email uniqueness collision
        candidate = db.scalars(
            select(Candidate).where(Candidate.auth_user_id == auth_user_id)
        ).one_or_none()
        if candidate is not None:
            return candidate
        logger.warning(
            "Candidate provisioning conflict: auth_user_id %s collided on unique constraint (e.g. email)",
            auth_user_id,
        )
        raise CandidateIdentityConflict("An account with conflicting credentials already exists.")
        
    return candidate


def start_session(
    db: Session,
    candidate_id: uuid.UUID,
    auth_user_id: Optional[uuid.UUID] = None,
    refresh_token: Optional[str] = None,
    access_token_expires_at: Optional[datetime] = None,
) -> str:
    """
    Creates a new server-side session. Stores the encrypted refresh token at rest.
    Returns the raw 32-byte opaque session token (never stored).
    """
    settings = get_settings()
    now = _now()
    raw_token = new_session_token()
    th = token_hash(raw_token)
    
    encrypted_rt = None
    if refresh_token:
        effective_auth_id = str(auth_user_id or candidate_id)
        encrypted_rt = encrypt_refresh_token(
            refresh_token=refresh_token,
            token_hash=th,
            auth_user_id=effective_auth_id,
            key=settings.session_key_bytes,
            key_id=settings.session_key_id,
        )

    # Housekeeping: clean up expired sessions for this candidate
    db.execute(
        delete(AuthSession).where(
            AuthSession.candidate_id == candidate_id,
            or_(
                AuthSession.expires_at <= now,
                AuthSession.absolute_expires_at <= now,
                AuthSession.revoked_at.is_not(None),
            ),
        )
    )

    session = AuthSession(
        token_hash=th,
        candidate_id=candidate_id,
        auth_user_id=auth_user_id,
        encrypted_refresh_token=encrypted_rt,
        access_token_expires_at=access_token_expires_at or (now + timedelta(hours=1) if refresh_token else None),
        created_at=now,
        last_used_at=now,
        expires_at=now + SESSION_SLIDING_TTL,
        absolute_expires_at=now + SESSION_ABSOLUTE_TTL,
        refresh_locked_at=None,
        refresh_lock_id=None,
        revoked_at=None,
    )
    db.add(session)
    db.flush()
    return raw_token


def acquire_refresh_lock(db: Session, token_hash: str) -> Optional[uuid.UUID]:
    """
    Atomically acquires a refresh lock on an active session using CAS semantics.
    Generates and returns a unique fencing identifier (UUID) if the lock is acquired.
    Rejects acquisition if:
    - Session does not exist
    - Session is revoked (revoked_at is not None)
    - Session is expired (now >= expires_at or now >= absolute_expires_at)
    - Session has an active unexpired lock (refresh_locked_at within the last 30s)
    """
    now = _now()
    lock_cutoff = now - timedelta(seconds=30)
    new_lock_id = uuid.uuid4()

    stmt = (
        update(AuthSession)
        .where(
            AuthSession.token_hash == token_hash,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
            AuthSession.absolute_expires_at > now,
            or_(
                AuthSession.refresh_locked_at.is_(None),
                AuthSession.refresh_locked_at < lock_cutoff,
            ),
        )
        .values(
            refresh_locked_at=now,
            refresh_lock_id=new_lock_id,
        )
    )
    result = db.execute(stmt)
    db.flush()
    if (result.rowcount or 0) == 1:
        return new_lock_id
    return None


def persist_rotated_refresh_token(
    db: Session,
    token_hash: str,
    expected_lock_id: uuid.UUID,
    new_encrypted_refresh_token: str,
    access_token_expires_at: Optional[datetime] = None,
) -> bool:
    """
    Persists a newly rotated encrypted refresh token using CAS fencing.
    Requires exact match on expected_lock_id.
    Guarantees:
    - Stale workers whose lock timed out or was superseded cannot commit (rowcount == 0).
    - Revoked sessions (e.g. user logged out concurrently) cannot be revived (rowcount == 0).
    - Expired sessions cannot be revived (rowcount == 0).
    Clears the lock atomically upon successful persistence.
    """
    now = _now()
    stmt = (
        update(AuthSession)
        .where(
            AuthSession.token_hash == token_hash,
            AuthSession.refresh_lock_id == expected_lock_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.absolute_expires_at > now,
        )
        .values(
            encrypted_refresh_token=new_encrypted_refresh_token,
            access_token_expires_at=access_token_expires_at,
            refresh_locked_at=None,
            refresh_lock_id=None,
            last_used_at=now,
        )
    )
    result = db.execute(stmt)
    db.flush()
    return (result.rowcount or 0) == 1


def release_refresh_lock(
    db: Session,
    token_hash: str,
    expected_lock_id: uuid.UUID,
) -> bool:
    """
    Releases the refresh lock in case of transient upstream failure.
    Requires expected_lock_id match. Does NOT restore revoked sessions.
    """
    stmt = (
        update(AuthSession)
        .where(
            AuthSession.token_hash == token_hash,
            AuthSession.refresh_lock_id == expected_lock_id,
            AuthSession.revoked_at.is_(None),
        )
        .values(
            refresh_locked_at=None,
            refresh_lock_id=None,
        )
    )
    result = db.execute(stmt)
    db.flush()
    return (result.rowcount or 0) == 1


def revoke_session_on_refresh_failure(
    db: Session,
    token_hash: str,
    expected_lock_id: Optional[uuid.UUID] = None,
) -> bool:
    """
    Permanently revokes a session when refresh fails fatally (e.g. InvalidGrant).
    """
    now = _now()
    conditions = [
        AuthSession.token_hash == token_hash,
        AuthSession.revoked_at.is_(None),
    ]
    if expected_lock_id is not None:
        conditions.append(AuthSession.refresh_lock_id == expected_lock_id)
    stmt = (
        update(AuthSession)
        .where(*conditions)
        .values(
            revoked_at=now,
            refresh_locked_at=None,
            refresh_lock_id=None,
        )
    )
    result = db.execute(stmt)
    db.flush()
    return (result.rowcount or 0) == 1


def should_refresh_session(db: Session, token: str) -> bool:
    """
    Determines if an active session should rotate its Supabase refresh token.
    Triggers rotation if:
    - Session has an encrypted refresh token
    - Session is not revoked or expired
    - Session is not currently locked
    - Access token has expired or will expire in <= 5 minutes (or access_token_expires_at is None)
    """
    now = _now()
    th = token_hash(token)
    session = db.scalars(
        select(AuthSession).where(AuthSession.token_hash == th)
    ).one_or_none()
    if session is None or session.revoked_at is not None:
        return False
    if not session.encrypted_refresh_token:
        return False
    if session.refresh_locked_at is not None and now - session.refresh_locked_at < timedelta(seconds=30):
        return False
    if now > session.expires_at or now > session.absolute_expires_at:
        return False
    if session.access_token_expires_at is None:
        return True
    return now >= session.access_token_expires_at - timedelta(minutes=5)


async def rotate_session_refresh_token(
    db: Session,
    token: str,
    supabase_auth: Optional[object] = None,
) -> bool:
    """
    Executes the Supabase refresh token rotation pipeline:
    1. Validates active session
    2. Acquires atomic CAS refresh lock with unique fencing UUID
    3. Decrypts stored refresh token server-side (AES-256-GCM + AAD)
    4. Calls Supabase GoTrue refresh endpoint
    5. Encrypts newly returned refresh token (AES-256-GCM + AAD)
    6. Persists rotated token verifying the fencing identifier
    7. Cleans up lock state atomically
    8. Handles failures safely (InvalidGrant -> revoke fail-closed; transient -> release lock)
    Never exposes or logs tokens.
    """
    now = _now()
    th = token_hash(token)
    session = db.scalars(
        select(AuthSession).where(AuthSession.token_hash == th)
    ).one_or_none()
    if session is None or session.revoked_at is not None:
        return False

    if now > session.expires_at or now > session.absolute_expires_at:
        session.revoked_at = now
        db.flush()
        return False

    if not session.encrypted_refresh_token:
        return False

    # 1. Acquire atomic CAS refresh lock
    lock_id = acquire_refresh_lock(db, th)
    if not lock_id:
        logger.info("Could not acquire refresh lock for session %s (in flight or invalid)", th[:8])
        return False

    settings = get_settings()
    effective_auth_id = str(session.auth_user_id or session.candidate_id)

    # 2. Decrypt stored refresh token server-side
    try:
        current_rt, _ = decrypt_refresh_token(
            encrypted_data=session.encrypted_refresh_token,
            token_hash=th,
            auth_user_id=effective_auth_id,
            primary_key=settings.session_key_bytes,
            primary_key_id=settings.session_key_id,
            fallback_key=settings.session_fallback_key_bytes,
            fallback_key_id=settings.session_fallback_key_id,
        )
    except DecryptionError as exc:
        logger.error("Failed to decrypt refresh token for session %s: %s", th[:8], exc)
        revoke_session_on_refresh_failure(db, th, expected_lock_id=lock_id)
        db.flush()
        return False

    # 3. Call Supabase GoTrue refresh endpoint
    if supabase_auth is None:
        from app.services.supabase_auth_service import SupabaseAuthService
        supabase_auth = SupabaseAuthService()

    from app.services.supabase_auth_service import InvalidGrantError
    try:
        token_data = await supabase_auth.refresh_session(current_rt)
    except InvalidGrantError as exc:
        logger.warning("Upstream refresh token invalid/revoked for session %s: %s", th[:8], exc)
        revoke_session_on_refresh_failure(db, th, expected_lock_id=lock_id)
        db.flush()
        return False
    except Exception as exc:
        logger.warning("Transient error during token refresh for session %s: %s", th[:8], exc)
        release_refresh_lock(db, th, expected_lock_id=lock_id)
        db.flush()
        return False

    new_rt = token_data.get("refresh_token")
    if not new_rt:
        logger.warning("Supabase refresh response missing refresh_token for session %s", th[:8])
        release_refresh_lock(db, th, expected_lock_id=lock_id)
        db.flush()
        return False

    expires_in = token_data.get("expires_in", 3600)
    access_token_expires_at = now + timedelta(seconds=expires_in)

    # 4. Encrypt newly returned refresh token
    new_encrypted_rt = encrypt_refresh_token(
        refresh_token=new_rt,
        token_hash=th,
        auth_user_id=effective_auth_id,
        key=settings.session_key_bytes,
        key_id=settings.session_key_id,
    )

    # 5. Persist with fencing identifier
    persisted = persist_rotated_refresh_token(
        db=db,
        token_hash=th,
        expected_lock_id=lock_id,
        new_encrypted_refresh_token=new_encrypted_rt,
        access_token_expires_at=access_token_expires_at,
    )
    if not persisted:
        logger.warning("Fencing check rejected token persistence for session %s", th[:8])
        return False

    return True


def session_candidate(db: Session, token: str) -> Optional[Candidate]:
    """
    Retrieves the authenticated candidate for the given raw session token.
    Enforces liveness, revocation checks, and the 30-day absolute expiration cap.
    Applies sliding window renewal if last_used_at > 1 hour.
    """
    now = _now()
    th = token_hash(token)
    
    session = db.scalars(
        select(AuthSession).where(AuthSession.token_hash == th)
    ).one_or_none()

    if session is None or session.revoked_at is not None:
        return None

    # Check refresh lock state
    if session.refresh_locked_at is not None:
        if now - session.refresh_locked_at < timedelta(seconds=30):
            # Refresh in flight by concurrent request
            return None
        else:
            # Refresh timed out / crashed -> permanently revoke fail-closed
            session.revoked_at = now
            db.flush()
            return None

    # Enforce sliding and absolute expiry
    if now > session.expires_at or now > session.absolute_expires_at:
        session.revoked_at = now
        db.flush()
        return None

    # Sliding expiry renewal (capped by absolute_expires_at)
    if now - session.last_used_at > timedelta(hours=1):
        session.last_used_at = now
        session.expires_at = min(now + SESSION_SLIDING_TTL, session.absolute_expires_at)
        db.flush()

    return db.scalars(
        select(Candidate).where(Candidate.id == session.candidate_id)
    ).one_or_none()


def end_session(db: Session, token: str) -> Optional[AuthSession]:
    """
    Revokes the session server-side.
    Returns the revoked session record for remote logout processing.
    """
    th = token_hash(token)
    session = db.scalars(
        select(AuthSession).where(AuthSession.token_hash == th)
    ).one_or_none()
    
    if session is not None:
        session.revoked_at = _now()
        db.flush()
    return session


def cleanup_expired_sessions(db: Session, max_age_seconds: int = 0) -> int:
    """Deletes expired or revoked sessions across all candidates."""
    cutoff = _now() - timedelta(seconds=max_age_seconds)
    result = db.execute(
        delete(AuthSession).where(
            or_(
                AuthSession.expires_at <= cutoff,
                AuthSession.absolute_expires_at <= cutoff,
                AuthSession.revoked_at <= cutoff - timedelta(days=7),
            )
        )
    )
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# Legacy backwards-compatibility stubs for Phase 1 tests
# ---------------------------------------------------------------------------

def new_candidate(db: Session, email: str, name: str, password: str) -> Candidate:
    """Phase 1 legacy candidate creation for backwards compatibility."""
    candidate = Candidate(
        email=email.strip().lower(),
        name=name.strip(),
        password_hash=hash_password(password),
        created_at=_now()
    )
    db.add(candidate)
    db.flush()
    return candidate


def authenticate(db: Session, email: str, password: str) -> Candidate:
    """Phase 1 legacy scrypt authentication."""
    candidate = db.scalars(
        select(Candidate).where(Candidate.email == email.strip().lower()).with_for_update()
    ).one_or_none()
    if candidate is None or candidate.password_hash is None:
        verify_password(password, DUMMY_HASH)
        raise InvalidCredentials
    now = _now()
    if candidate.locked_until is not None and candidate.locked_until > now:
        raise AccountLocked
    if not verify_password(password, candidate.password_hash):
        candidate.failed_logins += 1
        if candidate.failed_logins >= MAX_FAILED_LOGINS:
            candidate.failed_logins = 0
            candidate.locked_until = now + LOCKOUT
        raise InvalidCredentials
    candidate.failed_logins = 0
    candidate.locked_until = None
    return candidate
