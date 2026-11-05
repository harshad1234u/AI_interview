"""add supabase auth integration and enrich auth sessions

Revision ID: e8f3b2c1a0d9
Revises: d7e2f1a9b3c4
Create Date: 2026-11-04 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e8f3b2c1a0d9'
down_revision: Union[str, Sequence[str], None] = 'd7e2f1a9b3c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Modify candidates table:
    # Drop legacy unique constraint on email to prevent collision with preserved legacy records
    op.drop_constraint('uq_candidates_email', 'candidates', type_='unique')
    
    # Add external identity link column and unique index
    op.add_column('candidates', sa.Column('auth_user_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index('uq_candidates_auth_user_id', 'candidates', ['auth_user_id'], unique=True)
    
    # Create unique index for active authenticated candidates by email
    op.create_index(
        'uq_candidates_email_active',
        'candidates',
        ['email'],
        unique=True,
        postgresql_where=sa.text('auth_user_id IS NOT NULL')
    )

    # 2. Modify auth_sessions table
    op.add_column('auth_sessions', sa.Column('auth_user_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('auth_sessions', sa.Column('encrypted_refresh_token', sa.Text(), nullable=True))
    op.add_column('auth_sessions', sa.Column('access_token_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('auth_sessions', sa.Column('last_used_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.add_column('auth_sessions', sa.Column('absolute_expires_at', sa.DateTime(timezone=True), server_default=sa.text("NOW() + INTERVAL '30 days'"), nullable=False))
    op.add_column('auth_sessions', sa.Column('refresh_locked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('auth_sessions', sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True))
    
    op.create_index('ix_auth_sessions_auth_user_id', 'auth_sessions', ['auth_user_id'])
    op.create_index('ix_auth_sessions_absolute_expires_at', 'auth_sessions', ['absolute_expires_at'])


def downgrade() -> None:
    # WARNING: HIGH RISK, IRREVERSIBLE OPERATION.
    # Downgrading permanently destroys all Supabase Auth linkage and encrypted refresh tokens.
    op.drop_index('ix_auth_sessions_absolute_expires_at', table_name='auth_sessions')
    op.drop_index('ix_auth_sessions_auth_user_id', table_name='auth_sessions')
    op.drop_column('auth_sessions', 'revoked_at')
    op.drop_column('auth_sessions', 'refresh_locked_at')
    op.drop_column('auth_sessions', 'absolute_expires_at')
    op.drop_column('auth_sessions', 'last_used_at')
    op.drop_column('auth_sessions', 'access_token_expires_at')
    op.drop_column('auth_sessions', 'encrypted_refresh_token')
    op.drop_column('auth_sessions', 'auth_user_id')
    
    op.drop_index('uq_candidates_email_active', table_name='candidates')
    op.drop_index('uq_candidates_auth_user_id', table_name='candidates')
    op.drop_column('candidates', 'auth_user_id')
    op.create_unique_constraint('uq_candidates_email', 'candidates', ['email'])
