import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, insert, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.ai.gateway import ErrorKind, ModelCapability
from app.db.session import engine
from app.models import Candidate
from app.models.interview_session import PRE_PLAN_STATUSES, InterviewStatus

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _current_heads() -> set[str]:
    with engine.connect() as conn:
        return set(MigrationContext.configure(conn).get_current_heads())


def test_downgrade_upgrade_roundtrip_and_models_in_sync() -> None:
    cfg = Config(str(ALEMBIC_INI))
    command.downgrade(cfg, "base")
    tables = set(inspect(engine).get_table_names())
    assert not tables & {"candidates", "resumes", "candidate_facts", "interview_sessions"}

    command.upgrade(cfg, "head")
    # Fails if SQLAlchemy models have changes with no matching migration.
    command.check(cfg)
    assert _current_heads() == set(ScriptDirectory.from_config(cfg).get_heads())


def test_candidates_table_schema() -> None:
    insp = inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("candidates")}
    # Credentials and lockout in Phase 12; external Supabase auth_user_id in Phase 2 (e8f3b2c1a0d9).
    assert set(cols) == {"id", "email", "name", "created_at", "password_hash", "failed_logins",
                         "locked_until", "auth_user_id"}
    assert {n for n, c in cols.items() if c["nullable"]} == {"password_hash", "locked_until", "auth_user_id"}
    assert insp.get_pk_constraint("candidates")["constrained_columns"] == ["id"]
    indexes = {i["name"]: i for i in insp.get_indexes("candidates")}
    assert "uq_candidates_auth_user_id" in indexes
    assert indexes["uq_candidates_auth_user_id"]["unique"]
    assert "uq_candidates_email_active" in indexes
    assert indexes["uq_candidates_email_active"]["unique"]
    assert "auth_user_id IS NOT NULL" in indexes["uq_candidates_email_active"]["dialect_options"]["postgresql_where"]
    checks = {c["name"] for c in insp.get_check_constraints("candidates")}
    assert checks == {"ck_candidates_email_lowercase", "ck_candidates_name_not_blank",
                      "ck_candidates_failed_logins_not_negative"}


def test_interview_sessions_table_schema() -> None:
    insp = inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("interview_sessions")}
    # Lifecycle (Phase 5) + the validated plan (Phase 6) + consent, clock and finish reason
    # (Phase 7) + the retry link (history). Answers live in interview_questions, never here.
    assert set(cols) == {"id", "candidate_id", "status", "created_at", "updated_at", "plan",
                         "plan_provider_call_id", "ai_consent", "active_seconds", "active_since",
                         "finish_reason", "retry_of_id"}
    assert [n for n, c in cols.items() if c["nullable"]] == [
        "plan", "plan_provider_call_id", "active_since", "finish_reason", "retry_of_id"]
    assert {c["name"] for c in insp.get_check_constraints("interview_sessions")} == {
        "ck_interview_sessions_status", "ck_interview_sessions_plan_matches_status",
        "ck_interview_sessions_plan_is_object", "ck_interview_sessions_finish_reason",
        "ck_interview_sessions_active_seconds_not_negative",
        "ck_interview_sessions_finish_reason_only_when_finished",
        "ck_interview_sessions_retry_of_another_session",
    }
    fks = {fk["referred_table"]: (fk["constrained_columns"], fk["options"].get("ondelete"))
           for fk in insp.get_foreign_keys("interview_sessions")}
    assert fks == {"candidates": (["candidate_id"], "CASCADE"),
                   "provider_calls": (["plan_provider_call_id"], "SET NULL"),
                   "interview_sessions": (["retry_of_id"], "SET NULL")}
    indexes = {i["name"]: i for i in insp.get_indexes("interview_sessions")}
    active = indexes["uq_interview_sessions_one_active_per_candidate"]
    assert active["unique"] and active["column_names"] == ["candidate_id"]
    assert "COMPLETED" in active["dialect_options"]["postgresql_where"]
    assert not indexes["ix_interview_sessions_candidate_id"]["unique"]


def test_interview_sessions_migration_downgrades_alone() -> None:
    """The Phase 5 revision only adds interview_sessions; stepping back leaves the rest intact."""
    cfg = Config(str(ALEMBIC_INI))
    before = {t: {c["name"] for c in inspect(engine).get_check_constraints(t)}
              for t in ("candidates", "resumes", "candidate_facts", "provider_calls")}
    command.downgrade(cfg, "a41f0c9e7b25")  # through the Phase 6 revision as well
    try:
        insp = inspect(engine)
        assert "interview_sessions" not in insp.get_table_names()
        # Stepping back through Phase 12 also drops its one candidates CHECK, nothing else.
        before["candidates"].discard("ck_candidates_failed_logins_not_negative")
        # ...and through Phase 13, its one candidate_facts CHECK.
        before["candidate_facts"].discard("ck_candidate_facts_provenance_matches_source")
        assert {t: {c["name"] for c in insp.get_check_constraints(t)} for t in before} == before
    finally:
        command.upgrade(cfg, "head")
    assert "interview_sessions" in inspect(engine).get_table_names()


def test_database_generates_id_and_created_at(db_session: Session) -> None:
    c = Candidate(email="gen@example.com", name="Gen")
    db_session.add(c)
    db_session.flush()
    assert c.id is not None
    assert c.created_at is not None and c.created_at.tzinfo is not None


@pytest.mark.parametrize(
    ("row", "constraint"),
    [
        ({"email": "a@example.com", "name": None}, None),
        ({"email": None, "name": "A"}, None),
        ({"email": "A@Example.com", "name": "A"}, "ck_candidates_email_lowercase"),
        ({"email": "a@example.com", "name": "   "}, "ck_candidates_name_not_blank"),
    ],
    ids=["null-name", "null-email", "uppercase-email", "blank-name"],
)
def test_database_rejects_invalid_rows(
    db_session: Session, row: dict, constraint: str | None
) -> None:
    with pytest.raises(IntegrityError) as exc:
        db_session.execute(insert(Candidate).values(**row))
    if constraint:
        assert exc.value.orig.diag.constraint_name == constraint


def test_database_enforces_unique_email(db_session: Session) -> None:
    # Phase 2 partial unique index uq_candidates_email_active applies when auth_user_id is not null
    auth_id_1 = uuid.uuid4()
    auth_id_2 = uuid.uuid4()
    db_session.execute(insert(Candidate).values(email="u@example.com", name="A", auth_user_id=auth_id_1))
    with pytest.raises(IntegrityError) as exc, db_session.begin_nested():
        db_session.execute(insert(Candidate).values(email="u@example.com", name="B", auth_user_id=auth_id_2))
    assert exc.value.orig.diag.constraint_name == "uq_candidates_email_active"
    # Legacy unlinked records (auth_user_id is None) do not conflict under the partial index
    db_session.execute(insert(Candidate).values(email="legacy@example.com", name="Legacy 1"))
    db_session.execute(insert(Candidate).values(email="legacy@example.com", name="Legacy 2"))


# ---------- resumes / candidate_facts ----------


def test_resume_tables_schema() -> None:
    insp = inspect(engine)
    assert {c["name"] for c in insp.get_columns("resumes")} == {
        "id", "candidate_id", "raw_text", "parsed_json", "upload_status", "created_at"
    }
    assert {c["name"] for c in insp.get_columns("candidate_facts")} == {
        "id", "candidate_id", "resume_id", "category", "fact", "extracted_fact", "source",
        "source_section", "source_text", "status", "extracted_by", "provider_call_id", "created_at",
    }
    fks = {
        fk["name"]: (fk["constrained_columns"], fk["referred_table"], fk["options"].get("ondelete"))
        for table in ("resumes", "candidate_facts")
        for fk in insp.get_foreign_keys(table)
    }
    assert fks == {
        "fk_resumes_candidate_id_candidates": (["candidate_id"], "candidates", "CASCADE"),
        "fk_candidate_facts_candidate_id_candidates": (["candidate_id"], "candidates", "CASCADE"),
        "fk_candidate_facts_resume_id_resumes": (["resume_id", "candidate_id"], "resumes", "CASCADE"),
        "fk_candidate_facts_provider_call_id_provider_calls": (
            ["provider_call_id"], "provider_calls", "SET NULL"
        ),
    }
    assert {c["name"] for c in insp.get_check_constraints("resumes")} == {"ck_resumes_upload_status"}
    assert {c["name"] for c in insp.get_check_constraints("candidate_facts")} == {
        "ck_candidate_facts_category", "ck_candidate_facts_source", "ck_candidate_facts_status",
        "ck_candidate_facts_fact_not_blank", "ck_candidate_facts_extracted_fact_not_blank",
        "ck_candidate_facts_extracted_by", "ck_candidate_facts_provenance_matches_source",
    }


def test_provider_calls_schema_holds_no_content_columns() -> None:
    insp = inspect(engine)
    assert {c["name"] for c in insp.get_columns("provider_calls")} == {
        "id", "request_id", "capability", "requested_provider", "provider", "model", "succeeded",
        "attempts", "fallback_used", "error_type", "latency_ms", "input_tokens", "output_tokens",
        "total_tokens", "provider_request_id", "created_at",
    }
    assert insp.get_foreign_keys("provider_calls") == []  # no link to a candidate
    assert {c["name"] for c in insp.get_check_constraints("provider_calls")} == {
        "ck_provider_calls_capability", "ck_provider_calls_error_type"
    }


