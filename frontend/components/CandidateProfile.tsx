"use client";

import { type FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { SignedIn, useAuth } from "@/components/AuthProvider";
import ManualFactsSection from "@/components/ManualFactsSection";
import ResumeSection from "@/components/ResumeSection";
import {
  ApiError,
  type Candidate,
  type CandidateInput,
  deleteCandidate,
  updateCandidate,
} from "@/lib/api";
import { LOGIN_PATH } from "@/lib/auth";

type FieldErrors = Partial<Record<keyof CandidateInput, string>>;

const NAME_MAX_LENGTH = 200;

/** The signed-in candidate's profile: who they are comes from the session, never the browser. */
export default function CandidateProfile() {
  return <SignedIn>{(candidate) => <Profile candidate={candidate} />}</SignedIn>;
}

function Profile({ candidate }: { candidate: Candidate }) {
  const { setCandidate } = useAuth();
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<CandidateInput>({ name: "", email: "" });
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function signedOut() {
    setCandidate(null);
    router.replace(LOGIN_PATH);
  }

  function showError(err: unknown) {
    if (!(err instanceof ApiError)) {
      setError("Something went wrong. Please try again.");
      return;
    }
    if (err.status === 404) return signedOut();
    if (err.status === 409) {
      setFieldErrors({ email: err.message });
      setError("Please fix the highlighted fields.");
      return;
    }
    setFieldErrors(err.fieldErrors as FieldErrors);
    setError(err.message);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const changes: Partial<CandidateInput> = {};
    if (form.name !== candidate.name) changes.name = form.name;
    if (form.email !== candidate.email) changes.email = form.email;
    if (Object.keys(changes).length === 0) return setEditing(false);
    setBusy(true);
    setError(null);
    setFieldErrors({});
    try {
      setCandidate(await updateCandidate(candidate.id, changes));
      setEditing(false);
    } catch (err) {
      showError(err);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    const warning =
      "Delete your profile? This permanently removes your account, your resume data and your " +
      "interviews, and logs you out.";
    if (!window.confirm(warning)) return;
    setBusy(true);
    setError(null);
    try {
      await deleteCandidate(candidate.id);
      signedOut();
    } catch (err) {
      showError(err);
      setBusy(false);
    }
  }

  function startEdit() {
    setForm({ name: candidate.name, email: candidate.email });
    setFieldErrors({});
    setError(null);
    setEditing(true);
  }

  const errorBox = error && (
    <p className="form-error" role="alert">
      {error}
    </p>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
      {/* Quick Launch Practice Card */}
      <section className="card active-session-banner" aria-labelledby="interview-heading">
        <div>
          <span className="badge badge-primary">Practice Hub</span>
          <h2 id="interview-heading" style={{ fontSize: "1.35rem", marginTop: "0.4rem", color: "var(--text)" }}>
            Ready for your next interview practice?
          </h2>
          <p className="hint" style={{ marginTop: "0.25rem" }}>
            Plan a targeted session by role, test your voice &amp; video setup, and practice realistic questions.
          </p>
        </div>
        <Link className="button-link primary" href="/interview" style={{ padding: "0.75rem 1.5rem", whiteSpace: "nowrap" }}>
          Go to Practice Interviews →
        </Link>
      </section>

      {/* Profile Details Card */}
      <section className="card" aria-labelledby="profile-heading">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "1rem" }}>
          <div>
            <h2 id="profile-heading" style={{ fontSize: "1.2rem" }}>Profile Information</h2>
            <p className="hint">Your basic candidate account details</p>
          </div>
          {!editing && (
            <button type="button" onClick={startEdit} disabled={busy} style={{ fontSize: "0.85rem", padding: "0.4rem 0.8rem" }}>
              Edit Profile
            </button>
          )}
        </div>

        {!editing && (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(14rem, 1fr))", gap: "1rem", padding: "0.75rem 0" }}>
              <div>
                <span className="hint" style={{ fontSize: "0.75rem", textTransform: "uppercase", letterSpacing: "0.05em" }}>Name</span>
                <p style={{ fontWeight: 600, fontSize: "1.05rem" }}>{candidate.name}</p>
              </div>
              <div>
                <span className="hint" style={{ fontSize: "0.75rem", textTransform: "uppercase", letterSpacing: "0.05em" }}>Email</span>
                <p style={{ fontWeight: 600, fontSize: "1.05rem" }}>{candidate.email}</p>
              </div>
              <div>
                <span className="hint" style={{ fontSize: "0.75rem", textTransform: "uppercase", letterSpacing: "0.05em" }}>Member Since</span>
                <p style={{ fontWeight: 600, fontSize: "1.05rem" }}>{new Date(candidate.created_at).toLocaleDateString()}</p>
              </div>
            </div>
            {errorBox}
            <div style={{ marginTop: "1rem", paddingTop: "0.75rem", borderTop: "1px solid var(--border)", display: "flex", justifyContent: "flex-end" }}>
              <button type="button" className="danger" onClick={remove} disabled={busy} style={{ fontSize: "0.8rem", padding: "0.35rem 0.75rem" }}>
                {busy ? "Deleting…" : "Delete account"}
              </button>
            </div>
          </>
        )}

        {editing && (
          <form onSubmit={submit} style={{ marginTop: "0.5rem" }}>
            {(["name", "email"] as const).map((field) => (
              <div className="field" key={field}>
                <label htmlFor={field}>{field === "name" ? "Full Name" : "Email Address"}</label>
                <input
                  id={field}
                  name={field}
                  type={field === "email" ? "email" : "text"}
                  autoComplete={field}
                  required
                  maxLength={field === "name" ? NAME_MAX_LENGTH : undefined}
                  value={form[field]}
                  onChange={(e) => setForm({ ...form, [field]: e.target.value })}
                  aria-invalid={Boolean(fieldErrors[field])}
                  aria-describedby={fieldErrors[field] ? `${field}-error` : undefined}
                />
                {fieldErrors[field] && (
                  <span id={`${field}-error`} className="field-error">
                    {fieldErrors[field]}
                  </span>
                )}
              </div>
            ))}
            {errorBox}
            <div className="actions">
              <button type="submit" className="primary" disabled={busy}>
                {busy ? "Saving…" : "Save changes"}
              </button>
              <button type="button" onClick={() => setEditing(false)} disabled={busy}>
                Cancel
              </button>
            </div>
          </form>
        )}
      </section>

      {/* Manual Skills & Projects Section */}
      <ManualFactsSection key={`manual-${candidate.id}`} candidateId={candidate.id} />

      {/* Resume Upload & Review Section */}
      <ResumeSection key={candidate.id} candidateId={candidate.id} />
    </div>
  );
}
