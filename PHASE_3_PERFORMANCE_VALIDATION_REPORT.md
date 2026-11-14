# Phase 3 — End-to-End Performance Benchmarking & Evidence Reconciliation Report

**Repository:** `D:\code_placed\ai_interview`
**Branch:** `perf/phase-3-performance-validation`
**Base Commit:** `22078272db798d43a8f9001e6c280db378107267` (Phase 2C HEAD)
**Execution Date:** 2026-11-14
**Audit & Calibration Date:** 2026-11-14
**Status:** Completed, Independently Audited & Evidence-Reconciled

---

## 1. Executive Summary

Phase 3 established an initial local performance and architectural baseline for the AI Interview Coach application across frontend rendering, local API transitions, database query execution, and external AI provider connections.

Following an independent technical audit, all performance claims, benchmark numbers, and architectural observations have been calibrated against the underlying test harnesses, build manifests, and execution logs.

> [!NOTE]
> **Working Tree & Source Scope:**
> No application source files were modified during the documentation-calibration pass. The previously implemented logging formatter correction remains an uncommitted change in `backend/app/main.py` alongside its regression test in `backend/tests/test_logging_formatter.py`.

### Key Calibrated Findings:

1. **Turn Transition Optimization (Local TestClient Measurements, $N=6$ per run):** In-process benchmarks against local PostgreSQL demonstrate that deferring question history refresh out of the critical turn path eliminates one sequential API call. Two local benchmark executions observed **55.40 ms (22.0% reduction)** and **61.58 ms (31.3% reduction)** saved per turn. These represent local TestClient measurements under two different host-load conditions; they do not establish production latency, and further repeated testing is required for statistical confidence. They should not be interpreted as a guaranteed production performance range.
2. **AI Gateway Connection Pooling (Observed HTTP Request Latency on Metadata Endpoints):** Benchmarks comparing fresh and reused HTTP clients against external model-listing endpoints (`GET /models`) observed latency differences between fresh connections and reused keep-alive connections. Across three distinct runs ($N=3$ per condition), the observed request latency difference varied between **607 ms and 2,172 ms per connection**, with speedup ratios on HTTP metadata calls ranging between **2.92× and 21.1×**. This measured difference represents composite HTTP request latency (which includes TCP establishment, TLS negotiation, network routing, internet jitter, remote endpoint response time, and other transport effects). These tests do not measure LLM prompt evaluation or token generation inference, and the observed differences must not be presented as guaranteed TLS savings for every connection.
3. **Camera Render Decoupling (Architectural Event Isolation):** Validated through Phase 2C code review and a Node.js architectural unit simulation that telemetry state updates subscribe locally within `CandidateCamera`. Parent `InterviewFlow`, `InterviewTurn`, and sibling panels (`QuestionsPanel`, `VoiceHub`, `InterviewControls`, `InterviewHeader`) remain isolated from high-frequency (1 Hz) expression updates. This isolation is supported by architectural analysis and Node.js simulation, not actual React Profiler or DOM render measurements.
4. **Development Loading Snapshots (Localhost $N=1$):** Single-run unthrottled Chrome DevTools traces on `localhost:3000` recorded DOM and paint timings for `/login` (LCP 162 ms) and `/` (LCP 709 ms). Turbopack build manifests confirm that the 1.32 MB `@vladmandic/face-api` chunk is code-split dynamically and excluded from initial landing and login bundles.
5. **Database Query Execution & Planning:** In steady state ($N=5$ on established connections), query planning takes **~0.086 ms** and execution takes **~0.036 ms** on the test database. Initial observations of 5–7 ms planning times were cold-start artifacts of PostgreSQL catalog cache loading during connection initialization.
6. **Production Logging Status:** The custom `RedactingFormatter` fix passed local regression testing (16/16 unit tests, 4,794 full suite tests) and is ready for deployment validation. **Production resolution has not yet been confirmed**, as the fix currently resides as an uncommitted local change in `backend/app/main.py` and `backend/tests/test_logging_formatter.py`, and has not been deployed to Render.

---

## 2. Comprehensive Evidence & Classification Matrix

