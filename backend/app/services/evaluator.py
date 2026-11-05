"""Answer evaluation: the code side of Phase 8.

Transaction contract (same pattern as the interviewer):
T1: prepare_evaluation checks eligibility, status and AI consent under the session row lock. If no
    model is needed, it stores the observations-only evaluation and commits. If a model is
    permitted, it commits (so no DB lock or connection is held while waiting for a model) and
    returns a PendingEvaluation.
T2: save_evaluation re-acquires the session row lock, re-checks that the evaluation has not been
    stored concurrently, stores the evaluation and the call records together, flushes, and the
    caller commits.
"""

import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.gateway import CallRecord
from app.models import AnswerEvaluation, AnswerEvaluationCriterion, InterviewQuestion
from app.models import InterviewSession, ProviderCall
from app.models.answer_evaluation import Criterion, Rubric
from app.models.interview_question import AnswerKind, EvaluationStatus, QuestionSource
from app.models.interview_session import RulesReason
from app.schemas.evaluation import (
    ClaimView,
    CriterionView,
    EvaluationProposal,
    EvaluationView,
    Observations,
)
from app.schemas.interview_plan import InterviewPlan, PlannedTopic, QuestionType
from app.services.interview_planner import SENSITIVE, own_words
from app.services.interviewer import (
    FINISHED,
    HIRING,
    MARKUP,
    URL,
    QuestionNotFound,
    find_phrase,
    lock_session,
    normalize,
    stored_plan,
    ungrounded_claim,
)
from app.services.interview_state_machine import SessionNotFound

logger = logging.getLogger(__name__)
ES = EvaluationStatus
C = Criterion

RUBRIC_VERSION = 1
RUBRICS = {
    Rubric.TECHNICAL_PROJECT: (C.TECHNICAL_ACCURACY, C.RELEVANCE, C.COMPLETENESS, C.REASONING, C.COMMUNICATION),
    Rubric.BEHAVIORAL_HR: (C.RELEVANCE, C.STRUCTURE, C.SPECIFICITY, C.EVIDENCE, C.REFLECTION,
                           C.OUTCOME),  # PRD §41
}
RUBRIC_OF = {QuestionType.TECHNICAL: Rubric.TECHNICAL_PROJECT,
             QuestionType.PROJECT: Rubric.TECHNICAL_PROJECT,
             QuestionType.BEHAVIORAL: Rubric.BEHAVIORAL_HR, QuestionType.HR: Rubric.BEHAVIORAL_HR}
EVALUATION_SECONDS = 60.0  # longest model wait per evaluation request (2 proposals at most)
MAX_ATTEMPTS = 2  # the first evaluation, and one re-evaluation of observations only (E9)

NOT_FINISHED = "Answers can be evaluated only after the interview has finished."
NOT_ANSWERED = "This question has no answer to evaluate."
PROVIDERS_DOWN = "AI evaluation is temporarily unavailable. Your interview session has been saved."
QUOTA_USED = ("The daily AI evaluation limit has been reached, so this answer has observations "
              "only. Your interview session has been saved.")
NOTICES = {RulesReason.PROVIDERS_FAILED: PROVIDERS_DOWN, RulesReason.QUOTA_REACHED: QUOTA_USED}


def notice_for(reason: RulesReason | None) -> str | None:
    """The message shown with an evaluation that fell back for a reason the candidate should know."""
    return NOTICES.get(reason) if reason else None

# Feedback rules (plan E6). Matched on the feedback text only, as rule codes, never logged.
PSYCHOLOGICAL = re.compile(r"\b(?:nervous(?:ness)?|anxious|anxiety|confident|confidence|"
                           r"personality|emotions?|emotional|stress(?:ed)?|accent)\b", re.IGNORECASE)
CORRECTNESS = re.compile(r"\b(?:incorrect(?:ly)?|wrong|false|inaccurate|untrue|mistaken|"
                         r"erroneous|correct(?:ly|ness)?)\b", re.IGNORECASE)
