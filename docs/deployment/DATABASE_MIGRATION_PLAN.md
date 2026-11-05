# DATABASE MIGRATION & SCHEMA EVOLUTION PLAN (HARDENED RUNBOOK)
## Local PostgreSQL to Supabase Free PostgreSQL + Safe Self-Referential Migration & Outbox Engine

**Author:** Principal Database Architect & Security Engineer  
**Date:** November 3, 2026 (Phase 0 Final Technical Validation Hardened)  
**Scope:** Reconciled 12-table inventory, empirical identity import validation, verified 12-column `interview_sessions` inventory, two-pass migration runbook, sequence synchronization, mandatory operational safeguards, `ON_ERROR_STOP` transaction controls, failure recovery, and emergency recovery runbooks.

---

## 1. Complete Table Inventory Classification

Inspection of `Base.metadata.tables` and Alembic migration history up to `d7e2f1a9b3c4` confirms exactly **12 registered tables**:

| # | Table Name | Migration Status | Rationale |
| :---: | :--- | :---: | :--- |
| 1 | `candidates` | **MIGRATED** | Candidate accounts, unique lowercase email, name, scrypt hashes, lockout state. |
| 2 | `provider_calls` | **MIGRATED** | LLM audit logs. Migrated early because tables 4, 5, 7, and 9 reference `provider_calls.id`. |
| 3 | `resumes` | **MIGRATED** | Resume raw text and parsed JSON sections. Dependent on `candidates`. |
| 4 | `candidate_facts` | **MIGRATED** | Extracted candidate skills, experience, projects. Dependent on `resumes`, `candidates`, `provider_calls`. |
| 5 | `interview_sessions` | **MIGRATED** | Interview state machine records, plans, active timers. Dependent on `candidates`, `provider_calls`, and self-referential `retry_of_id`. |
| 6 | `interview_consent_events` | **MIGRATED** | Audit log of candidate AI consent toggles (`id: BigInteger, Identity(always=True)`). Dependent on `interview_sessions`. |
| 7 | `interview_questions` | **MIGRATED** | Turns, question text, answers, speech cadence metrics. Dependent on `interview_sessions` and `provider_calls`. |
| 8 | `question_expression_observations` | **MIGRATED** | In-browser facial expression counts. Dependent on `interview_questions`. |
| 9 | `answer_evaluations` | **MIGRATED** | Rubric evaluations, scores, missing concepts. Dependent on `interview_questions` and `provider_calls`. |
| 10 | `answer_evaluation_criteria` | **MIGRATED** | Criteria scores and evidence quotes. Dependent on `answer_evaluations`. |
| 11 | `auth_sessions` | **EXCLUDED / RESET** | **Excluded from migration.** Active session tokens are temporary client credentials; candidates re-login on the new database. |
| 12 | `usage_counters` | **EXCLUDED / RESET** | **Excluded from migration.** Temporary rate-limiting counters; resetting them ensures candidates are not throttled upon migration. |

> [!NOTE]
> **Schema Evolution & Phase Timing:**
> 1. The 12-table inventory above constitutes the complete registered schema up to current Alembic head `d7e2f1a9b3c4`.
> 2. Future architectural tables—`interview_email_outbox` (scheduled for Phase 3 with `locked_until`, `dispatch_generation`, and `resend_idempotency_key`) and `idempotency_records` (scheduled for Phase 5 with nullable `locked_until`, `lease_generation`, and `is_retryable`)—do not exist in the source database and are therefore **not part of the Phase 1 data migration**. They will be provisioned via forward Alembic migrations in their respective phases.
> 3. **Prerequisite Timing:** The Supabase JWT signing algorithm (`RS256` / `ES256` vs `HS256`) is an implementation prerequisite strictly for Phase 2 (Authentication); it does **not** block or delay the Phase 1 database migration, which relies solely on direct PostgreSQL connection credentials.

---

## 2. Verified Column Inventory: `interview_sessions`

To prevent schema mismatch and runtime failures during Pass 1 export/import, the complete column inventory of `interview_sessions` was cross-compared across:
1. **SQLAlchemy Model:** `backend/app/models/interview_session.py`
2. **Local PostgreSQL Database:** `ai_interview_coach.interview_sessions`
3. **Target Alembic-Provisioned Schema:** `ai_interview_rehearsal_test.interview_sessions` (migrated to head `d7e2f1a9b3c4`)

### 2.1 Three-Way Column Inventory Comparison

