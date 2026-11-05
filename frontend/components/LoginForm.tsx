"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";
import { useAuth } from "@/components/AuthProvider";
import { ApiError, forgotPassword, getApiEndpoint, login, register } from "@/lib/api";
import {
  type AuthFieldErrors,
  type AuthForm,
  type AuthMode,
  PASSWORD_MAX_LENGTH,
  PASSWORD_MIN_LENGTH,
  validateAuthForm,
} from "@/lib/auth";

const EMPTY: AuthForm = { name: "", email: "", password: "" };

export default function LoginForm() {
  const { candidate, setCandidate } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();

  const [mode, setMode] = useState<AuthMode>("login");
  const [form, setForm] = useState<AuthForm>(EMPTY);
  const [fieldErrors, setFieldErrors] = useState<AuthFieldErrors>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Verification & Recovery states
  const [verificationSent, setVerificationSent] = useState<string | null>(null);
  const [showForgot, setShowForgot] = useState(false);
  const [forgotEmail, setForgotEmail] = useState("");
  const [forgotSuccess, setForgotSuccess] = useState<string | null>(null);
  const [forgotBusy, setForgotBusy] = useState(false);

  // Check URL query parameters for feedback messages
  const urlError = (() => {
    const err = searchParams?.get("error");
    if (err === "verification_expired" || err === "verification_invalid") {
      return "Your verification link has expired or is invalid. Please register again or request a new link.";
    } else if (err === "oauth_cancelled") {
      return "Google sign-in was cancelled. Please try again.";
    } else if (err === "oauth_state_invalid" || err === "oauth_state_mismatch") {
      return "OAuth security verification failed. Please try signing in again.";
    } else if (err === "oauth_code_invalid" || err === "oauth_exchange_failed") {
      return "Failed to complete Google sign-in. Please try again.";
    }
    return null;
  })();

  const activeError = error || urlError;

  // Already signed in (or just signed in): redirect to dashboard
  useEffect(() => {
    if (candidate) router.replace("/");
  }, [candidate, router]);

  function switchMode(next: AuthMode) {
    setMode(next);
    setForm({ ...form, password: "" });
    setFieldErrors({});
    setError(null);
    setShowForgot(false);
    setVerificationSent(null);
    setForgotSuccess(null);
  }

  async function handleGoogleLogin() {
    window.location.href = getApiEndpoint("/api/auth/google/start");
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const errors = validateAuthForm(mode, form);
    setFieldErrors(errors);
    setError(Object.keys(errors).length ? "Please fix the highlighted fields." : null);
    if (Object.keys(errors).length) return;
    setBusy(true);
    setVerificationSent(null);

    try {
      const email = form.email.trim();
      if (mode === "login") {
        const candidateProfile = await login({ email, password: form.password });
        setCandidate(candidateProfile);
      } else {
        const res = await register({ name: form.name.trim(), email, password: form.password });
        setVerificationSent(res.email);
        setForm(EMPTY);
      }
    } catch (err) {
      setForm((f) => ({ ...f, password: "" }));
      if (!(err instanceof ApiError)) {
        setError("Something went wrong. Please try again.");
      } else if (err.status === 403) {
        setError("Email verification is required before signing in. Please check your inbox.");
      } else if (err.status === 409) {
        setFieldErrors({ email: err.message });
        setError("An account with this email already exists.");
      } else {
        setFieldErrors(err.fieldErrors as AuthFieldErrors);
        setError(err.message);
      }
    } finally {
      setBusy(false);
    }
  }

  async function submitForgotPassword(e: FormEvent) {
    e.preventDefault();
    if (!forgotEmail.trim()) return;
    setForgotBusy(true);
    setError(null);
    try {
      const res = await forgotPassword(forgotEmail.trim());
      setForgotSuccess(res.message);
    } catch {
      setForgotSuccess("If an account exists for this email, a password reset link has been sent.");
    } finally {
      setForgotBusy(false);
    }
  }

  const fields: { key: keyof AuthForm; label: string; type: string; autoComplete: string }[] = [
    ...(mode === "register"
      ? [{ key: "name" as const, label: "Full Name", type: "text", autoComplete: "name" }]
      : []),
    { key: "email", label: "Email Address", type: "email", autoComplete: "email" },
    {
      key: "password",
      label: "Password",
      type: "password",
      autoComplete: mode === "login" ? "current-password" : "new-password",
    },
  ];

  return (
    <section className="auth-card" aria-labelledby="auth-heading">
      {/* Segmented Mode Switcher */}
      <div className="auth-mode-tabs" role="tablist" aria-label="Sign in mode">
        <button
          type="button"
          role="tab"
          aria-selected={mode === "login"}
          className={`auth-mode-tab ${mode === "login" ? "active" : ""}`}
          onClick={() => switchMode("login")}
        >
          Sign In
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={mode === "register"}
          className={`auth-mode-tab ${mode === "register" ? "active" : ""}`}
          onClick={() => switchMode("register")}
        >
          Create Account
        </button>
      </div>

      <h2 id="auth-heading" style={{ fontSize: "1.35rem", marginBottom: "0.25rem" }}>
        {mode === "login" ? "Welcome back" : "Create your practice profile"}
      </h2>
      <p className="hint" style={{ marginBottom: "1.25rem" }}>
        {mode === "login"
          ? "Log in to access your planned interviews, reports, and skills."
          : "Sign up to track interview progress, store resume facts, and practice."}
      </p>

      {/* Google OAuth Button */}
      <div style={{ marginBottom: "1.25rem" }}>
        <button
          type="button"
          onClick={handleGoogleLogin}
          style={{
            width: "100%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: "0.75rem",
            padding: "0.65rem 1rem",
            borderRadius: "6px",
            border: "1px solid var(--border)",
            backgroundColor: "var(--card-bg, #fff)",
            color: "var(--foreground, #111)",
            fontWeight: 500,
            fontSize: "0.95rem",
            cursor: "pointer",
          }}
        >
          <svg width="18" height="18" viewBox="0 0 24 24">
            <path
              fill="#4285F4"
              d="M23.745 12.27c0-.7-.06-1.4-.19-2.07H12v4.51h6.6c-.29 1.52-1.14 2.82-2.4 3.68v3.05h3.88c2.27-2.09 3.66-5.17 3.66-9.17z"
            />
            <path
              fill="#34A853"
              d="M12 24c3.24 0 5.95-1.08 7.93-2.91l-3.88-3.05c-1.08.72-2.45 1.16-4.05 1.16-3.12 0-5.77-2.1-6.72-4.93H1.24v3.15C3.26 21.36 7.35 24 12 24z"
            />
            <path
              fill="#FBBC05"
              d="M5.28 14.27c-.25-.72-.38-1.49-.38-2.27s.13-1.55.38-2.27V6.58H1.24C.45 8.15 0 9.99 0 12s.45 3.85 1.24 5.42l4.04-3.15z"
            />
            <path
              fill="#EA4335"
              d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.42-3.42C17.95 1.19 15.24 0 12 0 7.35 0 3.26 2.64 1.24 6.58l4.04 3.15c.95-2.83 3.6-4.98 6.72-4.98z"
            />
          </svg>
          Continue with Google
        </button>
      </div>

      <div style={{ display: "flex", alignItems: "center", margin: "1.25rem 0", color: "var(--hint, #888)" }}>
        <div style={{ flex: 1, height: "1px", backgroundColor: "var(--border, #ddd)" }} />
        <span style={{ padding: "0 0.75rem", fontSize: "0.85rem" }}>or with email</span>
        <div style={{ flex: 1, height: "1px", backgroundColor: "var(--border, #ddd)" }} />
      </div>

      {verificationSent ? (
        <div
          className="notice"
          style={{
            padding: "1rem",
            backgroundColor: "rgba(52, 168, 83, 0.1)",
            border: "1px solid #34a853",
            borderRadius: "6px",
            marginBottom: "1rem",
          }}
        >
          <h3 style={{ fontSize: "1.1rem", color: "#2e7d32", marginBottom: "0.5rem" }}>
            Check your email
          </h3>
          <p style={{ fontSize: "0.9rem", marginBottom: "0.75rem" }}>
            We sent a verification link to <strong>{verificationSent}</strong>. Please click the link to confirm your account and sign in.
          </p>
          <button
            type="button"
            className="link"
            style={{ fontSize: "0.85rem", fontWeight: 600 }}
            onClick={() => switchMode("login")}
          >
            Return to sign in
          </button>
        </div>
      ) : (
        <form onSubmit={submit} noValidate>
          {fields.map(({ key, label, type, autoComplete }) => (
            <div className="field" key={key}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                <label htmlFor={key}>{label}</label>
                {key === "password" && mode === "login" && (
                  <button
                    type="button"
                    className="link"
                    style={{ fontSize: "0.8rem" }}
                    onClick={() => {
                      setShowForgot(!showForgot);
                      setForgotEmail(form.email);
                    }}
                  >
                    Forgot password?
                  </button>
                )}
              </div>
              <input
                id={key}
                name={key}
                type={type}
                autoComplete={autoComplete}
                required
                maxLength={key === "password" ? PASSWORD_MAX_LENGTH : key === "name" ? 200 : undefined}
                minLength={key === "password" && mode === "register" ? PASSWORD_MIN_LENGTH : undefined}
                value={form[key]}
                onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                aria-invalid={Boolean(fieldErrors[key])}
                aria-describedby={fieldErrors[key] ? `${key}-error` : undefined}
                placeholder={key === "email" ? "you@example.com" : key === "password" ? "••••••••" : "Jane Doe"}
              />
              {key === "password" && mode === "register" && !fieldErrors.password && (
                <span className="hint">Minimum {PASSWORD_MIN_LENGTH} characters.</span>
              )}
              {fieldErrors[key] && (
                <span id={`${key}-error`} className="field-error">{fieldErrors[key]}</span>
              )}
            </div>
          ))}

          {showForgot && (
            <div
              className="notice"
              style={{
                marginBottom: "1rem",
                padding: "1rem",
                borderRadius: "6px",
                border: "1px solid var(--border)",
              }}
            >
              <h4 style={{ margin: "0 0 0.5rem 0", fontSize: "0.95rem" }}>Reset your password</h4>
              {forgotSuccess ? (
                <p style={{ fontSize: "0.85rem", color: "#2e7d32", margin: 0 }}>{forgotSuccess}</p>
              ) : (
                <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.5rem" }}>
                  <input
                    type="email"
                    placeholder="Enter your email"
                    value={forgotEmail}
                    onChange={(e) => setForgotEmail(e.target.value)}
                    style={{ flex: 1, padding: "0.4rem 0.6rem", fontSize: "0.85rem" }}
                  />
                  <button
                    type="button"
                    onClick={submitForgotPassword}
                    disabled={forgotBusy || !forgotEmail}
                    style={{ padding: "0.4rem 0.8rem", fontSize: "0.85rem" }}
                  >
                    {forgotBusy ? "Sending…" : "Send Reset Link"}
                  </button>
                </div>
              )}
            </div>
          )}

          {activeError && <p className="form-error" role="alert">{activeError}</p>}

          <div className="actions" style={{ marginTop: "1.25rem" }}>
            <button type="submit" className="primary" style={{ width: "100%", padding: "0.75rem" }} disabled={busy}>
              {busy
                ? mode === "login"
                  ? "Signing in…"
                  : "Creating account…"
                : mode === "login"
                  ? "Sign In to Account"
                  : "Create Account & Verify"}
            </button>
          </div>
        </form>
      )}

      <div style={{ marginTop: "1.5rem", paddingTop: "1rem", borderTop: "1px solid var(--border)", textAlign: "center" }}>
        <p className="hint">
          {mode === "login" ? "New to AI Interview Coach? " : "Already have an account? "}
          <button
            type="button"
            className="link"
            style={{ fontWeight: 600 }}
            onClick={() => switchMode(mode === "login" ? "register" : "login")}
            disabled={busy}
          >
            {mode === "login" ? "Create an account" : "Sign in instead"}
          </button>
        </p>
      </div>
    </section>
  );
}