# Beyond the interviewer's HIRING words: any comparison with other people (§5 P1, §54).
RANKING = re.compile(r"\b(?:ranks|ranked|other\s+(?:candidates|applicants|people)|"
                     r"compared\s+(?:to|with)\s+others?|top\s+\d+)\b", re.IGNORECASE)
SCORE_TALK = re.compile(r"\d\s*/\s*\d|\bout\s+of\s+\d|%|\bscor(?:e|es|ed|ing)\b|"
                        r"\b(?:rating|grade|percent)\b", re.IGNORECASE)


class EvaluationNotAllowed(Exception):
    """409. The message names the reason; never any content."""


# ---------- pure: rubric, observations, rules ----------


@dataclass(frozen=True)
class EvaluationContext:
    """What one evaluation may see: the stored plan topic, the asked question and its answer."""

    topic: PlannedTopic
    question: str
    answer: str
    mode: str
    difficulty: str

    @property
    def rubric(self) -> Rubric:
        return RUBRIC_OF[self.topic.question_type]  # code chooses, never the model (E3)

    @property
    def criteria(self) -> tuple[Criterion, ...]:
        return RUBRICS[self.rubric]


def context(plan: InterviewPlan, question: InterviewQuestion) -> EvaluationContext:
    topic = plan.topics[question.topic_position - 1]  # the plan snapshot, not today's facts
    return EvaluationContext(topic, question.text, question.answer_text or "",
                             plan.settings.mode, topic.difficulty)


def observations(ctx: EvaluationContext) -> tuple[int, list[str]]:
    """Deterministic (E8): the word count, and the planned concepts that literally occur in the
    answer (Phase 7 §8.1 matching). Says nothing about what is missing or how good it is."""
    mentioned = [c for c in ctx.topic.expected_concepts if find_phrase(ctx.answer, c)]
    return len(ctx.answer.split()), mentioned


def planned(ctx: EvaluationContext, concept: str) -> str | None:
    """The planned concept a model's concept name refers to, in the plan's own spelling."""
    return next((c for c in ctx.topic.expected_concepts if normalize(c) == normalize(concept)),
                None)


def proposal_codes(ctx: EvaluationContext, proposal: EvaluationProposal) -> set[str]:
    """Business and safety rules (plan E3-E6). Rule codes only, never content."""
    codes = set()
    names = [c.criterion for c in proposal.criteria]
    if len(names) != len(set(names)) or set(names) != set(ctx.criteria):
        codes.add("wrong_criteria")
    if any(c.score is not None and not c.evidence for c in proposal.criteria):
        codes.add("score_without_evidence")
    quotes = [q for c in proposal.criteria for q in c.evidence] + proposal.claims_to_verify
    if any(q not in ctx.answer for q in quotes):  # exact substring: no normalization (E5)
        codes.add("quote_not_in_answer")
    for concept in proposal.missing_concepts:
        match = planned(ctx, concept)
        if match is None:
            codes.add("concept_not_planned")
        elif find_phrase(ctx.answer, match):
            codes.add("missing_concept_mentioned")
    feedback = proposal.feedback
    cited = [f.fact for f in ctx.topic.facts]
    for code, pattern in (("hiring_language", HIRING), ("hiring_language", RANKING),
                          ("psychological_claim", PSYCHOLOGICAL),
                          ("correctness_verdict", CORRECTNESS), ("overall_score", SCORE_TALK)):
        if pattern.search(feedback):
            codes.add(code)
    if SENSITIVE.search(own_words(feedback, cited)):
        codes.add("sensitive_attribute")
    if URL.search(feedback) or MARKUP.search(feedback):
        codes.add("url_or_markup")
    if ungrounded_claim(feedback, ctx.topic):
        codes.add("ungrounded_claim")
    return codes


# ---------- persistence ----------


@dataclass
class ProposalOutcome:
    """What the agent produced for one evaluation request. No content is logged from it."""

    proposal: EvaluationProposal | None = None  # validated, or None
    rules_reason: RulesReason | None = None  # why there is no proposal
    records: list[CallRecord] = field(default_factory=list)
    rejected: list[frozenset[str]] = field(default_factory=list)


