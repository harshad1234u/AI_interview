"""Interview report (Phase 9, docs/phase-9-report-plan.md): a deterministic view of stored rows.

Nothing is stored and no model is called (R7, R9). The report is rebuilt from the session's plan
snapshot, questions, answers and evaluations on every read, so the same rows always give the same
report. Scores are shown one criterion of one answer at a time and never combined (R2). Every
sentence is fixed text chosen by code (plan §6); stored text (questions, quotes, concepts,
feedback) is only placed into fields, never interpreted.

Transaction contract (as elsewhere): complete_report() locks the session row, checks, builds and
applies REPORT_READY, all in the caller's transaction. The caller commits.
"""

import logging
import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy.orm import Session

from app.models import AnswerEvaluation, InterviewQuestion, InterviewSession
from app.models.answer_evaluation import Criterion
from app.models.interview_question import AnswerKind, EvaluationStatus, QuestionSource
from app.models.interview_session import FinishReason, InterviewStatus
from app.schemas.interview_plan import InterviewPlan
from app.schemas.report import (
    AnswerCounts,
    AnswerEvidence,
    AnswerObservation,
    AvailableSpeechMetrics,
    CommunicationObservations,
    CompetencyCoverage,
    Coverage,
    CriterionItem,
    EvaluationCounts,
    InterviewReport,
    MissingConcept,
    PracticeItem,
    PractiseCriterion,
    ReportQuestion,
    ReportSummary,
    RetryAvailability,
    SpeechMetrics,
    TopicCoverage,
    TopicToPractise,
    ToPractise,
    Unavailable,
)
from app.services.evaluator import (
    RUBRICS,
    evaluation_view,
    notice_for,
    session_evaluations,
)
from app.services.expressions import session_observations
from app.services.expressions import signals as expression_signals
from app.services.interview_state_machine import SessionEvent, apply_event
from app.services.interviewer import load_history, lock_session, stored_plan

logger = logging.getLogger(__name__)
S, ES, K = InterviewStatus, EvaluationStatus, AnswerKind

NOT_FINISHED = "The report is available after the interview has finished."
NOT_GENERATED = "The report has not been generated yet."
PENDING = "Evaluate every answer before requesting the report: {n} not evaluated yet."

# ---------- fixed wording (plan §6) ----------

FINISH_TEXT = {
    FinishReason.PLAN_COMPLETED: "The interview ended after the last topic that could be asked.",
    FinishReason.TIME_LIMIT: "The interview ended at its time limit.",
    FinishReason.CANDIDATE_ENDED: "You ended the interview early, so this report is partial.",
    FinishReason.NO_ELIGIBLE_TOPIC: "No question could be asked: the profile details the planned "
                                    "topics relied on changed after planning.",
    None: "The reason the interview ended was not recorded.",
}
ANSWER_NOTE = {
    K.ANSWER: "Answered",
    K.SKIP: "Skipped",
    K.DONT_KNOW: "Answered: I don't know",
    K.CLARIFY: "Asked for clarification",
    None: "Not answered: the interview ended first.",
}
NOT_ASKED = "Not asked in this interview."
CONCEPT_PRACTICE = "Practise explaining {concept} with an example."
CRITERION_PRACTICE = {
    Criterion.TECHNICAL_ACCURACY: "Practise explaining technical concepts, algorithms and trade-offs accurately.",
    Criterion.RELEVANCE: "Practise answering the question that was asked before adding detail.",
    Criterion.COMPLETENESS: "Practise covering each part of the question.",
    Criterion.REASONING: "Practise explaining why, not only what.",
    Criterion.COMMUNICATION: "Practise organising your answer into a few clear points.",
    Criterion.STRUCTURE: "Practise describing the situation, what you did and the result, in order.",
    Criterion.SPECIFICITY: "Practise giving one concrete example.",
    Criterion.EVIDENCE: "Practise supporting each statement with a specific example.",
    Criterion.REFLECTION: "Practise saying what you learned.",
    Criterion.OUTCOME: "Practise stating the result.",
}
# The evaluator's own anchors (evaluator_agent prompt), shown with every score.
SCORE_MEANING = {1: "not shown", 2: "slightly", 3: "partly", 4: "mostly", 5: "clearly and fully"}
SCORE_SCALE = {**{str(k): v for k, v in SCORE_MEANING.items()}, "null": "too little to judge"}
SHOWN_CLEARLY, TO_PRACTISE = (4, 5), (1, 2)  # 3 and null are in neither list (R1)
SPEECH_METRICS_UNAVAILABLE = Unavailable(note="Speech metrics are not available: answers were given as text.")
SPEECH_METRICS = SPEECH_METRICS_UNAVAILABLE
# Phase 10: true exactly when there is a topic to practise. Whether its facts are still valid is
# checked when the retry is requested, not here.
RETRY = RetryAvailability(available=True, note="You can retry the topics to practise in a new "
                                                "interview.")
