import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.interview_session import RulesReason
from app.models.resume import checked_enum

ANSWER_MAX_LENGTH = 10_000  # Unicode code points, after stripping (D5)
# Room for the longest deterministic rephrase: a 54-character frame + a 250-character topic title
# + a 300-character plan question = 604 (tested). Approved 2026-10-02 in place of the spec's 400;
# the same limit applies to the column, the API schema and the tests.
QUESTION_MAX_LENGTH = 640
ANCHOR_MAX_LENGTH = 100
MAX_FOLLOW_UPS = 2  # per question (D1)
MAX_REPHRASES = 2  # per question chain (D3)
ONE_OPEN_QUESTION_INDEX = "uq_interview_questions_one_open_per_session"


class QuestionKind(StrEnum):
    TOPIC = "TOPIC"  # a planned topic's question, verbatim
    FOLLOW_UP = "FOLLOW_UP"
    REPHRASE = "REPHRASE"


class QuestionSource(StrEnum):
    AI = "AI"  # model wording that passed validation
    RULES = "RULES"  # deterministic wording


class AnswerKind(StrEnum):
    ANSWER = "ANSWER"
    CLARIFY = "CLARIFY"  # the candidate asks for the question to be put another way
    DONT_KNOW = "DONT_KNOW"
    SKIP = "SKIP"


class EvaluationStatus(StrEnum):
    # Stored, and not evaluated: no score, rubric result, evidence or missing-concept analysis
    # exists for it. Every answer starts here; only the evaluator (Phase 8) moves it on, and only
    # after the interview finished.
    NOT_EVALUATED = "NOT_EVALUATED"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # SKIP, DONT_KNOW or CLARIFY: nothing to evaluate
    OBSERVATIONS_ONLY = "OBSERVATIONS_ONLY"  # deterministic observations, no scores
    EVALUATED = "EVALUATED"  # validated AI rubric evaluation


class InterviewQuestion(Base):
    """One asked question and, once given, its answer. Answers live here and nowhere else."""

    __tablename__ = "interview_questions"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_interview_questions_session_id_seq"),
        Index(ONE_OPEN_QUESTION_INDEX, "session_id", unique=True,
              postgresql_where=text("answered_at IS NULL")),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint("topic_position >= 1", name="topic_position_positive"),
        CheckConstraint(f"followup_index BETWEEN 0 AND {MAX_FOLLOW_UPS}", name="followup_index_range"),
        CheckConstraint("kind <> 'TOPIC' OR followup_index = 0", name="topic_has_no_followup_index"),
        CheckConstraint("kind <> 'FOLLOW_UP' OR followup_index >= 1",
                        name="follow_up_has_followup_index"),
        CheckConstraint(f"rephrase_index BETWEEN 0 AND {MAX_REPHRASES}", name="rephrase_index_range"),
        CheckConstraint("(kind = 'REPHRASE') = (rephrase_index >= 1)", name="rephrase_index_matches_kind"),
        CheckConstraint("btrim(text) <> ''", name="text_not_blank"),
        CheckConstraint("(source = 'AI') = (rules_reason IS NULL)", name="rules_reason_matches_source"),
        CheckConstraint("(anchor IS NOT NULL) = (kind = 'FOLLOW_UP' AND source = 'AI')",
                        name="anchor_only_on_ai_follow_ups"),
        CheckConstraint("clock_at_ask >= 0", name="clock_at_ask_not_negative"),
        CheckConstraint(f"char_length(answer_text) <= {ANSWER_MAX_LENGTH}", name="answer_text_length"),
        CheckConstraint(
            "(answer_kind IS NULL) = (answered_at IS NULL) "
            "AND (answer_kind IS NULL) = (evaluation_status IS NULL)",
            name="answer_fields_together",
        ),
        CheckConstraint("answer_text IS NULL OR answer_kind IS NOT NULL", name="answer_text_needs_kind"),
        CheckConstraint("speech_duration_ms IS NULL OR speech_duration_ms >= 0",
                        name="speech_duration_not_negative"),
        CheckConstraint("speech_wpm IS NULL OR speech_wpm >= 0",
                        name="speech_wpm_not_negative"),
    )
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE")
    )
    seq: Mapped[int]
    topic_position: Mapped[int]
    kind: Mapped[QuestionKind] = mapped_column(checked_enum(QuestionKind, "kind"))
    followup_index: Mapped[int] = mapped_column(SmallInteger)
    rephrase_index: Mapped[int] = mapped_column(SmallInteger)
    text: Mapped[str] = mapped_column(String(QUESTION_MAX_LENGTH))
    source: Mapped[QuestionSource] = mapped_column(checked_enum(QuestionSource, "source"))
    rules_reason: Mapped[RulesReason | None] = mapped_column(checked_enum(RulesReason, "rules_reason"))
    # The answer phrase an AI follow-up is about. Internal: never returned by the API.
    anchor: Mapped[str | None] = mapped_column(String(ANCHOR_MAX_LENGTH))
    # The model call whose wording was used; NULL for RULES text (or if call metadata is purged).
    provider_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_calls.id", ondelete="SET NULL")
    )
    asked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    # Active interview seconds elapsed when the question was asked (topic time allocation).
    clock_at_ask: Mapped[float] = mapped_column(Double)
    answer_kind: Mapped[AnswerKind | None] = mapped_column(checked_enum(AnswerKind, "answer_kind"))
    # Candidate-written, untrusted. Never logged, never in provider_calls or error messages.
    answer_text: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evaluation_status: Mapped[EvaluationStatus | None] = mapped_column(
        checked_enum(EvaluationStatus, "evaluation_status")
    )
    speech_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    speech_wpm: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