@dataclass
class EvaluationOutcome:
    session: InterviewSession
    question: InterviewQuestion
    plan: InterviewPlan
    evaluation: AnswerEvaluation | None
    records: list[CallRecord] = field(default_factory=list)


@dataclass
class PendingEvaluation:
    """An evaluation waiting for the model. Holds no connection or lock."""

    session_id: uuid.UUID
    question_id: uuid.UUID
    context: EvaluationContext
    attempt: int  # 1, or 2 for the one re-evaluation (E9)
    deadline: float  # time.monotonic()

    def codes(self, proposal: EvaluationProposal) -> set[str]:
        return proposal_codes(self.context, proposal)


def _load(db: Session, session: InterviewSession, question_id: uuid.UUID
          ) -> tuple[InterviewQuestion, AnswerEvaluation | None]:
    question = db.scalar(
        select(InterviewQuestion)
        .where(InterviewQuestion.id == question_id, InterviewQuestion.session_id == session.id)
        .execution_options(populate_existing=True)
    )
    if question is None:
        raise QuestionNotFound(question_id)
    evaluation = db.scalar(select(AnswerEvaluation)
                           .where(AnswerEvaluation.question_id == question.id)
                           .execution_options(populate_existing=True))
    return question, evaluation


def session_evaluations(db: Session, session_id: uuid.UUID) -> dict[uuid.UUID, AnswerEvaluation]:
    """Stored evaluations of a session's answers, by question id."""
    return {row.question_id: row for row in db.scalars(
        select(AnswerEvaluation).join(InterviewQuestion)
        .where(InterviewQuestion.session_id == session_id))}


def prepare_evaluation(db: Session, session_id: uuid.UUID, question_id: uuid.UUID, *,
                       model_available: bool) -> EvaluationOutcome | PendingEvaluation:
    """T1: lock, check, decide. Completes the evaluation itself when no model is needed (the
    caller commits); otherwise commits and returns a PendingEvaluation."""
    session = lock_session(db, session_id)  # the session row only: nothing on it changes
    question, evaluation = _load(db, session, question_id)
    if session.status not in FINISHED:
        raise EvaluationNotAllowed(NOT_FINISHED)
    if question.answered_at is None:
        raise EvaluationNotAllowed(NOT_ANSWERED)
    plan = stored_plan(session)
    done = EvaluationOutcome(session, question, plan, evaluation)
    status = question.evaluation_status
    if status == ES.NOT_EVALUATED and question.answer_kind != AnswerKind.ANSWER:
        question.evaluation_status = ES.NOT_APPLICABLE  # by the stored kind, never the text (E7)
        db.flush()
        logger.info("Answer %s of session %s: %s", question.id, session.id, ES.NOT_APPLICABLE)
        return done
    model = session.ai_consent and model_available
    retry = (status == ES.OBSERVATIONS_ONLY and evaluation.attempts < MAX_ATTEMPTS and model)
    if status != ES.NOT_EVALUATED and not retry:
        return done  # final, or no permitted re-evaluation: the stored result, no call
    ctx = context(plan, question)
    if model:
        pending = PendingEvaluation(session.id, question.id, ctx, 2 if retry else 1,
                                    time.monotonic() + EVALUATION_SECONDS)
        db.commit()  # releases the lock and the connection: none is held while a model runs
        return pending
    reason = RulesReason.NOT_CONFIGURED if session.ai_consent else RulesReason.NOT_REQUESTED
    done.evaluation = _store(db, question, ctx, 1, None, reason)
    return done


