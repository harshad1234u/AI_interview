# Vercel Deployment Guide — Frontend (`https://www.YOUR_DOMAIN`)

This guide provides the exact configuration, dashboard procedures, and verification steps to deploy the **AI Interview Coach** Next.js frontend to **Vercel** under the custom domain `https://www.YOUR_DOMAIN`.

---

## 1. Architecture Overview

- **Service:** Vercel (Production Next.js Edge/Serverless Hosting)
- **Production URL:** `https://www.YOUR_DOMAIN`
- **Optional Apex Redirect:** `https://YOUR_DOMAIN` → redirects to `https://www.YOUR_DOMAIN`
- **Backend API Target:** `https://api.YOUR_DOMAIN`
- **Transport Security:** All client-side fetch requests include `credentials: "include"`. Because both frontend and backend share the parent domain (`YOUR_DOMAIN`), requests are **same-site cross-origin**, enabling host-only `SameSite=Lax` cookies to be sent securely without third-party cookie restrictions.

---

## 2. Vercel Project Configuration

### 2.1 Project Settings
When importing the Git repository in the [Vercel Dashboard](https://vercel.com/dashboard):

| Setting | Value | Notes |
|---|---|---|
| **Project Name** | `ai-interview-coach` | Or your preferred project slug |
| **Framework Preset** | `Next.js` | Automatically detected |
| **Root Directory** | `frontend` | **Crucial:** Must point to the `frontend` subdirectory |
| **Build Command** | `next build` (or `npm run build`) | Default Next.js build |
| **Output Directory** | `.next` | Automatically handled by Vercel |
| **Install Command** | `npm install` | Default |
| **Node.js Version** | `20.x` or `22.x` | Managed in Project Settings → General |

### 2.2 Environment Variables (Vercel Dashboard)
Navigate to **Project Settings** → **Environment Variables** and add:

| Key | Value | Environments |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `https://api.YOUR_DOMAIN` | Production, Preview, Development |

> **Important:** Do NOT append a trailing slash (e.g., use `https://api.YOUR_DOMAIN`, NOT `https://api.YOUR_DOMAIN/`). The application includes client-side normalization to strip accidental trailing slashes, but omitting it in the dashboard is best practice.

---

## 3. Custom Domain Setup in Vercel

### 3.1 Adding the Custom Domain
1. In the Vercel project dashboard, navigate to **Settings** → **Domains**.
2. In the input box, enter your production domain:
   ```text
   www.YOUR_DOMAIN
   ```
3. Click **Add**.
4. Vercel will recommend adding the apex domain as well:
   ```text
   YOUR_DOMAIN (redirects to www.YOUR_DOMAIN)
   ```
   Select this option to automatically redirect bare root traffic (`https://YOUR_DOMAIN`) to `https://www.YOUR_DOMAIN`.

### 3.2 DNS Records Configured at Your Domain Registrar
Vercel will display the required DNS records:

| Record Type | Host / Name | Value / Target | Purpose |
|---|---|---|---|
| **CNAME** | `www` | `cname.vercel-dns.com` | Directs `www.YOUR_DOMAIN` to Vercel |
| **A** *(Optional for Apex redirect)* | `@` (or blank) | `76.76.21.21` | Redirects apex `YOUR_DOMAIN` to `www` |

*Note: If using Cloudflare for DNS, set SSL/TLS mode to **Full (strict)** and set Proxy status to **DNS only** (gray cloud) during initial verification, or enable Cloudflare proxying once SSL is validated.*

### 3.3 Verification & HTTPS Certificate
- Once the DNS records propagate (typically 1–15 minutes), Vercel automatically:
  1. Verifies domain ownership via DNS lookup.
  2. Provisions a free Let's Encrypt TLS/SSL certificate.
  3. Activates automatic HTTPS redirection.
- The status indicator in the Vercel dashboard will change to **Valid Configuration** with a green checkmark.

---

## 4. Local Build & Compatibility Verification

Before deploying, you can verify that the frontend compiles cleanly in production mode locally:

```bash
cd frontend
npm run typecheck
npm run test
npm run build
```

**Expected Local Verification Output:**
```text
✓ Types generated successfully
✓ 45 tests passed
✓ Compiled successfully in ~1000ms
✓ Generating static pages (6/6) in ~900ms
Route (app)
┌ ○ /
├ ○ /_not-found
├ ○ /interview
└ ○ /login
```

---

## 5. Post-Deployment Verification Checklist

Once deployed to Vercel:
1. [ ] Navigate to `https://www.YOUR_DOMAIN` in an incognito window.
2. [ ] Confirm the landing page loads over HTTPS with a valid certificate.
3. [ ] Open DevTools Network tab and verify that the page renders without 404s or script errors.
4. [ ] Check that `https://YOUR_DOMAIN` redirects cleanly to `https://www.YOUR_DOMAIN` (with HTTP 307 or 308).