def test_database_accepts_every_error_kind_and_nothing_else(db_session: Session) -> None:
    # The CHECK constraint is hand-written in migrations: keep it in step with ErrorKind.
    insert_call = text(
        "INSERT INTO provider_calls (request_id, capability, requested_provider, succeeded, "
        "attempts, fallback_used, error_type, latency_ms) "
        "VALUES ('r', 'PROFILE_EXTRACTION', 'groq', false, 1, false, :e, 0)"
    )
    for kind in ErrorKind:
        db_session.execute(insert_call, {"e": kind.value})
    with pytest.raises(IntegrityError) as exc:
        db_session.execute(insert_call, {"e": "QUOTA_GUESS"})
    assert exc.value.orig.diag.constraint_name == "ck_provider_calls_error_type"


def test_existing_facts_default_to_rule_extraction(db_session: Session) -> None:
    c = _candidate(db_session)
    fact_id = _fact(db_session, c, _resume(db_session, c))
    row = db_session.execute(
        text("SELECT extracted_by, provider_call_id FROM candidate_facts WHERE id = :i"), {"i": fact_id}
    ).one()
    assert tuple(row) == ("RULES", None)
    with pytest.raises(IntegrityError) as exc:
        _fact(db_session, c, _resume(db_session, c), extracted_by="LLM_SELF_APPROVED")
    assert exc.value.orig.diag.constraint_name == "ck_candidate_facts_extracted_by"


def _candidate(db: Session, email: str = "c@example.com") -> str:
    return str(db.scalar(insert(Candidate).values(email=email, name="C").returning(Candidate.id)))


def _resume(db: Session, candidate_id: str, status: str = "PROCESSED") -> str:
    return str(db.scalar(text(
        "INSERT INTO resumes (candidate_id, parsed_json, upload_status) "
        "VALUES (:c, '{}', :s) RETURNING id"
    ), {"c": candidate_id, "s": status}))


def _fact(db: Session, candidate_id: str, resume_id: str, **overrides: str) -> str:
    row = {"category": "SKILL", "fact": "Python", "extracted_fact": "Python", "source": "RESUME",
           "source_section": "Skills", "source_text": "Python", **overrides}
    cols = ", ".join(row)
    return str(db.scalar(text(
        f"INSERT INTO candidate_facts (candidate_id, resume_id, {cols}) "
        f"VALUES (:candidate_id, :resume_id, {', '.join(':' + k for k in row)}) RETURNING id"
    ), {"candidate_id": candidate_id, "resume_id": resume_id, **row}))


def test_fact_defaults_to_pending_review(db_session: Session) -> None:
    c = _candidate(db_session)
    fact_id = _fact(db_session, c, _resume(db_session, c))
    status = db_session.scalar(text("SELECT status FROM candidate_facts WHERE id = :i"), {"i": fact_id})
    assert status == "PENDING_REVIEW"


@pytest.mark.parametrize(
    ("bad", "constraint"),
    [
        ({"status": "TRUSTED"}, "ck_candidate_facts_status"),
        ({"category": "PERSONALITY"}, "ck_candidate_facts_category"),
        ({"source": "AI_INFERENCE"}, "ck_candidate_facts_source"),
        ({"fact": "  "}, "ck_candidate_facts_fact_not_blank"),
        ({"extracted_fact": ""}, "ck_candidate_facts_extracted_fact_not_blank"),
    ],
    ids=["status", "category", "source", "blank-fact", "blank-extracted"],
)
def test_database_rejects_invalid_facts(db_session: Session, bad: dict, constraint: str) -> None:
    c = _candidate(db_session)
    r = _resume(db_session, c)
    with pytest.raises(IntegrityError) as exc:
        _fact(db_session, c, r, **bad)
    assert exc.value.orig.diag.constraint_name == constraint


def test_database_rejects_invalid_resumes(db_session: Session) -> None:
    c = _candidate(db_session)
    with pytest.raises(IntegrityError) as exc, db_session.begin_nested():
        _resume(db_session, c, status="DONE")
    assert exc.value.orig.diag.constraint_name == "ck_resumes_upload_status"
    with pytest.raises(IntegrityError) as exc:
        _resume(db_session, str(uuid.uuid4()))
    assert exc.value.orig.diag.constraint_name == "fk_resumes_candidate_id_candidates"


def test_fact_cannot_point_at_another_candidates_resume(db_session: Session) -> None:
    alice, bob = _candidate(db_session, "a@example.com"), _candidate(db_session, "b@example.com")
    bobs_resume = _resume(db_session, bob)
    with pytest.raises(IntegrityError) as exc:
        _fact(db_session, alice, bobs_resume)
    assert exc.value.orig.diag.constraint_name == "fk_candidate_facts_resume_id_resumes"


def test_deleting_candidate_cascades_in_the_database(db_session: Session) -> None:
    c = _candidate(db_session)
    _fact(db_session, c, _resume(db_session, c))
    db_session.execute(text("DELETE FROM candidates WHERE id = :c"), {"c": c})
    for table in ("resumes", "candidate_facts"):
        left = db_session.scalar(text(f"SELECT count(*) FROM {table} WHERE candidate_id = :c"), {"c": c})
        assert left == 0


# ---------- Phase 6: interview plan ----------

PLAN_REVISION, BEFORE_PLAN_REVISION = "b8d14f2a9c63", "e5813b741341"
HEAD = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
SUPABASE_AUTH_REVISION = "e8f3b2c1a0d9"
CAS_FENCING_REVISION = "f9a1b2c3d4e5"
LATEST_REVISION = HEAD  # the newest revision: every round trip must end here
CALL_COLUMNS = ("INSERT INTO provider_calls (request_id, capability, requested_provider, "
                "succeeded, attempts, fallback_used, latency_ms) ")


def _session(db: Session, status: str, plan: str | None) -> None:
    """Raw insert, one candidate each (so the one-active-session index never interferes)."""
    with db.begin_nested():
        db.execute(text(
            "INSERT INTO interview_sessions (candidate_id, status, plan) "
            "VALUES (:c, :s, CAST(:p AS jsonb))"
        ), {"c": _candidate(db, f"{uuid.uuid4().hex}@example.com"), "s": status, "p": plan})


@pytest.mark.parametrize("status", [s.value for s in InterviewStatus])
def test_plan_exists_exactly_when_the_session_is_past_planning(
    db_session: Session, status: str
) -> None:
    """Every lifecycle state, against PostgreSQL itself: READY and every later state (PAUSED and
    COMPLETED included) must have a plan; CREATED..INTERVIEW_PLANNING must not."""
    past_planning = status not in {s.value for s in PRE_PLAN_STATUSES}
    _session(db_session, status, '{"version": 1}' if past_planning else None)  # legitimate
    with pytest.raises(IntegrityError) as exc:
        _session(db_session, status, None if past_planning else '{"version": 1}')
    assert exc.value.orig.diag.constraint_name == "ck_interview_sessions_plan_matches_status"


@pytest.mark.parametrize("plan", ["[]", '"plan"', "null", "1"])
def test_plan_must_be_a_json_object(db_session: Session, plan: str) -> None:
    with pytest.raises(IntegrityError) as exc:
        _session(db_session, "READY", plan)
    assert exc.value.orig.diag.constraint_name == "ck_interview_sessions_plan_is_object"


def test_status_and_plan_updates_are_checked_too(db_session: Session) -> None:
    sid = db_session.scalar(text(
        "INSERT INTO interview_sessions (candidate_id, status) VALUES (:c, 'INTERVIEW_PLANNING') "
        "RETURNING id"), {"c": _candidate(db_session)})
    update = text("UPDATE interview_sessions SET status = :s, plan = CAST(:p AS jsonb) WHERE id = :i")
    for status, plan in [("READY", None), ("INTERVIEW_PLANNING", "{}")]:  # half a planning step
        with pytest.raises(IntegrityError), db_session.begin_nested():
            db_session.execute(update, {"s": status, "p": plan, "i": sid})
    db_session.execute(update, {"s": "READY", "p": "{}", "i": sid})  # both together: fine
    for status in ("ASKING", "WAITING_FOR_ANSWER", "PAUSED", "REPORT_GENERATION", "COMPLETED"):
        db_session.execute(update, {"s": status, "p": "{}", "i": sid})  # later steps keep it
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.execute(text("UPDATE interview_sessions SET plan = NULL WHERE id = :i"),
                           {"i": sid})


def test_database_accepts_every_capability_and_nothing_else(db_session: Session) -> None:
    insert_call = text(CALL_COLUMNS + "VALUES ('r', :c, 'groq', true, 1, false, 0)")
    for capability in ModelCapability:
        db_session.execute(insert_call, {"c": capability.value})
    with pytest.raises(IntegrityError) as exc:
        db_session.execute(insert_call, {"c": "SELF_APPROVAL"})
    assert exc.value.orig.diag.constraint_name == "ck_provider_calls_capability"


