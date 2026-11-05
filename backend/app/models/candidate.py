import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, SmallInteger, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

NAME_MAX_LENGTH = 200
EMAIL_MAX_LENGTH = 254  # RFC 5321 path limit
EMAIL_ACTIVE_UNIQUE_INDEX = "uq_candidates_email_active"
EMAIL_UNIQUE_CONSTRAINT = EMAIL_ACTIVE_UNIQUE_INDEX
AUTH_USER_ID_UNIQUE_INDEX = "uq_candidates_auth_user_id"


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("btrim(name) <> ''", name="name_not_blank"),
        CheckConstraint("failed_logins >= 0", name="failed_logins_not_negative"),
        Index(AUTH_USER_ID_UNIQUE_INDEX, "auth_user_id", unique=True),
        Index(
            EMAIL_ACTIVE_UNIQUE_INDEX,
            "email",
            unique=True,
            postgresql_where=text("auth_user_id IS NOT NULL"),
        ),
    )
    # Fetch server-generated id/created_at on INSERT via RETURNING.
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    # External Supabase Auth user ID (UUID)
    auth_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    email: Mapped[str] = mapped_column(String(EMAIL_MAX_LENGTH))
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Phase 1 legacy columns - retained nullable for backwards compatibility
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Wrong passwords since the last success or lock; the lock itself is locked_until.
    failed_logins: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