Every performance metric reported across Phase 3 is documented below with its exact source, environment, sample size, classification, and confidence limitations:

| Metric | Reported Value | Source File / Command | Sample Size ($N$) | Environment | Classification | Confidence & Limitations |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Turn Latency (Run 1)** | 134.96ms opt vs 196.54ms leg (61.58ms / 31.3% saved) | `pytest -s backend/tests/benchmark_phase3_backend.py` (03:51 UTC) | $N=6$ completed turns | Localhost FastAPI TestClient + Postgres (idle) | **Directly Measured (Local)** | Local in-process TestClient measurement only. Excludes network transit latency; does not establish production latency. |
| **Turn Latency (Run 2)** | 196.26ms opt vs 251.66ms leg (55.40ms / 22.0% saved) | `backend_benchmark_results.json` via Task 5894 (03:56 UTC) | $N=6$ completed turns | Localhost FastAPI TestClient + Postgres (post-test load) | **Directly Measured (Local)** | Local measurement captured under higher host CPU load during test suite execution. |
| **Network RTT Scaling** | 8.1ms (LAN) to 157.4ms (Roaming) saved per turn | `benchmark_interview_turn.py` (`run_network_simulation`) | $N=50$ synthetic iterations | Mathematical formula: $(t_{\text{srv}} + \text{RTT})$ | **Simulated** | Theoretical mathematical model. Illustrates calculated $1 \times \text{RTT}$ savings; not measured on live cellular carriers. |
| **AI Pooling: Groq Run 1** | 358.5ms pooled vs 2,530.9ms unpooled (7.06×, 2,172ms diff) | Task 5827 stdout (03:51 UTC) | $N=3$ unpooled, $N=3$ pooled | Public HTTPS (`api.groq.com/openai/v1/models`) | **Directly Measured (HTTP Transport)** | Measures composite observed HTTP request latency difference on `GET /models` metadata endpoint. Not LLM inference. |
| **AI Pooling: Groq Run 2** | 512.1ms pooled vs 2,301.8ms unpooled (4.49×, 1,790ms diff) | `backend_benchmark_results.json` (03:56 UTC) | $N=3$ unpooled, $N=3$ pooled | Public HTTPS (`api.groq.com/openai/v1/models`) | **Directly Measured (HTTP Transport)** | Observed request latency difference on metadata endpoint; highlights internet jitter and endpoint response variance. |
| **AI Pooling: Groq Run 3** | 402.4ms pooled vs 1,175.3ms unpooled (2.92×, 772.9ms diff) | Audit review run (`benchmark_pooling.py`, 04:18 UTC) | $N=3$ unpooled, $N=3$ pooled | Public HTTPS (`api.groq.com/openai/v1/models`) | **Directly Measured (HTTP Transport)** | Observed request latency difference between fresh and reused HTTP client on metadata endpoint. |
| **AI Pooling: Gemini Run 1** | 150.8ms pooled vs 1,469.5ms unpooled (9.74×, 1,318ms diff) | Task 5827 stdout (03:51 UTC) | $N=3$ unpooled, $N=3$ pooled | Public HTTPS (`generativelanguage.googleapis.com`) | **Directly Measured (HTTP Transport)** | Measures composite observed HTTP request latency difference on `GET /models` metadata endpoint. Not LLM inference. |
| **AI Pooling: Gemini Run 2** | 97.1ms pooled vs 2,048.7ms unpooled (21.1×, 1,952ms diff) | `backend_benchmark_results.json` (03:56 UTC) | $N=3$ unpooled, $N=3$ pooled | Public HTTPS (`generativelanguage.googleapis.com`) | **Directly Measured (HTTP Transport)** | Observed request latency difference on metadata endpoint; ratio sensitive to small warm latency denominator. |
| **AI Pooling: Gemini Run 3** | 285.3ms pooled vs 892.4ms unpooled (3.13×, 607.2ms diff) | Audit review run (`benchmark_pooling.py`, 04:18 UTC) | $N=3$ unpooled, $N=3$ pooled | Public HTTPS (`generativelanguage.googleapis.com`) | **Directly Measured (HTTP Transport)** | Observed request latency difference on metadata endpoint. |
| **Interview Transport Savings** | 10–20 seconds projected across 10-turn interview | Extrapolation calculation: $20\text{ calls} \times \Delta t_{\text{conn}}$ | Calculated | Projected interview model | **Estimated / Theoretical** | Theoretical estimate based on repeated serial requests. Real savings vary if calls are concurrent or rules-based. |
| **React Render Counts** | CandidateCamera: 2 (stable), 10 (change); Siblings/Parents: 1 | `benchmark_phase3_camera_react.test.ts` | 10 sample ticks | Pure Node.js unit mock harness | **Simulated (Architectural Event Model)** | Validates that event emitter subscriptions isolate telemetry updates. Does not mount React DOM or run React Profiler. |
| **Camera Lifecycle Cleanup** | 5/5 tracks stopped, 5/5 intervals cleared, 0 dangling listeners | `benchmark_phase3_camera_react.test.ts` | 5 repeated cycles | Pure Node.js unit mock harness | **Directly Measured (Unit Mock)** | Validates component cleanup logic in simulated camera harness. |
| **Camera Init (Warm)** | 24.5 ms | `benchmark_phase3_camera_react.test.ts` | 1 invocation | Pure Node.js unit mock harness | **Simulated** | Synthetic mock stream creation time; not hardware webcam capture. |
| **Camera Disable Latency** | 0.8 ms | `benchmark_phase3_camera_react.test.ts` | 1 invocation | Pure Node.js unit mock harness | **Directly Measured (Unit Mock)** | Time to clear listeners and intervals in mock object. |
| **Face-API WebGL Inference** | 45 ms – 62 ms | Test fixture pacing values in `expressions.ts` | 5 test samples | Node.js pacing simulation | **Estimated / Simulated** | Pacing benchmark uses hardcoded fixture values based on published `@vladmandic/face-api` WebGL benchmarks. |
| **Face-API CPU Fallback** | 180 ms – 240 ms | Test fixture pacing values in `expressions.ts` | 5 test samples | Node.js pacing simulation | **Estimated / Simulated** | Simulated adaptive interval pacing based on published CPU fallback benchmarks. |
| **Face-API Chunk Size** | 1,323.70 KB raw (~380 KB gzipped) | Next.js Turbopack build manifest | Deterministic build | Production compilation | **Directly Measured** | High confidence; exact asset size from production build output. |
| **Face-API Brotli Savings** | ~110 KB transfer (~60% reduction) | Engineering projection | Modeled | Static asset serving model | **Estimated / Hypothesis** | Expected compression ratio for Brotli level 11 on JS/binary model assets. |
| **Development Core Web Vitals** | LCP: 709ms (`/`), 162ms (`/login`), ~450ms (`/interview`) | Chrome DevTools MCP Performance Trace | $N=1$ per route | Localhost (`localhost:3000`) | **Localhost Development Snapshot** | Unthrottled local environment ($N=1$); confirms layout stability on dev server, but does not represent field CWV. |
| **Page Heap Memory** | 3.97 MB (`/`), 6.24 MB (`/login`) | Chrome DevTools MCP Memory Trace | $N=1$ per route | Localhost (`localhost:3000`) | **Directly Measured (Snapshot)** | Local browser heap size immediately following page load. |
| **Active Camera Heap** | ~22.5 MB active, ~8.5 MB teardown | Engineering estimate based on WebGL context | Modeled | Browser WebGL context | **Estimated** | Approximates TensorFlow.js texture memory allocation and GC reclamation. |
| **Interview Startup** | 162.9 ms total (42ms session, 51ms topics, 63ms next q) | In-process TestClient + mount estimates | $N=15$ synthetic sessions | Localhost TestClient | **Composite (Measured + Estimated)** | Backend steps (Steps 2–4) are directly measured locally; DOM mount steps (Steps 5–6) are estimated. |
| **Postgres Steady-State** | Plan: 0.086 ms, Exec: 0.036 ms (total 0.122 ms) | `test_plan.py` via `EXPLAIN (ANALYZE, BUFFERS)` | $N=5$ repeated runs | Local PostgreSQL 16 (established conn) | **Directly Measured** | High confidence for local database query execution on warm connections. |
| **Postgres Cold Planning** | Plan: 5.78 ms – 7.50 ms | Initial `benchmark_phase3_backend.py` run | $N=1$ cold run | Local PostgreSQL 16 (fresh conn) | **Directly Measured (Cold Artifact)** | Captures initial catalog cache load on brand-new connection; not recurring query planning overhead. |
| **Prepared Statement Impact** | ~0.05 ms per query (~0.3 ms per turn) | Recalibrated from steady-state DB timings | Modeled | Production DB driver | **Hypothesis** | Prepared statements provide modest CPU reduction under high concurrency; will not save 30–40ms per turn. |
| **Speech Synthesis Delay** | 80 ms – 120 ms initial phrase delay | Browser Web Speech API quirk documentation | Observational | Browser runtime | **Estimated / Observational** | Known cold-start voice initialization lag in Chromium Web Speech API. |
| **External LLM Latency** | 800 ms – 2,500 ms per response | Operational telemetry on Groq/Gemini APIs | Observational | Live external APIs | **Estimated / Observational** | Standard full token-generation latency for 150–300 output tokens over public AI APIs. |

