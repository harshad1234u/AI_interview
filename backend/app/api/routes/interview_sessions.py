import asyncio
"""HTTP layer for interview sessions. Lifecycle decisions live in services/interview_state_machine."""

import logging
import uuid
from typing import Annotated

import anyio.from_thread
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai.evaluator_agent import propose_evaluation
from app.ai.gateway import CallRecord, ModelGateway
from app.ai.interviewer_agent import WordingRequest, propose_wording
from app.ai.planner_agent import PlannerOutcome, propose_plan
from app.ai.providers import get_model_gateway
from app.api.deps import current_candidate
from app.api.routes.candidates import get_candidate
from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.models import AnswerEvaluation, Candidate, InterviewQuestion, InterviewSession
from app.models.interview_question import ANSWER_MAX_LENGTH, AnswerKind
from app.models.interview_session import FinishReason, InterviewStatus
from app.schemas.interview_plan import (
    InterviewPlan,
    InterviewPlanResult,
    InterviewPlanView,
    ModelCallSummary,
    PlanSettings,
    RulesReason,
)
from app.schemas.evaluation import (
    AnswerEvaluationItem,
    BatchEvaluationResult,
    EvaluationList,
    EvaluationRequest,
    EvaluationResult,
)
from app.schemas.interview_session import (
    InterviewSessionCreate,
    InterviewSessionResponse,
    RetryRequest,
    SessionActionRequest,
)
from app.schemas.interview_turn import (
    AnswerRequest,
    AnswerResult,
    AnswerView,
    ConsentHistory,
    ConsentRequest,
    ConsentState,
    Evaluation,
    ExpressionObservationRequest,
    NextQuestionRequest,
    Progress,
    QuestionHistory,
    QuestionView,
    TurnResult,
)
from app.schemas.report import ReportRequest, ReportResult
from app.services.interview_planner import (
    PlanningNotAllowed,
    PlanRejected,
    PlanStale,
    ProjectRequired,
    fallback_plan,
    prepare_planning,
    save_plan,
)
from app.services.evaluator import (
    EvaluationNotAllowed,
    PendingEvaluation,
    ProposalOutcome,
    evaluation_view,
    notice_for,
    prepare_evaluation,
    save_evaluation,
    session_evaluations,
)
from app.services.report import NOT_GENERATED, ReportNotAllowed, complete_report, session_report
from app.services.expressions import ObservationNotAllowed, save_observation
from app.services.limits import AI_EVALUATION, AI_PLANNING, AI_WORDING, admit_ai
from app.services.retry import RetryNotAllowed, create_retry
from app.services.interviewer import (
    ConsentNotAllowed,
    LateAnswer,
    PendingTurn,
    QuestionNotFound,
    TurnNotAllowed,
    TurnOutcome,
    WordingOutcome,
    consent_history,
    elapsed_seconds,
    load_history,
    lock_and_enforce_deadline,
    prepare_turn,
    save_turn,
    set_consent,
    stored_plan,
    submit_answer,
    time_remaining_display,
)
from app.services.interview_state_machine import (
    ActiveSessionExists,
    InvalidTransition,
    ProfileNotReviewed,
    SessionEvent,
    SessionNotFound,
    apply_event,
    create_session,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["interview sessions"])

NOT_FOUND = "Interview session not found"


def get_interview_session(
    session_id: uuid.UUID,
    current: Candidate = Depends(current_candidate),
    db: Session = Depends(get_db),
) -> InterviewSession:
    """Single access point for a session by id. Another candidate's session is a 404, exactly
    like one that does not exist (A4)."""
    session = db.get(InterviewSession, session_id)
    if session is None or session.candidate_id != current.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return session


def owned_session_id(
    session_id: uuid.UUID,
    current: Candidate = Depends(current_candidate),
    db: Session = Depends(get_db),
) -> uuid.UUID:
    """The same check for routes whose service locks and loads the session itself. Reads one
    column, so the service's own SELECT ... FOR UPDATE still loads fresh state. A session never
    changes owner, so checking before the lock is safe."""
    owner = db.scalar(
        select(InterviewSession.candidate_id).where(InterviewSession.id == session_id))
    if owner != current.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return session_id


