import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, SmallInteger, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.interview_question import QuestionSource
from app.models.interview_session import RulesReason
from app.models.resume import checked_enum

FEEDBACK_MAX_LENGTH = 600
MIN_SCORE, MAX_SCORE = 1, 5


class Rubric(StrEnum):
    """Chosen by code from the plan topic's question type (plan E3), never by a model."""

    TECHNICAL_PROJECT = "TECHNICAL_PROJECT"
    BEHAVIORAL_HR = "BEHAVIORAL_HR"


class Criterion(StrEnum):
    # TECHNICAL_PROJECT: technical_accuracy, relevance, completeness, reasoning, communication.
    # BEHAVIORAL_HR: relevance, structure, specificity, evidence, reflection, outcome (PRD §41).
    TECHNICAL_ACCURACY = "technical_accuracy"
    RELEVANCE = "relevance"
    COMPLETENESS = "completeness"
    REASONING = "reasoning"
    COMMUNICATION = "communication"
    STRUCTURE = "structure"
    SPECIFICITY = "specificity"
    EVIDENCE = "evidence"
    REFLECTION = "reflection"
    OUTCOME = "outcome"


def _is_array(column: str) -> str:
    return f"jsonb_typeof({column}) = 'array'"


class AnswerEvaluation(Base):
    """The evaluation of one stored answer, made after the interview finished. The answer itself
    is not copied: question_id reaches it, its session (with the plan snapshot) and the candidate.
    Holds no overall score: criteria scores are never combined."""

    __tablename__ = "answer_evaluations"
    __table_args__ = (
        CheckConstraint("(source = 'AI') = (rules_reason IS NULL)", name="rules_reason_matches_source"),
        CheckConstraint("rubric_version >= 1", name="rubric_version_positive"),
        CheckConstraint("attempts BETWEEN 1 AND 2", name="attempts_range"),
        CheckConstraint("word_count >= 0", name="word_count_not_negative"),
        CheckConstraint(f"char_length(feedback) <= {FEEDBACK_MAX_LENGTH}", name="feedback_length"),
        CheckConstraint(" AND ".join(_is_array(c) for c in (
            "concepts_mentioned", "missing_concepts", "claims_to_verify")), name="lists_are_arrays"),
        # Deterministic observations carry no model content.
        CheckConstraint("source = 'AI' OR (feedback IS NULL AND missing_concepts = '[]'::jsonb "
                        "AND claims_to_verify = '[]'::jsonb)", name="rules_has_no_model_content"),
    )
    __mapper_args__ = {"eager_defaults": True}

    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_questions.id", ondelete="CASCADE"), primary_key=True
    )
    rubric: Mapped[Rubric] = mapped_column(checked_enum(Rubric, "rubric"))
    rubric_version: Mapped[int] = mapped_column(SmallInteger)
    source: Mapped[QuestionSource] = mapped_column(checked_enum(QuestionSource, "source"))
    rules_reason: Mapped[RulesReason | None] = mapped_column(checked_enum(RulesReason, "rules_reason"))
    attempts: Mapped[int] = mapped_column(SmallInteger)  # evaluation decisions run: 1, or 2 (E9)
    word_count: Mapped[int]
    concepts_mentioned: Mapped[list[str]] = mapped_column(JSONB)
    missing_concepts: Mapped[list[str]] = mapped_column(JSONB)
    claims_to_verify: Mapped[list[str]] = mapped_column(JSONB)  # exact quotes: needs verification
    feedback: Mapped[str | None] = mapped_column(Text)
    provider_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_calls.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    # In no particular order: the API lists them in rubric order.
    criteria: Mapped[list["AnswerEvaluationCriterion"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, lazy="selectin",
    )


class AnswerEvaluationCriterion(Base):
    """One criterion's score. The database enforces the name, the 1-5 range, one row per
    criterion and evidence behind every score."""

    __tablename__ = "answer_evaluation_criteria"
    __table_args__ = (
        CheckConstraint(f"score BETWEEN {MIN_SCORE} AND {MAX_SCORE}", name="score_range"),
        CheckConstraint(_is_array("evidence"), name="evidence_is_array"),
        CheckConstraint("score IS NULL OR jsonb_array_length(evidence) >= 1",
                        name="score_needs_evidence"),
    )

    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("answer_evaluations.question_id", ondelete="CASCADE"), primary_key=True
    )
    criterion: Mapped[Criterion] = mapped_column(checked_enum(Criterion, "criterion"),
                                                 primary_key=True)
    score: Mapped[int | None] = mapped_column(SmallInteger)  # NULL = insufficient evidence
    evidence: Mapped[list[Any]] = mapped_column(JSONB)  # exact quotes from the answer