def save_evaluation(db: Session, pending: PendingEvaluation,
                    outcome: ProposalOutcome) -> EvaluationOutcome:
    """T2: lock again, record the calls whatever happens, re-check, write. The caller commits."""
    records = outcome.records
    try:
        session = lock_session(db, pending.session_id)
        question, evaluation = _load(db, session, pending.question_id)
    except (SessionNotFound, QuestionNotFound):
        db.rollback()
        if records:  # the calls happened (and cost): keep their metadata on its own
            db.add_all(ProviderCall.from_record(r) for r in records)
            db.commit()
        raise
    calls = [ProviderCall.from_record(r) for r in records]
    db.add_all(calls)
    db.flush()
    plan = stored_plan(session)
    done = EvaluationOutcome(session, question, plan, evaluation, records)
    if (evaluation.attempts if evaluation else 0) >= pending.attempt:
        return done  # a concurrent request stored this attempt first: keep its result
    proposal, reason, call_id = outcome.proposal, outcome.rules_reason, None
    if not session.ai_consent:  # withdrawn during the call: the output is discarded (D2)
        proposal, reason = None, RulesReason.CONSENT_WITHDRAWN
    elif proposal is not None:
        call_id = next(c.id for c in reversed(calls) if c.succeeded)
    done.evaluation = _store(db, question, pending.context, pending.attempt, proposal,
                             reason or RulesReason.PROVIDERS_FAILED, call_id, evaluation)
    return done


def _store(db: Session, question: InterviewQuestion, ctx: EvaluationContext, attempt: int,
           proposal: EvaluationProposal | None, reason: RulesReason | None,
           call_id: uuid.UUID | None = None,
           existing: AnswerEvaluation | None = None) -> AnswerEvaluation:
    """Write the evaluation and the answer's status together. The answer itself is not touched."""
    word_count, mentioned = observations(ctx)
    row = existing or AnswerEvaluation(question_id=question.id)
    row.rubric, row.rubric_version, row.attempts = ctx.rubric, RUBRIC_VERSION, attempt
    row.word_count, row.concepts_mentioned = word_count, mentioned
    row.provider_call_id = call_id
    if proposal is None:
        row.source, row.rules_reason = QuestionSource.RULES, reason
        row.missing_concepts, row.claims_to_verify, row.feedback = [], [], None
    else:
        row.source, row.rules_reason = QuestionSource.AI, None
        row.missing_concepts = list(dict.fromkeys(planned(ctx, c) for c in
                                                  proposal.missing_concepts))
        row.claims_to_verify = list(dict.fromkeys(proposal.claims_to_verify))
        row.feedback = proposal.feedback
        row.criteria = [AnswerEvaluationCriterion(criterion=c.criterion, score=c.score,
                                                  evidence=list(dict.fromkeys(c.evidence)))
                        for c in proposal.criteria]
    if existing is not None:
        row.updated_at = func.clock_timestamp()
    else:
        db.add(row)
    question.evaluation_status = ES.OBSERVATIONS_ONLY if proposal is None else ES.EVALUATED
    db.flush()
    db.refresh(row)  # server timestamps, criteria
    logger.info("Answer %s evaluated: %s, attempt %d%s", question.id, question.evaluation_status,
                attempt, f", {row.rules_reason}" if row.rules_reason else "")
    return row


def evaluation_view(plan: InterviewPlan, q: InterviewQuestion,
                    row: AnswerEvaluation | None) -> EvaluationView | None:
    """The API view of a stored evaluation, criteria in rubric order (also used by the report)."""
    if row is None:
        return None
    order = RUBRICS[row.rubric]
    return EvaluationView(
        rubric=row.rubric, rubric_version=row.rubric_version, source=row.source,
        rules_reason=row.rules_reason,
        criteria=[CriterionView(criterion=c.criterion, score=c.score, evidence=c.evidence)
                  for c in sorted(row.criteria, key=lambda c: order.index(c.criterion))],
        missing_concepts=row.missing_concepts,
        claims_to_verify=[ClaimView(quote=quote) for quote in row.claims_to_verify],
        feedback=row.feedback,
        observations=Observations(
            word_count=row.word_count,
            planned_concepts=plan.topics[q.topic_position - 1].expected_concepts,
            concepts_mentioned=row.concepts_mentioned),
        created_at=row.created_at, updated_at=row.updated_at)
