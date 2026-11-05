# Production End-to-End (E2E) Verification Checklist

This document defines the live validation protocol for the **AI Interview Coach** production deployment under the shared custom domain topology:
- **Frontend:** `https://www.YOUR_DOMAIN`
- **Backend API:** `https://api.YOUR_DOMAIN`

---

## 1. Pre-Flight Infrastructure Checks

Before testing user journeys, run these automated terminal checks:

### 1.1 DNS Resolution & SSL Verification
- [ ] **Frontend DNS:** `nslookup www.YOUR_DOMAIN` resolves to `cname.vercel-dns.com`.
- [ ] **Backend DNS:** `nslookup api.YOUR_DOMAIN` resolves to your Render service (`<service>.onrender.com`).
- [ ] **Apex Redirect:** `curl -I https://YOUR_DOMAIN` returns HTTP 307 or 308 redirecting to `https://www.YOUR_DOMAIN`.
- [ ] **HTTPS Certificates:** Both `https://www.YOUR_DOMAIN` and `https://api.YOUR_DOMAIN` load without TLS warnings.

### 1.2 Backend Health & Security Endpoints
- [ ] **Health Check:**
  ```bash
  curl -i https://api.YOUR_DOMAIN/api/health
  ```
  Returns:
  ```json
  HTTP/2 200
  content-type: application/json

  {"status":"ok","database":"ok"}
  ```
- [ ] **Interactive Docs Disabled (Security Invariant):**
  - `curl -I https://api.YOUR_DOMAIN/docs` returns `404 Not Found`.
  - `curl -I https://api.YOUR_DOMAIN/openapi.json` returns `404 Not Found`.
- [ ] **CORS Preflight Check:**
  ```bash
  curl -i -X OPTIONS https://api.YOUR_DOMAIN/api/auth/login \
    -H "Origin: https://www.YOUR_DOMAIN" \
    -H "Access-Control-Request-Method: POST" \
    -H "Access-Control-Request-Headers: Content-Type"
  ```
  Returns:
  - `HTTP/2 200`
  - `access-control-allow-origin: https://www.YOUR_DOMAIN`
  - `access-control-allow-credentials: true`

---

## 2. Live User Journey Verification Checklist

Execute these 12 test flows in an incognito/private browser session:

### Step 1: Open Custom Frontend Domain
- [ ] Navigate to `https://www.YOUR_DOMAIN`.
- [ ] Confirm page loads cleanly without redirect loops.
- [ ] Confirm browser address bar shows secure padlock icon (`https://www.YOUR_DOMAIN`).

### Step 2: Register a New Account
- [ ] Click "Sign up" or navigate to `/login`.
- [ ] Enter a test email (e.g., `test-candidate@YOUR_DOMAIN`), valid password (12+ characters, mixed case, number, symbol), and candidate name.
- [ ] Click "Register".
- [ ] Confirm the UI displays: "Verification email sent. Please check your inbox to activate your account."
- [ ] In DevTools Network tab, inspect response from `POST https://api.YOUR_DOMAIN/api/auth/register`:
  - Returns HTTP 201 with `{ "verification_required": true }`.
  - Zero sensitive tokens or passwords are returned in the response body.

### Step 3: Verify Email (Hardened Two-Step Flow)
- [ ] Check inbox for confirmation email from `noreply@YOUR_DOMAIN`.
- [ ] Inspect the link URL before clicking:
  - Must target the backend: `https://api.YOUR_DOMAIN/api/auth/verify?token_hash=...&type=signup`
  - Must **NOT** target the frontend (`www.YOUR_DOMAIN/api/auth/verify` would 404).
- [ ] **Test Scanner Prefetch Safety (Step 1 - GET):** Click or open the confirmation link in the browser:
  - Verify browser renders the lightweight **Confirm Your Email** page (HTTP 200 OK).
  - Verify no session cookie is issued on GET.
  - Refresh the page or open it in multiple tabs: verify repeated GET requests do NOT consume the token.
- [ ] **Confirm Email (Step 2 - POST):** Click the **Confirm Email** button on the confirmation page:
  - Form issues a POST request with CSRF HMAC protection.
  - Token is verified server-to-server with Supabase GoTrue.
  - Browser is cleanly redirected via HTTP 303 to `https://www.YOUR_DOMAIN/interview`.
- [ ] Inspect DevTools Application → Cookies for `api.YOUR_DOMAIN`:
  - Cookie Name: `coach_session`
  - Domain: `api.YOUR_DOMAIN` (host-only, no leading dot)
  - HttpOnly: Checked (`true`)
  - Secure: Checked (`true`)
  - SameSite: `Lax`
  - Path: `/`
