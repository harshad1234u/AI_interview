"""Answer evaluation contracts (Phase 8): the only shape a model may return, and the API views.

The model output carries evaluation content only. There is no field for an overall score, a
recommended follow-up, an action, a status or a finish reason (plan C-E1..C-E4), and unknown
fields are rejected.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models.answer_evaluation import (
    FEEDBACK_MAX_LENGTH,
    MAX_SCORE,
    MIN_SCORE,
    Criterion,
    Rubric,
)
from app.models.interview_question import EvaluationStatus, QuestionSource
from app.models.interview_session import InterviewStatus, RulesReason
from app.schemas.interview_plan import ConceptText, ModelCallSummary

# A quote must be an exact substring of the answer (checked by code). Only surrounding whitespace
# is removed here; nothing else about it is changed.
Quote = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=300)]
Score = Annotated[int, Field(ge=MIN_SCORE, le=MAX_SCORE, strict=True)]

# ---------- model output ----------


class CriterionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion: Criterion
    score: Score | None  # null = the answer gives too little to judge
    evidence: list[Quote] = Field(max_length=3)


class EvaluationProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criteria: list[CriterionProposal] = Field(min_length=1, max_length=len(Criterion))
    missing_concepts: list[ConceptText] = Field(max_length=8)
    claims_to_verify: list[Quote] = Field(max_length=5)
    feedback: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                               max_length=FEEDBACK_MAX_LENGTH)]


# ---------- requests ----------


class EvaluationRequest(BaseModel):
    """Empty on purpose: what is evaluated, and how, is decided by code."""

    model_config = ConfigDict(extra="forbid")


# ---------- responses ----------


class CriterionView(BaseModel):
    criterion: Criterion
    score: int | None  # 1-5; null = insufficient evidence. Never combined with other criteria.
    evidence: list[str]  # exact quotes from the answer


class ClaimView(BaseModel):
    quote: str  # exact quote from the answer
    status: Literal["NEEDS_VERIFICATION"] = "NEEDS_VERIFICATION"  # never "true" or "false"


class Observations(BaseModel):
    """Deterministic, computed by code: no model involved."""

    word_count: int
    planned_concepts: list[str]
    concepts_mentioned: list[str]  # planned concepts that literally occur in the answer


class EvaluationView(BaseModel):
    rubric: Rubric
    rubric_version: int
    source: QuestionSource  # AI rubric evaluation, or RULES (observations only)
    rules_reason: RulesReason | None
    criteria: list[CriterionView]  # empty for RULES
    missing_concepts: list[str]  # planned concepts only
    claims_to_verify: list[ClaimView]
    feedback: str | None  # untrusted model text: render as plain text
    observations: Observations
    created_at: datetime
    updated_at: datetime


class EvaluationResult(BaseModel):
    session_id: uuid.UUID
    question_id: uuid.UUID
    status: EvaluationStatus
    evaluation: EvaluationView | None  # None for NOT_APPLICABLE
    notice: str | None  # PRD §31 message when AI evaluation was unavailable
    model_calls: list[ModelCallSummary]  # empty when no model was asked


class BatchEvaluationResult(BaseModel):
    session_id: uuid.UUID
    total_evaluated: int
    evaluations: list[EvaluationResult]
    notices: list[str]


class AnswerEvaluationItem(BaseModel):
    question_id: uuid.UUID
    seq: int
    status: EvaluationStatus
    evaluation: EvaluationView | None


class EvaluationList(BaseModel):
    session_id: uuid.UUID
    status: InterviewStatus
    evaluations: list[AnswerEvaluationItem]  # answered questions, in interview order