---

## 3. Reconciliation of Interview Turn Benchmark Numbers

### A. Context of the Two Benchmark Executions
An investigation of execution logs confirmed that the discrepancy between the initial report and `backend_benchmark_results.json` represents **two distinct, consecutive local benchmark executions**:

1. **Run 1 (Executed at 03:51:08 UTC, Task 5827):**
   - **Command:** `$env:PYTHONPATH="backend"; pytest -s backend/tests/benchmark_phase3_backend.py`
   - **Execution Context:** Standalone backend execution on an idle test environment ($N=6$ completed turns).
   - **Captured Results:**
     - `POST /answers`: Mean = **71.55 ms** (p50: 71.78 ms, p95: 91.91 ms)
     - `POST /questions/next`: Mean = **63.41 ms** (p50: 58.89 ms, p95: 83.99 ms)
     - `GET /questions` (History): Mean = **61.58 ms** (p50: 37.99 ms, p95: 153.49 ms)
     - **Legacy Turn Total:** Mean = **196.54 ms** (p50: 172.69 ms, p95: 310.64 ms)
     - **Optimized Turn Total:** Mean = **134.96 ms** (p50: 134.70 ms, p95: 161.68 ms)
     - **Observed Savings:** **61.58 ms saved per turn (31.3% reduction, 1.46× faster)**.
   - *Status:* Recorded in the initial report draft.

