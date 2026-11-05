# AI Interview Coach

A resume-aware, adaptive interview **practice and coaching** platform for students and entry-level candidates. It is an educational learning tool, not a hiring or candidate screening system. See [prd.md](prd.md) for the comprehensive product requirements.

---

## 1. Core Concept & Invariant

> **AI Interview Coach simulates realistic, role-specific interviews, evaluates answers using transparent rubrics, adapts follow-up questions based on the candidate's responses, and generates an actionable improvement plan.**

The core architectural invariant is:

$$\text{LLM Proposes} \longrightarrow \text{Orchestrator Decides} \longrightarrow \text{Validator Verifies} \longrightarrow \text{Database Stores}$$

- **LLM Proposes:** AI models only propose candidate resume facts, interview plan topic allocations, follow-up/rephrase wordings, and rubric-based evaluations backed by exact quotes.
- **Orchestrator Decides:** Deterministic state machines decide question transitions, timing, finish conditions, retry eligibility, and usage quotas.
- **Validator Verifies:** Strict Pydantic schemas and cryptographic signatures validate every model response and client submission before execution.
- **Database Stores:** PostgreSQL constraints independently enforce invariants (status transitions, cascade deletions, check constraints, unique constraints).

---

## 2. Key Features

1. **Authentication & Session Security (BFF Architecture):**
   - Supabase Auth integration with server-side Backend-for-Frontend (BFF) session management.
   - Opaque, host-only `coach_session` HttpOnly, Secure, SameSite=Lax cookies.
   - AES-256-GCM encrypted refresh tokens stored in PostgreSQL with atomic Compare-And-Swap (CAS) fencing (`refresh_lock_id`).
   - Two-step email verification protected against automated link-prefetching email scanners.
   - Google OAuth 2.0 PKCE flow and secure password reset.
2. **Profile & Resume Parsing:**
   - Deterministic PDF text extraction via `pypdf` with typographic normalization and heading detection.
   - Verbatim fact extraction across Skills, Projects, Experience, Education, and Certifications.
   - Candidate review workflow (`PENDING_REVIEW` → `APPROVED` / `REJECTED`) with manual fact creation.
   - Optional grounded AI extraction with anti-hallucination verification.
3. **Adaptive Interview Planning & Orchestration:**
   - Role-specific interview plans (software developer intern) across technical, behavioral, and project deep dives.
   - Finite state machine orchestrator managing question timing, pauses, rephrases, and follow-ups.
   - Server-authoritative countdown timer with connection recovery.
4. **Voice & Expression Analysis:**
   - Real-time speech-to-text input with pace (WPM), word count, and filler sound measurement.
   - Optional, client-side facial expression analysis using on-device `face-api.js` (WebAssembly/WebGL). No video or biometric data is ever stored on the server.
5. **Evaluation & Non-Judgmental Reporting:**
   - Rubric-based scoring with exact quote evidence from candidate answers.
   - Actionable practice recommendations without arbitrary combined numerical scores.
   - Interview retry linking: practice recommended topics in a focused follow-up attempt.

---

## 3. Technology Stack

- **Frontend:** Next.js 16 (App Router, Turbopack), React 19, TypeScript, Tailwind CSS.
- **Backend:** FastAPI, Pydantic v2, SQLAlchemy 2.0 (async & sync sessions), Psycopg v3.
- **Database & Auth:** PostgreSQL 14+, Supabase Auth (GoTrue), Supavisor connection pooling.
- **AI Gateway:** Multi-provider gateway with automatic fallback (Groq, Google Gemini, NVIDIA NIM).
- **Testing:** Pytest (4,700+ tests), Node.js native test runner (`node --test`), Playwright / CDP visual QA.

---

## 4. System Architecture

```text
Browser Client (Next.js)
   │  HTTPS + HttpOnly Session Cookie
   ▼
FastAPI Backend (Render Web Service)
   ├─ Security Middleware (OriginCheck CSRF, BodySizeLimit, SensitiveDataRedactionFilter)
   ├─ Supabase Auth Client (GoTrue REST API)
   ├─ Interview State Machine (Deterministic Orchestrator)
   ├─ Model Gateway (Groq / Gemini / NVIDIA with fallback circuit breaker)
   └─ SQLAlchemy Engine (Psycopg 3, prepare_threshold=None)
         │  Port 6543 (Transaction Mode)
         ▼
Supabase Database & Connection Pooler (Supavisor)
   └─ PostgreSQL (Candidates, Auth Sessions, Resumes, Questions, Evaluations)
```