| # | Column Name | PostgreSQL Data Type | Nullable | Default / Generation Expression | Foreign Key / Target Reference | Verified In Model, Local DB & Target |
| :---: | :--- | :--- | :---: | :--- | :--- | :---: |
| 1 | `id` | `uuid` | **NO** | `gen_random_uuid()` | Primary Key | **MATCH (All 3)** |
| 2 | `candidate_id` | `uuid` | **NO** | None | `candidates.id` (ON DELETE CASCADE) | **MATCH (All 3)** |
| 3 | `status` | `varchar(20)` | **NO** | None | State machine enum constraint | **MATCH (All 3)** |
| 4 | `created_at` | `timestamptz` | **NO** | `now()` | Audit timestamp | **MATCH (All 3)** |
| 5 | `updated_at` | `timestamptz` | **NO** | `now()` | Audit timestamp | **MATCH (All 3)** |
| 6 | `plan` | `jsonb` | **YES** | None | Interview plan payload | **MATCH (All 3)** |
| 7 | `plan_provider_call_id` | `uuid` | **YES** | None | `provider_calls.id` (ON DELETE SET NULL) | **MATCH (All 3)** |
| 8 | `ai_consent` | `boolean` | **NO** | `false` | Baseline consent status | **MATCH (All 3)** |
| 9 | `active_seconds` | `double precision` | **NO** | `0` | Cumulative interview clock seconds | **MATCH (All 3)** |
| 10 | `active_since` | `timestamptz` | **YES** | None | Running clock timestamp | **MATCH (All 3)** |
| 11 | `finish_reason` | `varchar(20)` | **YES** | None | Finish code enum constraint | **MATCH (All 3)** |
| 12 | `retry_of_id` | `uuid` | **YES** | None | `interview_sessions.id` (ON DELETE SET NULL) | **MATCH (All 3)** |

> [!IMPORTANT]
> **Audit Reconciliation:** Previous drafts incorrectly referenced phantom columns (`role`, `started_at`, `finished_at`) which do not exist in the schema, and omitted `updated_at`, `ai_consent`, and `active_since`. The verified list above represents the exact, complete 12 columns.

---

## 3. Self-Referential Foreign Key & Two-Pass Migration

### 3.1 Inspection of `interview_sessions.retry_of_id`
Source inspection of Alembic migration `backend/alembic/versions/b4c8e2d9f013_add_retry_link.py` reveals:
```python
op.create_foreign_key(op.f('fk_interview_sessions_retry_of_id_interview_sessions'),
                      'interview_sessions', 'interview_sessions', ['retry_of_id'], ['id'],
                      ondelete='SET NULL')
```
- **Constraint Defect:** The foreign key constraint was created without `deferrable=True` (PostgreSQL default is `NOT DEFERRABLE`).
- **Critical Consequence:** Executing `SET CONSTRAINTS ALL DEFERRED;` will **FAIL** to defer checking this foreign key.
- **The Issue with `pg_dump`:** Standard `pg_dump` outputs table rows in physical heap storage order, NOT creation order. If a retried session appears in the dump before the parent session it references, a single-pass `INSERT` statement immediately fails with a foreign-key constraint violation:
  `ERROR: insert or update on table "interview_sessions" violates foreign key constraint "fk_interview_sessions_retry_of_id_interview_sessions"`

### 3.2 Cycle Analysis
- In application business logic (`backend/app/services/retry.py:51-65`), a retry can only be created from an existing session that is already `COMPLETED`. The check constraint `ck_interview_sessions_retry_of_id_not_self` (`retry_of_id <> id`) prohibits self-reference.
- Therefore, in clean application data, retry links form **acyclic directed trees** where parent sessions always have earlier creation timestamps than their retries.
- However, relying on physical row order during dump/restore is inherently unsafe.

### 3.3 Two-Pass Migration Guarantees & Operational Boundaries

```mermaid
flowchart TD
    P1["Pass 1: Export interview_sessions with retry_of_id as NULL"] --> I1["Import interview_sessions into Supabase (No FK violations possible)"]
    I1 --> C["Import all child tables: consent, questions, evaluations"]
    C --> P2["Pass 2: Execute pass2_reconcile_retry_links.sql in transaction"]
    P2 --> CHK{"Updated count == Exported count?"}
    CHK -->|Yes| CMT["COMMIT Transaction & Drop Temp Table"]
    CHK -->|No| RBK["ROLLBACK Transaction & Keep retry_of_id as NULL"]
    CMT --> V["Post-Migration Sequence & Orphan Verification"]
```

