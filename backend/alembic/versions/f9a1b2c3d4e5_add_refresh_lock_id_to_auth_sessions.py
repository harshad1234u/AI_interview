"""add refresh_lock_id to auth_sessions for CAS fencing

Revision ID: f9a1b2c3d4e5
Revises: e8f3b2c1a0d9
Create Date: 2026-11-04 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f9a1b2c3d4e5'
down_revision: Union[str, Sequence[str], None] = 'e8f3b2c1a0d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'auth_sessions',
        sa.Column('refresh_lock_id', postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('auth_sessions', 'refresh_lock_id')