2. **Run 2 (Executed at 03:55:40 UTC, Task 5894, Saved to JSON at 03:56:17 UTC):**
   - **Command:** `npm --prefix frontend test; $env:PYTHONPATH="backend"; pytest backend/tests/test_interview_turn_optimization.py backend/tests/benchmark_phase3_backend.py`
   - **Execution Context:** Executed immediately following the full frontend test suite under higher host CPU load ($N=6$ completed turns).
   - **Captured Results (Saved to `backend_benchmark_results.json`):**
     - `POST /answers`: Mean = **102.01 ms** (p50: 109.97 ms, p95: 114.86 ms)
     - `POST /questions/next`: Mean = **94.25 ms** (p50: 100.77 ms, p95: 112.42 ms)
     - `GET /questions` (History): Mean = **55.41 ms** (p50: 58.73 ms, p95: 63.74 ms)
     - **Legacy Turn Total:** Mean = **251.66 ms** (p50: 273.19 ms, p95: 279.55 ms)
     - **Optimized Turn Total:** Mean = **196.26 ms** (p50: 211.21 ms, p95: 219.60 ms)
     - **Observed Savings:** **55.40 ms saved per turn (22.0% reduction, 1.28× faster)**.
   - *Status:* Automatically persisted by line 289 of `benchmark_phase3_backend.py`.

