# Production Configuration & Deployment Guide

This guide details the exact environment variables, cloud provider settings, domain topology options, and external credentials required to deploy the **AI Interview Coach** application to production across **Vercel** (Frontend), **Render** (Backend), **Supabase** (Database & Auth), and **Google Cloud Console** (OAuth 2.0).

---

## 1. Supabase Dashboard Setup

**Target Supabase Project:** `ccvnligpunynsxymrane` (or your dedicated production project)  
**Region:** `ap-south-1` (Mumbai)

### 1.1 Authentication -> URL Configuration
Navigate to **Authentication** -> **URL Configuration**:
1. **Site URL:**
   - Topology A (Custom Domain): `https://app.yourdomain.com`
   - Topology B (Vercel Subdomain): `https://<your-frontend-domain>.vercel.app`
2. **Redirect URLs (Add both):**
   - Verification Callback:
     - Topology A: `https://api.yourdomain.com/api/auth/verify`
     - Topology B: `https://<your-backend-domain>.onrender.com/api/auth/verify`
   - Google OAuth Callback:
     - Topology A: `https://api.yourdomain.com/api/auth/google/callback`
     - Topology B: `https://<your-backend-domain>.onrender.com/api/auth/google/callback`

### 1.2 Authentication -> Providers -> Google
Navigate to **Authentication** -> **Providers** -> **Google**:
1. Toggle **Enable Google provider** to `ON`.
2. Enter **Client ID (for OAuth)** from Google Cloud Console (see Section 2).
3. Enter **Client Secret (for OAuth)** from Google Cloud Console.
4. Note the Supabase Callback URL displayed:
   `https://<project-ref>.supabase.co/auth/v1/callback`
   (This must be registered in Google Cloud Console).
5. Click **Save**.

### 1.3 Authentication -> Email Templates & Custom SMTP
The built-in Supabase email service is strictly rate-limited to 30 emails per hour for development. For production:
1. Navigate to **Project Settings** -> **Authentication** -> **SMTP Settings**.
2. Toggle **Enable Custom SMTP** to `ON`.
3. Provide your transactional email provider credentials (e.g. Resend, SendGrid, AWS SES, Postmark):
   - **Sender Email:** `noreply@yourdomain.com`
   - **Sender Name:** `AI Interview Coach`
   - **Host:** `smtp.resend.com` (or provider host)
   - **Port:** `465` or `587`
   - **Username:** `resend` (or API user)
   - **Password:** `<provider-api-key>`

### 1.4 Database Connection Pooler (Supavisor)
For production backend deployments on Render:
1. Navigate to **Project Settings** -> **Database**.
2. Under **Connection parameters**, locate **Connection Pooling**:
   - **Mode:** `Transaction`
   - **Port:** `6543`
3. Copy the **URI** connection string.
   Format:
   ```text
   postgresql+psycopg://postgres.<project-ref>:<db-password>@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?sslmode=require
   ```
> **Driver Compatibility Note:** The backend's SQLAlchemy engine in `backend/app/db/session.py` is configured with `connect_args={"connect_timeout": 5, "prepare_threshold": None}`. This disables psycopg3's client-side prepared statement cache, ensuring 100% reliability with Supavisor transaction poolers on port 6543.

### 1.5 Database Migration Execution (Supabase SQL Editor)
> **IMPORTANT:** Awaiting explicit operator approval before execution on the live database.

Before deploying the updated backend to production, run the Phase 3 CAS fencing schema migration in the **Supabase SQL Editor**:

```sql
BEGIN;

-- 1. Add CAS fencing column to auth_sessions
ALTER TABLE public.auth_sessions 
ADD COLUMN IF NOT EXISTS refresh_lock_id UUID NULL;

-- 2. Update Alembic schema version tracking
UPDATE public.alembic_version 
SET version_num = 'f9a1b2c3d4e5' 
WHERE version_num = 'e8f3b2c1a0d9';

COMMIT;
```

#### Rollback / Downgrade SQL (if ever needed):
```sql
BEGIN;

-- 1. Drop CAS fencing column
ALTER TABLE public.auth_sessions 
DROP COLUMN IF EXISTS refresh_lock_id;

-- 2. Revert Alembic schema version tracking
UPDATE public.alembic_version 
SET version_num = 'e8f3b2c1a0d9' 
WHERE version_num = 'f9a1b2c3d4e5';

COMMIT;
```

---

## 2. Google Cloud Console Setup