OwnedSessionId = Annotated[uuid.UUID, Depends(owned_session_id)]


def _call_summaries(records: list[CallRecord]) -> list[ModelCallSummary]:
    return [ModelCallSummary(
        request_id=r.request_id, requested_provider=r.requested_provider, provider=r.provider,
        model=r.model, fallback_used=r.fallback_used, attempts=r.attempts,
        error_type=r.error_type, latency_ms=r.latency_ms, input_tokens=r.input_tokens,
        output_tokens=r.output_tokens, total_tokens=r.total_tokens,
    ) for r in records]


def _commit(db: Session) -> None:
    try:
        db.commit()
    except OperationalError:
        db.rollback()
        raise  # the app turns this into a 503
    except SQLAlchemyError as exc:
        db.rollback()
        diag = getattr(getattr(exc, "orig", None), "diag", None)
        logger.error("Interview session write failed (%s, constraint %s)",
                     type(exc).__name__, getattr(diag, "constraint_name", None))
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not save interview session"
        ) from None


@router.post(
    "/interview-sessions",
    response_model=InterviewSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_interview_session(
    body: InterviewSessionCreate,
    candidate: Candidate = Depends(current_candidate),
    db: Session = Depends(get_db),
) -> InterviewSession:
    if body.abandon_active:
        active_sessions = list(db.scalars(
            select(InterviewSession).where(
                InterviewSession.candidate_id == candidate.id,
                InterviewSession.status != InterviewStatus.COMPLETED,
            )
        ))
        for s in active_sessions:
            s.status = InterviewStatus.COMPLETED
            s.finish_reason = FinishReason.CANDIDATE_ENDED
        if active_sessions:
            db.flush()
    try:
        session = create_session(db, candidate.id)
    except ActiveSessionExists as exc:
        db.rollback()
        raise _active_session_conflict(exc) from None
    _commit(db)
    return session


@router.post(
    "/interview-sessions/{session_id}/abandon",
    response_model=InterviewSessionResponse,
)
def abandon_interview_session(
    session_id: OwnedSessionId,
    db: Session = Depends(get_db),
) -> InterviewSession:
    """Explicitly abandon an active session, marking it COMPLETED with CANDIDATE_ENDED (AUDIT-008)."""
    session = db.get(InterviewSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    if session.status != InterviewStatus.COMPLETED:
        session.status = InterviewStatus.COMPLETED
        session.finish_reason = FinishReason.CANDIDATE_ENDED
        db.flush()
    _commit(db)
    return session


def _active_session_conflict(exc: ActiveSessionExists) -> HTTPException:
    detail = "This candidate already has an unfinished interview session"
    return HTTPException(
        status.HTTP_409_CONFLICT,
        f"{detail}: {exc.session_id}. Resume it, or delete it to start again."
        if exc.session_id else f"{detail}. Please try again.",
    )


@router.get("/interview-sessions/{session_id}", response_model=InterviewSessionResponse)
def read_interview_session(
    session: InterviewSession = Depends(get_interview_session),
) -> InterviewSession:
    return session


@router.get(
    "/candidates/{candidate_id}/interview-sessions",
    response_model=list[InterviewSessionResponse],
)
def list_candidate_interview_sessions(
    candidate: Candidate = Depends(get_candidate), db: Session = Depends(get_db)
) -> list[InterviewSession]:
    return list(db.scalars(
        select(InterviewSession)
        .where(InterviewSession.candidate_id == candidate.id)
        .order_by(InterviewSession.created_at.desc(), InterviewSession.id)
    ))


@router.post(
    "/interview-sessions/{session_id}/actions", response_model=InterviewSessionResponse
)
def act_on_interview_session(
    body: SessionActionRequest,
    session_id: OwnedSessionId,
    db: Session = Depends(get_db),
) -> InterviewSession:
    try:
        # Every interview write first applies the time limit (Phase 7, §6.3). The closure is
        # committed on its own, then the action is checked against the closed session.
        if lock_and_enforce_deadline(db, session_id):
            _commit(db)
        session = apply_event(db, session_id, SessionEvent(body.action))
    except SessionNotFound:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None
    except (InvalidTransition, ProfileNotReviewed) as exc:
        db.rollback()  # nothing was written; this releases the row lock
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    _commit(db)
    return session


@router.delete("/interview-sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_interview_session(
    session: InterviewSession = Depends(get_interview_session), db: Session = Depends(get_db)
) -> None:
    # PRD §59/§61: discarding a session deletes it. Allowed in any state.
    db.delete(session)
    _commit(db)


# ---------- planning (Phase 6) ----------


@router.post("/interview-sessions/{session_id}/plan", response_model=InterviewPlanResult)
def plan_interview_session(
    session_id: OwnedSessionId,
    settings: PlanSettings,
    db: Session = Depends(get_db),
    gateway: ModelGateway | None = Depends(get_model_gateway),
    app_settings: Settings = Depends(get_settings),
) -> InterviewPlanResult:
    """INTERVIEW_PLANNING -> READY with a validated plan. Never fails because AI failed: the
    deterministic plan (labelled RULES, with the reason) is used instead (PRD §30, §31).
    A model is asked only with the session's AI consent (PUT .../ai-consent)."""
    try:
        inputs = prepare_planning(db, session_id, settings)  # ends its read transaction
    except SessionNotFound:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None
    except (PlanningNotAllowed, ProjectRequired) as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    # No DB connection or lock is held from here until save_plan.
    if not inputs.ai_consent:
        outcome = PlannerOutcome(None, RulesReason.NOT_REQUESTED)
    elif gateway is None:
        outcome = PlannerOutcome(None, RulesReason.NOT_CONFIGURED)
    elif not admit_ai(db, AI_PLANNING, inputs.candidate_id, app_settings):
        outcome = PlannerOutcome(None, RulesReason.QUOTA_REACHED)
    else:
        # Sync route (sync DB session) calling the async gateway on the event loop.
        outcome = anyio.from_thread.run(propose_plan, gateway, inputs.settings, inputs.facts)
    try:
        plan = outcome.plan or fallback_plan(inputs.settings, inputs.facts, outcome.rules_reason)
    except PlanRejected as exc:
        # Not expected: every mode, duration and project-name case is covered by tests. Kept so
        # that a future catalog change fails loudly instead of storing an invalid plan.
        logger.error("Fallback plan for session %s failed validation: %s", session_id, exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            "Could not build an interview plan") from None

    try:
        session, plan = save_plan(db, inputs, plan, outcome.records)
    except SessionNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None
    except (PlanningNotAllowed, PlanStale) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    _commit(db)
    return InterviewPlanResult(
        session_id=session.id,
        status=session.status,
        plan=plan,
        model_calls=_call_summaries(outcome.records),
    )


@router.get("/interview-sessions/{session_id}/plan", response_model=InterviewPlanView)
def read_interview_plan(
    session: InterviewSession = Depends(get_interview_session),
) -> InterviewPlanView:
    if session.plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This interview session has no plan yet")
    return InterviewPlanView(session_id=session.id, status=session.status,
                             plan=InterviewPlan.model_validate(session.plan))


# ---------- AI consent (Phase 7, D2) ----------


@router.put("/interview-sessions/{session_id}/ai-consent", response_model=ConsentState)
def put_ai_consent(
    body: ConsentRequest,
    session_id: OwnedSessionId,
    db: Session = Depends(get_db),
) -> ConsentState:
    """Grant or withdraw consent to send this session's data to an AI provider. Governs the
    planner and the interviewer. Data already sent cannot be recalled by withdrawing."""
    try:
        session = set_consent(db, session_id, body.granted)
    except SessionNotFound:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None
    except ConsentNotAllowed as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    history = consent_history(db, session.id)
    _commit(db)
    return ConsentState(session_id=session.id, granted=session.ai_consent,
                        changed_at=history[-1].changed_at if history else None)


@router.get("/interview-sessions/{session_id}/ai-consent", response_model=ConsentHistory)
def get_ai_consent(
    session: InterviewSession = Depends(get_interview_session), db: Session = Depends(get_db)
) -> ConsentHistory:
    return ConsentHistory(session_id=session.id, granted=session.ai_consent,
                          history=consent_history(db, session.id))


# ---------- interview turns (Phase 7) ----------

ANSWER_TOO_LONG = f"Answer is too long: the limit is {ANSWER_MAX_LENGTH:,} characters."


def _question_view(plan: InterviewPlan, q: InterviewQuestion,
                   evaluation: AnswerEvaluation | None = None) -> QuestionView:
    topic = plan.topics[q.topic_position - 1]
    answer = None if q.answered_at is None else AnswerView(
        kind=q.answer_kind, text=q.answer_text, answered_at=q.answered_at,
        evaluation=Evaluation(status=q.evaluation_status,
                              detail=evaluation_view(plan, q, evaluation)))
    return QuestionView(
        id=q.id, seq=q.seq, topic_position=q.topic_position, topic=topic.topic,
        competency=topic.competency, question_type=topic.question_type,
        difficulty=topic.difficulty, basis=topic.basis, kind=q.kind,
        followup_index=q.followup_index, rephrase_index=q.rephrase_index, text=q.text,
        source=q.source, rules_reason=q.rules_reason, asked_at=q.asked_at, answer=answer,
        speech_duration_ms=q.speech_duration_ms, speech_wpm=q.speech_wpm)


def _progress(plan: InterviewPlan, elapsed: float) -> Progress:
    limit = plan.settings.duration_minutes * 60
    return Progress(topics_total=len(plan.topics),
                    time_remaining_seconds=time_remaining_display(limit - elapsed),
                    deadline_reached=elapsed >= limit)


def _turn_result(turn: TurnOutcome) -> TurnResult:
    session = turn.session
    return TurnResult(
        session_id=session.id, status=session.status, finish_reason=session.finish_reason,
        ai_consent=session.ai_consent,
        question=_question_view(turn.plan, turn.question) if turn.question else None,
        progress=_progress(turn.plan, turn.elapsed), model_calls=_call_summaries(turn.records))


@router.post("/interview-sessions/{session_id}/questions/next", response_model=TurnResult)
def next_question(
    session_id: OwnedSessionId,
    body: NextQuestionRequest,
    db: Session = Depends(get_db),
    gateway: ModelGateway | None = Depends(get_model_gateway),
    candidate: Candidate = Depends(current_candidate),
    settings: Settings = Depends(get_settings),
) -> TurnResult:
    """The question to answer now. Code decides what it is (topic, follow-up, rephrase) or that
    the interview is over; a model may only word a follow-up or rephrase, with consent. While a
    question is open this returns it again, with no model call."""
    try:
        turn = prepare_turn(db, session_id, model_available=gateway is not None)
        if isinstance(turn, PendingTurn):
            # No DB connection or lock is held from here until save_turn.
            if admit_ai(db, AI_WORDING, candidate.id, settings):
                request = WordingRequest(turn.topic, turn.answer, turn.original_question,
                                         tuple(turn.anchor_options))
                outcome = anyio.from_thread.run(propose_wording, gateway, request, turn.codes,
                                                turn.deadline)
            else:
                outcome = WordingOutcome(rules_reason=RulesReason.QUOTA_REACHED)
            turn = save_turn(db, turn, outcome)
    except SessionNotFound:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None
    except TurnNotAllowed as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    _commit(db)
    return _turn_result(turn)


@router.post("/interview-sessions/{session_id}/answers", response_model=AnswerResult)
def answer_question(
    body: AnswerRequest,
    session_id: OwnedSessionId,
    db: Session = Depends(get_db),
) -> AnswerResult:
    """Store the answer to the open question (NOT_EVALUATED). Late answers are refused and never
    stored. Repeating a stored answer exactly changes nothing."""
    text = (body.text or "").strip()
    # Checked before the session is touched: an oversized answer writes nothing (D5).
    if len(text) > ANSWER_MAX_LENGTH:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, ANSWER_TOO_LONG)
    if body.kind == AnswerKind.ANSWER and not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "An answer needs text.")
    try:
        session, question = submit_answer(
            db,
            session_id,
            body.question_id,
            body.kind,
            text,
            speech_duration_ms=body.speech_duration_ms,
            speech_wpm=body.speech_wpm,
        )
    except (SessionNotFound, QuestionNotFound) as exc:
        db.rollback()
        detail = NOT_FOUND if isinstance(exc, SessionNotFound) else "Question not found"
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail) from None
    except (LateAnswer, TurnNotAllowed) as exc:
        db.rollback()  # a deadline closure was already committed; nothing else is kept
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    plan = stored_plan(session)
    _commit(db)
    return AnswerResult(session_id=session.id, status=session.status,
                        question=_question_view(plan, question))