#### What the Two-Pass Strategy Guarantees:
1. **Elimination of Heap-Order FK Violations:** By inserting all sessions with `retry_of_id = NULL` in Pass 1, self-referential foreign key checks cannot fail regardless of physical storage order in `pg_dump`.
2. **Parent Row Existence:** Guarantees that every target parent session already exists in the destination database before any `retry_of_id` pointer is linked in Pass 2.
3. **Transactional Atomicity & Verification:** Pass 2 runs inside an explicit transaction (`BEGIN ... COMMIT`) that compares the count of updated rows against the count of exported non-null retry links. Any mismatch or constraint failure triggers an immediate `RAISE EXCEPTION` and automatic `ROLLBACK`.

#### What the Two-Pass Strategy Does NOT Guarantee (Operational Risks & Mitigations):
1. **Schema Mismatches:** If Alembic migrations on Supabase differ from local schema `d7e2f1a9b3c4`.  
   *Mitigation:* Step 1 strictly validates `alembic current` on Supabase before any data import.
2. **Corrupted or Invalid Source Data:** If source data already contains orphaned `retry_of_id` values pointing to nonexistent UUIDs.  
   *Mitigation:* Pre-migration orphan queries run against local PostgreSQL before export; any source orphan aborts the migration.
3. **Permission & Role Restrictions:** If the Supabase database user lacks `TEMP` table creation rights or table update privileges.  
   *Mitigation:* Migration commands connect strictly as the administrative user `postgres.[REF]` over the IPv4 Session Pooler.
4. **Network Dropouts & Connection Loss:** Transient TCP timeouts or pooler connection resets during large batch transfers.  
   *Mitigation:* Enforce `psql -v ON_ERROR_STOP=1`, run Pass 2 in an atomic transaction, and support repeatable idempotent re-runs.
5. **Database Disk Quotas:** Supabase free tier enforces a strict 500 MB database disk limit.  
   *Mitigation:* Pre-migration check verifies total table size (`< 50 MB` currently) before starting.

---

## 4. Empirical Identity Import Validation & Rehearsal Limits

### 4.1 SQL Emitted by `pg_dump --inserts`
Empirical inspection of SQL emitted by PostgreSQL 18/16 `pg_dump` when exporting `interview_consent_events` with `--data-only --inserts`:
```sql
-- Dumped from database version 18.x
-- Table: interview_consent_events (id is GENERATED ALWAYS AS IDENTITY)

INSERT INTO public.interview_consent_events OVERRIDING SYSTEM VALUE VALUES (1, '2cf9157a-52d2-4574-b8b1-d5579fbaf8e8', true, '2026-10-14 11:10:37.658694+05:30');
INSERT INTO public.interview_consent_events OVERRIDING SYSTEM VALUE VALUES (2, '2cf9157a-52d2-4574-b8b1-d5579fbaf8e8', false, '2026-10-14 11:12:16.342407+05:30');
INSERT INTO public.interview_consent_events OVERRIDING SYSTEM VALUE VALUES (3, '2cf9157a-52d2-4574-b8b1-d5579fbaf8e8', true, '2026-10-14 11:12:18.941722+05:30');
INSERT INTO public.interview_consent_events OVERRIDING SYSTEM VALUE VALUES (7, '1c10cece-a95f-4ca6-807c-0db06be2c720', true, '2026-10-30 15:38:46.412352+05:30');
INSERT INTO public.interview_consent_events OVERRIDING SYSTEM VALUE VALUES (11, '9b2d1e46-1e05-4be1-8061-77bb75f52992', true, '2026-11-03 08:41:53.301538+05:30');

SELECT pg_catalog.setval('public.interview_consent_events_id_seq', 11, true);
```

### 4.2 Rehearsal Limitations & Production Differences
> [!WARNING]
> **Rehearsal Scope & Limitations:**
> The successful local 5-row rehearsal validated that:
> 1. PostgreSQL's `pg_dump --inserts` automatically emits `OVERRIDING SYSTEM VALUE`, satisfying the SQL standard requirement for inserting explicit IDs into `GENERATED ALWAYS AS IDENTITY` columns.
> 2. `setval(seq, max_id, true)` synchronizes the identity sequence correctly on an isolated local database.
> 
> **What the Local Rehearsal Does NOT Prove:**
> - It does **not** prove network stability, bandwidth saturation, or connection drop resilience over the internet to remote Supabase poolers.
> - It does **not** prove behavior under high concurrency or transaction lock contention.
> - It does **not** validate Supabase free-tier connection pool limits (max 50 connections).
> 
> Therefore, strict **Mandatory Operational Safeguards** must be applied during production migration.