---

## 5. Local Development Setup

### Prerequisites
- Python 3.11+
- Node.js 20+ and npm
- PostgreSQL 14+ running locally (or Supabase local CLI)

### 1. Database Setup
Create local development and test databases in PostgreSQL:
```sql
CREATE USER coach WITH PASSWORD 'choose-a-password';
CREATE DATABASE ai_interview_coach OWNER coach;
CREATE DATABASE ai_interview_coach_test OWNER coach;
```

### 2. Backend Setup
```bash
cd backend
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
# Edit .env with your DATABASE_URL and Supabase credentials

alembic upgrade head
python -m app.main
```
The backend starts at `http://127.0.0.1:8000`. Health check:
```bash
curl http://127.0.0.1:8000/api/health
```

### 3. Frontend Setup
```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev
```
Open `http://localhost:3000` in your browser.

---

## 6. Environment Configuration

### Backend Configuration (`backend/.env`)

| Variable | Required | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `DATABASE_URL` | Yes | — | PostgreSQL connection URI (`postgresql+psycopg://...`) |
| `ENVIRONMENT` | No | `development` | `development`, `test`, or `production` |
| `API_HOST` | No | `127.0.0.1` | API host bind address |
| `API_PORT` | No | `8000` | API port bind |
| `CORS_ORIGINS` | No | `http://localhost:3000` | Allowed origins (comma-separated, `*` disallowed) |
| `FASTAPI_PUBLIC_URL` | No | `http://localhost:8000` | Public backend URL (used in email confirmation links) |
| `FRONTEND_URL` | No | `http://localhost:3000` | Public frontend URL |
| `AUTH_COOKIE_SECURE` | No | `false` | Set `true` in production (enforces HTTPS-only cookies) |
| `COOKIE_SAMESITE` | No | `lax` | Cookie SameSite policy (`lax` recommended) |
| `COOKIE_DOMAIN` | No | ` ` | Cookie domain scope (omit for host-only cookies) |
| `SUPABASE_URL` | Yes | — | Supabase project URL (`https://<project-id>.supabase.co`) |
| `SUPABASE_ANON_KEY` | Yes | — | Supabase public anonymous API key |
| `SUPABASE_SERVICE_ROLE_KEY` | No | — | Supabase service-role secret key (for global revocation) |
| `SESSION_ENCRYPTION_KEY` | Yes | — | 64-char hex key for AES-256-GCM refresh token encryption |
| `SESSION_KEY_ID` | No | `k1` | Active encryption key identifier |
| `AI_PROVIDERS` | No | ` ` | Comma-separated providers: `groq,gemini,nvidia` (empty = rules only) |
| `AI_DAILY_REQUEST_LIMIT` | No | `2000` | Daily model call cost circuit breaker |

### Frontend Configuration (`frontend/.env.local`)

| Variable | Required | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `NEXT_PUBLIC_API_URL` | Yes | `http://localhost:8000` | Target backend API URL |

---

## 7. Database Migrations (Alembic)

```bash
cd backend
alembic upgrade head    # Apply all migrations
alembic current         # Check current applied revision
alembic check           # Verify models and migrations match
```

### Migration History

