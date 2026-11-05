# Phase 9 — Report Generation: Final Plan (revision 2)

Status: **approved (2026-10-02); implemented as specified (§9 lists the implementation notes).**
- R1–R18 were approved as recommended.
- C-R1–C-R9 are resolved as recommended.
- Revision 1 (`1c4bb06`) was the draft. This revision changes only what §0 lists.

## 0. Approval and changes from revision 1

The approval message takes precedence over this draft where they differ. None of the changes
contradicts an approved decision.

| # | Revision 1 | Revision 2 | Source |
|---|---|---|---|
| V1 | `report_version: 1` in the response (R18) | **Removed.** No report version, snapshot or history of any kind. | Approval R7, R18 |
| V2 | `generated_at` (the time of the read) in the response (R8) | **Removed.** Repeated reads of the same data return identical content. | Approval R8, §7 |
| V3 | Retry section absent (R1, C-R6) | **Present and explicit:** `retry: {available: false, note}`, and likewise `speech_metrics` under communication observations | Approval §4: "represent that explicitly rather than inventing data" |
| V4 | R15: "The fixed text passes the evaluator's `HIRING` … checks" | Every fixed sentence passes them **except the §6 limitations notice**. That notice deliberately says "not a hiring assessment", "Scores", "correctness" and "wrong". It is tested to equal the approved text exactly. | Internal inconsistency of revision 1, §5.3 vs §6 |
| V5 | Summary "started/finished time" (R1) | **Active time only** (`active_seconds`). No finish timestamp is stored: `updated_at` changes again at `REPORT_READY`, and nothing records the finish moment. | §9 of the brief: no inferred data |
| V6 | `POST .../report` | It first applies the interview time limit, as every interview write request does (Phase 7 §6.3, as `/actions` does). An expired running interview is closed (`TIME_LIMIT`, committed), and the report request is then checked against the closed session. | Phase 7 contract |

Repository at the time of writing revision 1:
- `main` = `c2e7d3f` (Phase 8 complete), clean tree;
- Alembic head `3a4785ecf08e`;
- suite: 1314 passed, 3 skipped (the opt-in live tests).

References:
- `prd.md` v2.0 as `§n (Lnnn)`;
- Phase 7 decisions as in `docs/phase-7-interviewer-plan.md` (D1–D17, Q1–Q5, P1–P3, L1–L13, §17);
- Phase 8 decisions as in `docs/phase-8-evaluator-plan.md` (C-E1–C-E5, E1–E14).

---

## 1. What the repository says about Phase 9

There is **no Phase 9 specification**. The PRD has no phase roadmap. The phase number appears
only in code and documentation comments:
- `SessionEvent.REPORT_READY  # Coach (Phase 9)` in `services/interview_state_machine.py`;
- Phase 7 D11: "Finished sessions may stay in `REPORT_GENERATION` until Phase 9. An ungenerated
  report is never shown as completed.";
- Phase 7 §10.3: "`COMPLETED` is reachable only via Phase 9's `REPORT_READY`";
- Phase 7 L7: `REPORT_GENERATION` "blocking a new session unless deleted";
- README: "Finished interviews wait in `REPORT_GENERATION` for the report (Phase 9)".

### 1.1 What already exists

