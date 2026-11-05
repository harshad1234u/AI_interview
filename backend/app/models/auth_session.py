import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuthSession(Base):
    """A signed-in browser session (BFF pattern). Holds SHA-256(token) only: the raw token lives in the HttpOnly cookie."""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        Index("ix_auth_sessions_auth_user_id", "auth_user_id"),
        Index("ix_auth_sessions_absolute_expires_at", "absolute_expires_at"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Deleting a candidate signs out every browser (ON DELETE CASCADE).
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    # Supabase external auth identity reference
    auth_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # Server-side AES-256-GCM encrypted refresh token
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Informational timestamp for last issued access token expiration
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Sliding expiration timestamp (default 7 days)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Hard absolute session expiration cap (default 30 days)
    absolute_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("NOW() + INTERVAL '30 days'")
    )
    # Pre-committed unusable state timestamp during token refresh
    refresh_locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Unique CAS fencing identifier for token refresh concurrency control
    refresh_lock_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # Explicit revocation timestamp (logout, refresh failure, admin revocation)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
