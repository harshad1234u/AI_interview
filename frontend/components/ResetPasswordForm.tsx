"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";
import { ApiError, resetPassword } from "@/lib/api";
import {
  PASSWORD_MAX_LENGTH,
  PASSWORD_MIN_LENGTH,
  parseRecoveryHash,
  type ResetPasswordFieldErrors,
  type ResetPasswordForm as FormState,
  validateResetPasswordForm,
} from "@/lib/auth";

export default function ResetPasswordForm() {
  const searchParams = useSearchParams();

  // Token is stored strictly in memory state, NEVER in localStorage or sessionStorage.
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [isInitializing, setIsInitializing] = useState(true);

  // Form states
  const [form, setForm] = useState<FormState>({ password: "", confirmPassword: "" });
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<ResetPasswordFieldErrors>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;

    // Check hash first (Supabase default for recovery), then query params
    const rawHash = window.location.hash;
    const rawSearch = window.location.search;

    let token: string | null = null;
    let urlErr: string | null = null;

    if (rawHash) {
      const parsed = parseRecoveryHash(rawHash);
      if (parsed.error || parsed.errorCode) {
        if (parsed.errorCode === "otp_expired" || parsed.error === "access_denied") {
          urlErr = "This password reset link has expired or has already been used. Please request a new link.";
        } else {
          urlErr = parsed.errorDescription
            ? decodeURIComponent(parsed.errorDescription.replace(/\+/g, " "))
            : "Authentication error. The reset link is invalid.";
        }
      } else if (parsed.accessToken) {
        token = parsed.accessToken;
      }
    }

    if (!token && !urlErr && searchParams) {
      const err = searchParams.get("error");
      const errCode = searchParams.get("error_code");
      const errDesc = searchParams.get("error_description");
      if (errCode === "otp_expired" || err === "access_denied") {
        urlErr = "This password reset link has expired or has already been used. Please request a new link.";
      } else if (err) {
        urlErr = errDesc
          ? decodeURIComponent(errDesc.replace(/\+/g, " "))
          : "Authentication error. The reset link is invalid.";
      } else {
        const queryToken = searchParams.get("access_token");
        if (queryToken) token = queryToken;
      }
    }

    // Immediately scrub token and sensitive hash fragments from browser URL and history
    if (rawHash || rawSearch.includes("access_token")) {
      window.history.replaceState(null, "", window.location.pathname);
    }

    /* eslint-disable react-hooks/set-state-in-effect -- URL fragment token extraction on mount */
    if (urlErr) {
      setError(urlErr);
    } else if (token) {
      setAccessToken(token);
    }

    setIsInitializing(false);
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [searchParams]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);

    const errors = validateResetPasswordForm(form);
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;

    if (!accessToken) {
      setError("No valid reset session was found. Please request a new password reset link.");
      return;
    }

    setBusy(true);
    try {
      await resetPassword(accessToken, form.password);
      setSuccess(true);
      setForm({ password: "", confirmPassword: "" });
      setAccessToken(null); // Clear token from memory
    } catch (err) {
      setForm((prev) => ({ ...prev, password: "", confirmPassword: "" }));
      if (err instanceof ApiError) {
        if (err.status === 401 || err.status === 403) {
          setError("Your password reset link has expired or is invalid. Please request a new link.");
          setAccessToken(null);
        } else {
          setError(err.message);
        }
      } else {
        setError("Could not update password. Please check your connection and try again.");
      }
    } finally {
      setBusy(false);
    }
  }

  if (isInitializing) {
    return (
      <section className="auth-card" style={{ textAlign: "center", padding: "2.5rem 1.5rem" }}>
        <div className="badge badge-primary" style={{ marginBottom: "0.75rem" }}>Verifying Link…</div>
        <p className="hint">Validating your password recovery credentials…</p>
      </section>
    );
  }

  // Success view
  if (success) {
    return (
      <section className="auth-card" style={{ textAlign: "center", padding: "2.5rem 1.5rem" }}>
        <div
          style={{
            width: "48px",
            height: "48px",
            borderRadius: "50%",
            backgroundColor: "rgba(52, 168, 83, 0.15)",
            color: "#2e7d32",
            display: "grid",
            placeItems: "center",
            margin: "0 auto 1rem auto",
            fontSize: "1.5rem",
          }}
        >
          ✓
        </div>
        <h2 style={{ fontSize: "1.35rem", marginBottom: "0.5rem" }}>Password Reset Complete</h2>
        <p className="hint" style={{ marginBottom: "1.5rem" }}>
          Your password has been updated successfully. You can now sign in with your new credentials.
        </p>
        <Link href="/login" className="button-link primary" style={{ display: "inline-block", width: "100%", padding: "0.75rem" }}>
          Sign In Now →
        </Link>
      </section>
    );
  }

  // Missing or expired token without a valid active session
  if (!accessToken) {
    return (
      <section className="auth-card" style={{ padding: "2rem" }}>
        <h2 style={{ fontSize: "1.35rem", marginBottom: "0.5rem" }}>Reset Password</h2>
        {error ? (
          <div
            className="notice"
            style={{
              padding: "1rem",
              marginBottom: "1.25rem",
              borderRadius: "6px",
            }}
          >
            <p style={{ margin: 0, fontSize: "0.9rem" }}>{error}</p>
          </div>
        ) : (
          <p className="hint" style={{ marginBottom: "1.25rem" }}>
            No password reset token was detected. To reset your password, please request a link using the button below.
          </p>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <Link href="/login" className="button-link primary" style={{ width: "100%", textAlign: "center", padding: "0.75rem" }}>
            Request New Reset Link
          </Link>
          <Link href="/" className="link" style={{ textAlign: "center", fontSize: "0.875rem" }}>
            Return to Home
          </Link>
        </div>
      </section>
    );
  }

  // Valid token form
  return (
    <section className="auth-card" aria-labelledby="reset-heading">
      <h2 id="reset-heading" style={{ fontSize: "1.35rem", marginBottom: "0.25rem" }}>
        Set a new password
      </h2>
      <p className="hint" style={{ marginBottom: "1.25rem" }}>
        Enter a new secure password for your account (at least {PASSWORD_MIN_LENGTH} characters).
      </p>

      {error && (
        <p className="form-error" role="alert" style={{ marginBottom: "1rem" }}>
          {error}
        </p>
      )}

      <form onSubmit={handleSubmit} noValidate>
        <div className="field">
          <label htmlFor="new-password">New Password</label>
          <div className="password-input-wrapper">
            <input
              id="new-password"
              name="password"
              type={showPassword ? "text" : "password"}
              autoComplete="new-password"
              required
              minLength={PASSWORD_MIN_LENGTH}
              maxLength={PASSWORD_MAX_LENGTH}
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
              aria-invalid={Boolean(fieldErrors.password)}
              aria-describedby={fieldErrors.password ? "password-error" : undefined}
              placeholder="••••••••"
              disabled={busy}
            />
            <button
              type="button"
              className="password-toggle-btn"
              onClick={() => setShowPassword((prev) => !prev)}
              aria-label={showPassword ? "Hide password" : "Show password"}
              title={showPassword ? "Hide password" : "Show password"}
              disabled={busy}
            >
              {showPassword ? (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                  <line x1="1" y1="1" x2="23" y2="23" />
                </svg>
              ) : (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                  <circle cx="12" cy="12" r="3" />
                </svg>
              )}
            </button>
          </div>
          {fieldErrors.password && (
            <span id="password-error" className="field-error">
              {fieldErrors.password}
            </span>
          )}
        </div>

        <div className="field">
          <label htmlFor="confirm-password">Confirm Password</label>
          <div className="password-input-wrapper">
            <input
              id="confirm-password"
              name="confirmPassword"
              type={showConfirmPassword ? "text" : "password"}
              autoComplete="new-password"
              required
              minLength={PASSWORD_MIN_LENGTH}
              maxLength={PASSWORD_MAX_LENGTH}
              value={form.confirmPassword}
              onChange={(e) => setForm({ ...form, confirmPassword: e.target.value })}
              aria-invalid={Boolean(fieldErrors.confirmPassword)}
              aria-describedby={fieldErrors.confirmPassword ? "confirm-password-error" : undefined}
              placeholder="••••••••"
              disabled={busy}
            />
            <button
              type="button"
              className="password-toggle-btn"
              onClick={() => setShowConfirmPassword((prev) => !prev)}
              aria-label={showConfirmPassword ? "Hide password" : "Show password"}
              title={showConfirmPassword ? "Hide password" : "Show password"}
              disabled={busy}
            >
              {showConfirmPassword ? (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                  <line x1="1" y1="1" x2="23" y2="23" />
                </svg>
              ) : (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                  <circle cx="12" cy="12" r="3" />
                </svg>
              )}
            </button>
          </div>
          {fieldErrors.confirmPassword && (
            <span id="confirm-password-error" className="field-error">
              {fieldErrors.confirmPassword}
            </span>
          )}
        </div>

        <div className="actions" style={{ marginTop: "1.5rem" }}>
          <button
            type="submit"
            className="primary"
            style={{ width: "100%", padding: "0.75rem" }}
            disabled={busy}
          >
            {busy ? "Updating Password…" : "Update Password"}
          </button>
        </div>
      </form>

      <div style={{ marginTop: "1.5rem", paddingTop: "1rem", borderTop: "1px solid var(--border)", textAlign: "center" }}>
        <Link href="/login" className="link" style={{ fontSize: "0.875rem" }}>
          Back to Sign In
        </Link>
      </div>
    </section>
  );
}
