"use client";

import Link from "next/link";
import BackendStatus from "@/components/BackendStatus";
import CandidateProfile from "@/components/CandidateProfile";
import LandingPage from "@/components/LandingPage";
import { useAuth } from "@/components/AuthProvider";

export default function Home() {
  const { candidate } = useAuth();

  // Loading auth session state
  if (candidate === undefined) {
    return (
      <main id="content" style={{ minHeight: "60vh", display: "grid", placeItems: "center" }}>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "1rem" }}>
          <div className="badge badge-primary">Checking Session…</div>
          <p className="hint">Connecting to AI Interview Coach platform…</p>
        </div>
      </main>
    );
  }

  // Visitor not logged in: display the comprehensive SaaS landing page
  if (candidate === null) {
    return (
      <main id="content">
        <LandingPage />
      </main>
    );
  }

  // Authenticated candidate: display the candidate dashboard & profile workspace
  return (
    <main id="content">
      {/* Dashboard Welcome Header */}
      <div className="dashboard-header">
        <div>
          <span className="eyebrow">Candidate Dashboard</span>
          <h1 style={{ fontSize: "1.75rem", marginTop: "0.25rem" }}>
            Welcome, {candidate.name}
          </h1>
          <p className="hint">Manage your profile facts, review resume skills, and take practice interviews.</p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
          <BackendStatus />
          <Link href="/interview" className="button-link primary" style={{ padding: "0.65rem 1.25rem" }}>
            Start Practice Interview →
          </Link>
        </div>
      </div>

      <CandidateProfile />
    </main>
  );
}
