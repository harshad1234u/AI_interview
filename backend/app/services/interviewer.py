"""Interviewer (PRD §12-§17, §29-§33): consent, deadline, decisions, wording rules, persistence.

LLM proposes, orchestrator decides (PRD §57). Every action, topic, finish and finish reason is
chosen here by code; a model only proposes the wording of a FOLLOW_UP or REPHRASE that code has
already chosen (app/ai/interviewer_agent.py), and that wording is validated here before use.

Transaction contract (as in the planner): no connection or row lock is held while a model runs.
prepare_turn() locks, enforces the deadline, decides, and either completes the turn itself or
commits and hands back a PendingTurn; save_turn() locks again, re-checks status, turn, deadline,
consent and cited facts, and writes. Callers commit.

Untrusted text: answers, approved facts, the stored plan and any model output. None of it ever
chooses an action, and none of it is logged.
"""

import difflib
import logging
import math
import re
import time
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.gateway import CallRecord
from app.models import CandidateFact, InterviewConsentEvent, InterviewQuestion, InterviewSession
from app.models import ProviderCall
from app.models.interview_question import (
    MAX_FOLLOW_UPS,
    MAX_REPHRASES,
    AnswerKind,
    EvaluationStatus,
    QuestionKind,
    QuestionSource,
)
from app.models.interview_session import FinishReason, InterviewStatus, RulesReason
from app.models.resume import FactCategory, FactStatus
from app.schemas.interview_plan import (
    FactSnapshot,
    InterviewPlan,
    PlannedTopic,
    PlanSource,
)
from app.schemas.interview_turn import FollowUpWording, RephraseWording
from app.services.interview_planner import SENSITIVE, mentions, own_words
from app.services.interview_state_machine import SessionEvent, SessionNotFound, apply_event

logger = logging.getLogger(__name__)
S, E, K = InterviewStatus, SessionEvent, QuestionKind

# Approved wording (plan rev 5 §2, §8)
FOLLOW_UP_TEMPLATE = "You mentioned {term}. Can you tell me more about that?"
GENERIC_FOLLOW_UP = "Could you elaborate on your answer?"
REPHRASE_TEMPLATE = "Let me put that another way. This question is about {topic}: {question}"
LATE_ANSWER = "The interview time limit was reached, so this answer was not saved."
PROFILE_CHANGED = "Your approved profile changed. Please try again."

RESERVE_SECONDS = 60.0  # no question starts with less left (Q3)
TURN_SECONDS = 30.0  # longest model wait per turn (§6.5)
DUPLICATE_RATIO = 0.85  # PRD §33
TERM_MAX_CHARS, TERM_MAX_WORDS = 60, 6
RUNNING = (S.ASKING, S.WAITING_FOR_ANSWER, S.NEXT_DECISION)  # the clock runs, the deadline applies
FINISHED = (S.REPORT_GENERATION, S.COMPLETED)
ASKS = {K.TOPIC: E.NEXT_TOPIC, K.FOLLOW_UP: E.FOLLOW_UP, K.REPHRASE: E.REPHRASE}
# A rephrase failing these is a duplicate: no re-request, next topic (P3, C7).
DUPLICATE_CODES = frozenset({"duplicate_question", "rephrase_substitutes_planned_question"})


# ---------- errors ----------


class ConsentNotAllowed(Exception):
    def __init__(self) -> None:
        super().__init__("This interview session is completed: AI consent can no longer be granted.")


class TurnNotAllowed(Exception):
    """409. The message names the reason; never any content."""


class QuestionNotFound(Exception):
    pass


class LateAnswer(Exception):
    def __init__(self) -> None:
        super().__init__(LATE_ANSWER)


# ---------- §8.1 matching normalization and terms ----------


def _tokens(text: str) -> list[tuple[str, int, int]]:
    """Matching tokens with their [start, end) span in `text` (§8.1 step 1): NFKC, casefold,
    punctuation (Unicode P*) except '#' as a separator, whitespace collapsed. Characters are
    normalized with their combining marks, so every token maps back to what was typed."""
    out: list[tuple[str, int, int]] = []
    chars: list[str] = []
    start = end = 0
    i = 0
    while i < len(text):
        j = i + 1
        while j < len(text) and unicodedata.combining(text[j]):
            j += 1
        for ch in unicodedata.normalize("NFKC", text[i:j]).casefold():
            if ch.isspace() or (unicodedata.category(ch).startswith("P") and ch != "#"):
                if chars:
                    out.append(("".join(chars), start, end))
                    chars = []
            else:
                if not chars:
                    start = i
                chars.append(ch)
                end = j
        i = j
    if chars:
        out.append(("".join(chars), start, end))
    return out