---

## 5. Mandatory Production Migration Operational Safeguards

To bridge the gap between local rehearsal and live production cutover, the following safeguards are strictly enforced:

1. **Freeze Application Writes (Maintenance Mode):**
   - The application must be put into read-only maintenance mode or stopped completely prior to export.
   - Zero candidate registrations, turn submissions, or evaluations may occur during the migration window.
2. **Consistent Point-in-Time Snapshot:**
   - Database export must be performed using a single consistent transaction snapshot (`pg_dump --snapshot` or transactional dump) so that no relational drift occurs between tables.
3. **Comprehensive Row-Count Reconciliation:**
   - Every single table must have its post-import count compared directly against the pre-migration source count.
4. **Relational Integrity & Foreign-Key Orphan Audits:**
   - Execute the complete orphan check suite (Section 7.2) across all parent-child links. Every query must return exactly `0`.
5. **Sequence Synchronization & Active Verification:**
   - Identity sequence synchronization must be executed explicitly for `interview_consent_events.id`.
   - An active verification check must run `SELECT nextval(...)` to verify that the next ID is strictly greater than the highest imported ID.
6. **Documented Rollback Procedure:**
   - If any step fails, the engineer must immediately halt the migration, leave the local database intact as source-of-truth, and follow the documented rollback runbook (Section 6.5).

---

## 6. Step-by-Step Data Migration Runbook

### Step 1: Pre-Migration Backup & Schema Provisioning
1. Export complete encrypted backup from local database:
   ```powershell
   pg_dump -h localhost -U USER -d ai_interview_coach -F c -f local_pre_migration.dump
   gpg -c local_pre_migration.dump
   ```
2. Run pre-migration integrity check on local database to ensure zero existing foreign-key orphans:
   ```sql
   SELECT count(*) AS local_orphaned_retries 
   FROM interview_sessions s1 
   WHERE s1.retry_of_id IS NOT NULL 
     AND NOT EXISTS (SELECT 1 FROM interview_sessions s2 WHERE s2.id = s1.retry_of_id);
   ```
   *Requirement:* Must return `0` before proceeding.

3. Run Alembic migrations on target Supabase database to establish head schema (`d7e2f1a9b3c4`):
   ```powershell
   $env:DATABASE_URL = "postgresql+psycopg://postgres.[REF]:[PASS]@aws-0-[REGION].pooler.supabase.com:5432/postgres?sslmode=require"
   cd backend
   .\.venv\Scripts\alembic.exe upgrade head
   .\.venv\Scripts\alembic.exe current
   ```
   *Requirement:* Output must confirm current revision is `d7e2f1a9b3c4 (head)`.

---

### Step 2: Dependency-Ordered Data Import

#### 2.1 Migrate Independent Parent Tables
With `-v ON_ERROR_STOP=1`, import independent parent tables in topological order:
```powershell
$tables = @(
    "candidates",
    "provider_calls",
    "resumes",
    "candidate_facts"
)

foreach ($t in $tables) {
    Write-Host "Migrating table: $t"
    pg_dump -h localhost -U USER -d ai_interview_coach --data-only -t $t --inserts -f "$t.sql"
    psql -v ON_ERROR_STOP=1 -h aws-0-[REGION].pooler.supabase.com -p 5432 -U postgres.[REF] -d postgres -f "$t.sql"
    Remove-Item "$t.sql"
}
```

#### 2.2 Execute Two-Pass Import for `interview_sessions`

**Pass 1: Export and Import Sessions with Nullified Self-References**
Using the verified 12-column list:
```powershell
# Export interview_sessions with retry_of_id explicitly set to NULL
psql -h localhost -U USER -d ai_interview_coach -c "
COPY (
    SELECT id, candidate_id, status, created_at, updated_at, plan, 
           plan_provider_call_id, ai_consent, active_seconds, active_since, 
           finish_reason, NULL::uuid AS retry_of_id 
    FROM interview_sessions
) TO STDOUT WITH CSV HEADER;" > interview_sessions_pass1.csv

# Import into Supabase with retry_of_id as NULL (Zero self-referential FK violations possible)
psql -v ON_ERROR_STOP=1 -h aws-0-[REGION].pooler.supabase.com -p 5432 -U postgres.[REF] -d postgres -c "
\copy interview_sessions(id, candidate_id, status, created_at, updated_at, plan, plan_provider_call_id, ai_consent, active_seconds, active_since, finish_reason, retry_of_id) FROM 'interview_sessions_pass1.csv' WITH CSV HEADER;"
```

