"""add technical_accuracy criterion and speech metrics columns

Revision ID: d7e2f1a9b3c4
Revises: c5d1a7e3b920
Create Date: 2026-11-02 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7e2f1a9b3c4'
down_revision: Union[str, Sequence[str], None] = 'c5d1a7e3b920'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_CRITERIA = ("'relevance', 'completeness', 'reasoning', 'communication', 'structure', "
                "'specificity', 'evidence', 'reflection', 'outcome'")
NEW_CRITERIA = f"'technical_accuracy', {OLD_CRITERIA}"
CRITERION_CHECK = 'ck_answer_evaluation_criteria_criterion'


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_='check')
    op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    # 1. Update criterion check constraint to allow 'technical_accuracy'
    _replace_check('answer_evaluation_criteria', CRITERION_CHECK, f"criterion IN ({NEW_CRITERIA})")

    # 2. Add speech metrics columns to interview_questions
    op.add_column('interview_questions', sa.Column('speech_duration_ms', sa.Integer(), nullable=True))
    op.add_column('interview_questions', sa.Column('speech_wpm', sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        op.f('ck_interview_questions_speech_duration_not_negative'),
        'interview_questions',
        'speech_duration_ms IS NULL OR speech_duration_ms >= 0',
    )
    op.create_check_constraint(
        op.f('ck_interview_questions_speech_wpm_not_negative'),
        'interview_questions',
        'speech_wpm IS NULL OR speech_wpm >= 0',
    )


def downgrade() -> None:
    op.drop_constraint(op.f('ck_interview_questions_speech_wpm_not_negative'), 'interview_questions', type_='check')
    op.drop_constraint(op.f('ck_interview_questions_speech_duration_not_negative'), 'interview_questions', type_='check')
    op.drop_column('interview_questions', 'speech_wpm')
    op.drop_column('interview_questions', 'speech_duration_ms')

    # Delete any evaluations that used 'technical_accuracy' before restoring check constraint
    op.execute("DELETE FROM answer_evaluation_criteria WHERE criterion = 'technical_accuracy'")
    _replace_check('answer_evaluation_criteria', CRITERION_CHECK, f"criterion IN ({OLD_CRITERIA})")