def test_deleting_call_metadata_keeps_the_plan(db_session: Session) -> None:
    call = db_session.scalar(text(
        CALL_COLUMNS + "VALUES ('r', 'INTERVIEW_PLANNING', 'groq', true, 1, false, 0) RETURNING id"))
    sid = db_session.scalar(text(
        "INSERT INTO interview_sessions (candidate_id, status, plan, plan_provider_call_id) "
        "VALUES (:c, 'READY', '{}', :p) RETURNING id"), {"c": _candidate(db_session), "p": call})
    db_session.execute(text("DELETE FROM provider_calls WHERE id = :p"), {"p": call})
    row = db_session.execute(text(
        "SELECT plan, plan_provider_call_id FROM interview_sessions WHERE id = :i"), {"i": sid}).one()
    assert tuple(row) == ({}, None)


@pytest.fixture
def committed_planned_session() -> Iterator[None]:
    """A READY session with a plan, committed for real (not rolled back); cleaned up after."""
    with engine.begin() as conn:
        cid = conn.scalar(insert(Candidate).values(
            email=f"{uuid.uuid4().hex}@example.com", name="Downgrade").returning(Candidate.id))
        conn.execute(text("INSERT INTO interview_sessions (candidate_id, status, plan) "
                          "VALUES (:c, 'READY', CAST(:p AS jsonb))"),
                     {"c": cid, "p": '{"version": 1}'})
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM candidates WHERE id = :c"), {"c": cid})


def test_downgrade_refuses_while_plans_exist(committed_planned_session: None) -> None:
    cfg = Config(str(ALEMBIC_INI))
    with pytest.raises(RuntimeError, match="Refusing to downgrade: 1 interview session"):
        command.downgrade(cfg, BEFORE_PLAN_REVISION)
    # Nothing changed: still at head, and the plan is still there.
    assert _current_heads() == {HEAD}
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM interview_sessions WHERE plan IS NOT NULL"))


def test_plan_revision_downgrades_and_re_upgrades_cleanly() -> None:
    cfg = Config(str(ALEMBIC_INI))
    with engine.begin() as conn:  # planning-call metadata, which the old CHECK can't hold
        conn.execute(text(CALL_COLUMNS + "VALUES ('roundtrip-test', 'INTERVIEW_PLANNING', "
                                         "'groq', false, 1, false, 0)"))
    command.downgrade(cfg, BEFORE_PLAN_REVISION)
    try:
        insp = inspect(engine)
        assert {c["name"] for c in insp.get_columns("interview_sessions")} == {
            "id", "candidate_id", "status", "created_at", "updated_at"}
        with engine.connect() as conn:
            assert not conn.scalar(text(
                "SELECT count(*) FROM provider_calls WHERE request_id = 'roundtrip-test'"))
    finally:
        command.upgrade(cfg, "head")
    command.check(cfg)
    assert _current_heads() == {HEAD}


# ---------- Phase 7: interviewer ----------

INTERVIEWER_REVISION = "178659f38d45"
QUESTION_CHECKS = {f"ck_interview_questions_{n}" for n in (
    "kind", "source", "rules_reason", "answer_kind", "evaluation_status", "seq_positive",
    "topic_position_positive", "followup_index_range", "topic_has_no_followup_index",
    "follow_up_has_followup_index", "rephrase_index_range", "rephrase_index_matches_kind",
    "text_not_blank", "rules_reason_matches_source", "anchor_only_on_ai_follow_ups",
    "clock_at_ask_not_negative", "answer_text_length", "answer_fields_together",
    "answer_text_needs_kind", "speech_duration_not_negative", "speech_wpm_not_negative")}


def test_interviewer_tables_schema() -> None:
    insp = inspect(engine)
    assert {c["name"] for c in insp.get_columns("interview_questions")} == {
        "id", "session_id", "seq", "topic_position", "kind", "followup_index", "rephrase_index",
        "text", "source", "rules_reason", "anchor", "provider_call_id", "asked_at", "clock_at_ask",
        "answer_kind", "answer_text", "answered_at", "evaluation_status", "speech_duration_ms", "speech_wpm"}
    assert {c["name"] for c in insp.get_check_constraints("interview_questions")} == QUESTION_CHECKS
    fks = {fk["referred_table"]: (fk["constrained_columns"], fk["options"].get("ondelete"))
           for fk in insp.get_foreign_keys("interview_questions")}
    assert fks == {"interview_sessions": (["session_id"], "CASCADE"),
                   "provider_calls": (["provider_call_id"], "SET NULL")}
    open_index = {i["name"]: i for i in insp.get_indexes("interview_questions")}[
        "uq_interview_questions_one_open_per_session"]
    assert open_index["unique"]
    assert "answered_at IS NULL" in open_index["dialect_options"]["postgresql_where"]
    assert {c["name"] for c in insp.get_columns("interview_consent_events")} == {
        "id", "session_id", "granted", "changed_at"}  # no answer, prompt or payload columns
    [fk] = insp.get_foreign_keys("interview_consent_events")
    assert (fk["referred_table"], fk["options"].get("ondelete")) == ("interview_sessions", "CASCADE")
    assert insp.get_foreign_keys("provider_calls") == []  # still no link back to anyone


def _waiting_session(db: Session, **columns: object) -> str:
    sets = "".join(f", {k}" for k in columns)
    values = "".join(f", :{k}" for k in columns)
    return str(db.scalar(text(
        f"INSERT INTO interview_sessions (candidate_id, status, plan{sets}) "
        f"VALUES (:c, 'WAITING_FOR_ANSWER', CAST('{{}}' AS jsonb){values}) RETURNING id"),
        {"c": _candidate(db, f"{uuid.uuid4().hex}@example.com"), **columns}))


QUESTION = {"seq": 1, "topic_position": 1, "kind": "TOPIC", "followup_index": 0,
            "rephrase_index": 0, "text": "Explain an index.", "source": "RULES",
            "rules_reason": "NOT_REQUESTED", "anchor": None, "clock_at_ask": 0.0,
            "answer_kind": None, "answer_text": None, "answered_at": None,
            "evaluation_status": None}
ANSWERED = {"answer_kind": "ANSWER", "answer_text": "B-trees.",
            "answered_at": "2026-10-01T10:00Z", "evaluation_status": "NOT_EVALUATED"}


def _question(db: Session, session_id: str, **overrides: object) -> None:
    row = {**QUESTION, **overrides}
    with db.begin_nested():
        db.execute(text(
            f"INSERT INTO interview_questions (session_id, {', '.join(row)}) "
            f"VALUES (:session_id, {', '.join(':' + k for k in row)})"),
            {"session_id": session_id, **row})


@pytest.mark.parametrize(("bad", "constraint"), [
    ({"followup_index": 3, "kind": "FOLLOW_UP"}, "followup_index_range"),
    ({"rephrase_index": 3, "kind": "REPHRASE"}, "rephrase_index_range"),  # R9 backstop
    ({"kind": "TOPIC", "followup_index": 1}, "topic_has_no_followup_index"),
    ({"kind": "FOLLOW_UP", "followup_index": 0}, "follow_up_has_followup_index"),
    ({"kind": "REPHRASE", "rephrase_index": 0}, "rephrase_index_matches_kind"),
    ({"kind": "TOPIC", "rephrase_index": 1}, "rephrase_index_matches_kind"),
    ({"kind": "SCORE"}, "kind"),
    ({"text": "  "}, "text_not_blank"),
    ({"source": "AI"}, "rules_reason_matches_source"),
    ({"rules_reason": None}, "rules_reason_matches_source"),
    ({"rules_reason": "MODEL_SAID_SO"}, "rules_reason"),
    ({"anchor": "indexes"}, "anchor_only_on_ai_follow_ups"),
    ({"kind": "FOLLOW_UP", "followup_index": 1, "source": "AI", "rules_reason": None},
     "anchor_only_on_ai_follow_ups"),
    ({"clock_at_ask": -1}, "clock_at_ask_not_negative"),
    ({"seq": 0}, "seq_positive"),
    ({"topic_position": 0}, "topic_position_positive"),
    ({**ANSWERED, "evaluation_status": "SCORED"}, "evaluation_status"),  # E1
    ({**ANSWERED, "evaluation_status": None}, "answer_fields_together"),
    ({**ANSWERED, "answered_at": None}, "answer_fields_together"),
    ({**ANSWERED, "answer_kind": "HIRED"}, "answer_kind"),
    ({"answer_text": "orphan text"}, "answer_text_needs_kind"),
    ({**ANSWERED, "answer_text": "x" * 10_001}, "answer_text_length"),  # N9 backstop
])
def test_database_rejects_invalid_questions(db_session: Session, bad: dict,
                                            constraint: str) -> None:
    sid = _waiting_session(db_session)
    with pytest.raises(IntegrityError) as exc:
        _question(db_session, sid, **bad)
    assert exc.value.orig.diag.constraint_name == f"ck_interview_questions_{constraint}"