@router.get("/interview-sessions/{session_id}/questions", response_model=QuestionHistory)
def read_questions(
    session: InterviewSession = Depends(get_interview_session), db: Session = Depends(get_db)
) -> QuestionHistory:
    """Questions and answers so far. Reading never changes the session (the deadline is applied
    by the next write request), but progress reports whether it has been reached."""
    if session.plan is None:
        return QuestionHistory(session_id=session.id, status=session.status,
                               finish_reason=session.finish_reason, progress=None, questions=[])
    plan = stored_plan(session)
    evaluations = session_evaluations(db, session.id)
    return QuestionHistory(
        session_id=session.id, status=session.status, finish_reason=session.finish_reason,
        progress=_progress(plan, elapsed_seconds(db, session)),
        questions=[_question_view(plan, q, evaluations.get(q.id))
                   for q in load_history(db, session.id)])


# ---------- answer evaluation (Phase 8) ----------


@router.post("/interview-sessions/{session_id}/questions/{question_id}/evaluation",
             response_model=EvaluationResult)
def evaluate_answer(
    session_id: OwnedSessionId,
    question_id: uuid.UUID,
    body: EvaluationRequest,
    db: Session = Depends(get_db),
    gateway: ModelGateway | None = Depends(get_model_gateway),
    candidate: Candidate = Depends(current_candidate),
    settings: Settings = Depends(get_settings),
) -> EvaluationResult:
    """Evaluate one stored answer, once the interview has finished. Code decides the rubric and
    whether a model may be asked (the session's AI consent); a stored result is returned again
    with no model call. The evaluation never changes the interview."""
    try:
        result = prepare_evaluation(db, session_id, question_id,
                                    model_available=gateway is not None)
        if isinstance(result, PendingEvaluation):
            # No DB connection or lock is held from here until save_evaluation.
            if admit_ai(db, AI_EVALUATION, candidate.id, settings):
                outcome = anyio.from_thread.run(propose_evaluation, gateway, result.context,
                                                result.codes, result.deadline)
            else:
                outcome = ProposalOutcome(rules_reason=RulesReason.QUOTA_REACHED)
            result = save_evaluation(db, result, outcome)
    except SessionNotFound:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None
    except QuestionNotFound:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Question not found") from None
    except EvaluationNotAllowed as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    _commit(db)
    row = result.evaluation
    return EvaluationResult(
        session_id=result.session.id, question_id=result.question.id,
        status=result.question.evaluation_status,
        evaluation=evaluation_view(result.plan, result.question, row),
        notice=notice_for(row.rules_reason) if row else None,
        model_calls=_call_summaries(result.records))