| Revision | Description |
| :--- | :--- |
| `e3b2e5da06bf` | Create initial `candidates` table |
| `c7292e1e2002` | Create `resumes` and `candidate_facts` tables |
| `c6cd3fd96db6` | Add `provider_calls` audit log and fact provenance |
| `a41f0c9e7b25` | Support `MODEL_UNAVAILABLE` fallback status |
| `e5813b741341` | Create `interview_sessions` orchestrator table |
| `b8d14f2a9c63` | Add interview plan schema validation and constraints |
| `178659f38d45` | Add `interview_questions`, turn state machine, and consent tracking |
| `3871903c395d` | Support `NO_ELIGIBLE_TOPIC` finish reason |
| `3a4785ecf08e` | Add `answer_evaluations` and criterion quote rubrics |
| `5c0e1d2f7a91` | Add `auth_sessions` with SHA-256 hashed session tokens |
| `9d2e6f1a7c40` | Add network-level rate limits and `usage_counters` |
| `b4c8e2d9f013` | Add retry interview session links |
| `c5d1a7e3b920` | Add facial expression observation count telemetry |
| `70b0b7891bc0` | Add manual candidate facts with unique constraints |
| `d7e2f1a9b3c4` | Add technical accuracy and speech metrics to answer evaluations |
| `e8f3b2c1a0d9` | Add Supabase Auth user integration (`auth_user_id`) |
| `f9a1b2c3d4e5` | Add `refresh_lock_id` UUID column for atomic CAS fencing |

---

## 8. Running Automated Tests

```bash
# Backend Test Suite (Pytest)
cd backend
pytest -v

# Frontend Unit Tests (Node native test runner)
cd frontend
npm test

# TypeScript Typecheck
npm run typecheck

# Production Build Verification
npm run build
```

---

## 9. Production Deployment Architecture

The application is architected for deployment across dedicated best-of-breed platforms:

- **Frontend:** Vercel (`https://www.YOUR_DOMAIN`)
- **Backend:** Render Web Service (`https://api.YOUR_DOMAIN`)
- **Database & Auth:** Supabase PostgreSQL with Supavisor Connection Pooler (`aws-0-ap-south-1.pooler.supabase.com:6543`)
- **Custom Domain:** Shared parent domain (`YOUR_DOMAIN`) enabling secure host-only cookies without third-party cookie restrictions.

Comprehensive deployment guides are available in [`docs/deployment/`](docs/deployment/):
- [`DEPLOYMENT_ARCHITECTURE.md`](docs/deployment/DEPLOYMENT_ARCHITECTURE.md) — Topology and cross-subdomain cookie architecture
- [`PRODUCTION_CONFIGURATION_GUIDE.md`](docs/deployment/PRODUCTION_CONFIGURATION_GUIDE.md) — Master environment and deployment runbook
- [`SUPABASE_PRODUCTION_CONFIGURATION.md`](docs/deployment/SUPABASE_PRODUCTION_CONFIGURATION.md) — Supabase pooler, email templates, and Auth setup
- [`PRODUCTION_E2E_CHECKLIST.md`](docs/deployment/PRODUCTION_E2E_CHECKLIST.md) — Step-by-step verification checklist
- [`RENDER_DEPLOYMENT_GUIDE.md`](docs/deployment/RENDER_DEPLOYMENT_GUIDE.md) — FastAPI Web Service configuration on Render
- [`VERCEL_DEPLOYMENT_GUIDE.md`](docs/deployment/VERCEL_DEPLOYMENT_GUIDE.md) — Next.js deployment and environment setup
- [`CUSTOM_DOMAIN_DNS_GUIDE.md`](docs/deployment/CUSTOM_DOMAIN_DNS_GUIDE.md) — DNS records for Vercel, Render, and Supabase custom domains
- [`DATABASE_MIGRATION_PLAN.md`](docs/deployment/DATABASE_MIGRATION_PLAN.md) — Migration execution and rollback scripts

---

## 10. Security Notes

- **Zero Client Token Exposure:** Raw Supabase JWT access and refresh tokens are never exposed to browser JavaScript or stored in client local storage. Tokens remain sealed in encrypted server-side PostgreSQL sessions.
- **Log Privacy & Redaction:** Container stdout/stderr is monitored by `SensitiveDataRedactionFilter` and `RedactingFormatter`, automatically redacting query parameters, authorization headers, tracebacks, and JSON credentials.
- **CSRF & Origin Verification:** State-changing API requests require matching `Origin` or `Referer` headers against the allowlist. Email confirmation links are protected by cryptographic HMAC-SHA256 tokens.
- **Pooler Prepared Statement Safety:** The backend sets `prepare_threshold=None` in Psycopg 3 to avoid prepared statement conflicts across Supavisor transaction pooling.

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