- [ ] **Test Single-Use Protection (Token Reuse):** Open the original link and click **Confirm Email** a second time:
  - Browser redirects to `https://www.YOUR_DOMAIN/login?error=verification_expired`.
  - UI displays error alert: *"Your verification link has expired or is invalid. Please register again or request a new link."*
- [ ] **Test Invalid/Malformed Token Protection:** Manually visit `https://api.YOUR_DOMAIN/api/auth/verify?token_hash=bogus_hash_123&type=signup` and submit:
  - Browser redirects to `https://www.YOUR_DOMAIN/login?error=verification_expired`.
- [ ] **Test Missing Token Protection:** Manually visit `https://api.YOUR_DOMAIN/api/auth/verify`:
  - Browser redirects to `https://www.YOUR_DOMAIN/login?error=verification_expired` (no 422 JSON error).
- [ ] **Verify Log Redaction & Security Boundaries:**
  - Inspect application logs in container stdout: verify query parameters such as `token_hash` are redacted (`token_hash=[REDACTED]`).
  - Note: Platform edge proxy access logs on Render dashboard record the external HTTP GET request URI; ensure Render dashboard access is restricted to authorized operations staff.
  - Note: Two-step confirmation provides GET-prefetch resistance against automated email scanners; scanners equipped with full headless browser form-submission agents may still interact with the page.

### Step 4: Log In
- [ ] In another browser tab/window, test manual email/password login at `https://www.YOUR_DOMAIN/login`.
- [ ] Enter confirmed email and password.
- [ ] Click "Sign in".
- [ ] Confirm immediate redirect to `/interview` with candidate profile loaded.

### Step 5: Refresh the Browser
- [ ] While on `https://www.YOUR_DOMAIN/interview`, press `F5` / `Cmd+R` to reload the page.
- [ ] Verify page does NOT bounce back to `/login`.

### Step 6: Confirm Session Persistence & Refresh
- [ ] Inspect DevTools Network tab on page reload.
- [ ] Observe call to `GET https://api.YOUR_DOMAIN/api/auth/me`.
- [ ] Returns HTTP 200 with candidate details (`id`, `name`, `email`).
- [ ] Observe that the request automatically included the `coach_session` cookie via `credentials: "include"`.

### Step 7: Test Google OAuth
- [ ] Open a separate clean browser profile / incognito window.
- [ ] Visit `https://www.YOUR_DOMAIN/login`.
- [ ] Click "Continue with Google".
- [ ] Confirm browser redirects through `api.YOUR_DOMAIN/api/auth/google/start` to Google's sign-in screen.
- [ ] Select or log in with a Google Account.
- [ ] Confirm Google redirects to Supabase (`ccvnligpunynsxymrane.supabase.co/auth/v1/callback`), which redirects to `api.YOUR_DOMAIN/api/auth/google/callback`, which redirects to `https://www.YOUR_DOMAIN/interview`.
- [ ] Confirm candidate profile displays your Google name and email.

### Step 8: Test Password Recovery
- [ ] On `/login`, click "Forgot Password?".
- [ ] Enter registered email address.
- [ ] Click "Send Reset Link".
- [ ] Confirm password reset email arrives with custom domain sender.
- [ ] Click reset link and submit a new valid password.
- [ ] Confirm login succeeds with the new password.

### Step 9: Log Out
- [ ] While logged in on `/interview`, click "Sign out".
- [ ] Verify browser immediately redirects to `/login`.

### Step 10: Confirm Session Revocation
- [ ] In DevTools Application → Cookies, verify `coach_session` cookie has been cleared (max-age=0).
- [ ] Attempt to manually navigate back to `https://www.YOUR_DOMAIN/interview`.
- [ ] Confirm application immediately detects missing/unauthorized session and redirects to `/login`.
- [ ] Attempt to call `curl -i https://api.YOUR_DOMAIN/api/auth/me` with the previous cookie value -> returns HTTP 401 Unauthorized.

### Step 11: Create and Complete a Mock Interview
- [ ] Log back in to `/interview`.
- [ ] Upload a test resume PDF (or enter manual candidate skills/facts).
- [ ] Confirm resume parsing completes and candidate facts appear for review.
- [ ] Click "Start Interview" to generate the tailored interview plan.
- [ ] Answer generated interview questions (test voice input or text input).
- [ ] Test follow-up question logic and response submission.
- [ ] Complete all interview turns.

### Step 12: Verify Report Generation & Saved History
- [ ] Confirm interview state machine transitions cleanly to `REPORT_GENERATION` and then `COMPLETED`.
- [ ] Verify comprehensive report renders on screen with:
  - Technical accuracy score
  - Criterion evaluations
  - Speech pacing and filler word analysis (if audio used)
  - Topic-by-topic performance breakdown
- [ ] Click "Export Report" / "Download Document" and confirm clean formatting.
- [ ] Check interview history list to confirm the session is persisted and reviewable.