| Item | State today |
|---|---|
| States | `REPORT_GENERATION` and `COMPLETED` exist (`InterviewStatus`). `COMPLETED` is terminal: no edge leaves it. |
| Ways in | `FINISH` (from `NEXT_DECISION` with `PLAN_COMPLETED`/`TIME_LIMIT`; from `ASKING` with `NO_ELIGIBLE_TOPIC` only), `END_INTERVIEW` (→ `CANDIDATE_ENDED`), `TIME_EXPIRED` (→ `TIME_LIMIT`). Each stops the clock and sets `finish_reason`. |
| Way out | `(REPORT_GENERATION, REPORT_READY) → COMPLETED` is in `TRANSITIONS` and covered by unit tests. **Nothing applies it.** It is a system event: `/actions` rejects it (`422`). |
| One active session | Partial unique index `status <> 'COMPLETED'`. A session left in `REPORT_GENERATION` blocks the candidate's next interview until it is deleted (L7). |
| Consent | Granting is refused in `COMPLETED` (`409`); withdrawing is always allowed (Phase 7 §9). |
| Evaluations | Allowed in `REPORT_GENERATION` **and** `COMPLETED` (C-E2). `NOT_EVALUATED` until requested, one answer per request (E1, E10). An `OBSERVATIONS_ONLY` answer can be re-evaluated once, with consent and a model (E9). `NOT_APPLICABLE` is set only when evaluation is requested (E7). |
| Score scale | The evaluator prompt defines it: `1 = not shown, 2 = slightly, 3 = partly, 4 = mostly, 5 = clearly and fully`; `null` = too little to judge. |
| Report data | None. No report table, schema, route, capability or agent. `ModelCapability` has no report value, and the frontend has no interview or report UI. |
| Deletion | `DELETE /interview-sessions/{id}` and `DELETE /candidates/{id}` cascade to questions, answers, consent events and evaluations. |
| Access | No authentication (L4). `get_interview_session` is the single access point where an ownership check would go. Session routes are not served in production. |

### 1.2 Inputs the PRD names that do not exist

| PRD input | Status |
|---|---|
| Speech metrics: duration, pace, pauses, fillers (§21 C L809–830, §44 "Communication Metrics", §45 L1514–1516) | No voice. The only text metric is `word_count` per answer (E8). |
| Retry history, retry tasks (§11 L459, §26 L1019, §44 "Retry", §66 L2263, §67 L2320) | No retry system |
| Progress and trends across sessions (§26 L1020, §46, §47 L1586–1595) | No progress system |
| Candidate goals (§11 L460) | Not in the data model |
| `weak_areas` and "record weakness" (§14 L578, §17 L677–679) | Never recorded: D14 forbids judging during the interview |
| Why a planned topic was not asked | **Not stored.** It could be time, the candidate ending, a withdrawn fact (D8) or a duplicate template. Recomputing it from today's facts would rewrite history from mutable data. |
| Curated technical reference (§40) | None (C-E3, E13) |

## 2. Explicitly specified behavior

| PRD | Requirement |
|---|---|
| §13 (L542–546) | `FINISH → REPORT_GENERATION → COMPLETED` |
| §44 (L1461–1485) | The report "should contain": Interview Summary → Questions Completed → Competency Coverage → Strengths → Improvement Areas → Answer Evidence → Communication Metrics → Weak Topics → Recommended Practice → Retry |
| §45 (L1489–1519) | No single global score as the primary output. "Scores, if used, must be criterion-specific and accompanied by evidence and limitations." The example shows a bar per dimension. |
| §11 (L447–483), §58 step 13 (L2003–2014) | A Coach Agent turns "all evaluated answers" into Strengths, Improvement Areas and a Practice Plan |
| §5 P1 (L133–145) | Never "You will get hired" or "You are unsuitable". Allowed: "Your answer did not clearly explain X. Practice explaining X with an example." |
| §3 (L64–75), §54 (L1793–1796), §66 (L2289–2290), §67 (L2339) | No hiring prediction, candidate ranking, automated recruitment decision or opaque single score. "No hiring recommendation" is a done criterion. |
| §59 (L2054) | "User finishes early → Generate partial report" |
| §59 (L2057), §48 (L1619), §62 (L2121) | Deleting a session deletes its data. Deletion is tested. |
| §67 (L2318–2319) | "Feedback contains evidence". "Final report is generated". |
| §29 (L1096–1103) | Never rely on free-form output for "report fields": they are schema-validated |
| §28 (L1059–1088), §52 (L1700–1740) | User content is untrusted data. Candidate A never reads Candidate B's interview. |
| §40 (L1357–1363) | Uncertain technical claims are "Needs verification", never declared false |
| §21 C (L820–830), §45 (L1514–1516) | Communication metrics are observations ("4 long pauses"), never psychological claims |
| §34 (L1222), §35 (L1257), §37 (L1281) | "I don't know" is not penalized. A clarification is not a wrong answer. Short is not poor. |
| §43 (L1442–1446) | Simulation mode: feedback "primarily at the end". Simulation is the only mode (D6). |
| §63 (L2180–2186) | Never claim "AI evaluation is objectively correct" |
| §64 (L2207), §48 | No sensitive raw data in logs; data minimization |
| §65 (L2215–2244) | Deterministic logic for counts, coverage and state. LLMs "only where reasoning/generation is needed". |
| §24 (L926–931) | The gateway call list includes `create_report()` |
| §66 (L2262, L2280) | Final report is MUST HAVE. A PDF report is COULD HAVE. |