def normalize(text: str) -> str:
    return " ".join(token for token, _, _ in _tokens(text))


def find_phrase(text: str, phrase: str) -> str | None:
    """Where `phrase` occurs in `text` as a whole-token run (after normalization): the original
    span, exactly as typed. None if it doesn't occur."""
    want = [token for token, _, _ in _tokens(phrase)]
    have = _tokens(text)
    for i in range(len(have) - len(want) + 1) if want else ():
        if [token for token, _, _ in have[i:i + len(want)]] == want:
            return text[have[i][1]:have[i + len(want) - 1][2]]
    return None


def is_duplicate(a: str, b: str) -> bool:
    """PRD §33: normalized equality, or similarity >= 0.85."""
    na, nb = normalize(a), normalize(b)
    # autojunk off: with it, questions over 200 characters would barely ever look similar.
    return na == nb or difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio() >= DUPLICATE_RATIO


URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://|\bwww\.")
MARKUP = re.compile(r"<\s*/?\s*[a-z!][^>]*>|\[[^\]]*\]\([^)]*\)", re.IGNORECASE)
UNSAFE_CHARS = set("{}<>[]`")


def safe_term(term: str) -> bool:
    """§8.1 step 3, except the used-term check (term_already_used)."""
    return (1 <= len(term) <= TERM_MAX_CHARS and len(term.split()) <= TERM_MAX_WORDS
            and any(c.isalpha() for c in term)
            and not any(unicodedata.category(c).startswith("C") for c in term)
            and not UNSAFE_CHARS & set(term) and not URL.search(term)
            and not SENSITIVE.search(term))


def template_term(question: InterviewQuestion) -> str | None:
    """The term a deterministic follow-up was asked about."""
    prefix, suffix = FOLLOW_UP_TEMPLATE.split("{term}")
    if (question.kind == K.FOLLOW_UP and question.source == QuestionSource.RULES
            and question.text.startswith(prefix) and question.text.endswith(suffix)):
        return question.text[len(prefix):-len(suffix)]
    return None


# ---------- decisions (pure) ----------


class Action(StrEnum):
    TOPIC = "TOPIC"  # the first topic, or the next one
    FOLLOW_UP = "FOLLOW_UP"
    REPHRASE = "REPHRASE"
    FINISH = "FINISH"


@dataclass(frozen=True)
class Intent:
    action: Action
    finish_reason: FinishReason | None = None


@dataclass(frozen=True)
class Step:
    """The outcome of a turn: ask this question, or finish (kind None)."""

    kind: QuestionKind | None
    finish_reason: FinishReason | None = None
    topic_position: int = 0
    text: str = ""
    source: QuestionSource = QuestionSource.RULES
    rules_reason: RulesReason | None = None
    anchor: str | None = None
    followup_index: int = 0
    rephrase_index: int = 0


@dataclass(frozen=True)
class TurnContext:
    """Everything a decision may depend on. Never model output, never a judgement of answers."""

    plan: InterviewPlan
    history: tuple[InterviewQuestion, ...]  # asked questions in order, all answered
    elapsed: float  # active seconds, exact
    valid: frozenset[int]  # positions of topics whose cited facts are still approved, unchanged
    limit: float | None = None  # seconds; None = the plan's duration (tests may pass any)

    @property
    def time_limit(self) -> float:
        return self.limit if self.limit is not None else self.plan.settings.duration_minutes * 60.0

    @property
    def remaining(self) -> float:
        return self.time_limit - self.elapsed

    @property
    def last(self) -> InterviewQuestion | None:
        return self.history[-1] if self.history else None

    def topic(self, position: int) -> PlannedTopic:
        return self.plan.topics[position - 1]

    @property
    def topic_share(self) -> float:
        """Exact seconds per planned topic: fixed for the session, never redistributed (Q4)."""
        return self.time_limit / len(self.plan.topics)

    @property
    def topic_used(self) -> float:
        position = self.last.topic_position
        return self.elapsed - next(q.clock_at_ask for q in self.history
                                   if q.topic_position == position)

    @property
    def original(self) -> InterviewQuestion:
        """The current chain's original: the latest question that is not a rephrase (§8.4)."""
        return next(q for q in reversed(self.history) if q.kind != K.REPHRASE)