### 2.1 OAuth 2.0 Web Client Creation
1. Go to [Google Cloud Console](https://console.cloud.google.com/) -> **APIs & Services** -> **Credentials**.
2. Configure **OAuth consent screen**:
   - User Type: **External**
   - App Name: `AI Interview Coach`
   - User support email: `your-email@example.com`
   - Developer contact email: `your-email@example.com`
   - Scopes: `openid`, `.../auth/userinfo.email`, `.../auth/userinfo.profile`
3. Click **Create Credentials** -> **OAuth client ID**:
   - Application type: **Web application**
   - Name: `AI Interview Coach Web Client`
   - **Authorized JavaScript origins:**
     - Topology A: `https://app.yourdomain.com`, `https://api.yourdomain.com`
     - Topology B: `https://<your-frontend-domain>.vercel.app`, `https://<your-backend-domain>.onrender.com`
   - **Authorized redirect URIs:**
     - `https://<your-supabase-project-id>.supabase.co/auth/v1/callback`
4. Click **Create**.
5. Copy the **Client ID** and **Client Secret**.

---

## 3. Domain Topology & Cookie Architecture

Modern web browsers enforce strict security boundaries on cookies sent via cross-origin `fetch(..., { credentials: "include" })`. Choose one of the following two production deployment topologies:

### Topology A: Shared Custom Domain (Recommended for Production)
Frontend and backend share a common second-level domain (e.g. `app.example.com` and `api.example.com`).
- **Advantages:** First-party cookie context, highest security, immune to browser third-party cookie restrictions, supports `SameSite=Lax`.
- **Backend Configuration:**
  ```text
  COOKIE_DOMAIN=.example.com
  COOKIE_SAMESITE=lax
  COOKIE_SECURE=true
  CORS_ORIGINS=https://app.example.com
  FRONTEND_URL=https://app.example.com
  FASTAPI_PUBLIC_URL=https://api.example.com
  ```
- **Frontend Configuration:**
  ```text
  NEXT_PUBLIC_API_URL=https://api.example.com
  ```

### Topology B: Default Platform Subdomains (Vercel + Render)
Frontend runs on `*.vercel.app` and backend runs on `*.onrender.com`.
- **Context:** Because `vercel.app` and `onrender.com` are distinct public suffixes, browser requests from frontend to backend are **cross-site**.
- **Backend Configuration:**
  ```text
  COOKIE_DOMAIN=
  COOKIE_SAMESITE=none
  COOKIE_SECURE=true
  CORS_ORIGINS=https://<your-app>.vercel.app
  FRONTEND_URL=https://<your-app>.vercel.app
  FASTAPI_PUBLIC_URL=https://<your-api>.onrender.com
  ```
- **Frontend Configuration:**
  ```text
  NEXT_PUBLIC_API_URL=https://<your-api>.onrender.com
  ```

---

## 4. Render (Backend Deployment) Setup

Deploy the backend as a **Web Service** on [Render](https://render.com).

### 4.1 Service Specification
- **Runtime:** `Python` (3.11.x)
- **Root Directory:** `backend`
- **Build Command:**
  ```bash
  pip install --upgrade pip && pip install -r requirements.txt
  ```
- **Start Command:**
  ```bash
  uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 2
  ```
- **Health Check Path:** `/api/health`

### 4.2 Environment Variables (Render Dashboard)

| Variable Name | Required Value / Format | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://postgres.<ref>:<pw>@<pooler>:6543/postgres?sslmode=require` | Supabase Supavisor Transaction Pooler URI |
| `ENVIRONMENT` | `production` | Disables interactive OpenAPI/Swagger docs in production |
| `API_HOST` | `0.0.0.0` | Bind host for Render container |
| `CORS_ORIGINS` | `https://<your-frontend-domain>` | Exact frontend origin (no trailing slash, no wildcards) |
| `COOKIE_SECURE` | `true` | Ensures cookies are transmitted over HTTPS only (synonym: `AUTH_COOKIE_SECURE`) |
| `COOKIE_SAMESITE` | `lax` (Topology A) or `none` (Topology B) | Cookie SameSite policy |
| `COOKIE_DOMAIN` | `.yourdomain.com` (Topology A) or leave empty (Topology B) | Scope cookie across shared subdomains |
| `SUPABASE_URL` | `https://<your-project-id>.supabase.co` | Supabase GoTrue HTTPS API endpoint |
| `SUPABASE_ANON_KEY` | `<supabase-anon-key>` | Public API key for client-safe operations |
| `SUPABASE_SERVICE_ROLE_KEY` | `<supabase-service-role-key>` | Secret key for remote session revocation on logout |
| `SESSION_ENCRYPTION_KEY` | `<64-hex-character-key>` | Primary 256-bit AES-GCM key (`openssl rand -hex 32`) |
| `SESSION_KEY_ID` | `k1` | Key identifier tag |
| `SESSION_ENCRYPTION_KEY_FALLBACK` | *(Leave empty initially)* | Fallback key used only during zero-downtime key rotation |
| `SESSION_FALLBACK_KEY_ID` | `k0` | Fallback key identifier |
| `FASTAPI_PUBLIC_URL` | `https://<your-backend-domain>` | Public backend URL for OAuth callback redirects |
| `FRONTEND_URL` | `https://<your-frontend-domain>` | Public frontend URL for auth success/error redirects |
| `LOG_LEVEL` | `INFO` | Application log level (never logs tokens or credentials) |
| `MAX_RESUME_SIZE_MB` | `5` | Maximum resume upload size |
| `AI_PROVIDERS` | `groq,gemini,nvidia` | Priority-ordered AI providers (or empty to disable) |
| `GROQ_API_KEY` | `<your-groq-api-key>` | Provider secret key |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Structured JSON output model |
| `GEMINI_API_KEY` | `<your-gemini-api-key>` | Google AI Studio secret key |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Multimodal / fast evaluation model |
| `NVIDIA_API_KEY` | `<your-nvidia-api-key>` | NVIDIA NIM secret key |
| `NVIDIA_MODEL` | `nvidia/nemotron-3-super-120b-a12b` | High-parameter evaluation fallback |
| `AI_DAILY_REQUEST_LIMIT` | `2000` | Global tenant safety limit |

---

## 5. Vercel (Frontend Deployment) Setup

Deploy the frontend to [Vercel](https://vercel.com).

### 5.1 Project Settings
- **Framework Preset:** `Next.js`
- **Root Directory:** `frontend`
- **Build Command:** `next build` (or default)
- **Output Directory:** `.next` (default)
- **Node.js Version:** `20.x` or `22.x`

### 5.2 Environment Variables (Vercel Dashboard)

| Variable Name | Environment | Value | Description |
|---|---|---|---|
| `NEXT_PUBLIC_API_URL` | Production, Preview, Development | `https://<your-backend-domain>` | Backend API URL (no trailing slash) |

> **BFF Transport Invariant:** All browser requests to the backend automatically transmit cookies with `credentials: 'include'`. The `coach_session` cookie is marked `HttpOnly` and `Secure`, ensuring that zero tokens or session secrets are accessible to client-side scripts.

---

## 6. Key Generation & Secret Security

### 6.1 Session Encryption Key Generation
Run the following command locally in your terminal to generate a cryptographically secure 256-bit key:
```bash
openssl rand -hex 32
```
Example output format: `f8a4e32d91b7c014...` (64 hex characters).  
Set this value as `SESSION_ENCRYPTION_KEY` in the Render dashboard. Never commit this key to version control or paste it in public forums.

### 6.2 Key Rotation Procedure (Zero Downtime)
When rotating encryption keys in production:
1. Generate a new 256-bit key: `openssl rand -hex 32`.
2. Move the existing key to `SESSION_ENCRYPTION_KEY_FALLBACK` with `SESSION_FALLBACK_KEY_ID=k1`.
3. Set the new key as `SESSION_ENCRYPTION_KEY` with `SESSION_KEY_ID=k2`.
4. Deploy the updated environment variables. Existing active sessions will seamlessly decrypt via the fallback key and automatically re-encrypt under the new key on their next refresh.
5. After all active sessions have refreshed (e.g. 7 days), remove `SESSION_ENCRYPTION_KEY_FALLBACK`.

---

## 7. Post-Deployment Verification Checklist

Once services are provisioned and environment variables are entered:

1. [ ] **Health Check:**  
   `GET https://<your-backend-domain>/api/health` returns `{"status": "ok", "database": "ok"}` with HTTP 200.
2. [ ] **Frontend Landing Page:**  
   Visit `https://<your-frontend-domain>` - page loads cleanly without unauthenticated redirect loops.
3. [ ] **Email Registration & Verification:**  
   Register a test email -> verify email received -> click confirmation link -> redirected to `/interview` with authenticated `coach_session` cookie.
4. [ ] **Google OAuth:**  
   Click "Continue with Google" -> authenticate on Google -> redirected back to `/interview` -> candidate profile populated.
5. [ ] **Session Refresh:**  
   Inspect backend logs after calling `/api/auth/me` or `/api/auth/refresh` -> token rotated and CAS fencing validated.
6. [ ] **Logout Invalidation:**  
   Click "Sign Out" -> session revoked in database -> remote Supabase session terminated -> browser cookie cleared -> `/api/auth/me` returns 401.
