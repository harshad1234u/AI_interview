# Supabase Production Configuration Guide

This guide details the exact dashboard settings, redirect configurations, Google OAuth integration, email transport rules, and database pooling parameters required for the **AI Interview Coach** production deployment.

---

## 1. Supabase Project Information

- **Project Ref:** `ccvnligpunynsxymrane`
- **Region:** `ap-south-1` (Mumbai)
- **Supabase API URL:** `https://ccvnligpunynsxymrane.supabase.co`
- **Authentication Architecture:** Backend-For-Frontend (BFF). The frontend never talks directly to Supabase Auth or PostgREST. All authentication requests, PKCE exchanges, and token rotations are mediated server-side by the FastAPI backend on `api.YOUR_DOMAIN`.

---

## 2. Authentication -> URL Configuration

Navigate to **Authentication** → **URL Configuration** in the [Supabase Dashboard](https://supabase.com/dashboard/project/ccvnligpunynsxymrane/auth/url-configuration):

### 2.1 Site URL
Set the primary production frontend URL:
```text
https://www.YOUR_DOMAIN
```

### 2.2 Redirect URLs (Allowlist)
Add both of the following application backend endpoints to the Redirect URLs table:

1. **Email Verification Callback:**
   ```text
   https://api.YOUR_DOMAIN/api/auth/verify
   ```
2. **Google OAuth Callback:**
   ```text
   https://api.YOUR_DOMAIN/api/auth/google/callback
   ```

*(Keep `http://localhost:8000/api/auth/verify` and `http://localhost:8000/api/auth/google/callback` if you wish to preserve local development testing concurrently).*

---

## 3. Google OAuth 2.0 Integration

To enable "Continue with Google", you must connect Supabase with Google Cloud Console.

### 3.1 Critical Distinction: Callback URLs
There are two distinct callback URLs in this architecture:

| URL Type | Exact URL | Where Registered |
|---|---|---|
| **Supabase Provider Callback URL** | `https://ccvnligpunynsxymrane.supabase.co/auth/v1/callback` | **Google Cloud Console** (Authorized Redirect URI) |
| **Application Backend Callback URL** | `https://api.YOUR_DOMAIN/api/auth/google/callback` | **Supabase Dashboard** (Redirect URLs Allowlist) |

### 3.2 Google Cloud Console Configuration
1. Open [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services** → **Credentials**.
2. Configure **OAuth consent screen**:
   - User Type: **External**
   - App Name: `AI Interview Coach`
   - Scopes: `openid`, `.../auth/userinfo.email`, `.../auth/userinfo.profile`
3. Create Credentials → **OAuth client ID**:
   - Application Type: **Web application**
   - Name: `AI Interview Coach Web Client`
   - **Authorized JavaScript origins:**
     - `https://www.YOUR_DOMAIN`
     - `https://api.YOUR_DOMAIN`
   - **Authorized redirect URIs:**
     - `https://ccvnligpunynsxymrane.supabase.co/auth/v1/callback`
4. Copy the generated **Client ID** and **Client Secret**.

### 3.3 Supabase Provider Settings
1. In the Supabase Dashboard, navigate to **Authentication** → **Providers** → **Google**.
2. Toggle **Enable Google provider** to `ON`.
3. Paste:
   - **Client ID (for OAuth):** `<your-google-client-id>`
   - **Client Secret (for OAuth):** `<your-google-client-secret>`
4. Confirm the Callback URL displayed matches:
   `https://ccvnligpunynsxymrane.supabase.co/auth/v1/callback`
5. Click **Save**.

---

## 4. Custom SMTP & Email Delivery

The default Supabase built-in email service is capped at **30 emails per hour** for testing. In production, registration and password recovery emails will fail once that cap is reached.

### 4.1 Configuring Custom SMTP
1. Navigate to **Project Settings** → **Authentication** → **SMTP Settings**.
2. Toggle **Enable Custom SMTP** to `ON`.
3. Enter your transactional email provider details (e.g. Resend, SendGrid, AWS SES, Postmark):
   - **Sender Email:** `noreply@YOUR_DOMAIN`
   - **Sender Name:** `AI Interview Coach`
   - **Host:** `smtp.resend.com` (or provider host)
   - **Port:** `465` (SSL) or `587` (TLS)
   - **Username:** `resend` (or API user)
   - **Password:** `<provider-api-key>`
4. Click **Save**.

### 4.2 Email Confirmation Template
In **Authentication** → **Email Templates** → **Confirm signup**, configure the confirmation link format:

```html
<h2>Confirm your signup</h2>
<p>Follow this link to confirm your account:</p>
<p><a href="{{ if .RedirectTo }}{{ .RedirectTo }}{{ else }}https://api.YOUR_DOMAIN/api/auth/verify{{ end }}?token_hash={{ .TokenHash }}&type=signup">Confirm your email</a></p>
```
*(Or simply `<a href="{{ .RedirectTo }}?token_hash={{ .TokenHash }}&type=signup">Confirm your email</a>`)*.

#### Architectural Rationale & Why `{{ .SiteURL }}` and `{{ .ConfirmationURL }}` Must NOT Be Used:
1. **Why NOT `{{ .SiteURL }}`:**
   - Supabase `Site URL` is configured as the frontend (`https://www.YOUR_DOMAIN`).
   - If the template used `{{ .SiteURL }}/api/auth/verify...`, the link would point to `https://www.YOUR_DOMAIN/api/auth/verify`.
   - `https://www.YOUR_DOMAIN` is hosted on Vercel, which does not host the FastAPI backend router. The candidate would receive a **404 Not Found**.
2. **Why NOT `{{ .ConfirmationURL }}`:**
   - `{{ .ConfirmationURL }}` executes Supabase GoTrue's client-side flow: it verifies the token on `https://<project-ref>.supabase.co/auth/v1/verify` and then redirects the browser with access and refresh tokens in the URL fragment (`#access_token=...&refresh_token=...`).
   - This violates the Backend-For-Frontend (BFF) architecture and exposes sensitive Supabase tokens to browser JavaScript.
3. **Why `{{ .RedirectTo }}` Is Correct:**
   - During registration, the FastAPI backend dynamically passes `redirect_to = f"{settings.fastapi_public_url}/api/auth/verify"`.
   - In production, `{{ .RedirectTo }}` expands to `https://api.YOUR_DOMAIN/api/auth/verify`.
   - In local development, `{{ .RedirectTo }}` expands to `http://localhost:8000/api/auth/verify`.

#### 4.3 Scanner Prefetch Resistance & Threat Model Boundaries
Email security scanners (such as Microsoft Defender Safe Links, Apple Mail Privacy Protection, and Google Mail link-preview bots) automatically prefetch or crawl links in incoming emails using background `GET` requests. If token verification were performed on `GET`, the single-use Supabase GoTrue verification token would be consumed prematurely, causing the candidate to see an expired token error when they subsequently clicked the link.

To address this, email verification uses a **hardened two-step confirmation process**:

1. **Step 1: GET Confirmation Page (GET-Prefetch Safe):**
   - Automated scanners and candidates both land on `GET https://api.YOUR_DOMAIN/api/auth/verify?token_hash=...&type=signup`.
   - The GET endpoint **does NOT consume the GoTrue verification token**. Repeated GET requests leave the single-use token completely unconsumed and valid.
   - Strict token format validation (`^[a-zA-Z0-9_.\-@]{1,256}$`) rejects malformed, oversized, or script-injected hashes before rendering.
   - All dynamically inserted hidden inputs (`token_hash`, `type`, `csrf_token`) are strictly escaped with `html.escape(..., quote=True)` to prevent attribute breaking or HTML injection.
   - The backend renders a lightweight, self-contained confirmation page with:
     - `Referrer-Policy: no-referrer` header & `<meta name="referrer" content="no-referrer">` (prevents token leakage).
     - `<meta name="robots" content="noindex, nofollow, noarchive">` and `Cache-Control: no-store, no-cache, must-revalidate, max-age=0` (prevents indexing and caching).
     - An anti-CSRF HMAC signature binding `token_hash` and `type` to server secrets.
     - An explicit **Confirm Email** button that submits a `POST` request.

2. **Step 2: Explicit POST Confirmation:**
   - The single-use verification token is consumed **exclusively** when an explicit form submission triggers `POST https://api.YOUR_DOMAIN/api/auth/verify`.
   - The backend validates the CSRF HMAC signature and consumes the token via server-to-server GoTrue verification.
   - The candidate is idempotently provisioned in PostgreSQL, the refresh token is encrypted with AES-256-GCM, the host-only `coach_session` HttpOnly cookie is set, and the browser is redirected via HTTP 303 to `https://www.YOUR_DOMAIN/interview`.
   - If the token is invalid, expired, or already used, the candidate is redirected to `https://www.YOUR_DOMAIN/login?error=verification_expired`.

#### 4.4 Security Boundaries & Known Limitations
1. **Scope of Email-Scanner Protection:**
   - **What is protected:** This architecture provides robust **GET-prefetch resistance** against automated scanners that crawl or preview links using HTTP `GET` requests without form execution.
   - **What is NOT protected (Explicit Limitation):** Advanced security sandbox environments or headless browser crawlers that are configured to execute JavaScript AND automatically submit HTML forms or click interactive buttons will still trigger the POST request. This implementation provides **GET-prefetch resistance**, not guaranteed immunity against arbitrary automated form-submitting sandbox agents.
2. **Infrastructure-Level Access Log Redaction:**
   - **Application-Level Protection:** Within the FastAPI application container, `SensitiveDataRedactionFilter` intercepts all log records across application, Uvicorn, and HTTP client loggers, automatically masking query parameters (`token_hash=[REDACTED]`).
   - **Render Edge-Router Limitation:** Render's platform reverse proxies and edge routing layer (which terminate TLS before forwarding traffic to the container) record incoming HTTP request lines into Render dashboard access logs. The query string of the initial GET request (`?token_hash=...`) cannot be redacted by container-level Python code.
   - **Render Dashboard Mitigation:** In the Render Dashboard, do not expose public log drains or dashboard view access to unauthorized personnel. If strict zero-query-logging is required at the edge, configure a Cloudflare WAF / Cloudflare Worker upstream to strip or encrypt sensitive query strings before forwarding to Render.

---

## 5. Database Connection Pooling (Supavisor)

For multi-worker production deployments on Render, connect through Supabase's managed connection pooler:

1. Navigate to **Project Settings** → **Database**.
2. Under **Connection parameters**, locate **Connection Pooling**:
   - **Mode:** `Transaction`
   - **Port:** `6543`
3. Copy the URI string:
   ```text
   postgresql+psycopg://postgres.ccvnligpunynsxymrane:<db-password>@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?sslmode=require
   ```
4. Set this as `DATABASE_URL` in the Render dashboard.

> **Zero Prepared Statement Conflicts:** The FastAPI backend's SQLAlchemy configuration in `backend/app/db/session.py` explicitly sets `connect_args={"prepare_threshold": None}`. This ensures psycopg3 will not cache named prepared statements, avoiding any `prepared statement does not exist` or `already exists` errors across pooled connections.

---

## 6. Database Migration `f9a1b2c3d4e5` (Awaiting Operator Approval)

The pending schema migration adds the `refresh_lock_id UUID` column to `public.auth_sessions` to enable atomic Compare-And-Swap (CAS) fencing during Supabase refresh-token rotations.

### Verification Status:
- Current cloud database revision: `e8f3b2c1a0d9` (verified via query).
- Column `refresh_lock_id`: Not yet added.
- Compatibility: 100% compatible. Non-blocking, nullable column addition.

### Execution Script (Paste in Supabase SQL Editor):
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

### Rollback Script (if ever needed):
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