@router.post(
    "/interview-sessions/{session_id}/evaluations/batch",
    response_model=BatchEvaluationResult,
)
def batch_evaluate_answers(
    session_id: OwnedSessionId,
    db: Session = Depends(get_db),
    gateway: ModelGateway | None = Depends(get_model_gateway),
    candidate: Candidate = Depends(current_candidate),
    settings: Settings = Depends(get_settings),
) -> BatchEvaluationResult:
    """Evaluate all answered questions in this session with server-side concurrency (AUDIT-003, AUDIT-010)."""
    session = db.get(InterviewSession, session_id)
    if session is None or session.candidate_id != candidate.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    if session.status not in (InterviewStatus.REPORT_GENERATION, InterviewStatus.COMPLETED):
        raise HTTPException(status.HTTP_409_CONFLICT, "Answers can be evaluated only after the interview has finished.")

    questions = [q for q in load_history(db, session_id) if q.answered_at is not None]
    if not questions:
        return BatchEvaluationResult(session_id=session_id, total_evaluated=0, evaluations=[], notices=[])

    # Step 1: Prepare evaluation for each question
    prepared_list = []
    for q in questions:
        try:
            res = prepare_evaluation(db, session_id, q.id, model_available=gateway is not None)
            prepared_list.append(res)
        except EvaluationNotAllowed as exc:
            logger.warning("Evaluation skipped for question %s: %s", q.id, exc)

    # Step 2: For any PendingEvaluation, run model proposals concurrently
    pendings = [p for p in prepared_list if isinstance(p, PendingEvaluation)]
    model_outcomes: dict[uuid.UUID, ProposalOutcome] = {}

    if pendings and gateway is not None:
        to_call = []
        for p in pendings:
            if admit_ai(db, AI_EVALUATION, candidate.id, settings):
                to_call.append(p)
            else:
                model_outcomes[p.question_id] = ProposalOutcome(rules_reason=RulesReason.QUOTA_REACHED)

        if to_call:
            async def _propose_all():
                async def _call_one(p: PendingEvaluation):
                    try:
                        return p.question_id, await propose_evaluation(gateway, p.context, p.codes, p.deadline)
                    except Exception as err:
                        logger.error("AI evaluation call failed for question %s: %s", p.question_id, err)
                        return p.question_id, ProposalOutcome(rules_reason=RulesReason.PROVIDERS_FAILED)
                return await asyncio.gather(*[_call_one(p) for p in to_call])

            results = anyio.from_thread.run(_propose_all)
            for qid, outcome in results:
                model_outcomes[qid] = outcome

    # Step 3: Save evaluations and construct results
    evaluation_results = []
    notices = []
    for p in prepared_list:
        if isinstance(p, PendingEvaluation):
            outcome = model_outcomes.get(p.question_id, ProposalOutcome(rules_reason=RulesReason.PROVIDERS_FAILED))
            saved = save_evaluation(db, p, outcome)
            _commit(db)
            row = saved.evaluation
            notice = notice_for(row.rules_reason) if row else None
            if notice:
                notices.append(notice)
            evaluation_results.append(EvaluationResult(
                session_id=saved.session.id, question_id=saved.question.id,
                status=saved.question.evaluation_status,
                evaluation=evaluation_view(saved.plan, saved.question, row),
                notice=notice,
                model_calls=_call_summaries(saved.records),
            ))
        else:
            row = p.evaluation
            notice = notice_for(row.rules_reason) if row else None
            if notice:
                notices.append(notice)
            evaluation_results.append(EvaluationResult(
                session_id=p.session.id, question_id=p.question.id,
                status=p.question.evaluation_status,
                evaluation=evaluation_view(p.plan, p.question, row),
                notice=notice,
                model_calls=_call_summaries(p.records),
            ))

    return BatchEvaluationResult(
        session_id=session_id,
        total_evaluated=len(evaluation_results),
        evaluations=evaluation_results,
        notices=list(dict.fromkeys(notices)),
    )


