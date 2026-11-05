# Custom Domain & DNS Configuration Guide

This guide details the exact DNS records and routing configuration required to host the **AI Interview Coach** under a single shared custom domain:

- **Frontend Application:** `https://www.YOUR_DOMAIN`
- **Backend API Service:** `https://api.YOUR_DOMAIN`
- **Apex Domain Redirect:** `https://YOUR_DOMAIN` → `https://www.YOUR_DOMAIN`

---

## 1. Domain Topology & Cookie Isolation

By using separate subdomains under the same parent domain (`YOUR_DOMAIN`):
1. **Same-Site Context:** Requests from `www.YOUR_DOMAIN` to `api.YOUR_DOMAIN` are considered **same-site** (sharing the same eTLD+1). This allows the browser to include `SameSite=Lax` cookies on credentialed API calls (`credentials: "include"`).
2. **Host-Only Security:** The authentication cookie `coach_session` is issued without a `Domain=` attribute. This scopes the cookie exclusively to `api.YOUR_DOMAIN`. JavaScript running on `www.YOUR_DOMAIN` or any other subdomain has zero access to the cookie.
3. **No Third-Party Cookie Restrictions:** Because the cookie is strictly first-party same-site, modern browser privacy protections (such as Safari ITP and Chrome third-party cookie restrictions) do not block or drop the cookie.

---

## 2. Master DNS Record Mapping Table

Configure the following records in your DNS management console (Cloudflare, Namecheap, GoDaddy, AWS Route 53, Google Domains/Squarespace, etc.):

| Type | Name / Host | Target / Value | TTL | Managed By | Purpose |
|---|---|---|---|---|---|
| **CNAME** | `www` | `cname.vercel-dns.com` | `Auto` (or `300`) | Vercel | Routes `www.YOUR_DOMAIN` to the Next.js frontend |
| **A** | `@` (or root) | `76.76.21.21` | `Auto` (or `300`) | Vercel | Redirects apex domain `YOUR_DOMAIN` to `www` |
| **CNAME** | `api` | `<your-backend>.onrender.com` | `Auto` (or `300`) | Render | Routes `api.YOUR_DOMAIN` to the FastAPI backend |

> **CRITICAL RULE FOR RENDER TARGET:**  
> Do **NOT** guess or invent `<your-backend>.onrender.com`. In your Render Web Service dashboard, navigate to **Settings** → **Custom Domains**, add `api.YOUR_DOMAIN`, and copy the exact CNAME value displayed on screen (e.g. `ai-interview-coach-api.onrender.com`).

---

## 3. Step-by-Step Configuration by Provider

### 3.1 Cloudflare DNS
If your domain uses Cloudflare nameservers:
1. Log in to [Cloudflare Dashboard](https://dash.cloudflare.com/) and select your domain.
2. Navigate to **DNS** → **Records**.
3. Add the Frontend record:
   - **Type:** `CNAME`
   - **Name:** `www`
   - **Target:** `cname.vercel-dns.com`
   - **Proxy status:** **DNS only (Gray Cloud)** during initial certificate provisioning.
4. Add the Apex redirect record:
   - **Type:** `A`
   - **Name:** `@`
   - **IPv4 address:** `76.76.21.21`
   - **Proxy status:** **DNS only (Gray Cloud)**
5. Add the Backend record:
   - **Type:** `CNAME`
   - **Name:** `api`
   - **Target:** `<your-backend>.onrender.com`
   - **Proxy status:** **DNS only (Gray Cloud)**
6. In **SSL/TLS** settings, ensure encryption mode is set to **Full (strict)**.
*(After Vercel and Render successfully issue their TLS certificates, you may optionally switch Proxy status to Proxied / Orange Cloud if desired).*

### 3.2 Standard DNS Providers (Namecheap, GoDaddy, Route 53)
1. Navigate to Advanced DNS settings.
2. Delete any default parking CNAME or A records for `@` or `www`.
3. Add the three records from the table in Section 2.
4. Set TTL to 300 seconds (5 minutes) for rapid propagation.

---

## 4. Verifying DNS Propagation

Run the following commands from your terminal to verify that DNS records have propagated globally:

### Linux / macOS / PowerShell (using `nslookup`):
```bash
# Check frontend CNAME
nslookup www.YOUR_DOMAIN

# Check backend CNAME
nslookup api.YOUR_DOMAIN

# Check apex A record
nslookup YOUR_DOMAIN
```

### Linux / macOS (using `dig`):
```bash
dig CNAME www.YOUR_DOMAIN +short
# Expected output: cname.vercel-dns.com.

dig CNAME api.YOUR_DOMAIN +short
# Expected output: <your-service>.onrender.com.
```

---

## 5. TLS / SSL Certificate Issuance Lifecycle

1. **Vercel Frontend:**
   - Once `www.YOUR_DOMAIN` points to `cname.vercel-dns.com`, Vercel runs an automated ACME challenge.
   - A Let's Encrypt certificate is issued automatically within 1 to 5 minutes.
   - Status in Vercel turns green: **Valid Configuration**.

2. **Render Backend:**
   - In Render, click the **Verify** button next to `api.YOUR_DOMAIN`.
   - Render verifies that `api.YOUR_DOMAIN` points to your `.onrender.com` hostname.
   - Render automatically provisions and installs a Let's Encrypt certificate.
   - Status in Render turns green: **Verified** (with lock symbol).

---

## 6. Common Pitfalls & Troubleshooting

- **Error: `SSL Handshake Failed` or `Too Many Redirects` (Cloudflare users):**
  - Cause: Cloudflare SSL set to "Flexible" instead of "Full (strict)".
  - Fix: In Cloudflare Dashboard, set SSL/TLS mode to **Full (strict)**.
- **Error: `CORS preflight request blocked`:**
  - Cause: Backend `CORS_ORIGINS` was configured with trailing slash or wrong scheme.
  - Fix: Verify Render has `CORS_ORIGINS=https://www.YOUR_DOMAIN` (no trailing slash).
- **Error: `Host not found` after adding CNAME:**
  - Cause: Some DNS registrars append the root domain automatically. If you entered `api.YOUR_DOMAIN` as the host name, it might have resolved to `api.YOUR_DOMAIN.YOUR_DOMAIN`.
  - Fix: Enter only `api` or `www` in the Host field, not the full domain.