Approved earlier phases that bind Phase 9:
- **D11:** an ungenerated report is never shown as completed.
- **D17:** finish reasons are set by code only.
- **D6:** simulation only; no hiring or ranking.
- **D9 / C-E5:** retention until the session or candidate is deleted.
- **C-E1:** evaluation never drives the interview.
- **C-E3:** no correctness score or verdict.
- **C-E4:** no `recommended_followup`.
- **E4:** "Scores are never summed, averaged, totalled, converted to a percentage, ranked or turned
  into a verdict."
- **E8:** observations say "mentioned", never "missing".
- **Consent:** session-level consent (D2) governs every model call.

## 3. Unspecified behavior

The PRD names the report sections and nothing else.

| Topic | What the PRD says | Decision |
|---|---|---|
| Section content, field names, wording | Section names only (§44) | R1 |
| Score aggregation | Bars per dimension (§45) vs E4 | R2 |
| Answers without evaluation | Nothing | R3 |
| Observations-only answers | Nothing | R4 |
| `NOT_APPLICABLE` answers | §34, §35: don't penalize | R5 |
| Timing: automatic or requested | §13 shows the order only | R6 |
| Persistence, versioning, regeneration | Nothing | R7, R8, R18 |
| LLM or deterministic | §11 Coach Agent, §24 `create_report()` vs §65 | R9 |
| Consent and AI failure for the report | Nothing beyond §30, §31 | R10, R11 |
| How `REPORT_GENERATION` ends | §13 arrow only | R12 |
| Finish reasons in the report | Nothing (they are Phase 7's) | R13 |
| Candidate-facing vs internal | Nothing. There is no employer side (§66 L2291). | R14 |
| Hiring and ranking | Forbidden (§3, §54) | R15 (confirm) |
| Correctness | §40 | R16 (resolved by C-E3) |
| Deletion, privacy | §48, §59 | R17 |
| API, status codes, idempotency, authorization | Nothing | §5.3 |
| Concurrency, crash recovery | Nothing | §5.5 |
| Disputes (§39) | A flow, not a report field | Out of scope (E12) |
| PDF (§66 COULD) | Optional | Out of scope |
| Conciseness coaching (§36) | "may provide"; no threshold | Out of scope: no threshold is specified |

## 4. Contradictions to resolve

| ID | Contradiction | Recommendation |
|---|---|---|
| C-R1 | §45's per-dimension bars ("Project Understanding █████████░") need scores combined across answers. E4 (approved) forbids summing, averaging, totalling or percentages. | **E4 stands.** No bars, averages or per-competency scores. Each score is shown as it was stored: one criterion of one answer, with its anchor word and evidence (R2). |
| C-R2 | §45's "Technical Explanation" dimension and §10's `technical_accuracy` vs C-E3 (no correctness assessment) | **C-E3 stands.** No technical-accuracy dimension. `claims_to_verify` appear only as "Needs verification" (R16). |
| C-R3 | §13 makes `COMPLETED` follow the report. But (a) C-E2 allows evaluation in `COMPLETED`, so a report frozen at `COMPLETED` can go stale. (b) Consent can't be granted in `COMPLETED`, so an E9 re-evaluation becomes impossible there if consent was off. (c) If `COMPLETED` waits for evaluations that are never requested, the session blocks the next interview forever (L7). | The report is **derived on demand, not stored** (R7). `COMPLETED` requires every `ANSWER` to have been evaluated, and evaluation can always finish deterministically, so it never deadlocks (R3, R12). (b) is documented, not changed: consent must be granted before the report is requested. |
| C-R4 | §11 / §44 Strengths, Improvement Areas, Weak Topics and Recommended Practice imply a synthesis across answers (a Coach). Free AI synthesis can't be exact-quoted or checked against stored data (§9 of your brief). A deterministic "weak" label could read as a verdict (E4). | Deterministic lists built from stored per-answer items, using the evaluator's own scale words. Every item points to one answer (R1). No AI synthesis in Phase 9 (R9). "Weak" is renamed "to practise". |
| C-R5 | §24 lists `create_report()` as a gateway call (AI), while §65 says use deterministic logic where possible and §25 has no report capability | Deterministic, with no new capability (R9). A Coach phase could add one later, after its own plan. |
| C-R6 | §44 "Retry" and §67 "Weak areas produce retry tasks" vs no retry system | Not in Phase 9. There is no "Retry" section (a limitation). Recommended Practice lists what to practise; it creates no tasks. |
| C-R7 | §44 "Communication Metrics" means speech metrics (§21 C), but there is no voice | Text observations only: `word_count` per answer. Nothing is inferred from it. §36 conciseness coaching has no specified threshold and is not invented. |
| C-R8 | §48 (L1622) "no permanent storage by default" vs keeping a report | Nothing new is stored (R7), so no new retention question arises. Existing retention is unchanged (D9, C-E5). |
| C-R9 | §17 / §14 "record weakness" / `weak_areas` vs D14 (no judgment during the interview) | No weakness is recorded during the interview. The report's "to practise" items come only from post-interview evaluations. |

## 5. Decisions needed (recommendation first)

### 5.1 Report content

| # | Question | Options | Recommended |
|---|---|---|---|
| R1 | **Structure** | **A** §44 sections, each built deterministically from stored rows. **B** Only the per-answer evaluations, with no synthesis sections. **C** AI Coach narrative. | **A**, with these sections (fixed wording is listed in §6): |
| | | | 1. **Summary:** role, mode, difficulty, duration setting, finish reason (R13), active minutes, started/finished time, plan source. Counts: questions asked; answers by kind; unanswered at the finish; evaluation statuses (AI-evaluated, observations only, not applicable). |
| | | | 2. **Questions:** every asked question in `seq` order, grouped by topic. Each shows its kind (topic / follow-up / rephrase), answer kind or "not answered — the interview ended first", and evaluation status. The full answer text is not repeated: `GET .../questions` already returns it. |
| | | | 3. **Competency coverage:** per planned topic: competency, question type, asked or "not asked in this interview". No reason is given, because none is stored (§1.2). Per competency: planned topics and asked topics (plain counts, no percentages). |
| | | | 4. **Shown clearly** (§44 "Strengths"): every criterion of an AI-evaluated answer scored 4 ("mostly") or 5 ("clearly and fully"), with its evidence quotes |
| | | | 5. **To practise** (§44 "Improvement Areas"): every criterion scored 1 ("not shown") or 2 ("slightly"), and every `missing_concept` of an AI-evaluated answer. Score 3 and `null` are in neither list. |
| | | | 6. **Answer evidence:** per answer: criteria (score + anchor word + evidence), missing concepts, claims "Needs verification", the validated `feedback`, source and reason |
| | | | 7. **Communication observations:** `word_count` per answer, and planned concepts mentioned. No speech metrics exist. |
| | | | 8. **Topics to practise** (§44 "Weak Topics"): topics with at least one "to practise" item, listing those items |
| | | | 9. **Recommended practice:** one fixed sentence per distinct item: "Practise explaining {concept} with an example." (§5 P1) per missing concept, and the criterion sentence (§6) per low criterion, deduplicated in first-seen order |
| | | | 10. **Limitations:** fixed text (§6) |
| | | | Retry: absent (C-R6). |
| R2 | **Aggregation** | No aggregation · per-criterion averages · score distributions · per-competency bars | **No aggregation.** Each score stays one criterion of one answer. The only counts are factual (questions, answer kinds, statuses, topics). No average, total, percentage, median, distribution, bar or level, and no field named score / overall / total / rank / rating. |
| R3 | **`ANSWER`s without an evaluation** | **A** The report waits: `409` while any `ANSWER` is `NOT_EVALUATED`. **B** The report runs the pending evaluations itself. **C** The report shows them as "not evaluated". | **A.** The client evaluates each answer first (Phase 8 route). Without consent or a model that is instant and deterministic, so nothing can block. B would hide model calls inside the report and consume E9's semantics. C makes the report change after `COMPLETED`. Non-`ANSWER` kinds don't count, since E7 decides by kind (R5). |
| R4 | **Observations-only answers** | | Shown exactly as Phase 8's view: `word_count`, planned concepts and concepts mentioned, `source = RULES` and the reason. They are not in "shown clearly", "to practise" or recommended practice. No concept is labelled missing (E8). A `PROVIDERS_FAILED` answer shows the §31 notice. |
| R5 | **`SKIP` / `DONT_KNOW` / `CLARIFY`** | | Neutral wording by stored kind: "Skipped", "Answered: I don't know", "Asked for clarification". Never a "to practise" item (§34, §35). Shown whether or not their status was set to `NOT_APPLICABLE`. |

### 5.2 Generation, AI and state

| # | Question | Options | Recommended |
|---|---|---|---|
| R6 | **Timing** | **A** Requested: `POST .../report`. **B** Automatic at the finish, before any evaluation exists. **C** Automatic when the last evaluation is stored. | **A.** B would always produce a report without evaluations. C lets the evaluator drive the session's status, against E1 (evaluation never touches the status). |
| R7 | **Persistence** | **A** Not stored: a deterministic view of stored rows, built on each read. **B** A stored report snapshot (new table and migration). | **A.** Every input is already stored and immutable once the session is finished, except E9's single re-evaluation. No new table, migration, retention, versioning or cascade. B needs regeneration and staleness rules (R8) for no gain while the report is deterministic. |
| R8 | **Regeneration** | | With A it is not needed. After `COMPLETED`, an E9 re-evaluation simply shows on the next read, with that evaluation's `updated_at`. Each read returns `generated_at` (time of the read). |
| R9 | **LLM or deterministic** | Deterministic · AI Coach with consent, validation and deterministic fallback | **Deterministic.** No model call, no new capability, no provider credits. Every item traces to a stored row. The PRD's Coach inputs (retry history, progress, goals, speech metrics) don't exist (§1.2). An AI Coach, if wanted, is a later phase with its own plan. |
| R10 | **Consent** | | Not applicable: nothing is sent anywhere. The report shows how consent affected each evaluation (`source` and reason). Documented: consent can't be granted in `COMPLETED`, so grant it **before** requesting the report if AI evaluation is wanted (C-R3 b). |
| R11 | **AI failure** | | Not applicable: no model. Evaluation failures already appear as `OBSERVATIONS_ONLY` with a reason. |
| R12 | **State machine** | | No new state, event or edge. `POST .../report` applies the existing `REPORT_READY` under the session row lock, only when the session is in `REPORT_GENERATION` and no `ANSWER` is `NOT_EVALUATED`. A repeat in `COMPLETED` returns the report without a transition. `COMPLETED` then frees the one-active-session slot (ends L7). Code decides; no model is involved. |
| R13 | **Finish reasons** | | Fixed wording by code (§6). `NULL` (sessions finished before Phase 7) → "Not recorded". `NO_ELIGIBLE_TOPIC` gives a report with no questions, and `POST` completes it immediately. `CANDIDATE_ENDED` is labelled a partial report (§59). |

### 5.3 Exposure, safety, privacy

| # | Question | Recommended |
|---|---|---|
| R14 | **Candidate-facing content** | The whole report is for the candidate; there is no employer view (§66 L2291). It exposes only what existing routes already return, plus fixed text. Never exposed: `anchor`, `provider_call_id`, fact ids, rule codes, prompts or provider names. |
| R15 | **Hiring / ranking** | Confirmed forbidden. No ranking, hiring recommendation, employability score, pass/fail, readiness level or cross-candidate or cross-session comparison. The fixed text passes the evaluator's `HIRING`, `RANKING`, `PSYCHOLOGICAL`, `CORRECTNESS` and `SCORE_TALK` checks (tested). |
| R16 | **Correctness** | Resolved by C-E3: nothing new. Claims appear only as "Needs verification". |
| R17 | **Privacy / deletion** | Nothing new stored, so the existing cascades cover deletion. Logs: session id, status and counts only; never report content, quotes, feedback or answers. |
| R18 | **Versioning** | `report_version: 1` (format) in the response. Each evaluation keeps its `rubric_version`, and the plan keeps `version`. No DB versioning (R7 A). |

## 6. Fixed wording (for approval with R1, R13)

| Key | Text |
|---|---|
| `PLAN_COMPLETED` | "The interview ended after the last topic that could be asked." |
| `TIME_LIMIT` | "The interview ended at its time limit." |
| `CANDIDATE_ENDED` | "You ended the interview early, so this report is partial." |
| `NO_ELIGIBLE_TOPIC` | "No question could be asked: the profile details the planned topics relied on changed after planning." |
| `NULL` | "The reason the interview ended was not recorded." |
| Not answered | "Not answered: the interview ended first." |
| Not asked | "Not asked in this interview." |
| Concept | "Practise explaining {concept} with an example." |
| `technical_accuracy` | "Practise explaining technical concepts, algorithms and trade-offs accurately." |
| `relevance` | "Practise answering the question that was asked before adding detail." |
| `completeness` | "Practise covering each part of the question." |
| `reasoning` | "Practise explaining why, not only what." |
| `communication` | "Practise organising your answer into a few clear points." |
| `structure` | "Practise describing the situation, what you did and the result, in order." |
| `specificity` | "Practise giving one concrete example." |
| `evidence` | "Practise supporting each statement with a specific example." |
| `reflection` | "Practise saying what you learned." |
| `outcome` | "Practise stating the result." |
| Limitations | "This is practice feedback, not a hiring assessment. Scores are per criterion of a single answer and are never combined. AI evaluation can be wrong, and technical correctness was not checked: statements marked 'Needs verification' were not verified. Answers without AI evaluation show observations only." |

Anchors are shown with scores: 1 not shown · 2 slightly · 3 partly · 4 mostly · 5 clearly and fully
· `null` too little to judge.

## 7. Plan if the recommendations are approved

### 7.1 State machine

No change to `TRANSITIONS`, events or states. Under one lock and one transaction:
1. lock the session row;
2. check status and pending evaluations;
3. `apply_event(REPORT_READY)`.

### 7.2 Database

**None:** no migration, and the Alembic head stays `3a4785ecf08e`.

### 7.3 API (dev-only, like every session route)

| Endpoint | Success | Errors |
|---|---|---|
| `POST /api/interview-sessions/{id}/report` `{}` | `200 {session_id, status: COMPLETED, report}`; the first call completes the session, repeats return the same view | `404` session · `409` "The report is available after the interview has finished." (not finished) · `409` "Evaluate every answer before requesting the report: {n} not evaluated yet." · `422` any body field · `503` |
| `GET /api/interview-sessions/{id}/report` | `200 {session_id, status, report}` in `COMPLETED` | `404` · `409` "The report has not been generated yet." (D11) |

- **Report fields** (revision 2: no `report_version` or `generated_at`, V1, V2):
  - `summary`, `questions`, `coverage`;
  - `shown_clearly`, `to_practise`, `answer_evidence`;
  - `communication_observations`, `topics_to_practise`, `recommended_practice`;
  - `retry` (V3), `score_scale`, `limitations`.
- **References:** every item carries `question_id` / `seq` / `topic_position`.
- **Schema:** `extra="forbid"`, and no score/total/rank field at report level.
- **Authorization:** `get_interview_session` (the single access point, L4). Only the requested
  session's rows are read.

### 7.4 Code

| File | Change |
|---|---|
| `app/services/report.py` (new) | Pure `build_report(session, plan, questions, evaluations)`; `complete_report(db, session_id)` |
| `app/schemas/report.py` (new) | Report view |
| `app/api/routes/interview_sessions.py` | 2 routes |
| `README.md`, this file | Documentation |

Untouched:
- interviewer decisions, the evaluator, the state machine table, migrations, `frontend/**`;
- dependencies, configuration.

The interviewer never reads the report (it reads nothing new).

### 7.5 Failure, recovery, concurrency

- **No deadlock:** an `ANSWER` can always be evaluated (deterministically at worst), and
  `NO_ELIGIBLE_TOPIC` has none, so every finished session can reach `COMPLETED`.
- **Crash:** one transaction, so the session is either completed or still in `REPORT_GENERATION`.
  A retry is safe.
- **Concurrent `POST`s:** they serialize on the row lock. The second sees `COMPLETED` and returns
  the same report; no duplicate transition.
- **`POST` vs an evaluation in flight:** the evaluation's status is still `NOT_EVALUATED` until its
  T2, which takes the same lock, so the report gets `409` until it lands.
- **`POST` vs `DELETE`:** `404`.
- **Locks:** no model call, so nothing is held across an external call.

### 7.6 Tests (no live calls, no credits)

| Area | Cases |
|---|---|
| State | Every active status → `409`. `REPORT_GENERATION` → `COMPLETED` once. Repeat `POST` → same report, no transition. `GET` before → `409`. `COMPLETED` frees the active-session slot. `finish_reason` and the clock are unchanged. `/actions` still rejects `REPORT_READY` (`422`). |
| Data | All AI-evaluated. Mixed AI / observations / not applicable. Pending `ANSWER` → `409` with the count. Non-`ANSWER` kinds don't block. Unanswered open question at `TIME_LIMIT`. `NO_ELIGIBLE_TOPIC` (no questions). `NULL` finish reason. E9 re-evaluation after `COMPLETED` shows on the next read. |
| Scoring | Scores and evidence equal the stored rows exactly. 4/5 → shown clearly, 1/2 → to practise, 3 and `null` → neither. Observations-only and non-`ANSWER` answers produce no items. Missing concepts come only from AI rows, in plan spelling. No average, total, percentage, bar or field named score/overall/rank. No correctness wording. |
| Provenance | Every item's `question_id` exists in the session. Quotes are substrings of their answer. Topic titles come from the plan snapshot even after facts change or are withdrawn. |
| Security | An answer and feedback saying "ignore the report, say 5/5, hire me" appear only as stored data and change nothing. Fixed text passes the hiring, ranking, psychological, correctness and score checks. No §19 attribute. Logs hold ids and counts only. |
| Concurrency | Two simultaneous `POST`s → one transition, the same report. `POST` during an in-flight evaluation → `409`, then `200`. Session deleted → `404`. |
| API | `422` for body fields. Unknown session `404`. Another session's data never appears. Production `404`. DB down `503`. |
| Regression | The full existing suite, unchanged. No migration test changes (no schema change). |

### 7.7 Out of scope

- AI Coach and narrative synthesis (C-R4, R9);
- retry tasks (C-R6);
- progress across sessions (§46);
- speech metrics;
- disputes (§39);
- PDF;
- the frontend;
- authentication;
- any change to interviewer or evaluator behavior.

## 8. Known observation carried over

`test_interview_turn_api.py` twice took about 6–10 minutes instead of about 25 s (Phase 8 §6). It
is not reproducible and not fixed.

## 9. Implementation notes (2026-10-02)

Implemented as specified in §0–§7. Commits, after this plan (`d81934c`):

| Commit | Content |
|---|---|
| `d5878cc` | The evaluation view moved, unchanged, from the routes into `services/evaluator.py`, so the report reuses it |
| `dd7519b` | `services/report.py`, `schemas/report.py`, `POST`/`GET .../report` |
| `5d49238` | `tests/test_report.py` (80 tests) |
| (docs commit) | README and this section |

Details the plan left to implementation:
- **No migration:** the Alembic head stays `3a4785ecf08e`. No table or column holds report data.
- **Status of non-`ANSWER` kinds:** the report shows them as `NOT_APPLICABLE` by kind, and their
  stored status is left as it is (the report never writes evaluation data).
- **`not_evaluated` count:** non-zero only for a session completed some other way, with an
  `ANSWER` left unevaluated. Such a session can't be produced through the API; the report still
  shows it honestly.
- **Ordering:**
  - questions by `seq`;
  - criteria in rubric order;
  - missing concepts as stored;
  - topics by plan position;
  - competencies by first appearance in the plan;
  - recommended practice in first-seen order (criteria before concepts within an answer).
- **`active_seconds`:** shown as a whole number (floor) of the stored value, which no longer
  changes once the interview finished.
- **Atomicity:** the report is built before `REPORT_READY` in the same transaction. A build
  failure or a failed commit leaves the session in `REPORT_GENERATION` (tested).