NO_RETRY = RetryAvailability(available=False, note="Nothing to retry.")
LIMITATIONS = (
    "This is practice feedback, not a hiring assessment. Scores are per criterion of a single "
    "answer and are never combined. AI evaluation can be wrong, and technical correctness was "
    "not checked: statements marked 'Needs verification' were not verified. Answers without AI "
    "evaluation show observations only.")


class ReportNotAllowed(Exception):
    """409. The message names the reason; never any content."""


# ---------- pure ----------


def _speech_metrics(questions: Sequence[InterviewQuestion]) -> SpeechMetrics:
    speech_answers = [q for q in questions if q.speech_duration_ms is not None and q.speech_duration_ms > 0]
    if not speech_answers:
        return SPEECH_METRICS_UNAVAILABLE
    total_ms = sum(q.speech_duration_ms for q in speech_answers if q.speech_duration_ms)
    total_secs = round(total_ms / 1000)
    wpms = [q.speech_wpm for q in speech_answers if q.speech_wpm is not None and q.speech_wpm > 0]
    avg_wpm = round(sum(wpms) / len(wpms)) if wpms else None
    wpm_text = f"about {avg_wpm} words per minute" if avg_wpm else "cadence measured"
    note = (f"Measured while speaking across {len(speech_answers)} answer(s): "
            f"{total_secs} seconds total speaking time, {wpm_text}.")
    return AvailableSpeechMetrics(
        available=True,
        note=note,
        total_spoken_seconds=total_secs,
        average_wpm=avg_wpm,
        spoken_answers_count=len(speech_answers),
    )


def build_report(session: InterviewSession, plan: InterviewPlan,
                 questions: Sequence[InterviewQuestion],
                 evaluations: dict[uuid.UUID, AnswerEvaluation]) -> InterviewReport:
    """The report for these stored rows. Pure: no database access, clock or model."""
    answered = [q for q in questions if q.answered_at is not None]
    kinds = [q.answer_kind for q in answered]
    statuses = [_status(q) for q in answered]
    summary = ReportSummary(
        settings=plan.settings, plan_source=plan.source, finish_reason=session.finish_reason,
        finish_reason_text=FINISH_TEXT[session.finish_reason],
        active_seconds=int(session.active_seconds), questions_asked=len(questions),
        answers=AnswerCounts(answer=kinds.count(K.ANSWER), skip=kinds.count(K.SKIP),
                             dont_know=kinds.count(K.DONT_KNOW), clarify=kinds.count(K.CLARIFY),
                             unanswered=len(questions) - len(answered)),
        evaluations=EvaluationCounts(
            evaluated=statuses.count(ES.EVALUATED),
            observations_only=statuses.count(ES.OBSERVATIONS_ONLY),
            not_applicable=statuses.count(ES.NOT_APPLICABLE),
            not_evaluated=statuses.count(ES.NOT_EVALUATED)),
    )
    # Order: the interview's question order (§31 R1).
    rows = [(q, evaluations[q.id]) for q in answered if q.id in evaluations]
    ai_rows = [(q, row) for q, row in rows if row.source == QuestionSource.AI]
    shown, low = _criteria(ai_rows)
    missing = _missing(ai_rows)
    topics_to_practise = _topics_to_practise(plan, low, missing)
    return InterviewReport(
        summary=summary,
        questions=[ReportQuestion(
            question_id=q.id, seq=q.seq, topic_position=q.topic_position, kind=q.kind, text=q.text,
            answer_kind=q.answer_kind, answer_note=ANSWER_NOTE[q.answer_kind],
            evaluation_status=_status(q) if q.answered_at is not None else None)
            for q in questions],
        coverage=_coverage(plan, questions),
        shown_clearly=shown,
        to_practise=ToPractise(criteria=low, missing_concepts=missing),
        answer_evidence=[AnswerEvidence(
            question_id=q.id, seq=q.seq, topic_position=q.topic_position,
            status=q.evaluation_status, evaluation=evaluation_view(plan, q, row),
            notice=notice_for(row.rules_reason))
            for q, row in rows],
        communication_observations=CommunicationObservations(
            answers=[AnswerObservation(question_id=q.id, seq=q.seq, word_count=row.word_count,
                                       concepts_mentioned=row.concepts_mentioned)
                     for q, row in rows],
            speech_metrics=_speech_metrics(questions)),
        topics_to_practise=topics_to_practise,
        recommended_practice=_practice(low, missing),
        retry=RETRY if topics_to_practise else NO_RETRY,
        score_scale=SCORE_SCALE, limitations=LIMITATIONS)