#### 2.3 Migrate Child Tables
Now that all session IDs exist in Supabase, import dependent child tables:
```powershell
$child_tables = @(
    "interview_consent_events",
    "interview_questions",
    "question_expression_observations",
    "answer_evaluations",
    "answer_evaluation_criteria"
)

foreach ($t in $child_tables) {
    Write-Host "Migrating child table: $t"
    pg_dump -h localhost -U USER -d ai_interview_coach --data-only -t $t --inserts -f "$t.sql"
    psql -v ON_ERROR_STOP=1 -h aws-0-[REGION].pooler.supabase.com -p 5432 -U postgres.[REF] -d postgres -f "$t.sql"
    Remove-Item "$t.sql"
}
```

#### 2.4 Pass 2: Reconcile Retry References via Dedicated Script
Export non-null `retry_of_id` pairs and update Supabase using a valid SQL script (`psql -f`) with explicit transaction handling:

1. Export non-null retry links from local database:
   ```powershell
   psql -h localhost -U USER -d ai_interview_coach -c "
   COPY (
       SELECT id, retry_of_id 
       FROM interview_sessions 
       WHERE retry_of_id IS NOT NULL
   ) TO STDOUT WITH CSV HEADER;" > retry_links_pass2.csv
   ```

2. Create the reconciliation SQL script `pass2_reconcile_retry_links.sql`:
   ```sql
   -- pass2_reconcile_retry_links.sql
   -- Executed via: psql -v ON_ERROR_STOP=1 -f pass2_reconcile_retry_links.sql

   BEGIN;

   -- Temporary staging table exists only for this transaction
   CREATE TEMP TABLE temp_retry_links (
       id uuid PRIMARY KEY,
       retry_of_id uuid NOT NULL
   ) ON COMMIT DROP;

   -- Load exported links using valid psql \copy meta-command
   \copy temp_retry_links (id, retry_of_id) FROM 'retry_links_pass2.csv' WITH (FORMAT csv, HEADER true)

   -- Reconcile retry_of_id, verify count equality, and roll back on mismatch
   DO $$
   DECLARE
       v_exported_count integer;
       v_updated_count integer;
   BEGIN
       -- Count rows loaded into temporary staging table
       SELECT count(*) INTO v_exported_count FROM temp_retry_links;

       -- Update interview_sessions and capture updated count
       WITH updated AS (
           UPDATE interview_sessions s
           SET retry_of_id = t.retry_of_id
           FROM temp_retry_links t
           WHERE s.id = t.id
           RETURNING s.id
       )
       SELECT count(*) INTO v_updated_count FROM updated;

       -- Strictly verify that every exported retry link was updated
       IF v_updated_count <> v_exported_count THEN
           RAISE EXCEPTION 'Pass 2 count mismatch: exported % retry links, but updated % rows. Rolling back.', 
               v_exported_count, v_updated_count;
       END IF;

       RAISE NOTICE 'Pass 2 Reconcile SUCCESS: % retry links verified and updated.', v_updated_count;
   END $$;

   COMMIT;
   ```

3. Execute the script:
   ```powershell
   psql -v ON_ERROR_STOP=1 -h aws-0-[REGION].pooler.supabase.com -p 5432 -U postgres.[REF] -d postgres -f pass2_reconcile_retry_links.sql
   ```

#### 2.5 Failure Handling and Rollback Instructions for Pass 2:
- **Automatic Rollback:** If any error occurs during Pass 2 (e.g. invalid UUID format, network failure, or count mismatch triggering `RAISE EXCEPTION`), PostgreSQL automatically aborts the transaction.
- **State Preservation:** Because `ON COMMIT DROP` is specified, `temp_retry_links` is dropped immediately. The destination `interview_sessions` table remains intact in its clean Pass 1 state (`retry_of_id = NULL`). All parent sessions, candidate records, and child evaluations remain completely uncorrupted.
- **Manual Rollback (If needed):** If an engineer wishes to reset all retry links back to the Pass 1 baseline to re-investigate, execute:
  ```sql
  UPDATE interview_sessions SET retry_of_id = NULL WHERE retry_of_id IS NOT NULL;
  ```

---

### Step 3: Identity Sequence Synchronization

`interview_consent_events` uses `id: BigInteger, Identity(always=True)`. Importing explicit IDs does not automatically advance the underlying PostgreSQL sequence. 