def test_database_accepts_valid_questions_and_the_answer_limit(db_session: Session) -> None:
    sid = _waiting_session(db_session)
    _question(db_session, sid, **{**ANSWERED, "answer_text": "é" * 10_000})  # code points
    _question(db_session, sid, seq=2, kind="REPHRASE", rephrase_index=2, **ANSWERED)
    _question(db_session, sid, seq=3, kind="FOLLOW_UP", followup_index=2, source="AI",
              rules_reason=None, anchor="indexes",
              **{**ANSWERED, "answer_kind": "SKIP", "answer_text": None})
    _question(db_session, sid, seq=4)  # the one open question


def test_one_open_question_and_unique_seq(db_session: Session) -> None:
    sid = _waiting_session(db_session)
    _question(db_session, sid)
    with pytest.raises(IntegrityError) as exc:
        _question(db_session, sid, seq=2)
    assert exc.value.orig.diag.constraint_name == "uq_interview_questions_one_open_per_session"
    with pytest.raises(IntegrityError) as exc:
        _question(db_session, sid, **ANSWERED)
    assert exc.value.orig.diag.constraint_name == "uq_interview_questions_session_id_seq"


@pytest.mark.parametrize(("status", "reason", "constraint"), [
    ("WAITING_FOR_ANSWER", "TIME_LIMIT", "ck_interview_sessions_finish_reason_only_when_finished"),
    ("NEXT_DECISION", "PLAN_COMPLETED", "ck_interview_sessions_finish_reason_only_when_finished"),
    ("REPORT_GENERATION", "MODEL_DECIDED", "ck_interview_sessions_finish_reason"),
    ("ASKING", "NO_ELIGIBLE_TOPIC", "ck_interview_sessions_finish_reason_only_when_finished"),
])
def test_finish_reason_is_checked(db_session: Session, status: str, reason: str,
                                  constraint: str) -> None:  # F3
    with pytest.raises(IntegrityError) as exc, db_session.begin_nested():
        _waiting_session(db_session, finish_reason=reason) if status == "WAITING_FOR_ANSWER" else \
            db_session.execute(text(
                "INSERT INTO interview_sessions (candidate_id, status, plan, finish_reason) "
                "VALUES (:c, :s, CAST('{}' AS jsonb), :r)"),
                {"c": _candidate(db_session), "s": status, "r": reason})
    assert exc.value.orig.diag.constraint_name == constraint


def test_finished_sessions_may_keep_no_reason_and_the_clock_cannot_go_negative(
    db_session: Session,
) -> None:
    for status in ("REPORT_GENERATION", "COMPLETED"):  # finished before Phase 7: NULL stays
        _session(db_session, status, "{}")
    with pytest.raises(IntegrityError) as exc, db_session.begin_nested():
        _waiting_session(db_session, active_seconds=-0.5)
    assert exc.value.orig.diag.constraint_name == \
        "ck_interview_sessions_active_seconds_not_negative"


def test_deletes_cascade_and_call_metadata_stays_unlinked(db_session: Session) -> None:
    sid = _waiting_session(db_session)
    call = db_session.scalar(text(
        CALL_COLUMNS + "VALUES ('r', 'QUESTION_GENERATION', 'groq', true, 1, false, 0) RETURNING id"))
    _question(db_session, sid, kind="FOLLOW_UP", followup_index=1, source="AI", rules_reason=None,
              anchor="indexes", provider_call_id=call)
    db_session.execute(text("INSERT INTO interview_consent_events (session_id, granted) "
                            "VALUES (:s, true)"), {"s": sid})
    db_session.execute(text("DELETE FROM provider_calls WHERE id = :p"), {"p": call})
    assert db_session.scalar(text("SELECT provider_call_id FROM interview_questions")) is None
    db_session.execute(text("DELETE FROM candidates"))
    for table in ("interview_questions", "interview_consent_events", "interview_sessions"):
        assert db_session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


@pytest.fixture
def committed_interview_rows(request: pytest.FixtureRequest) -> Iterator[None]:
    """A session with a question or a consent change, committed for real; cleaned up after."""
    with engine.begin() as conn:
        cid = conn.scalar(insert(Candidate).values(
            email=f"{uuid.uuid4().hex}@example.com", name="Downgrade").returning(Candidate.id))
        sid = conn.scalar(text("INSERT INTO interview_sessions (candidate_id, status, plan) "
                               "VALUES (:c, 'WAITING_FOR_ANSWER', CAST('{}' AS jsonb)) "
                               "RETURNING id"), {"c": cid})
        if request.param == "question":
            conn.execute(text(
                "INSERT INTO interview_questions (session_id, seq, topic_position, kind, "
                "followup_index, rephrase_index, text, source, rules_reason, clock_at_ask) "
                "VALUES (:s, 1, 1, 'TOPIC', 0, 0, 'Q?', 'RULES', 'NOT_REQUESTED', 0)"), {"s": sid})
        else:
            conn.execute(text("INSERT INTO interview_consent_events (session_id, granted) "
                              "VALUES (:s, true)"), {"s": sid})
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM candidates WHERE id = :c"), {"c": cid})


@pytest.mark.parametrize("committed_interview_rows", ["question", "consent"], indirect=True)
def test_interviewer_downgrade_refuses_while_rows_exist(committed_interview_rows: None) -> None:
    with pytest.raises(RuntimeError, match="Refusing to downgrade"):
        command.downgrade(Config(str(ALEMBIC_INI)), PLAN_REVISION)
    assert _current_heads() == {HEAD}
    assert "interview_questions" in inspect(engine).get_table_names()


def test_interviewer_revision_downgrades_and_re_upgrades_cleanly() -> None:
    cfg = Config(str(ALEMBIC_INI))
    with engine.begin() as conn:  # question-call metadata, which the old CHECK can't hold
        conn.execute(text(CALL_COLUMNS + "VALUES ('roundtrip-q', 'QUESTION_GENERATION', "
                                         "'groq', false, 1, false, 0)"))
    command.downgrade(cfg, PLAN_REVISION)
    try:
        insp = inspect(engine)
        assert not {"interview_questions", "interview_consent_events"} & set(insp.get_table_names())
        assert {c["name"] for c in insp.get_columns("interview_sessions")} == {
            "id", "candidate_id", "status", "created_at", "updated_at", "plan",
            "plan_provider_call_id"}
        with engine.connect() as conn:
            assert not conn.scalar(text(
                "SELECT count(*) FROM provider_calls WHERE request_id = 'roundtrip-q'"))
    finally:
        command.upgrade(cfg, "head")
    command.check(cfg)
    assert _current_heads() == {HEAD} == {LATEST_REVISION}


# ---------- NO_ELIGIBLE_TOPIC finish reason (Decision A) ----------

FINISH_REASON_REVISION = "3871903c395d"


def test_no_eligible_topic_is_a_stored_finish_reason(db_session: Session) -> None:
    for status in ("REPORT_GENERATION", "COMPLETED"):
        db_session.execute(text(
            "INSERT INTO interview_sessions (candidate_id, status, plan, finish_reason) "
            "VALUES (:c, :s, CAST('{}' AS jsonb), 'NO_ELIGIBLE_TOPIC')"),
            {"c": _candidate(db_session, f"{uuid.uuid4().hex}@example.com"), "s": status})


@pytest.fixture
def committed_no_eligible_topic() -> Iterator[None]:
    with engine.begin() as conn:
        cid = conn.scalar(insert(Candidate).values(
            email=f"{uuid.uuid4().hex}@example.com", name="Downgrade").returning(Candidate.id))
        conn.execute(text("INSERT INTO interview_sessions (candidate_id, status, plan, "
                          "finish_reason) VALUES (:c, 'REPORT_GENERATION', CAST('{}' AS jsonb), "
                          "'NO_ELIGIBLE_TOPIC')"), {"c": cid})
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM candidates WHERE id = :c"), {"c": cid})


def test_finish_reason_downgrade_refuses_while_a_session_uses_it(
    committed_no_eligible_topic: None,
) -> None:
    with pytest.raises(RuntimeError, match="Refusing to downgrade: 1 interview session"):
        command.downgrade(Config(str(ALEMBIC_INI)), INTERVIEWER_REVISION)
    assert _current_heads() == {HEAD}
    with engine.connect() as conn:  # nothing was changed or lost
        assert conn.scalar(text("SELECT count(*) FROM interview_sessions "
                                "WHERE finish_reason = 'NO_ELIGIBLE_TOPIC'")) == 1


def test_finish_reason_revision_downgrades_and_re_upgrades_cleanly() -> None:
    cfg = Config(str(ALEMBIC_INI))
    command.downgrade(cfg, INTERVIEWER_REVISION)
    try:
        assert _current_heads() == {INTERVIEWER_REVISION}
        with pytest.raises(IntegrityError) as exc, engine.begin() as conn:  # rolled back
            cid = conn.scalar(insert(Candidate).values(
                email=f"{uuid.uuid4().hex}@example.com", name="Old").returning(Candidate.id))
            conn.execute(text("INSERT INTO interview_sessions (candidate_id, status, plan, "
                              "finish_reason) VALUES (:c, 'REPORT_GENERATION', "
                              "CAST('{}' AS jsonb), 'NO_ELIGIBLE_TOPIC')"), {"c": cid})
        assert exc.value.orig.diag.constraint_name == "ck_interview_sessions_finish_reason"
    finally:
        command.upgrade(cfg, "head")
    command.check(cfg)
    assert _current_heads() == {HEAD} == {LATEST_REVISION}