def deadline_event(status: object, elapsed: float, limit: float
                   ) -> tuple[SessionEvent, FinishReason] | None:
    """§6.3: the event that closes an interview whose time is up, if any."""
    if status == S.ASKING and (elapsed >= limit or limit - elapsed < RESERVE_SECONDS):
        return E.TIME_EXPIRED, FinishReason.TIME_LIMIT
    if status == S.WAITING_FOR_ANSWER and elapsed >= limit:
        return E.TIME_EXPIRED, FinishReason.TIME_LIMIT
    if status == S.NEXT_DECISION and elapsed >= limit:
        return E.FINISH, FinishReason.TIME_LIMIT
    return None


def follow_up_due(ctx: TurnContext) -> bool:
    """Only kind, cap and time (D14, Q4): never answer length or quality."""
    last = ctx.last
    return (last.answer_kind == AnswerKind.ANSWER and last.followup_index < MAX_FOLLOW_UPS
            and ctx.topic_used < ctx.topic_share and ctx.remaining >= RESERVE_SECONDS)


def decide(ctx: TurnContext) -> Intent:
    """§5.1, first match wins. Whether wording exists is settled by resolve()."""
    if ctx.elapsed >= ctx.time_limit or ctx.remaining < RESERVE_SECONDS:
        return Intent(Action.FINISH, FinishReason.TIME_LIMIT)
    last = ctx.last
    if last is None:
        return Intent(Action.TOPIC)
    current = last.topic_position in ctx.valid  # D8: a withdrawn fact ends its topic
    if current and last.answer_kind == AnswerKind.CLARIFY and last.rephrase_index < MAX_REPHRASES:
        return Intent(Action.REPHRASE)
    if current and follow_up_due(ctx):
        return Intent(Action.FOLLOW_UP)
    return Intent(Action.TOPIC)


def next_topic(ctx: TurnContext) -> Step:
    """The next valid topic, in plan order, with its first template that is not a duplicate;
    finish when none is left: PLAN_COMPLETED, or NO_ELIGIBLE_TOPIC if nothing was asked yet
    (then no template can be a duplicate, so every topic lost a cited fact, D8). The template is
    used verbatim (D12)."""
    after = ctx.last.topic_position if ctx.last else 0
    asked = [q.text for q in ctx.history]
    ai_plan = ctx.plan.source == PlanSource.AI
    for topic in ctx.plan.topics:
        if topic.position <= after or topic.position not in ctx.valid:
            continue
        for template in topic.question_templates:
            if not any(is_duplicate(template, q) for q in asked):
                # The text is the plan's, so it carries the plan's provenance.
                return Step(K.TOPIC, topic_position=topic.position, text=template,
                            source=QuestionSource.AI if ai_plan else QuestionSource.RULES,
                            rules_reason=None if ai_plan else ctx.plan.rules_reason)
    return Step(None, FinishReason.PLAN_COMPLETED if ctx.history else FinishReason.NO_ELIGIBLE_TOPIC)


def vocabulary(ctx: TurnContext) -> list[str]:
    """§8.1: the current topic's cited facts (valid ⇒ still approved and unchanged), then its
    planned concepts. From the validated plan only: answers never add to it."""
    topic = ctx.topic(ctx.last.topic_position)
    return [f.fact for f in topic.facts] + list(topic.expected_concepts)


def used_terms(history: Iterable[InterviewQuestion]) -> set[str]:
    """Normalized terms of every template follow-up and accepted model anchor so far (P2, I2)."""
    terms = {template_term(q) for q in history} | {q.anchor for q in history}
    return {normalize(t) for t in terms if t}


def anchor_terms(ctx: TurnContext) -> list[str]:
    """Every vocabulary entry found in the last answer whose span, as typed, is safe and not used
    before in the session: the only anchors a follow-up may use."""
    answer, used = ctx.last.answer_text or "", used_terms(ctx.history)
    spans = (find_phrase(answer, entry) for entry in vocabulary(ctx))
    return list(dict.fromkeys(s for s in spans if s and safe_term(s) and normalize(s) not in used))


def deterministic_term(ctx: TurnContext) -> str | None:
    """§8.1: the first of anchor_terms, in vocabulary order."""
    terms = anchor_terms(ctx)
    return terms[0] if terms else None


