"""Interview report contract (Phase 9, docs/phase-9-report-plan.md).

Built by code from stored rows only, never by a model. No field combines criterion scores: there
is no overall, total, average, percentage, rank or verdict anywhere in it (R2, R15). Every item
names the question it comes from.
"""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.answer_evaluation import Criterion
from app.models.interview_question import AnswerKind, EvaluationStatus, QuestionKind
from app.models.interview_session import FinishReason, InterviewStatus
from app.schemas.evaluation import EvaluationView
from app.schemas.interview_plan import Competency, PlanSettings, PlanSource, QuestionType


class ReportRequest(BaseModel):
    """Empty on purpose: what the report contains is decided by code."""

    model_config = ConfigDict(extra="forbid")


class AnswerCounts(BaseModel):
    answer: int
    skip: int
    dont_know: int
    clarify: int
    unanswered: int  # open when the interview ended


class EvaluationCounts(BaseModel):
    evaluated: int  # AI rubric evaluation
    observations_only: int
    not_applicable: int  # SKIP, DONT_KNOW, CLARIFY: by answer kind (E7)
    not_evaluated: int  # only in sessions completed before Phase 9


class ReportSummary(BaseModel):
    settings: PlanSettings
    plan_source: PlanSource
    finish_reason: FinishReason | None  # as stored; NULL for sessions finished before Phase 7
    finish_reason_text: str
    active_seconds: int  # active interview time; no finish timestamp is stored
    questions_asked: int
    answers: AnswerCounts
    evaluations: EvaluationCounts


class ReportQuestion(BaseModel):
    question_id: uuid.UUID
    seq: int
    topic_position: int
    kind: QuestionKind
    text: str
    answer_kind: AnswerKind | None  # None: not answered
    answer_note: str
    evaluation_status: EvaluationStatus | None  # NOT_APPLICABLE for every non-ANSWER kind


class TopicCoverage(BaseModel):
    topic_position: int
    topic: str
    competency: Competency
    question_type: QuestionType
    questions_asked: int
    note: str | None  # "Not asked in this interview." when none was; the reason is not stored


class CompetencyCoverage(BaseModel):
    competency: Competency
    planned_topics: int
    asked_topics: int


class Coverage(BaseModel):
    topics: list[TopicCoverage]
    competencies: list[CompetencyCoverage]


class CriterionItem(BaseModel):
    """One criterion of one AI-evaluated answer, as stored."""

    question_id: uuid.UUID
    seq: int
    topic_position: int
    criterion: Criterion
    score: int
    score_meaning: str
    evidence: list[str]  # exact quotes from the answer


class PractiseCriterion(CriterionItem):
    practice: str


class MissingConcept(BaseModel):
    question_id: uuid.UUID
    seq: int
    topic_position: int
    concept: str  # a planned concept, in the plan's spelling
    practice: str


class ToPractise(BaseModel):
    criteria: list[PractiseCriterion]  # scores 1-2
    missing_concepts: list[MissingConcept]


class AnswerEvidence(BaseModel):
    question_id: uuid.UUID
    seq: int
    topic_position: int
    status: EvaluationStatus
    evaluation: EvaluationView
    notice: str | None  # PRD §31 message when AI evaluation was unavailable


class Unavailable(BaseModel):
    available: Literal[False] = False
    note: str


class AvailableSpeechMetrics(BaseModel):
    available: Literal[True] = True
    note: str
    total_spoken_seconds: int
    average_wpm: int | None = None
    spoken_answers_count: int


SpeechMetrics = AvailableSpeechMetrics | Unavailable


class RetryAvailability(BaseModel):
    """Whether POST .../retry has topics to practise (Phase 10). The report never creates one."""

    available: bool
    note: str


class AnswerObservation(BaseModel):
    question_id: uuid.UUID
    seq: int
    word_count: int
    concepts_mentioned: list[str]


class CommunicationObservations(BaseModel):
    answers: list[AnswerObservation]
    speech_metrics: SpeechMetrics


class TopicToPractise(BaseModel):
    topic_position: int
    topic: str
    competency: Competency
    criteria: list[Criterion]
    missing_concepts: list[str]


class PracticeItem(BaseModel):
    practice: str
    question_ids: list[uuid.UUID]  # the answers it comes from, in interview order


class ExpressionSignal(BaseModel):
    question_id: uuid.UUID
    seq: int
    summary: str  # cautious words, never a percentage or a claim about feelings


class ExpressionSignals(BaseModel):
    """Optional, experimental camera observations (see services/expressions.py). Never part of
    any score, feedback, topic to practise or retry decision."""

    available: bool
    note: str
    questions: list[ExpressionSignal]


def _camera_not_enabled() -> ExpressionSignals:
    return ExpressionSignals(available=False, questions=[],
                             note="Not available: the camera was not enabled for this interview.")


class InterviewReport(BaseModel):
    summary: ReportSummary
    questions: list[ReportQuestion]
    coverage: Coverage
    shown_clearly: list[CriterionItem]  # scores 4-5
    to_practise: ToPractise
    answer_evidence: list[AnswerEvidence]
    communication_observations: CommunicationObservations
    topics_to_practise: list[TopicToPractise]
    recommended_practice: list[PracticeItem]
    retry: RetryAvailability
    score_scale: dict[str, str]  # the evaluator's 1-5 anchors, and "null"
    limitations: str
    expression_signals: ExpressionSignals = Field(default_factory=_camera_not_enabled)


class ReportResult(BaseModel):
    session_id: uuid.UUID
    status: InterviewStatus
    report: InterviewReport