# ---------- question text limit: 640 (approved 2026-10-02) ----------


@pytest.mark.parametrize(("length", "stored"), [(639, True), (640, True), (641, False)])
def test_question_text_column_limit(db_session: Session, length: int, stored: bool) -> None:
    sid = _waiting_session(db_session)
    question = "é" * (length - 1) + "?"  # code points, like the answer limit
    if stored:
        _question(db_session, sid, text=question)
        assert db_session.scalar(text("SELECT text FROM interview_questions "
                                      "WHERE session_id = :s"), {"s": sid}) == question
    else:
        with pytest.raises(DataError) as exc:
            _question(db_session, sid, text=question)
        assert "character varying(640)" in str(exc.value.orig)


# ---------- Phase 8: answer evaluations ----------

EVALUATOR_REVISION = "3a4785ecf08e"
EVALUATION_CHECKS = {f"ck_answer_evaluations_{n}" for n in (
    "rubric", "source", "rules_reason", "rules_reason_matches_source", "rubric_version_positive",
    "attempts_range", "word_count_not_negative", "feedback_length", "lists_are_arrays",
    "rules_has_no_model_content")}
CRITERION_CHECKS = {f"ck_answer_evaluation_criteria_{n}" for n in (
    "criterion", "score_range", "evidence_is_array", "score_needs_evidence")}


def test_evaluation_tables_schema() -> None:
    insp = inspect(engine)
    assert {c["name"] for c in insp.get_columns("answer_evaluations")} == {
        "question_id", "rubric", "rubric_version", "source", "rules_reason", "attempts",
        "word_count", "concepts_mentioned", "missing_concepts", "claims_to_verify", "feedback",
        "provider_call_id", "created_at", "updated_at"}  # no answer copy, no overall score
    assert {c["name"] for c in insp.get_check_constraints("answer_evaluations")} == \
        EVALUATION_CHECKS
    fks = {fk["referred_table"]: (fk["constrained_columns"], fk["options"].get("ondelete"))
           for fk in insp.get_foreign_keys("answer_evaluations")}
    assert fks == {"interview_questions": (["question_id"], "CASCADE"),
                   "provider_calls": (["provider_call_id"], "SET NULL")}
    assert insp.get_pk_constraint("answer_evaluations")["constrained_columns"] == ["question_id"]
    assert {c["name"] for c in insp.get_columns("answer_evaluation_criteria")} == {
        "question_id", "criterion", "score", "evidence"}
    assert {c["name"] for c in insp.get_check_constraints("answer_evaluation_criteria")} == \
        CRITERION_CHECKS
    [fk] = insp.get_foreign_keys("answer_evaluation_criteria")
    assert (fk["referred_table"], fk["options"].get("ondelete")) == ("answer_evaluations", "CASCADE")
    assert insp.get_pk_constraint("answer_evaluation_criteria")["constrained_columns"] == [
        "question_id", "criterion"]


EVALUATION = {"rubric": "TECHNICAL_PROJECT", "rubric_version": 1, "source": "AI",
              "rules_reason": None, "attempts": 1, "word_count": 12,
              "concepts_mentioned": "[]", "missing_concepts": "[]", "claims_to_verify": "[]",
              "feedback": "Clear structure."}
JSON_COLUMNS = ("concepts_mentioned", "missing_concepts", "claims_to_verify")


def _answered_question(db: Session) -> str:
    sid = _waiting_session(db)
    _question(db, sid, **ANSWERED)
    return str(db.scalar(text("SELECT id FROM interview_questions WHERE session_id = :s"),
                         {"s": sid}))


def _evaluation(db: Session, question_id: str, **overrides: object) -> None:
    row = {**EVALUATION, **overrides}
    values = [f"CAST(:{k} AS jsonb)" if k in JSON_COLUMNS else f":{k}" for k in row]
    with db.begin_nested():
        db.execute(text(f"INSERT INTO answer_evaluations (question_id, {', '.join(row)}) "
                        f"VALUES (:q, {', '.join(values)})"), {"q": question_id, **row})


def _criterion(db: Session, question_id: str, criterion: str = "relevance",
               score: int | None = 4, evidence: str = '["B-trees."]') -> None:
    with db.begin_nested():
        db.execute(text("INSERT INTO answer_evaluation_criteria (question_id, criterion, score, "
                        "evidence) VALUES (:q, :c, :s, CAST(:e AS jsonb))"),
                   {"q": question_id, "c": criterion, "s": score, "e": evidence})


@pytest.mark.parametrize(("bad", "constraint"), [
    ({"rubric": "OVERALL"}, "rubric"),
    ({"source": "MODEL", "rules_reason": "NOT_REQUESTED", "feedback": None}, "source"),
    ({"source": "RULES", "feedback": None}, "rules_reason_matches_source"),
    ({"rules_reason": "NOT_REQUESTED"}, "rules_reason_matches_source"),
    ({"rules_reason": "MODEL_SAID_SO", "source": "RULES", "feedback": None}, "rules_reason"),
    ({"rubric_version": 0}, "rubric_version_positive"),
    ({"attempts": 3}, "attempts_range"),
    ({"word_count": -1}, "word_count_not_negative"),
    ({"feedback": "x" * 601}, "feedback_length"),
    ({"claims_to_verify": '{"a": 1}'}, "lists_are_arrays"),
    ({"source": "RULES", "rules_reason": "NOT_REQUESTED"}, "rules_has_no_model_content"),
    ({"source": "RULES", "rules_reason": "NOT_REQUESTED", "feedback": None,
      "missing_concepts": '["indexes"]'}, "rules_has_no_model_content"),
])
def test_database_rejects_invalid_evaluations(db_session: Session, bad: dict,
                                              constraint: str) -> None:
    qid = _answered_question(db_session)
    with pytest.raises(IntegrityError) as exc:
        _evaluation(db_session, qid, **bad)
    assert exc.value.orig.diag.constraint_name == f"ck_answer_evaluations_{constraint}"


@pytest.mark.parametrize(("criterion", "score", "evidence", "constraint"), [
    ("unknown_criterion", 4, '["B-trees."]', "ck_answer_evaluation_criteria_criterion"),
    ("overall", 4, '["B-trees."]', "ck_answer_evaluation_criteria_criterion"),
    ("relevance", 0, '["B-trees."]', "ck_answer_evaluation_criteria_score_range"),
    ("relevance", 6, '["B-trees."]', "ck_answer_evaluation_criteria_score_range"),
    ("relevance", 4, "[]", "ck_answer_evaluation_criteria_score_needs_evidence"),
    ("relevance", None, '"B-trees."', "ck_answer_evaluation_criteria_evidence_is_array"),
])
def test_database_rejects_invalid_criteria(db_session: Session, criterion: str, score: object,
                                           evidence: str, constraint: str) -> None:
    qid = _answered_question(db_session)
    _evaluation(db_session, qid)
    with pytest.raises(IntegrityError) as exc:
        _criterion(db_session, qid, criterion, score, evidence)
    assert exc.value.orig.diag.constraint_name == constraint


def test_one_evaluation_per_answer_one_row_per_criterion_and_no_orphans(
    db_session: Session,
) -> None:
    qid = _answered_question(db_session)
    _evaluation(db_session, qid)
    _criterion(db_session, qid, "relevance", 1)  # the range's ends, and "insufficient evidence"
    _criterion(db_session, qid, "reasoning", 5)
    _criterion(db_session, qid, "completeness", None, "[]")
    for insert, constraint in [
        (lambda: _evaluation(db_session, qid), "pk_answer_evaluations"),
        (lambda: _criterion(db_session, qid), "pk_answer_evaluation_criteria"),
        (lambda: _evaluation(db_session, str(uuid.uuid4())),
         "fk_answer_evaluations_question_id_interview_questions"),
        (lambda: _criterion(db_session, _answered_question(db_session)),
         "fk_answer_evaluation_criteria_question_id_answer_evaluations"),
    ]:
        with pytest.raises(IntegrityError) as exc:
            insert()
        assert exc.value.orig.diag.constraint_name == constraint


def test_answer_statuses_widened_and_still_closed(db_session: Session) -> None:
    sid = _waiting_session(db_session)
    for seq, status in enumerate(("NOT_EVALUATED", "NOT_APPLICABLE", "OBSERVATIONS_ONLY",
                                  "EVALUATED"), 1):
        _question(db_session, sid, seq=seq, **{**ANSWERED, "evaluation_status": status})
    with pytest.raises(IntegrityError) as exc:
        _question(db_session, sid, seq=9, **{**ANSWERED, "evaluation_status": "SCORED"})
    assert exc.value.orig.diag.constraint_name == "ck_interview_questions_evaluation_status"


