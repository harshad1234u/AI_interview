import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";
import ResetPasswordForm from "@/components/ResetPasswordForm";

export const metadata: Metadata = {
  title: "Reset Password — AI Interview Coach",
  description: "Set a new secure password for your AI Interview Coach account.",
};

export default function ResetPasswordPage() {
  return (
    <main id="content" className="auth-layout">
      {/* Left Column: Security Context & Assistance */}
      <div className="auth-intro">
        <Link href="/login" className="back-link">
          ← Back to Sign In
        </Link>
        <span className="badge badge-primary">Account Security</span>
        <h1 style={{ fontSize: "2.25rem", lineHeight: 1.2 }}>
          Reset Your Password
        </h1>
        <p className="lead">
          Create a new secure password to restore full access to your interview preparation dashboard, rubrics, and feedback.
        </p>

        <div className="card" style={{ background: "var(--surface-2)", marginTop: "0.5rem" }}>
          <h3 style={{ fontSize: "1rem", marginBottom: "0.5rem" }}>Security Reminders:</h3>
          <ul className="plain-list" style={{ fontSize: "0.875rem", color: "var(--text-secondary)" }}>
            <li><strong>Minimum 8 characters:</strong> Use a password or passphrase that is hard to guess.</li>
            <li><strong>Single-use link:</strong> For your security, recovery tokens are invalidated once used or expired.</li>
            <li><strong>Protected identity:</strong> Your password is cryptographically salted and hashed.</li>
          </ul>
        </div>
      </div>

      {/* Right Column: Reset Password Form Card */}
      <Suspense fallback={<div className="card" style={{ padding: "2rem", textAlign: "center" }}>Loading…</div>}>
        <ResetPasswordForm />
      </Suspense>
    </main>
  );
}