@router.get("/interview-sessions/{session_id}/evaluations", response_model=EvaluationList)
def read_evaluations(
    session: InterviewSession = Depends(get_interview_session), db: Session = Depends(get_db)
) -> EvaluationList:
    """The evaluation status of every answered question, with the stored evaluation if any."""
    if session.plan is None:
        return EvaluationList(session_id=session.id, status=session.status, evaluations=[])
    plan = stored_plan(session)
    evaluations = session_evaluations(db, session.id)
    return EvaluationList(session_id=session.id, status=session.status, evaluations=[
        AnswerEvaluationItem(question_id=q.id, seq=q.seq, status=q.evaluation_status,
                             evaluation=evaluation_view(plan, q, evaluations.get(q.id)))
        for q in load_history(db, session.id) if q.answered_at is not None])


# ---------- observed expression signals (optional camera, counts only) ----------


@router.put("/interview-sessions/{session_id}/questions/{question_id}/expressions",
            status_code=status.HTTP_204_NO_CONTENT)
def put_expression_observation(
    session_id: OwnedSessionId,
    question_id: uuid.UUID,
    body: ExpressionObservationRequest,
    db: Session = Depends(get_db),
) -> None:
    """Counts from the candidate's browser for one answered question (replaces earlier ones).
    No image or video is ever sent or stored; nothing here changes the interview or a score."""
    try:
        save_observation(db, session_id, question_id, body.status,
                         {**body.counts, "no_face": body.no_face,
                          "multiple_faces": body.multiple_faces})
    except SessionNotFound:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None
    except QuestionNotFound:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Question not found") from None
    except ObservationNotAllowed as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    _commit(db)