def test_evaluations_cascade_with_answers_sessions_and_candidates(db_session: Session) -> None:
    qid = _answered_question(db_session)
    call = db_session.scalar(text(
        CALL_COLUMNS + "VALUES ('r', 'ANSWER_EVALUATION', 'groq', true, 1, false, 0) RETURNING id"))
    _evaluation(db_session, qid, provider_call_id=call)
    _criterion(db_session, qid)
    db_session.execute(text("DELETE FROM provider_calls WHERE id = :p"), {"p": call})
    assert db_session.scalar(text("SELECT provider_call_id FROM answer_evaluations")) is None
    db_session.execute(text("DELETE FROM candidates"))
    for table in ("answer_evaluation_criteria", "answer_evaluations", "interview_questions"):
        assert db_session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


@pytest.fixture
def committed_evaluation_rows(request: pytest.FixtureRequest) -> Iterator[None]:
    """A finished answer with an evaluation row, or only a status, committed; cleaned up after."""
    with engine.begin() as conn:
        cid = conn.scalar(insert(Candidate).values(
            email=f"{uuid.uuid4().hex}@example.com", name="Downgrade").returning(Candidate.id))
        sid = conn.scalar(text("INSERT INTO interview_sessions (candidate_id, status, plan) "
                               "VALUES (:c, 'REPORT_GENERATION', CAST('{}' AS jsonb)) "
                               "RETURNING id"), {"c": cid})
        status = "NOT_APPLICABLE" if request.param == "status" else "OBSERVATIONS_ONLY"
        qid = conn.scalar(text(
            "INSERT INTO interview_questions (session_id, seq, topic_position, kind, "
            "followup_index, rephrase_index, text, source, rules_reason, clock_at_ask, "
            "answer_kind, answer_text, answered_at, evaluation_status) VALUES (:s, 1, 1, 'TOPIC', "
            "0, 0, 'Q?', 'RULES', 'NOT_REQUESTED', 0, 'ANSWER', 'A.', now(), :st) RETURNING id"),
            {"s": sid, "st": status})
        if request.param == "evaluation":
            conn.execute(text(
                "INSERT INTO answer_evaluations (question_id, rubric, rubric_version, source, "
                "rules_reason, attempts, word_count, concepts_mentioned, missing_concepts, "
                "claims_to_verify) VALUES (:q, 'TECHNICAL_PROJECT', 1, 'RULES', 'NOT_REQUESTED', "
                "1, 1, '[]', '[]', '[]')"), {"q": qid})
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM candidates WHERE id = :c"), {"c": cid})


@pytest.mark.parametrize("committed_evaluation_rows", ["evaluation", "status"], indirect=True)
def test_evaluator_downgrade_refuses_while_evaluations_exist(
    committed_evaluation_rows: None,
) -> None:
    with pytest.raises(RuntimeError, match="Refusing to downgrade"):
        command.downgrade(Config(str(ALEMBIC_INI)), FINISH_REASON_REVISION)
    assert _current_heads() == {HEAD}
    assert "answer_evaluations" in inspect(engine).get_table_names()


def test_evaluator_revision_downgrades_and_re_upgrades_cleanly() -> None:
    cfg = Config(str(ALEMBIC_INI))
    with engine.begin() as conn:  # evaluation-call metadata: the older CHECK cannot hold it
        conn.execute(text(CALL_COLUMNS + "VALUES ('roundtrip-e', 'ANSWER_EVALUATION', "
                                         "'groq', false, 1, false, 0)"))
    command.downgrade(cfg, FINISH_REASON_REVISION)
    try:
        assert _current_heads() == {FINISH_REASON_REVISION}
        insp = inspect(engine)
        assert not {"answer_evaluations", "answer_evaluation_criteria"} & set(insp.get_table_names())
        with engine.connect() as conn:
            assert not conn.scalar(text(
                "SELECT count(*) FROM provider_calls WHERE request_id = 'roundtrip-e'"))
    finally:
        command.upgrade(cfg, "head")
    command.check(cfg)
    assert _current_heads() == {HEAD} == {LATEST_REVISION}


# ---------- Phase 12: authentication ----------

AUTH_REVISION = "5c0e1d2f7a91"
TOKEN = "a" * 64  # stands in for a SHA-256 hex digest


def test_auth_schema() -> None:
    insp = inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("auth_sessions")}
    expected_cols = {
        "token_hash", "candidate_id", "auth_user_id", "encrypted_refresh_token",
        "access_token_expires_at", "created_at", "last_used_at", "expires_at",
        "absolute_expires_at", "refresh_locked_at", "refresh_lock_id", "revoked_at"
    }
    assert set(cols) == expected_cols
    nullable = {n for n, c in cols.items() if c["nullable"]}
    assert nullable == {"auth_user_id", "encrypted_refresh_token", "access_token_expires_at",
                        "refresh_locked_at", "refresh_lock_id", "revoked_at"}
    assert insp.get_pk_constraint("auth_sessions")["constrained_columns"] == ["token_hash"]
    fks = [(fk["constrained_columns"], fk["referred_table"], fk["options"].get("ondelete"))
           for fk in insp.get_foreign_keys("auth_sessions")]
    assert fks == [(["candidate_id"], "candidates", "CASCADE")]
    indexes = {i["name"]: i["column_names"] for i in insp.get_indexes("auth_sessions")}
    assert indexes["ix_auth_sessions_candidate_id"] == ["candidate_id"]
    assert indexes["ix_auth_sessions_auth_user_id"] == ["auth_user_id"]
    assert indexes["ix_auth_sessions_absolute_expires_at"] == ["absolute_expires_at"]
    assert {c["name"] for c in insp.get_check_constraints("auth_sessions")} == {
        "ck_auth_sessions_expires_after_created"}
    # No column could hold a raw token or a plaintext password.
    candidate_cols = {c["name"] for c in insp.get_columns("candidates")}
    assert not {"token", "password"} & (set(cols) | candidate_cols)


def _auth_session(db: Session, candidate_id: str, token: str = TOKEN, ttl: str = "1 day") -> None:
    db.execute(text("INSERT INTO auth_sessions (token_hash, candidate_id, expires_at) "
                    "VALUES (:t, :c, now() + CAST(:ttl AS interval))"),
               {"t": token, "c": candidate_id, "ttl": ttl})


def test_new_candidates_default_to_no_password_and_no_lock(db_session: Session) -> None:
    c = _candidate(db_session)
    assert tuple(db_session.execute(text(
        "SELECT password_hash, failed_logins, locked_until FROM candidates WHERE id = :c"),
        {"c": c}).one()) == (None, 0, None)


def test_database_rejects_invalid_auth_rows(db_session: Session) -> None:
    c = _candidate(db_session)
    _auth_session(db_session, c)
    cases = [
        (lambda: _auth_session(db_session, c), "pk_auth_sessions"),  # one row per token
        (lambda: _auth_session(db_session, str(uuid.uuid4()), "b" * 64),
         "fk_auth_sessions_candidate_id_candidates"),
        (lambda: _auth_session(db_session, c, "c" * 64, "-1 day"),
         "ck_auth_sessions_expires_after_created"),
        (lambda: db_session.execute(text("UPDATE candidates SET failed_logins = -1")),
         "ck_candidates_failed_logins_not_negative"),
    ]
    for write, constraint in cases:
        with pytest.raises(IntegrityError) as exc, db_session.begin_nested():
            write()
        assert exc.value.orig.diag.constraint_name == constraint


def test_deleting_candidate_deletes_their_auth_sessions(db_session: Session) -> None:
    keep, gone = _candidate(db_session, "keep@example.com"), _candidate(db_session)
    _auth_session(db_session, gone)
    _auth_session(db_session, gone, "b" * 64)
    _auth_session(db_session, keep, "c" * 64)
    db_session.execute(text("DELETE FROM candidates WHERE id = :c"), {"c": gone})
    left = db_session.scalars(text("SELECT candidate_id::text FROM auth_sessions")).all()
    assert left == [str(keep)]


@pytest.fixture
def committed_password() -> Iterator[None]:
    with engine.begin() as conn:
        cid = conn.scalar(text("INSERT INTO candidates (email, name, password_hash) "
                               "VALUES ('pw@example.com', 'P', 'scrypt$x') RETURNING id"))
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM candidates WHERE id = :c"), {"c": cid})


def test_auth_downgrade_refuses_while_passwords_exist(committed_password: None) -> None:
    with pytest.raises(RuntimeError, match="Refusing to downgrade: 1 candidate"):
        command.downgrade(Config(str(ALEMBIC_INI)), EVALUATOR_REVISION)
    assert _current_heads() == {HEAD} == {LATEST_REVISION}
    assert "auth_sessions" in inspect(engine).get_table_names()