### B. Statistical Limitations & Interpretation:
- **Sample Size ($N=6$ per run):** Both runs were conducted with a small sample size ($N=6$ completed turns) using FastAPI's in-process `TestClient` against a local PostgreSQL database.
- **Host Load Sensitivity:** The difference in absolute numbers (71 ms vs 102 ms for `/answers`) demonstrates that local in-process benchmarks are sensitive to background host CPU scheduling and disk activity.
- **Not Production Latency:** These in-process benchmarks exclude network transit latency, TLS negotiation, reverse proxy overhead, and remote concurrency. They do not establish production turn latency.
- **No Guaranteed Performance Range:** These two runs represent observed snapshots under specific local conditions, not an exhaustive statistical distribution or a guaranteed production performance range. Additional repeated testing across larger sample sizes ($N \ge 30$) under controlled load is necessary for robust statistical confidence.

---

## 4. Reconciliation of AI Connection Pooling Observations

### A. Independent Historical Observations ($N=3$ per condition)

The table below documents all three recorded benchmark runs comparing fresh HTTP clients against reused clients on metadata endpoints (`GET /models`):

| Target Provider | Run Reference & Timestamp | Unpooled Avg (Fresh Client) | Pooled Avg (Reused Socket) | Observed Request Latency Difference | Speedup Factor |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Groq** (`api.groq.com`) | Run 1 (Task 5827, 03:51 UTC) | 2,530.89 ms | 358.45 ms | **2,172.43 ms** | **7.06×** |
| **Groq** (`api.groq.com`) | Run 2 (`results.json`, 03:56 UTC) | 2,301.79 ms | 512.13 ms | **1,789.66 ms** | **4.49×** |
| **Groq** (`api.groq.com`) | Run 3 (Audit Review, 04:18 UTC) | 1,175.30 ms | 402.40 ms | **772.90 ms** | **2.92×** |
| **Gemini** (`generativelanguage.googleapis.com`) | Run 1 (Task 5827, 03:51 UTC) | 1,469.48 ms | 150.83 ms | **1,318.65 ms** | **9.74×** |
| **Gemini** (`generativelanguage.googleapis.com`) | Run 2 (`results.json`, 03:56 UTC) | 2,048.73 ms | 97.12 ms | **1,951.61 ms** | **21.10×** |
| **Gemini** (`generativelanguage.googleapis.com`) | Run 3 (Audit Review, 04:18 UTC) | 892.40 ms | 285.30 ms | **607.20 ms** | **3.13×** |

### B. Corrected Transport Interpretation & Caveats:
1. **Nature of the Latency Difference:** The observed latency difference between fresh and reused clients represents **composite HTTP request latency**, which includes:
   - TCP 3-way connection establishment
   - TLS 1.3 cryptographic negotiation
   - Internet routing path variations and packet jitter
   - Remote provider gateway queueing and endpoint response time
   - Other lower-level transport effects
   The entire difference cannot be attributed exclusively to TCP/TLS handshake overhead.
2. **Metadata Endpoints Only:** These benchmarks targeted model metadata listing endpoints (`GET /models`). They **do not measure LLM inference latency, prompt token ingestion, or token-generation speeds**.
3. **Small Sample Size ($N=3$):** Each condition was measured over only 3 requests. Because public internet transit and remote API gateways experience continuous latency jitter, the speedup ratio fluctuated widely (between 2.92× and 21.1×) across runs.
4. **No Guaranteed Savings:** The observed differences must not be presented as guaranteed TLS savings for every connection. Actual savings in production will vary depending on network topology, proximity to edge gateways, and connection reuse policies.

---

## 5. PostgreSQL Query Planning: Cold vs. Steady State

Repeated profiling on PostgreSQL 16 using `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` demonstrates the critical difference between cold-connection catalog initialization and steady-state execution:

```powershell
# Repeated EXPLAIN ANALYZE on candidates table (test_plan.py)
Run 1 (Cold Catalog Load): Planning = 6.973 ms | Execution = 0.384 ms
Run 2:                     Planning = 0.262 ms | Execution = 0.077 ms
Run 3:                     Planning = 0.111 ms | Execution = 0.040 ms
Run 4:                     Planning = 0.086 ms | Execution = 0.036 ms
Run 5:                     Planning = 0.086 ms | Execution = 0.036 ms
```