# ---------- report (Phase 9) ----------


@router.post("/interview-sessions/{session_id}/report", response_model=ReportResult)
def generate_report(
    body: ReportRequest,
    session_id: OwnedSessionId,
    db: Session = Depends(get_db),
) -> ReportResult:
    """REPORT_GENERATION -> COMPLETED once every answer has been evaluated, and the report. Built
    by code from stored data: no model call and nothing stored. A completed session returns the
    same report again."""
    try:
        # Every interview write first applies the time limit (Phase 7, §6.3).
        if lock_and_enforce_deadline(db, session_id):
            _commit(db)
        session, report = complete_report(db, session_id)
    except SessionNotFound:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None
    except ReportNotAllowed as exc:
        db.rollback()  # nothing was written; this releases the row lock
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    _commit(db)
    return ReportResult(session_id=session.id, status=session.status, report=report)


@router.get("/interview-sessions/{session_id}/report", response_model=ReportResult)
def read_report(
    session: InterviewSession = Depends(get_interview_session), db: Session = Depends(get_db)
) -> ReportResult:
    """The report of a completed interview, rebuilt from stored data. Reading changes nothing."""
    if session.status != InterviewStatus.COMPLETED:
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_GENERATED)  # D11
    return ReportResult(session_id=session.id, status=session.status,
                        report=session_report(db, session))


# ---------- retry (Phase 10) ----------


@router.post("/interview-sessions/{session_id}/retry", response_model=InterviewPlanView,
             status_code=status.HTTP_201_CREATED)
def retry_interview_session(
    body: RetryRequest,
    session_id: OwnedSessionId,
    db: Session = Depends(get_db),
) -> InterviewPlanView:
    """A new READY session, for the same candidate, with the completed interview's topics to
    practise whose facts are still valid, copied word for word. No model call; the source is
    not changed."""
    try:
        session, plan = create_retry(db, session_id)
    except SessionNotFound:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None
    except ActiveSessionExists as exc:
        db.rollback()
        raise _active_session_conflict(exc) from None
    except (RetryNotAllowed, ProfileNotReviewed) as exc:
        db.rollback()  # nothing was written; this releases the row lock
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    _commit(db)
    return InterviewPlanView(session_id=session.id, status=session.status, plan=plan)