def test_auth_revision_downgrades_and_re_upgrades_keeping_candidates() -> None:
    """Upgrade -> downgrade -> upgrade on a database holding a pre-auth candidate."""
    cfg = Config(str(ALEMBIC_INI))
    with engine.begin() as conn:
        cid = conn.scalar(text("INSERT INTO candidates (email, name) "
                               "VALUES ('kept@example.com', 'Kept') RETURNING id"))
        conn.execute(text("INSERT INTO auth_sessions (token_hash, candidate_id, expires_at) "
                          "VALUES (:t, :c, now() + interval '1 day')"), {"t": TOKEN, "c": cid})
    try:
        command.downgrade(cfg, EVALUATOR_REVISION)
        insp = inspect(engine)
        assert _current_heads() == {EVALUATOR_REVISION}
        assert "auth_sessions" not in insp.get_table_names()
        assert {c["name"] for c in insp.get_columns("candidates")} == {
            "id", "email", "name", "created_at"}
        command.upgrade(cfg, "head")
        command.check(cfg)
        assert _current_heads() == {HEAD} == {LATEST_REVISION}
        with engine.connect() as conn:
            # The candidate survived both directions, unchanged; sessions did not (signed out).
            assert tuple(conn.execute(text(
                "SELECT email, name, password_hash, failed_logins FROM candidates WHERE id = :c"),
                {"c": cid}).one()) == ("kept@example.com", "Kept", None, 0)
            assert conn.scalar(text("SELECT count(*) FROM auth_sessions")) == 0
            # Set auth_user_id to test active email uniqueness under uq_candidates_email_active
            conn.execute(text("UPDATE candidates SET auth_user_id = gen_random_uuid() WHERE id = :c"), {"c": cid})
            with pytest.raises(IntegrityError) as exc:  # unique email still enforced
                conn.execute(text("INSERT INTO candidates (email, name, auth_user_id) "
                                  "VALUES ('kept@example.com', 'Dup', gen_random_uuid())"))
            assert exc.value.orig.diag.constraint_name == "uq_candidates_email_active"
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM candidates WHERE id = :c"), {"c": cid})


# ---------- Phase 13: manual facts ----------

MANUAL_REVISION = "70b0b7891bc0"
PROVENANCE_CHECK = "ck_candidate_facts_provenance_matches_source"
MANUAL_COLUMNS = ("candidate_id, category, fact, extracted_fact, source, status, resume_id, "
                  "source_section, source_text, extracted_by")


def _manual(db: Session, candidate_id: str, fact: str = "Python", **overrides: object) -> None:
    row = {"c": candidate_id, "cat": "SKILL", "f": fact, "src": "MANUAL", "st": "APPROVED",
           "r": None, "sec": None, "txt": None, "by": None, **overrides}
    db.execute(text(f"INSERT INTO candidate_facts ({MANUAL_COLUMNS}) VALUES "
                    "(:c, :cat, :f, :f, :src, :st, :r, :sec, :txt, :by)"), row)


def test_manual_fact_schema() -> None:
    insp = inspect(engine)
    nullable = {c["name"] for c in insp.get_columns("candidate_facts") if c["nullable"]}
    assert nullable == {"resume_id", "source_section", "source_text", "extracted_by",
                        "provider_call_id"}
    indexes = {i["name"]: i for i in insp.get_indexes("candidate_facts")}
    index = indexes["uq_candidate_facts_manual_fact"]
    assert index["unique"]
    assert "MANUAL" in index["dialect_options"]["postgresql_where"]
    with engine.connect() as conn:
        definition = conn.scalar(text("SELECT indexdef FROM pg_indexes "
                                      "WHERE indexname = 'uq_candidate_facts_manual_fact'"))
    assert "lower((fact)::text)" in definition


def test_a_manual_fact_has_no_resume_provenance(db_session: Session) -> None:
    c = _candidate(db_session)
    resume = _resume(db_session, c)
    _manual(db_session, c)  # the valid shape
    for bad in [
        {"r": resume},  # manual, but pointing at a resume
        {"sec": "Skills"},
        {"txt": "Python"},
        {"by": "RULES"},
        {"st": "PENDING_REVIEW"},  # manual facts are approved for good (M1)
        {"st": "REJECTED"},
        {"src": "RESUME"},  # a resume fact without its provenance
        {"src": "RESUME", "r": resume, "sec": "Skills", "txt": "Python"},  # ...no extracted_by
    ]:
        with pytest.raises(IntegrityError) as exc, db_session.begin_nested():
            _manual(db_session, c, fact=f"Fact {len(str(bad))}", **bad)
        assert exc.value.orig.diag.constraint_name == PROVENANCE_CHECK, bad
    with pytest.raises(IntegrityError) as exc, db_session.begin_nested():
        _manual(db_session, c, fact="Typed", src="TYPED", r=resume, sec="Skills", txt="x",
                by="RULES")
    assert exc.value.orig.diag.constraint_name == "ck_candidate_facts_source"


def test_existing_resume_facts_are_unchanged_by_the_new_rules(db_session: Session) -> None:
    c = _candidate(db_session)
    fact = _fact(db_session, c, _resume(db_session, c))
    row = db_session.execute(text("SELECT source, resume_id IS NOT NULL, source_section, "
                                  "source_text, extracted_by FROM candidate_facts WHERE id = :f"),
                             {"f": fact}).one()
    assert row[0] == "RESUME" and row[1] and row[2] and row[3] and row[4] == "RULES"


def test_manual_duplicates_are_refused_by_the_database(db_session: Session) -> None:
    c, other = _candidate(db_session), _candidate(db_session, "other@example.com")
    _manual(db_session, c, "Python")
    for dup in ("Python", "PYTHON", "python"):
        with pytest.raises(IntegrityError) as exc, db_session.begin_nested():
            _manual(db_session, c, dup)
        assert exc.value.orig.diag.constraint_name == "uq_candidate_facts_manual_fact"
    _manual(db_session, c, "Python", cat="PROJECT")  # another category
    _manual(db_session, other, "Python")  # another candidate
    _fact(db_session, c, _resume(db_session, c), fact="Python")  # a resume fact is not indexed


def test_deleting_a_resume_keeps_manual_facts_and_deleting_the_candidate_removes_them(
    db_session: Session,
) -> None:
    c = _candidate(db_session)
    resume = _resume(db_session, c)
    _fact(db_session, c, resume)
    _manual(db_session, c)
    db_session.execute(text("DELETE FROM resumes WHERE id = :r"), {"r": resume})
    assert db_session.scalars(text("SELECT source FROM candidate_facts WHERE candidate_id = :c"),
                              {"c": c}).all() == ["MANUAL"]
    db_session.execute(text("DELETE FROM candidates WHERE id = :c"), {"c": c})
    assert db_session.scalar(text("SELECT count(*) FROM candidate_facts")) == 0


@pytest.fixture
def committed_manual_fact() -> Iterator[None]:
    with engine.begin() as conn:
        cid = conn.scalar(text("INSERT INTO candidates (email, name) "
                               "VALUES ('manual@example.com', 'M') RETURNING id"))
        conn.execute(text(f"INSERT INTO candidate_facts ({MANUAL_COLUMNS}) VALUES "
                          "(:c, 'SKILL', 'Go', 'Go', 'MANUAL', 'APPROVED', "
                          "NULL, NULL, NULL, NULL)"),
                     {"c": cid})
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM candidates WHERE id = :c"), {"c": cid})


def test_manual_downgrade_refuses_while_manual_facts_exist(committed_manual_fact: None) -> None:
    with pytest.raises(RuntimeError, match="Refusing to downgrade: 1 manual fact"):
        command.downgrade(Config(str(ALEMBIC_INI)), AUTH_REVISION)
    assert _current_heads() == {HEAD} == {LATEST_REVISION}


def test_manual_revision_downgrades_and_re_upgrades_keeping_resume_facts() -> None:
    """Upgrade -> downgrade -> upgrade on a database holding a Phase 12 resume fact."""
    cfg = Config(str(ALEMBIC_INI))
    with engine.begin() as conn:
        cid = conn.scalar(text("INSERT INTO candidates (email, name) "
                               "VALUES ('kept-fact@example.com', 'K') RETURNING id"))
        rid = conn.scalar(text("INSERT INTO resumes (candidate_id, parsed_json, upload_status) "
                               "VALUES (:c, '{}', 'PROCESSED') RETURNING id"), {"c": cid})
        conn.execute(text("INSERT INTO candidate_facts (candidate_id, resume_id, category, fact, "
                          "extracted_fact, source, source_section, source_text, status) VALUES "
                          "(:c, :r, 'SKILL', 'Rust', 'Rust', 'RESUME', 'Skills', 'Rust', "
                          "'APPROVED')"), {"c": cid, "r": rid})
    fact_row = "SELECT category, fact, source, source_section, extracted_by, status " \
               "FROM candidate_facts WHERE candidate_id = :c"
    try:
        command.downgrade(cfg, AUTH_REVISION)
        assert _current_heads() == {AUTH_REVISION}
        insp = inspect(engine)
        assert not any(c["nullable"] for c in insp.get_columns("candidate_facts")
                       if c["name"] in {"resume_id", "source_section", "source_text",
                                        "extracted_by"})
        command.upgrade(cfg, "head")
        command.check(cfg)
        assert _current_heads() == {HEAD} == {LATEST_REVISION}
        with engine.connect() as conn:
            assert tuple(conn.execute(text(fact_row), {"c": cid}).one()) == (
                "SKILL", "Rust", "RESUME", "Skills", "RULES", "APPROVED")
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM candidates WHERE id = :c"), {"c": cid})


# ---------- usage limits ----------

USAGE_REVISION = "9d2e6f1a7c40"