def resolve(ctx: TurnContext, intent: Intent, wording: BaseModel | None = None, *,
            duplicate: bool = False, reason: RulesReason | None = None) -> Step:
    """The question to ask (or the finish) for a decided intent. `wording` is model wording that
    already passed validation; without it the deterministic chain applies (P1, P3)."""
    if intent.action == Action.FINISH:
        return Step(None, intent.finish_reason)
    if intent.action == Action.TOPIC:
        return next_topic(ctx)
    last = ctx.last
    if intent.action == Action.FOLLOW_UP:
        base = {"kind": K.FOLLOW_UP, "topic_position": last.topic_position,
                "followup_index": last.followup_index + 1}
        if isinstance(wording, FollowUpWording):
            return Step(**base, text=wording.question, source=QuestionSource.AI,
                        anchor=wording.anchor)
        if term := deterministic_term(ctx):
            return Step(**base, text=FOLLOW_UP_TEMPLATE.format(term=term), rules_reason=reason)
        if not any(is_duplicate(GENERIC_FOLLOW_UP, q.text) for q in ctx.history):  # once (O1)
            return Step(**base, text=GENERIC_FOLLOW_UP, rules_reason=reason)
        return next_topic(ctx)
    # REPHRASE: transform the chain's original question, never substitute another (P3)
    base = {"kind": K.REPHRASE, "topic_position": last.topic_position,
            "followup_index": last.followup_index, "rephrase_index": last.rephrase_index + 1}
    if duplicate:
        return next_topic(ctx)
    if isinstance(wording, RephraseWording):
        return Step(**base, text=wording.question, source=QuestionSource.AI)
    framed = REPHRASE_TEMPLATE.format(topic=ctx.topic(last.topic_position).topic,
                                      question=ctx.original.text)
    if rephrase_duplicate(ctx, framed):
        return next_topic(ctx)
    return Step(**base, text=framed, rules_reason=reason)


def rephrase_duplicate(ctx: TurnContext, text: str) -> bool:
    return bool(_duplicate_codes(ctx, text, rephrase=True))


def _duplicate_codes(ctx: TurnContext, text: str, *, rephrase: bool) -> set[str]:
    codes = set()
    if any(is_duplicate(text, q.text) for q in ctx.history):  # the original included (A7)
        codes.add("duplicate_question")
    if rephrase:  # I3: never another planned question
        original = normalize(ctx.original.text)
        others = [t for topic in ctx.plan.topics for t in topic.question_templates
                  if normalize(t) != original]
        if any(is_duplicate(text, t) for t in others):
            codes.add("rephrase_substitutes_planned_question")
    return codes


# ---------- model wording validation (§8.2, §8.4, §8.5) ----------

EVALUATIVE = re.compile(r"\b(?:right|wrong|correct|incorrect|incomplete|you missed|you forgot|"
                        r"good answer|not quite)\b", re.IGNORECASE)
HIRING = re.compile(r"\b(?:hire|hired|hiring|unsuitable|rank|ranking|employable)\b", re.IGNORECASE)
REFERENCE = re.compile(r"\byou\s+(?:mentioned|said|told|described)\b", re.IGNORECASE)
# What a candidate can own or use: "your" before one of these (up to two words in between, as in
# "your Kafka setup") presumes it; "your own words", "your opinion", "your answer" do not.
# ponytail: a closed English word list, so an unlisted noun ("your Kafka broker") slips through;
# extend the list if live output shows such misses (L1).
OWNED = ("project|projects|app|apps|application|applications|system|systems|code|codebase|"
         "implementation|deployment|setup|stack|design|architecture|solution|service|services|"
         "pipeline|model|models|team|internship|job|work|experience|role|use|usage|choice|choices|"
         "decision|decisions|repo|repository|database|server|backend|frontend|api|product|"
         "version|configuration|config|cluster")
# Presumes something about the candidate: "you used/built/...", "did/have you ...", "your <owned>".
PRESUMES = re.compile(r"\b(?:you(?:'ve|\s+have|\s+had)?\s+(?:used|built|implemented|chose|"
                      r"designed|worked\s+on)|(?:did|have|had)\s+you|"
                      rf"your\s+(?:[\w.+#-]+\s+){{0,2}}?(?:{OWNED}))\b", re.IGNORECASE)