def _status(q: InterviewQuestion) -> EvaluationStatus:
    # E7 decides by the stored kind: a SKIP/DONT_KNOW/CLARIFY not yet requested is still not
    # applicable. Read only; the stored status is not changed.
    return q.evaluation_status if q.answer_kind == K.ANSWER else ES.NOT_APPLICABLE


def _coverage(plan: InterviewPlan, questions: Sequence[InterviewQuestion]) -> Coverage:
    asked = [q.topic_position for q in questions]
    topics = [TopicCoverage(
        topic_position=t.position, topic=t.topic, competency=t.competency,
        question_type=t.question_type, questions_asked=asked.count(t.position),
        note=None if t.position in asked else NOT_ASKED) for t in plan.topics]
    competencies = _unique(t.competency for t in plan.topics)
    return Coverage(topics=topics, competencies=[CompetencyCoverage(
        competency=c, planned_topics=sum(t.competency == c for t in topics),
        asked_topics=sum(t.competency == c and t.questions_asked > 0 for t in topics))
        for c in competencies])


def _criteria(rows: list[tuple[InterviewQuestion, AnswerEvaluation]]
              ) -> tuple[list[CriterionItem], list[PractiseCriterion]]:
    shown: list[CriterionItem] = []
    low: list[PractiseCriterion] = []
    for q, row in rows:
        for c in row.criteria:
            if c.score in SHOWN_CLEARLY:
                shown.append(CriterionItem(
                    question_id=q.id, seq=q.seq, topic_position=q.topic_position,
                    criterion=c.criterion, score=c.score, score_meaning=SCORE_MEANING[c.score],
                    evidence=c.evidence))
            elif c.score in TO_PRACTISE:
                low.append(PractiseCriterion(
                    question_id=q.id, seq=q.seq, topic_position=q.topic_position,
                    criterion=c.criterion, score=c.score, score_meaning=SCORE_MEANING[c.score],
                    practice=CRITERION_PRACTICE[c.criterion], evidence=c.evidence))
    return shown, low


def _missing(rows: list[tuple[InterviewQuestion, AnswerEvaluation]]) -> list[MissingConcept]:
    return [MissingConcept(question_id=q.id, seq=q.seq, topic_position=q.topic_position,
                           concept=concept, practice=CONCEPT_PRACTICE.format(concept=concept))
            for q, row in rows for concept in row.missing_concepts]


def _topics_to_practise(plan: InterviewPlan, low: list[PractiseCriterion],
                        missing: list[MissingConcept]) -> list[TopicToPractise]:
    positions = sorted({i.topic_position for i in [*low, *missing]})
    return [TopicToPractise(
        topic_position=p, topic=plan.topics[p - 1].topic, competency=plan.topics[p - 1].competency,
        criteria=_unique(i.criterion for i in low if i.topic_position == p),
        missing_concepts=_unique(i.concept for i in missing if i.topic_position == p))
        for p in positions]


def _practice(low: list[PractiseCriterion], missing: list[MissingConcept]) -> list[PracticeItem]:
    """One fixed sentence per distinct item, in interview order (seq, then criteria first)."""
    sources: dict[str, list[uuid.UUID]] = {}
    for item in sorted([*low, *missing], key=lambda i: (i.seq, isinstance(i, MissingConcept))):
        ids = sources.setdefault(item.practice, [])
        if item.question_id not in ids:
            ids.append(item.question_id)
    return [PracticeItem(practice=p, question_ids=ids) for p, ids in sources.items()]


def _unique(items: Iterable) -> list:
    return list(dict.fromkeys(items))


# ---------- database ----------


def session_report(db: Session, session: InterviewSession) -> InterviewReport:
    history = load_history(db, session.id)
    report = build_report(session, stored_plan(session), history,
                          session_evaluations(db, session.id))
    # Built apart from every other section: nothing above reads expression observations.
    report.expression_signals = expression_signals(history, session_observations(db, session.id))
    return report


def complete_report(db: Session, session_id: uuid.UUID) -> tuple[InterviewSession, InterviewReport]:
    """REPORT_GENERATION -> COMPLETED, under the session row lock, once every ANSWER has an
    evaluation (R3, R12). COMPLETED returns the same report again with no transition (R8). The
    report is built before the event, in the same transaction: a failure leaves nothing."""
    session = lock_session(db, session_id)
    if session.status not in (S.REPORT_GENERATION, S.COMPLETED):
        raise ReportNotAllowed(NOT_FINISHED)
    report = session_report(db, session)
    if session.status == S.COMPLETED:
        return session, report
    pending = report.summary.evaluations.not_evaluated
    if pending:
        raise ReportNotAllowed(PENDING.format(n=pending))
    apply_event(db, session.id, SessionEvent.REPORT_READY)  # the state machine decides
    logger.info("Interview session %s: report ready (%d questions)", session.id,
                report.summary.questions_asked)
    return session, report