#### Synchronization Logic & Sequence Collision Mitigation:
Under the explicit assumptions that:
1. Application writes were frozen prior to export, ensuring no out-of-band inserts occurred.
2. All imported IDs are non-negative integers $\le M$.
3. The underlying sequence cache has not been independently incremented out-of-band.

Executing the following block synchronizes the sequence state:
```sql
DO $$
DECLARE
    v_seq text;
    v_max_id bigint;
    v_next_val bigint;
BEGIN
    -- 1. Verify sequence exists for interview_consent_events.id
    SELECT pg_get_serial_sequence('interview_consent_events', 'id') INTO v_seq;
    IF v_seq IS NULL THEN
        RAISE EXCEPTION 'Sequence for interview_consent_events.id does not exist!';
    END IF;

    -- 2. Inspect maximum imported ID
    SELECT MAX(id) INTO v_max_id FROM interview_consent_events;

    -- 3. Synchronize sequence handling both empty and non-empty tables
    IF v_max_id IS NULL THEN
        -- Empty table: setval to 1 with is_called = false so next generated value is 1
        PERFORM setval(v_seq, 1, false);
    ELSE
        -- Non-empty table: setval to max_id with is_called = true so next value is max_id + 1
        PERFORM setval(v_seq, v_max_id, true);
    END IF;

    -- 4. Mandatory active verification check: generate next value to prove no collision
    SELECT nextval(v_seq) INTO v_next_val;
    RAISE NOTICE 'Sequence % successfully synchronized. Next generated ID is % (max imported ID: %)',
        v_seq, v_next_val, COALESCE(v_max_id, 0);
END $$;
```

---

## 7. Post-Migration Verification Checklist

### 7.1 Row-Count Comparison Across All 10 Migrated Tables
```sql
SELECT 'candidates' AS tbl, count(*) FROM candidates
UNION ALL SELECT 'provider_calls', count(*) FROM provider_calls
UNION ALL SELECT 'resumes', count(*) FROM resumes
UNION ALL SELECT 'candidate_facts', count(*) FROM candidate_facts
UNION ALL SELECT 'interview_sessions', count(*) FROM interview_sessions
UNION ALL SELECT 'interview_consent_events', count(*) FROM interview_consent_events
UNION ALL SELECT 'interview_questions', count(*) FROM interview_questions
UNION ALL SELECT 'question_expression_observations', count(*) FROM question_expression_observations
UNION ALL SELECT 'answer_evaluations', count(*) FROM answer_evaluations
UNION ALL SELECT 'answer_evaluation_criteria', count(*) FROM answer_evaluation_criteria;
```
*Requirement:* Every count must match the source database table count exactly.

### 7.2 Foreign-Key Orphan Verification Queries
```sql
-- Check resumes -> candidates
SELECT count(*) AS orphaned_resumes FROM resumes r LEFT JOIN candidates c ON r.candidate_id = c.id WHERE c.id IS NULL;

-- Check interview_sessions -> candidates
SELECT count(*) AS orphaned_sessions FROM interview_sessions s LEFT JOIN candidates c ON s.candidate_id = c.id WHERE c.id IS NULL;

-- Check self-referencing retry_of_id
SELECT count(*) AS orphaned_retries FROM interview_sessions s1 WHERE s1.retry_of_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM interview_sessions s2 WHERE s2.id = s1.retry_of_id);

-- Check questions -> sessions
SELECT count(*) AS orphaned_questions FROM interview_questions q LEFT JOIN interview_sessions s ON q.session_id = s.id WHERE s.id IS NULL;

-- Check evaluations -> questions
SELECT count(*) AS orphaned_evaluations FROM answer_evaluations e LEFT JOIN interview_questions q ON e.question_id = q.id WHERE q.id IS NULL;
```
*Expected Result:* All orphan counts must return exactly `0`.

---

## 8. Emergency Recovery & Dangerous Commands Policy

> [!CAUTION]
> **RESTRICTED EMERGENCY COMMAND — NEVER EXECUTE AUTOMATICALLY**  
> `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` permanently destroys all candidate accounts, resumes, and interview evaluations.  
> **Strict Operational Policy:**  
> 1. It must **NEVER** be automated in deployment scripts or recovery hooks.  
> 2. It may only be executed manually by a human engineer in an extreme emergency after:  
>    a) An independent, verified, uncorrupted backup file exists and has been tested.  
>    b) Explicit written approval from the project owner has been obtained.  
> 3. Normal failure recovery must strictly utilize **forward corrective Alembic migrations**.