WORD_EDGES = "\"'()[]{},;:!?"


def technical_terms(question: str) -> list[str]:
    """Names and technical-looking words: capitalized mid-sentence, or with a digit, +, # or ."""
    words = question.split()
    terms = []
    for i, raw in enumerate(words):
        word = raw.strip(WORD_EDGES).removesuffix(".").strip(WORD_EDGES)
        sentence_start = i == 0 or words[i - 1].rstrip("\"')").endswith((".", "?", "!", ":"))
        if word and (any(c.isdigit() or c in "+#." for c in word)
                     or (word[0].isupper() and not sentence_start and word != "I")):
            terms.append(word)
    return terms


def ungrounded_claim(text: str, topic: PlannedTopic) -> bool:
    """P1: text that presumes something about the candidate (names a cited fact, or a PRESUMES
    pattern) may only name technical terms found in the approved cited facts or the validated plan
    topic. Answer text is never grounding."""
    cited = [f.fact for f in topic.facts]
    if not (any(mentions(text, f) for f in cited) or PRESUMES.search(text)):
        return False
    grounded = [*cited, topic.topic, *topic.question_templates, *topic.expected_concepts]
    return any(not any(find_phrase(g, term) for g in grounded) for term in technical_terms(text))


def _content_codes(ctx: TurnContext, question: str, anchor: str | None) -> set[str]:
    """Rules for any model-written question."""
    topic = ctx.topic(ctx.last.topic_position)
    cited = [f.fact for f in topic.facts]
    own = own_words(question, cited)
    codes = set()
    if EVALUATIVE.search(question):
        codes.add("evaluative_language")
    if HIRING.search(question):
        codes.add("hiring_language")
    if SENSITIVE.search(own) or (anchor and SENSITIVE.search(anchor)):
        codes.add("sensitive_attribute")
    if URL.search(question) or MARKUP.search(question):
        codes.add("url_or_markup")
    projects = {f.fact for t in ctx.plan.topics for f in t.facts
                if f.category == FactCategory.PROJECT} - set(cited)
    if any(mentions(own, p) for p in projects):
        codes.add("uncited_project")
    if ungrounded_claim(question, topic):
        codes.add("ungrounded_claim")
    if REFERENCE.search(question) and not (anchor and find_phrase(question, anchor)):
        codes.add("unanchored_reference")
    return codes


def follow_up_codes(ctx: TurnContext, wording: FollowUpWording) -> set[str]:
    anchor, question = wording.anchor, wording.question
    codes = _content_codes(ctx, question, anchor)
    if not find_phrase(ctx.last.answer_text or "", anchor) or not any(c.isalpha() for c in anchor):
        codes.add("anchor_not_in_answer")
    if normalize(anchor) not in {normalize(v) for v in vocabulary(ctx)}:
        codes.add("anchor_not_in_plan_vocabulary")
    if not safe_term(anchor):
        codes.add("anchor_unsafe")
    if not find_phrase(question, anchor):
        codes.add("anchor_not_mentioned")
    if normalize(anchor) in used_terms(ctx.history):
        codes.add("term_already_used")
    return codes | _duplicate_codes(ctx, question, rephrase=False)


def rephrase_codes(ctx: TurnContext, wording: RephraseWording) -> set[str]:
    return (_content_codes(ctx, wording.question, None)
            | _duplicate_codes(ctx, wording.question, rephrase=True))


def wording_codes(ctx: TurnContext, wording: BaseModel) -> set[str]:
    if isinstance(wording, FollowUpWording):
        return follow_up_codes(ctx, wording)
    return rephrase_codes(ctx, wording)


# ---------- persistence ----------


