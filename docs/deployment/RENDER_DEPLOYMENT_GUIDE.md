# Render Deployment Guide — Backend API (`https://api.YOUR_DOMAIN`)

This guide provides the exact configuration, dashboard procedures, and verification steps to deploy the **AI Interview Coach** FastAPI backend to **Render** as a managed Web Service under the custom domain `https://api.YOUR_DOMAIN`.

---

## 1. Architecture Overview

- **Service Type:** Render Web Service
- **Runtime:** Python 3 (3.11.x)
- **Production URL:** `https://api.YOUR_DOMAIN`
- **Render Default Host:** `https://<service-name>.onrender.com`
- **Frontend Origin Allowed:** `https://www.YOUR_DOMAIN`
- **Health Check Path:** `/api/health`
- **Port Binding:** Dynamic `$PORT` assigned by Render, bound to `0.0.0.0`.
- **Database Connection:** Supabase Supavisor Transaction Pooler (`port 6543`) with `prepare_threshold=None` to ensure pooler compatibility.

---

## 2. Render Web Service Specification

### 2.1 Service Settings
In the [Render Dashboard](https://dashboard.render.com), create a new **Web Service** connected to your repository:

| Setting | Value | Explanation |
|---|---|---|
| **Name** | `ai-interview-coach-api` | Your Render service name |
| **Region** | `Singapore` or `Frankfurt` | Choose the region closest to Supabase `ap-south-1` (Mumbai) for minimum latency |
| **Branch** | `main` (or production branch) | Deployment source branch |
| **Root Directory** | `backend` | **Crucial:** Scopes deployment to the `backend/` directory |
| **Runtime** | `Python 3` | Uses Python 3.11 |
| **Build Command** | `pip install --upgrade pip && pip install -r requirements.txt` | Installs dependencies including `cryptography>=42.0.0` |
| **Start Command** | `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 2` | Starts production ASGI server on `$PORT` |
| **Health Check Path** | `/api/health` | Render continuously monitors this endpoint |
| **Auto-Deploy** | `Yes` | Deploys on git push to the configured branch |

---

## 3. Custom Domain Setup in Render (`api.YOUR_DOMAIN`)

Render natively supports custom domains and provides automated Let's Encrypt SSL/TLS certificates for all Web Services (see [Render Custom Domains Documentation](https://render.com/docs/custom-domains)).

Follow these exact steps:

1. **Open Web Service Settings:**
   In your Render dashboard, select the `ai-interview-coach-api` Web Service.
2. **Navigate to Custom Domains:**
   Click **Settings** in the left navigation menu, then scroll to the **Custom Domains** section.
3. **Add Subdomain:**
   Click **Add Custom Domain** and enter your exact API subdomain:
   ```text
   api.YOUR_DOMAIN
   ```
   Click **Save**.
4. **Obtain DNS Record Instructions:**
   Render will inspect the domain and display the exact DNS record instructions on screen.
   - For a subdomain like `api.YOUR_DOMAIN`, Render will specify a **CNAME** record pointing to your Render service address:
     - **Type:** `CNAME`
     - **Name / Host:** `api`
     - **Value / Target:** `<your-service-name>.onrender.com` (use the exact target displayed by Render).
5. **Configure DNS Record at Your Domain Registrar:**
   Log in to your DNS provider (Cloudflare, Namecheap, GoDaddy, Route53, etc.) and add the CNAME record as displayed by Render.
6. **Verify Domain in Render:**
   Return to Render and click **Verify**. Render checks DNS resolution.
7. **Confirm HTTPS Certificate Issuance:**
   Once DNS propagation completes, Render automatically requests and installs a Let's Encrypt TLS certificate. The status changes to **Verified** with a green lock icon.

> **DNS Caution:** Do not invent DNS record targets. Always copy the exact target shown in your Render dashboard, as Render binds certificates to the specific service hostname.

---

## 4. Production Environment Variables (Render Dashboard)

Navigate to **Environment** in the Render service dashboard and enter the following variables:

| Variable Name | Required Production Value | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://postgres.<ref>:<pw>@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?sslmode=require` | Supabase Supavisor Transaction Pooler URI |
| `ENVIRONMENT` | `production` | Disables interactive `/docs` and `/openapi.json` |
| `API_HOST` | `0.0.0.0` | Container bind address |
| `CORS_ORIGINS` | `https://www.YOUR_DOMAIN` | Exact frontend origin (no trailing slash, no wildcards) |
| `COOKIE_SECURE` | `true` | Ensures cookies are transmitted over HTTPS only |
| `COOKIE_SAMESITE` | `lax` | SameSite policy for shared-domain topology |
| `COOKIE_DOMAIN` | *(Leave empty / unset)* | **Omit to enforce host-only cookie scoped strictly to `api.YOUR_DOMAIN`** |
| `FASTAPI_PUBLIC_URL` | `https://api.YOUR_DOMAIN` | Public backend URL used in OAuth and email callbacks |
| `FRONTEND_URL` | `https://www.YOUR_DOMAIN` | Public frontend URL for auth success/error redirects |
| `SUPABASE_URL` | `https://ccvnligpunynsxymrane.supabase.co` | Supabase HTTPS endpoint |
| `SUPABASE_ANON_KEY` | `<supabase-anon-key>` | Public anon key from Supabase Dashboard |
| `SUPABASE_SERVICE_ROLE_KEY` | `<supabase-service-role-key>` | Secret key for remote session revocation on logout |
| `SESSION_ENCRYPTION_KEY` | `<64-hex-character-key>` | Primary 256-bit AES-GCM key (`openssl rand -hex 32`) |
| `SESSION_KEY_ID` | `k1` | Key identifier |
| `SESSION_ENCRYPTION_KEY_FALLBACK` | *(Leave empty initially)* | Fallback key for zero-downtime key rotation |
| `SESSION_FALLBACK_KEY_ID` | `k0` | Fallback key identifier |
| `LOG_LEVEL` | `INFO` | Application log level (never logs tokens or credentials) |
| `MAX_RESUME_SIZE_MB` | `5` | Maximum resume upload size |
| `AI_PROVIDERS` | `groq,gemini,nvidia` | Ordered AI provider fallback list (or leave empty to disable) |
| `GROQ_API_KEY` | `<your-groq-api-key>` | Provider API key |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Groq structured output model |
| `GEMINI_API_KEY` | `<your-gemini-api-key>` | Google AI Studio API key |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini evaluation model |
| `NVIDIA_API_KEY` | `<your-nvidia-api-key>` | NVIDIA NIM API key |
| `NVIDIA_MODEL` | `nvidia/nemotron-3-super-120b-a12b` | NVIDIA evaluation model |
| `AI_DAILY_REQUEST_LIMIT` | `2000` | Tenant quota safety circuit breaker |

---

## 5. Security & Key Generation

Generate the production session encryption key on your local machine before pasting into Render:

```bash
openssl rand -hex 32
```

- Output format: 64 hexadecimal characters (256 bits).
- Used by the BFF service to encrypt Supabase refresh tokens using **AES-256-GCM** before storing them in PostgreSQL.
- Never commit this key to version control or paste it into public channels.

---

## 6. Post-Deployment Verification Checklist

1. [ ] Check Render deployment logs: confirm `uvicorn` started on `0.0.0.0:$PORT` with 2 workers.
2. [ ] Test Health Endpoint:
   ```bash
   curl -i https://api.YOUR_DOMAIN/api/health
   ```
   Must return HTTP 200 with:
   ```json
   {"status":"ok","database":"ok"}
   ```
3. [ ] Test CORS Preflight from production frontend origin:
   ```bash
   curl -i -X OPTIONS https://api.YOUR_DOMAIN/api/auth/login \
     -H "Origin: https://www.YOUR_DOMAIN" \
     -H "Access-Control-Request-Method: POST" \
     -H "Access-Control-Request-Headers: Content-Type"
   ```
   Must return HTTP 200 with:
   - `access-control-allow-origin: https://www.YOUR_DOMAIN`
   - `access-control-allow-credentials: true`
4. [ ] Confirm interactive API docs are disabled in production:
   - `GET https://api.YOUR_DOMAIN/docs` returns HTTP 404.
   - `GET https://api.YOUR_DOMAIN/openapi.json` returns HTTP 404.