def test_usage_counters_schema() -> None:
    insp = inspect(engine)
    assert {c["name"] for c in insp.get_columns("usage_counters")} == {
        "scope", "key", "window_start", "count", "expires_at"}  # no address, no content
    assert insp.get_pk_constraint("usage_counters")["constrained_columns"] == [
        "scope", "key", "window_start"]
    assert insp.get_foreign_keys("usage_counters") == []  # deleting an account resets nothing
    assert {c["name"] for c in insp.get_check_constraints("usage_counters")} == {
        "ck_usage_counters_count_positive"}


def test_database_rejects_a_counter_below_one(db_session: Session) -> None:
    with pytest.raises(IntegrityError, match="ck_usage_counters_count_positive"),             db_session.begin_nested():
        db_session.execute(text("INSERT INTO usage_counters VALUES "
                                "('s', 'k', now(), 0, now() + interval '1 day')"))


def test_quota_reached_is_a_stored_rules_reason(db_session: Session) -> None:
    sid = _waiting_session(db_session)
    _question(db_session, sid, rules_reason="QUOTA_REACHED")
    question = _answered_question(db_session)
    _evaluation(db_session, question, source="RULES", rules_reason="QUOTA_REACHED", feedback=None)
    with pytest.raises(IntegrityError, match="ck_interview_questions_rules_reason"):
        _question(db_session, sid, seq=2, rules_reason="QUOTA_GONE")


@pytest.fixture
def committed_quota_question() -> Iterator[None]:
    with engine.begin() as conn:
        cid = conn.scalar(text("INSERT INTO candidates (email, name) "
                               "VALUES ('quota@example.com', 'Q') RETURNING id"))
        sid = conn.scalar(text("INSERT INTO interview_sessions (candidate_id, status, plan) "
                               "VALUES (:c, 'WAITING_FOR_ANSWER', '{}') RETURNING id"), {"c": cid})
        row = {**QUESTION, "rules_reason": "QUOTA_REACHED"}
        conn.execute(text(f"INSERT INTO interview_questions (session_id, {', '.join(row)}) "
                          f"VALUES (:session_id, {', '.join(':' + k for k in row)})"),
                     {"session_id": sid, **row})
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM candidates WHERE id = :c"), {"c": cid})


def test_usage_downgrade_refuses_while_quota_reached_is_stored(
    committed_quota_question: None
) -> None:
    with pytest.raises(RuntimeError, match="Refusing to downgrade: 1 question"):
        command.downgrade(Config(str(ALEMBIC_INI)), MANUAL_REVISION)
    assert _current_heads() == {HEAD} == {LATEST_REVISION}


def test_usage_revision_downgrades_and_re_upgrades_cleanly() -> None:
    cfg = Config(str(ALEMBIC_INI))
    try:
        command.downgrade(cfg, MANUAL_REVISION)
        assert _current_heads() == {MANUAL_REVISION}
        assert "usage_counters" not in inspect(engine).get_table_names()
        command.upgrade(cfg, "head")
        command.check(cfg)
        assert _current_heads() == {HEAD} == {LATEST_REVISION}
    finally:
        command.upgrade(cfg, "head")


# ---------- retry link (interview history) ----------

RETRY_LINK_REVISION = "b4c8e2d9f013"


def _completed_pair(db: Session) -> tuple[str, str]:
    """A completed interview and its retry, for one candidate."""
    cid = _candidate(db, f"{uuid.uuid4().hex}@example.com")
    source = str(db.scalar(text(
        "INSERT INTO interview_sessions (candidate_id, status, plan) "
        "VALUES (:c, 'COMPLETED', '{}') RETURNING id"), {"c": cid}))
    retry = str(db.scalar(text(
        "INSERT INTO interview_sessions (candidate_id, status, plan, retry_of_id) "
        "VALUES (:c, 'READY', '{}', :s) RETURNING id"), {"c": cid, "s": source}))
    return source, retry


def test_a_retry_keeps_its_link_until_the_source_is_deleted(db_session: Session) -> None:
    source, retry = _completed_pair(db_session)
    db_session.execute(text("DELETE FROM interview_sessions WHERE id = :s"), {"s": source})
    assert db_session.execute(text(
        "SELECT status, retry_of_id FROM interview_sessions WHERE id = :r"),
        {"r": retry}).one() == ("READY", None)


def test_a_session_cannot_retry_itself(db_session: Session) -> None:
    _, retry = _completed_pair(db_session)
    with pytest.raises(IntegrityError, match="ck_interview_sessions_retry_of_another_session"),             db_session.begin_nested():
        db_session.execute(text("UPDATE interview_sessions SET retry_of_id = id WHERE id = :r"),
                           {"r": retry})


@pytest.fixture
def committed_retry_link() -> Iterator[None]:
    with Session(engine) as db:
        source, _ = _completed_pair(db)
        cid = db.scalar(text("SELECT candidate_id FROM interview_sessions WHERE id = :s"),
                        {"s": source})
        db.commit()
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM candidates WHERE id = :c"), {"c": cid})


def test_retry_link_downgrade_refuses_while_a_retry_is_linked(committed_retry_link: None) -> None:
    with pytest.raises(RuntimeError, match="Refusing to downgrade: 1 interview session"):
        command.downgrade(Config(str(ALEMBIC_INI)), USAGE_REVISION)
    assert _current_heads() == {HEAD} == {LATEST_REVISION}


def test_retry_link_revision_downgrades_and_re_upgrades_cleanly() -> None:
    cfg = Config(str(ALEMBIC_INI))
    try:
        command.downgrade(cfg, USAGE_REVISION)
        assert "retry_of_id" not in {c["name"] for c in
                                     inspect(engine).get_columns("interview_sessions")}
        command.upgrade(cfg, "head")
        command.check(cfg)
        assert _current_heads() == {HEAD} == {LATEST_REVISION}
    finally:
        command.upgrade(cfg, "head")


# ---------- expression observations (optional camera) ----------


def test_expression_observations_schema_and_rules(db_session: Session) -> None:
    insp = inspect(engine)
    assert {c["name"] for c in insp.get_columns("question_expression_observations")} == {
        "question_id", "status", "neutral", "happy", "sad", "angry", "fearful", "disgusted",
        "surprised", "no_face", "multiple_faces", "updated_at"}  # counts only: no image, no time series
    [fk] = insp.get_foreign_keys("question_expression_observations")
    assert (fk["referred_table"], fk["options"].get("ondelete")) == ("interview_questions", "CASCADE")
    question = _answered_question(db_session)
    columns = "question_id, status, neutral, happy, sad, angry, fearful, disgusted, surprised, "               "no_face, multiple_faces"
    with pytest.raises(IntegrityError, match="unavailable_has_no_counts"), db_session.begin_nested():
        db_session.execute(text(f"INSERT INTO question_expression_observations ({columns}) "
                                "VALUES (:q, 'UNAVAILABLE', 1, 0, 0, 0, 0, 0, 0, 0, 0)"), {"q": question})
    with pytest.raises(IntegrityError, match="happy_range"), db_session.begin_nested():
        db_session.execute(text(f"INSERT INTO question_expression_observations ({columns}) "
                                "VALUES (:q, 'ANALYZED', 0, -1, 0, 0, 0, 0, 0, 0, 0)"), {"q": question})


def test_expression_revision_downgrades_and_re_upgrades_cleanly() -> None:
    cfg = Config(str(ALEMBIC_INI))
    try:
        command.downgrade(cfg, RETRY_LINK_REVISION)
        assert "question_expression_observations" not in inspect(engine).get_table_names()
        command.upgrade(cfg, "head")
        command.check(cfg)
        assert _current_heads() == {HEAD} == {LATEST_REVISION}
    finally:
        command.upgrade(cfg, "head")


def test_supabase_auth_and_cas_fencing_migrations_verify_schema() -> None:
    """Explicitly verifies Phase 2 (e8f3b2c1a0d9) and Phase 3 (f9a1b2c3d4e5) schema invariants."""
    insp = inspect(engine)
    candidates_cols = {c["name"]: c for c in insp.get_columns("candidates")}
    assert "auth_user_id" in candidates_cols
    assert candidates_cols["auth_user_id"]["nullable"] is True

    candidate_indexes = {i["name"]: i for i in insp.get_indexes("candidates")}
    assert "uq_candidates_email_active" in candidate_indexes
    assert candidate_indexes["uq_candidates_email_active"]["unique"] is True
    assert "uq_candidates_auth_user_id" in candidate_indexes
    assert candidate_indexes["uq_candidates_auth_user_id"]["unique"] is True

    auth_cols = {c["name"]: c for c in insp.get_columns("auth_sessions")}
    assert "refresh_lock_id" in auth_cols
    assert "refresh_locked_at" in auth_cols
    assert "encrypted_refresh_token" in auth_cols
    assert "revoked_at" in auth_cols
    assert "absolute_expires_at" in auth_cols

    auth_indexes = {i["name"]: i for i in insp.get_indexes("auth_sessions")}
    assert "ix_auth_sessions_auth_user_id" in auth_indexes
    assert "ix_auth_sessions_absolute_expires_at" in auth_indexes