def lock_session(db: Session, session_id: uuid.UUID) -> InterviewSession:
    """The session row, locked, as last committed (the same lock apply_event takes)."""
    session = db.scalar(
        select(InterviewSession)
        .where(InterviewSession.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if session is None:
        raise SessionNotFound(session_id)
    return session


def elapsed_seconds(db: Session, session: InterviewSession) -> float:
    """Active seconds so far, in database time (§6.2)."""
    if session.active_since is None:
        return session.active_seconds
    now = db.scalar(select(func.clock_timestamp()))
    return session.active_seconds + max((now - session.active_since).total_seconds(), 0.0)


def stored_plan(session: InterviewSession) -> InterviewPlan:
    return InterviewPlan.model_validate(session.plan)  # stored text is still untrusted (D12)


def enforce_deadline(db: Session, session: InterviewSession) -> bool:
    """§6.3: close a running interview whose time is up, under the caller's row lock. True if
    it was closed (the caller commits). Every interview write request runs this first."""
    if session.status not in RUNNING:
        return False
    plan = stored_plan(session)
    hit = deadline_event(session.status, elapsed_seconds(db, session),
                         plan.settings.duration_minutes * 60.0)
    if hit is None:
        return False
    event, reason = hit
    apply_event(db, session.id, event, finish_reason=reason if event == E.FINISH else None)
    return True


def lock_and_enforce_deadline(db: Session, session_id: uuid.UUID) -> bool:
    return enforce_deadline(db, lock_session(db, session_id))


def load_history(db: Session, session_id: uuid.UUID) -> tuple[InterviewQuestion, ...]:
    return tuple(db.scalars(
        select(InterviewQuestion)
        .where(InterviewQuestion.session_id == session_id)
        .order_by(InterviewQuestion.seq)
        .execution_options(populate_existing=True)
    ))


def valid_positions(db: Session, candidate_id: uuid.UUID, plan: InterviewPlan) -> frozenset[int]:
    """Topics whose cited facts are all still APPROVED and unchanged (D8)."""
    cited = {f.fact_id for t in plan.topics for f in t.facts}
    current = {
        i: FactSnapshot(fact_id=i, category=c, fact=f)
        for i, c, f in db.execute(
            select(CandidateFact.id, CandidateFact.category, CandidateFact.fact).where(
                CandidateFact.id.in_(cited),
                CandidateFact.candidate_id == candidate_id,
                CandidateFact.status == FactStatus.APPROVED,
            )
        )
    } if cited else {}
    return frozenset(t.position for t in plan.topics
                     if all(current.get(f.fact_id) == f for f in t.facts))


@dataclass
class TurnOutcome:
    session: InterviewSession
    plan: InterviewPlan
    elapsed: float
    question: InterviewQuestion | None  # the open question; None once the interview is over
    records: list[CallRecord] = field(default_factory=list)


@dataclass
class PendingTurn:
    """A turn waiting for model wording. Holds no connection or lock."""

    session_id: uuid.UUID
    ctx: TurnContext
    intent: Intent
    deadline: float  # time.monotonic(): min(now + 30 s, when 60 s remain) (§6.5)

    @property
    def topic(self) -> PlannedTopic:
        return self.ctx.topic(self.ctx.last.topic_position)

    @property
    def answer(self) -> str | None:  # only for a FOLLOW_UP, never clarification text
        return self.ctx.last.answer_text if self.intent.action == Action.FOLLOW_UP else None

    @property
    def original_question(self) -> str | None:
        return self.ctx.original.text if self.intent.action == Action.REPHRASE else None

    @property
    def anchor_options(self) -> list[str]:
        """Told to the model so it need not guess; the validator still checks the anchor."""
        return anchor_terms(self.ctx) if self.intent.action == Action.FOLLOW_UP else []

    def codes(self, wording: BaseModel) -> set[str]:
        return wording_codes(self.ctx, wording)


@dataclass
class WordingOutcome:
    """What the agent produced for one decision. No content is logged from it."""

    wording: BaseModel | None = None  # validated wording, or None
    rules_reason: RulesReason | None = None  # why there is no wording
    duplicate: bool = False  # a rephrase rejected as a duplicate: next topic, no re-request
    records: list[CallRecord] = field(default_factory=list)
    rejected: list[frozenset[str]] = field(default_factory=list)


def _open_question(db: Session, session_id: uuid.UUID) -> InterviewQuestion | None:
    return db.scalar(select(InterviewQuestion).where(
        InterviewQuestion.session_id == session_id, InterviewQuestion.answered_at.is_(None)))


def _outcome(db: Session, session: InterviewSession, plan: InterviewPlan,
             records: list[CallRecord]) -> TurnOutcome:
    question = _open_question(db, session.id) if session.status == S.WAITING_FOR_ANSWER else None
    return TurnOutcome(session, plan, elapsed_seconds(db, session), question, records)


def _resting(db: Session, session: InterviewSession, plan: InterviewPlan,
             records: list[CallRecord]) -> TurnOutcome:
    """The answer for a session that is not deciding: the open question, or the finish."""
    if session.status == S.PAUSED:
        raise TurnNotAllowed("Resume the interview first.")
    if session.status in (S.WAITING_FOR_ANSWER, *FINISHED):
        return _outcome(db, session, plan, records)
    raise TurnNotAllowed(f"Cannot ask a question in an interview session in {session.status}.")


def prepare_turn(db: Session, session_id: uuid.UUID, *, model_available: bool
                 ) -> TurnOutcome | PendingTurn:
    """T1 (§11): lock, enforce the deadline, decide. A turn needing no model is completed here
    (the caller commits); otherwise this commits and returns a PendingTurn."""
    session = lock_session(db, session_id)
    if session.plan is None or session.status == S.READY:
        raise TurnNotAllowed("The interview has not started.")
    plan = stored_plan(session)
    enforce_deadline(db, session)
    if session.status not in (S.ASKING, S.NEXT_DECISION):
        return _resting(db, session, plan, [])
    ctx = TurnContext(plan, load_history(db, session.id), elapsed_seconds(db, session),
                      valid_positions(db, session.candidate_id, plan))
    intent = decide(ctx)
    if intent.action in (Action.FOLLOW_UP, Action.REPHRASE) and session.ai_consent \
            and model_available:
        pending = PendingTurn(session.id, ctx, intent, time.monotonic() + min(
            TURN_SECONDS, ctx.remaining - RESERVE_SECONDS))
        db.commit()  # releases the lock and the connection: none is held while a model runs
        return pending
    reason = RulesReason.NOT_CONFIGURED if session.ai_consent else RulesReason.NOT_REQUESTED
    return _apply(db, session, ctx, resolve(ctx, intent, reason=reason), [])


def save_turn(db: Session, turn: PendingTurn, outcome: WordingOutcome) -> TurnOutcome:
    """T2 (§11): lock again and re-check status, turn, deadline and reserve, consent and cited
    facts before writing. The model calls are recorded whatever happens. The caller commits."""
    records = outcome.records
    try:
        session = lock_session(db, turn.session_id)
    except SessionNotFound:
        _record_alone(db, records)
        raise
    calls = [ProviderCall.from_record(r) for r in records]
    db.add_all(calls)
    db.flush()
    plan = turn.ctx.plan
    if enforce_deadline(db, session):
        return _outcome(db, session, plan, records)
    history = load_history(db, session.id)
    if session.status != S.NEXT_DECISION or len(history) != len(turn.ctx.history):
        # Another request took this turn (e.g. a concurrent duplicate): keep its question.
        try:
            return _resting(db, session, plan, records)
        except TurnNotAllowed:
            db.commit()  # the calls stay recorded
            raise
    ctx = replace(turn.ctx, elapsed=elapsed_seconds(db, session))
    if ctx.remaining < RESERVE_SECONDS:  # the reserve was reached during the call
        return _apply(db, session, ctx, Step(None, FinishReason.TIME_LIMIT), records)
    if session.ai_consent:
        step = resolve(ctx, turn.intent, outcome.wording, duplicate=outcome.duplicate,
                       reason=outcome.rules_reason)
    else:  # withdrawn during the call: the wording is discarded (D2)
        step = resolve(ctx, turn.intent, reason=RulesReason.CONSENT_WITHDRAWN)
    if step.kind is not None and step.topic_position not in valid_positions(
            db, session.candidate_id, plan):
        db.commit()  # the calls stay recorded; the retry skips the topic (D8)
        raise TurnNotAllowed(PROFILE_CHANGED)
    call_id = calls[-1].id if step.source == QuestionSource.AI and step.kind != K.TOPIC else None
    return _apply(db, session, ctx, step, records, call_id)


def _record_alone(db: Session, records: list[CallRecord]) -> None:
    db.rollback()
    if records:
        db.add_all(ProviderCall.from_record(r) for r in records)
        db.commit()


def _apply(db: Session, session: InterviewSession, ctx: TurnContext, step: Step,
           records: list[CallRecord], call_id: uuid.UUID | None = None) -> TurnOutcome:
    if step.kind is None:
        if session.status == S.ASKING and step.finish_reason == FinishReason.TIME_LIMIT:
            # The reserve was reached between enforce_deadline and decide (§6.3 edge from ASKING)
            apply_event(db, session.id, E.TIME_EXPIRED)
        else:  # from ASKING only NO_ELIGIBLE_TOPIC, which the state machine enforces
            apply_event(db, session.id, E.FINISH, finish_reason=step.finish_reason)
        logger.info("Interview session %s finished: %s", session.id, step.finish_reason)
        return _outcome(db, session, ctx.plan, records)
    if session.status == S.NEXT_DECISION:
        apply_event(db, session.id, ASKS[step.kind])
    question = InterviewQuestion(
        session_id=session.id, seq=len(ctx.history) + 1, topic_position=step.topic_position,
        kind=step.kind, followup_index=step.followup_index, rephrase_index=step.rephrase_index,
        text=step.text, source=step.source, rules_reason=step.rules_reason, anchor=step.anchor,
        provider_call_id=call_id, clock_at_ask=ctx.elapsed,
    )
    db.add(question)
    db.flush()
    apply_event(db, session.id, E.QUESTION_ASKED)
    logger.info("Interview session %s asked question %d (%s, topic %d, %s%s)", session.id,
                question.seq, step.kind, step.topic_position, step.source,
                f", {step.rules_reason}" if step.rules_reason else "")
    return TurnOutcome(session, ctx.plan, ctx.elapsed, question, records)


def submit_answer(db: Session, session_id: uuid.UUID, question_id: uuid.UUID, kind: AnswerKind,
                  text: str | None, speech_duration_ms: int | None = None,
                  speech_wpm: int | None = None) -> tuple[InterviewSession, InterviewQuestion]:
    """§6.4, under the row lock. `text` is already stripped and length-checked. A replay of the
    stored answer changes nothing. The caller commits."""
    session = lock_session(db, session_id)
    question = db.scalar(
        select(InterviewQuestion)
        .where(InterviewQuestion.id == question_id, InterviewQuestion.session_id == session.id)
        .execution_options(populate_existing=True)
    )
    if question is None:
        raise QuestionNotFound(question_id)
    text = text or None
    if question.answered_at is not None:
        if (question.answer_kind, question.answer_text) == (kind, text):
            return session, question  # identical replay
        raise TurnNotAllowed("This question was already answered differently.")
    if enforce_deadline(db, session):
        db.commit()  # the closure stands even though this answer is refused
    if session.status == S.REPORT_GENERATION and session.finish_reason == FinishReason.TIME_LIMIT:
        raise LateAnswer()  # the answer text is not stored, logged or sent anywhere
    if session.status != S.WAITING_FOR_ANSWER:
        raise TurnNotAllowed(f"Cannot answer a question in an interview session in {session.status}.")
    question.answer_kind, question.answer_text = kind, text
    question.answered_at = func.clock_timestamp()
    question.evaluation_status = EvaluationStatus.NOT_EVALUATED  # D10a: stored, not evaluated
    if speech_duration_ms is not None:
        question.speech_duration_ms = speech_duration_ms
    if speech_wpm is not None:
        question.speech_wpm = speech_wpm
    db.flush()
    for event in (E.ANSWER_SUBMITTED, E.ANSWER_PROCESSED, E.ANSWER_EVALUATED):
        apply_event(db, session.id, event)  # one transaction: no in-between state is committed
    logger.info("Interview session %s: question %d answered (%s)", session.id, question.seq, kind)
    return session, question


# ---------- consent (D2, D2b, D2c) ----------


def set_consent(db: Session, session_id: uuid.UUID, granted: bool) -> InterviewSession:
    """Grant or withdraw AI consent under the row lock. A change is recorded with its time; a
    repeat of the current value records nothing. Withdrawing is always allowed. The caller
    commits."""
    session = lock_session(db, session_id)
    if granted and session.status == S.COMPLETED:
        raise ConsentNotAllowed()
    if session.ai_consent != granted:
        session.ai_consent = granted
        db.add(InterviewConsentEvent(session_id=session.id, granted=granted))
        db.flush()
        # Ids and the new value only (D2c).
        logger.info("Interview session %s: AI consent %s", session.id,
                    "granted" if granted else "withdrawn")
    return session


def consent_history(db: Session, session_id: uuid.UUID) -> list[InterviewConsentEvent]:
    return list(db.scalars(
        select(InterviewConsentEvent)
        .where(InterviewConsentEvent.session_id == session_id)
        .order_by(InterviewConsentEvent.id)
    ))


def time_remaining_display(remaining: float) -> int:
    """API display only (§6.2): decisions use the exact value."""
    return max(0, math.floor(remaining))
