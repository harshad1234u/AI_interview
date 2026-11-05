# FREE-TIER SERVICE LIMITS & DEGRADATION MATRIX (FINAL HARDENED)
## AI Interview Coach — Practice & Learning System

**Author:** Principal DevOps Lead & Cloud Infrastructure Architect  
**Date:** November 3, 2026 (Phase 0 Final Technical Validation Hardened)  
**Scope:** Verified official service quotas, downtime policies, dormancy restrictions, and realistic operational capacity for zero-cost infrastructure.

---

## 1. Official Free-Tier Limitations Matrix

| Component | Provider / Service | Official Free Quota / Limits | Dormancy & Operational Policies | Architectural Impact & Mitigation |
| :--- | :--- | :--- | :--- | :--- |
| **Frontend & Proxy** | **Vercel Hobby** | - 100 GB monthly bandwidth<br>- 1 concurrent build<br>- 45-minute maximum build timeout<br>- 100 custom domains | - **Serverless Execution Limit:** Up to 60s (configurable).<br>- **External Proxied Request Timeout:** Official Vercel documentation specifies a **120-second timeout** for external rewrites (`/api/*` to Render).<br>- Edge network caching for static assets. | - Static pages prerendered (`/`, `/login`, `/_not-found`).<br>- `/api/*` rewrites proxy traffic to Render.<br>- Zero secrets in client bundle.<br>- **Cold-Start Reality:** Render's 50–90s cold start is within Vercel's 120s timeout window.<br>- **Safe Recovery:** `frontend/lib/api.ts` automatically retries idempotent GET requests after health check, but strictly avoids blind replay of mutations (turns, sessions). |
| **Backend** | **Render Free Web Service** | - 512 MB RAM<br>- 0.1 Shared CPU<br>- 750 free instance hours / month (covers 1 continuous service)<br>- Ephemeral filesystem<br>- Outbound IPv4 only<br>- Outbound SMTP ports (25, 465, 587) blocked | - **Inactivity Spin-Down:** Container sleeps after 15 minutes of silence.<br>- **Cold Start:** First request takes 50–90 seconds to initialize.<br>- **Dormant Retry Limitation:** When sleeping with no traffic, **no code runs**; sweeps cannot execute until awakened. | - **No Local Disk Storage:** All data stored in PostgreSQL.<br>- **No SMTP:** Email sent via HTTPS REST API (Resend over port 443).<br>- **Cold Start UX:** Frontend provides wake-up progress banner and exponential backoff retry against `/api/health`. |
| **Database** | **Supabase Free PostgreSQL** | - 500 MB database storage<br>- 2 active free projects<br>- Up to 50 pooler connections<br>- **NO automated daily downloadable backups or PITR on free tier** | - **Inactivity Pause:** Pauses after 7 consecutive days of zero database activity.<br>- Unpausing takes ~15–30s via dashboard or incoming traffic.<br>- Direct IPv6 vs Pooler IPv4. | - **Connection Routing:** Connect via Supabase Session Pooler (`aws-0-[region].pooler.supabase.com:5432`).<br>- **Conservative SQLAlchemy Pool:** `pool_size=5`, `max_overflow=2`, `pool_pre_ping=True`.<br>- **Manual Backups:** Weekly manual `pg_dump` with AES-256 encryption. |
| **DNS & Edge** | **Cloudflare Free** | - Unlimited bandwidth proxying<br>- Universal SSL/TLS certificate<br>- DDoS attack mitigation<br>- 5 free Custom WAF rules<br>- Bot Fight Mode<br>- Turnstile (1M challenges/mo free) | - Used for `harshad.me` DNS management.<br>- Initial record for `interview.harshad.me` set to DNS only (Grey Cloud) for Vercel SSL verification. | - Cloudflare DNS manages domain routing for Vercel and Resend verification.<br>- Cloudflare proxying is optional post-verification.<br>- **DMARC Safety:** Inspect existing zone DMARC records before creating any subdomain policy. Apex email records remain untouched. |
| **Email** | **Resend Free** | - 3,000 emails / month<br>- 100 emails / day maximum<br>- Rate limit: 2 requests / second<br>- HTTPS REST API | - **Dedicated Subdomain:** Verified `mail.harshad.me` DNS records in Cloudflare unlock delivery to **ANY candidate email address** without touching root domain MX/SPF. | - Outbox pattern isolates interview completion from email failures.<br>- Bounded retries (3 total attempts tracked via `scheduled_at`), worker lease fencing (`locked_until`), and 24h Resend idempotency key (retries past 24h dormancy may deliver duplicate emails; strictly at-least-once delivery). |
| **Authentication** | **Supabase Auth + Google OAuth** | - 50,000 Monthly Active Users (MAU)<br>- Free Google OAuth provider support | - High reliability (Supabase managed auth). | - Secure session handling via `@supabase/ssr` on frontend and server-side Supabase JWT verification on FastAPI enforcing strict `ALLOWED_JWT_ALGORITHMS` allowlist and trusted `email_confirmed_at` check via Supabase Admin API. Signing prerequisite resolved before Phase 2. |
| **AI Providers** | **Groq, Gemini, NVIDIA NIM** | - Groq Free: 30 RPM, 14,400 RPD<br>- Gemini Free: 15 RPM, 1,500 RPD<br>- NVIDIA NIM: 1,000 free credits | - Free rate limits can return HTTP 429 during bursts.<br>- **Zero Provider Idempotency:** REST endpoints do NOT support request idempotency keys. | - Bounded batch evaluation semaphore (`asyncio.Semaphore(2)`).<br>- Deterministic rule-based fallback automatically takes over if daily limits are reached.<br>- **Ambiguous Timeout Trade-off:** Client timeouts trigger duplicate external LLM calls on retry, consuming additional quota, while PostgreSQL database mutations remain strictly at-most-once with stale responses discarded via fencing tokens. |

---

## 2. Infrastructure Capacity & Target Metrics

- **Concurrent Candidates Capacity:** 20 concurrent active candidates (*Unverified target metric until load-tested in Phase 5*).
- **Target RPO (Recovery Point Objective):** $\le 7$ days (or interval between manual dumps) (*Target metric until tested by rehearsal*).
- **Target RTO (Recovery Time Objective):** 15–30 minutes (*Target metric until tested by rehearsal*).

---

## 3. Quota Exhaustion & Degradation Playbook

| Trigger Condition | Immediate Symptom | Application Response | Candidate Impact |
| :--- | :--- | :--- | :--- |
| **Render Container Sleep** | Initial page load delay of 50–90 seconds; possible timeout if queue exceeds 120s | Frontend detects delay, displays wake-up status banner, and polls `/api/health`. Replays GET requests automatically; reconciles state before prompting mutation resubmit. | 30–60s initial wait; zero duplicate operations; transparent recovery. |
| **Supabase 7-Day Inactivity Pause** | Database connection fails with timeout | Backend returns `503 Service Unavailable` with clean message. | Unpauses within 15–30s via Supabase dashboard. |
| **Resend Daily Quota Hit (100 emails/day)** | Resend API returns HTTP 429 / 400 | Outbox records `QUOTA_EXHAUSTED` and defers retry to next UTC day (`scheduled_at`). | **Zero impact on interview.** Report viewable immediately in browser. |
| **AI Provider Quota Exhausted** | Provider returns HTTP 429 or daily limit reached | Gateway cascades to fallback provider or deterministic rule-based planner/evaluator. | Interview continues smoothly with rule-based scoring. |