### Calibrated Database Findings:
- **Steady-State Planning:** Query planning overhead in steady state is **~0.086 ms**, not 5–7 ms.
- **Steady-State Execution:** Execution overhead is **~0.036 ms**.
- **Prepared Statement Assessment:** Prepared statement caching (`statement_cache_size`) will save ~0.05 ms per query under production load. It is a useful minor optimization for database CPU protection under high concurrency, but **will not yield 30–40 ms per-turn savings**.

---

## 6. Phase 4 Candidate Optimization Opportunities & Required Experiments

Optimization initiatives for Phase 4 are classified as candidate optimization opportunities or hypotheses. Before claiming measurable performance benefits, each initiative requires targeted experimental validation:

| Rank | Candidate Initiative | Classification | Potential Opportunity | Additional Experiment Needed Before Claiming Benefit |
| :--- | :--- | :--- | :--- | :--- |
| **1** | **Streaming Evaluation & LLM Responses** | **Candidate Optimization Opportunity** | Full token generation for evaluation takes 800–2,500 ms over public AI APIs. Streaming tokens via SSE or WebSockets could provide immediate feedback in 15–30 ms while generation completes. | **Experiment Needed:** Prototype an SSE/WebSocket streaming endpoint for evaluation feedback in the staging environment. Measure time-to-first-token (TTFT) and candidate perceived latency against live model generation. |
| **2** | **Face-API Model Weight Compression & Asset Caching** | **Candidate Optimization Opportunity** | The `@vladmandic/face-api` chunk is 1.32 MB uncompressed (~380 KB gzipped) and static model weights are fetched on initial camera start. Brotli compression and immutable cache-control headers could reduce transfer footprint. | **Experiment Needed:** Benchmark client-side asset transfer times and decompression CPU overhead under simulated throttled network conditions (e.g. Chrome DevTools 4G throttling) comparing Brotli vs gzip vs uncompressed model shards. |
| **3** | **Speech Synthesis Audio Pre-warming** | **Candidate Optimization Opportunity** | The Chromium Web Speech API occasionally exhibits an 80–120 ms pause on the very first synthesized phrase. Pre-warming `speechSynthesis.getVoices()` on page mount may reduce cold-start latency. | **Experiment Needed:** Profile cold vs pre-warmed audio onset latency across target browsers (Chrome, Edge, Safari) using `performance.now()` markers on `speechSynthesis.onstart` events to verify real millisecond savings. |
| **4** | **Database Prepared Statement Caching** | **Hypothesis / Minor Optimization** | Steady-state query planning is already sub-millisecond (~0.086 ms). Configuring driver statement caching (`statement_cache_size=100`) may reduce database backend CPU cycles under high concurrency. | **Experiment Needed:** Execute high-concurrency multi-worker load tests (e.g. Locust or k6 simulating 50–100 concurrent interview sessions) comparing database server CPU utilization with `statement_cache_size=0` vs `statement_cache_size=100`. |

---

## 7. Status of Production Logging Formatter Fix

The production logging issue on Render was investigated, reproduced, and fixed in `backend/app/main.py`:
- **Root Cause:** Uvicorn access logging emits a 5-tuple in `record.args` (`client_addr, method, path, http_version, status_code`). Python 3.11's `PercentStyle` raised `ValueError: Formatting field not found in record: 'client_addr'` when `RedactingFormatter` formatted strings referencing `%(client_addr)s`.
- **Implementation:** Added safe extraction of positional `recordcopy.args`, socket tuple normalization, neutral fallback defaults (`client_addr="unknown"`), preservation of structured fields (`request_id`), LogRecord immutability (`copy.copy`), and credential redaction.
- **Verification:** All 16 targeted formatter regression tests passed in 0.81s; all 4,794 full backend tests passed with 0 failures.

> [!IMPORTANT]
> **Production Status:**
> The formatter correction passed local regression testing and is ready for deployment validation. **Production resolution has not yet been confirmed**, as the fix currently resides as an uncommitted local change in `backend/app/main.py` and `backend/tests/test_logging_formatter.py`, and has not been deployed to Render.
