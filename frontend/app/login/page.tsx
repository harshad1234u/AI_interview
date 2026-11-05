import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";
import LoginForm from "@/components/LoginForm";

export const metadata: Metadata = {
  title: "Sign In — AI Interview Coach",
  description: "Sign in or create an account to start practicing realistic AI job interviews.",
};

export default function LoginPage() {
  return (
    <main id="content" className="auth-layout">
      {/* Left Column: Product Value & Privacy Guarantee */}
      <div className="auth-intro">
        <Link href="/" className="back-link">
          ← Back to Overview
        </Link>
        <span className="badge badge-primary">Practice Platform</span>
        <h1 style={{ fontSize: "2.25rem", lineHeight: 1.2 }}>
          Your Personal AI Interview Coach
        </h1>
        <p className="lead">
          Realistic simulations, voice-first conversations, and detailed rubrics built to elevate your interview performance.
        </p>

        <div className="card" style={{ background: "var(--surface-2)", marginTop: "0.5rem" }}>
          <h3 style={{ fontSize: "1rem", marginBottom: "0.5rem" }}>Platform Highlights:</h3>
          <ul className="plain-list" style={{ fontSize: "0.875rem", color: "var(--text-secondary)" }}>
            <li><strong>Voice-First Practice:</strong> Speak your answers aloud just like an actual interviewer video-call.</li>
            <li><strong>Resume-Driven Questions:</strong> Tailored to your real skills, projects, and target engineering roles.</li>
            <li><strong>Strict Privacy:</strong> Audio and facial expressions are processed in-browser and never stored.</li>
            <li><strong>Actionable Feedback:</strong> Deep evaluations with direct quotes, scoring, and retry recommendations.</li>
          </ul>
        </div>
      </div>

      {/* Right Column: Sleek Auth Card */}
      <Suspense fallback={<div className="card" style={{ padding: "2rem", textAlign: "center" }}>Loading...</div>}>
        <LoginForm />
      </Suspense>
    </main>
  );
}
