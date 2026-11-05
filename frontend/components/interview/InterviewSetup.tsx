"use client";

import Link from "next/link";
import { type FormEvent, useEffect, useState } from "react";
import {
  type ClientAction,
  type Difficulty,
  type Duration,
  type ExperienceLevel,
  type Fact,
  type InterviewMode,
  type InterviewPlan,
  type InterviewStatus,
  type PlanSettings,
  type TargetRole,
  actOnSession,
  getConsent,
  getInterviewPlan,
  getResumeFacts,
  listManualFacts,
  listResumes,
  planInterview,
  setConsent,
} from "@/lib/api";
import {
  COPY,
  DIFFICULTY_LABEL,
  LEVEL_LABEL,
  MODE_LABEL,
  ROLE_LABEL,
  RULES_REASON_TEXT,
  errorText,
} from "@/lib/interview";
import { MANUAL_LABEL } from "@/lib/profile";
import { exportFileName, planDocument, topicDescription } from "@/lib/exportDoc";
import ExportButtons from "@/components/interview/ExportButtons";
import CameraPanel from "@/components/interview/CameraPanel";
import type { Camera } from "@/components/interview/useExpressionCamera";
import type { Voice } from "@/components/interview/useVoice";
import { VoiceSetup } from "@/components/interview/VoicePanel";

type StepProps = { sessionId: string; onStatus: (status: InterviewStatus) => void };

function useAction(sessionId: string, onStatus: (status: InterviewStatus) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (action: ClientAction) => {
    setBusy(true);
    setError(null);
    try {
      onStatus((await actOnSession(sessionId, action)).status);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run };
}

/** Visual Stepper Header */
function SetupStepper({ currentStep }: { currentStep: 1 | 2 | 3 }) {
  const steps = [
    { num: 1, label: "Confirm Profile" },
    { num: 2, label: "Plan Interview" },
    { num: 3, label: "Ready & Tech Check" },
  ];

  return (
    <div className="setup-stepper" aria-label="Setup steps">
      {steps.map((s) => (
        <div key={s.num} className={`stepper-step ${currentStep >= s.num ? "active" : ""}`}>
          <span className="stepper-bubble">{s.num}</span>
          <span>{s.label}</span>
        </div>
      ))}
    </div>
  );
}

// ---------- PROFILE_REVIEW ----------

export function ProfileConfirm({ sessionId, candidateId, onStatus }: StepProps & { candidateId: string }) {
  const [facts, setFacts] = useState<Fact[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const { busy, error, run } = useAction(sessionId, onStatus);

  useEffect(() => {
    let ignore = false;
    const controller = new AbortController();
    Promise.all([
      listResumes(candidateId, controller.signal),
      listManualFacts(candidateId, controller.signal),
    ])
      .then(async ([resumes, manual]) => {
        const lists = await Promise.all(
          resumes.map((r) => getResumeFacts(r.id, controller.signal))
        );
        if (!ignore) {
          setFacts([...lists.flat(), ...manual]);
          setLoadError(null);
        }
      })
      .catch((err) => {
        if (!ignore && !controller.signal.aborted) {
          setLoadError(errorText(err));
        }
      });
    return () => {
      ignore = true;
      controller.abort();
    };
  }, [candidateId]);

  const approved = facts?.filter((f) => f.status === "APPROVED") ?? [];
  const pending = facts?.filter((f) => f.status === "PENDING_REVIEW").length ?? 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <SetupStepper currentStep={1} />
      <span className="eyebrow">Step 1 · Profile Review</span>
      <h2 tabIndex={-1} data-screen-heading style={{ fontSize: "1.4rem" }}>
        Confirm Your Practice Profile
      </h2>
      <p className="hint">{COPY.confirmIntro}</p>

      {loadError && <p className="form-error" role="alert">{loadError}</p>}
      {facts === null && !loadError && <p>Loading your facts…</p>}

      {facts && (
        <div className="card" style={{ background: "var(--surface-2)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.75rem" }}>
            <span style={{ fontWeight: 600 }}>
              {approved.length} approved {approved.length === 1 ? "fact" : "facts"}
              {pending > 0 && ` · ${pending} pending review`}
            </span>
            <Link href="/" style={{ fontSize: "0.85rem" }}>
              Edit in Profile →
            </Link>
          </div>

          {approved.length > 0 ? (
            <ul className="plain-list" style={{ maxHeight: "280px", overflowY: "auto", fontSize: "0.9rem" }}>
              {approved.map((f) => (
                <li key={f.id} style={{ padding: "0.3rem 0" }}>
                  <span className="badge badge-primary">{f.category.toLowerCase()}</span>{" "}
                  <span>{f.fact}</span>
                  {f.source === "MANUAL" && <span className="hint"> · {MANUAL_LABEL}</span>}
                </li>
              ))}
            </ul>
          ) : (
            <p className="hint">
              No approved facts found. You can still proceed with general role questions, or add skills in your profile.
            </p>
          )}
        </div>
      )}

      {error && (
        <div role="alert">
          <p className="form-error">{error}</p>
          <p className="hint">
            {COPY.pendingFacts} <Link href="/">Go to your profile</Link>
          </p>
        </div>
      )}

      <div className="actions" style={{ marginTop: "1rem" }}>
        <button
          type="button"
          className="primary"
          style={{ padding: "0.65rem 1.5rem" }}
          onClick={() => run("CONFIRM_PROFILE")}
          disabled={busy}
        >
          {busy ? "Confirming…" : "Confirm Profile & Continue →"}
        </button>
      </div>
    </div>
  );
}

// ---------- AI consent (per session, default off) ----------

export function ConsentControl({ sessionId, note }: { sessionId: string; note?: string }) {
  const [granted, setGranted] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getConsent(sessionId, controller.signal)
      .then((c) => setGranted(c.granted))
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorText(err));
      });
    return () => controller.abort();
  }, [sessionId]);

  async function change(next: boolean) {
    setBusy(true);
    setError(null);
    try {
      setGranted((await setConsent(sessionId, next)).granted);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="consent-box" aria-live="polite">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "0.5rem" }}>
        <div>
          <span style={{ fontWeight: 700, color: "var(--text)" }}>
            {granted ? COPY.consentOn : COPY.consentOff}
          </span>
          {note && <p className="hint" style={{ marginTop: "0.25rem" }}>{note}</p>}
          {granted === false && <p className="hint">{COPY.consentDisclosure}</p>}
        </div>
        {granted !== null && (
          <button
            type="button"
            onClick={() => change(!granted)}
            disabled={busy}
            style={{ fontSize: "0.85rem", padding: "0.4rem 0.85rem" }}
          >
            {busy ? "Saving…" : granted ? "Turn Off AI Assistance" : "Turn On AI Assistance"}
          </button>
        )}
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
    </div>
  );
}

// ---------- INTERVIEW_PLANNING ----------

const ROLES = Object.keys(ROLE_LABEL) as TargetRole[];
const LEVELS = Object.keys(LEVEL_LABEL) as ExperienceLevel[];
const MODES = Object.keys(MODE_LABEL) as InterviewMode[];
const DIFFICULTIES = Object.keys(DIFFICULTY_LABEL) as Difficulty[];
const DURATIONS: Duration[] = [15, 30, 45];
const DEFAULTS: PlanSettings = {
  target_role: "SOFTWARE_DEVELOPER_INTERN",
  experience_level: "STUDENT",
  mode: "MIXED",
  difficulty: "MEDIUM",
  duration_minutes: 15,
};

export function PlanForm({ sessionId, onStatus }: StepProps) {
  const [settings, setSettings] = useState<PlanSettings>(DEFAULTS);
  const [consent, setConsentChoice] = useState(false);
  const [serverConsent, setServerConsent] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getConsent(sessionId, controller.signal)
      .then((c) => {
        setServerConsent(c.granted);
        setConsentChoice(c.granted);
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorText(err));
      });
    return () => controller.abort();
  }, [sessionId]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (serverConsent !== consent) {
        setServerConsent((await setConsent(sessionId, consent)).granted);
      }
      onStatus((await planInterview(sessionId, settings)).status);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  const select = <K extends keyof PlanSettings>(
    key: K,
    label: string,
    values: PlanSettings[K][],
    text: (v: PlanSettings[K]) => string,
  ) => (
    <div className="field">
      <label htmlFor={`plan-${key}`}>{label}</label>
      <select
        id={`plan-${key}`}
        value={String(settings[key])}
        onChange={(e) => {
          const raw = e.target.value;
          const value = (key === "duration_minutes" ? Number(raw) : raw) as PlanSettings[K];
          setSettings({ ...settings, [key]: value });
        }}
      >
        {values.map((v) => (
          <option key={String(v)} value={String(v)}>
            {text(v)}
          </option>
        ))}
      </select>
    </div>
  );

  return (
    <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <SetupStepper currentStep={2} />
      <span className="eyebrow">Step 2 · Configuration</span>
      <h2 tabIndex={-1} data-screen-heading style={{ fontSize: "1.4rem" }}>
        Configure Your Interview Session
      </h2>
      <p className="hint">
        Customize the target position, difficulty tier, and duration to tailor your practice experience.
      </p>

      <div className="plan-grid" style={{ marginTop: "0.5rem" }}>
        {select("target_role", "Target Job Role", ROLES, (v) => ROLE_LABEL[v])}
        {select("experience_level", "Experience Level", LEVELS, (v) => LEVEL_LABEL[v])}
        {select("mode", "Interview Type", MODES, (v) => MODE_LABEL[v])}
        {select("difficulty", "Difficulty Tier", DIFFICULTIES, (v) => DIFFICULTY_LABEL[v])}
        {select("duration_minutes", "Session Duration", DURATIONS, (v) => `${v} minutes`)}
      </div>

      <div className="card" style={{ background: "var(--surface-2)", marginTop: "0.5rem" }}>
        <div className="field checkbox" style={{ margin: 0 }}>
          <input
            id="plan-consent"
            type="checkbox"
            checked={consent}
            disabled={serverConsent === null}
            onChange={(e) => setConsentChoice(e.target.checked)}
            aria-describedby="plan-consent-help"
          />
          <label htmlFor="plan-consent" style={{ fontWeight: 600 }}>
            {COPY.consentLabel}
          </label>
        </div>
        <p id="plan-consent-help" className="hint" style={{ marginTop: "0.4rem" }}>
          {COPY.consentDisclosure}
        </p>
      </div>

      {error && <p className="form-error" role="alert">{error}</p>}

      <div className="actions" style={{ marginTop: "1rem" }}>
        <button
          type="submit"
          className="primary"
          style={{ padding: "0.65rem 1.75rem" }}
          disabled={busy || serverConsent === null}
        >
          {busy ? "Generating Plan…" : "Generate Interview Plan →"}
        </button>
      </div>
    </form>
  );
}

// ---------- READY ----------

export function ReadyScreen({
  sessionId,
  onStatus,
  candidateName,
  attempt,
  camera,
  voice,
}: StepProps & {
  candidateName: string;
  attempt?: number;
  camera: Camera;
  voice: Voice;
}) {
  const [plan, setPlan] = useState<InterviewPlan | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const { busy, error, run } = useAction(sessionId, onStatus);

  useEffect(() => {
    const controller = new AbortController();
    getInterviewPlan(sessionId, controller.signal)
      .then((view) => setPlan(view.plan))
      .catch((err) => {
        if (!controller.signal.aborted) setLoadError(errorText(err));
      });
    return () => controller.abort();
  }, [sessionId]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem" }}>
      <SetupStepper currentStep={3} />
      <span className="eyebrow">Step 3 · Verification &amp; Tech Check</span>
      <h2 tabIndex={-1} data-screen-heading style={{ fontSize: "1.4rem" }}>
        Ready to Enter the Interview Room
      </h2>
      <p className="hint">{COPY.readyIntro}</p>

      {loadError && <p className="form-error" role="alert">{loadError}</p>}

      {plan && (
        <>
          {/* Plan Summary Tiles */}
          <div className="dashboard-stats-grid">
            <div className="stat-card">
              <span className="stat-label">Role &amp; Level</span>
              <p style={{ fontWeight: 700, fontSize: "1rem" }}>{ROLE_LABEL[plan.settings.target_role]}</p>
              <span className="stat-hint">{LEVEL_LABEL[plan.settings.experience_level]}</span>
            </div>
            <div className="stat-card">
              <span className="stat-label">Format &amp; Length</span>
              <p style={{ fontWeight: 700, fontSize: "1rem" }}>{MODE_LABEL[plan.settings.mode]}</p>
              <span className="stat-hint">{plan.settings.duration_minutes} Minutes · {DIFFICULTY_LABEL[plan.settings.difficulty]}</span>
            </div>
            <div className="stat-card">
              <span className="stat-label">Planning Engine</span>
              <p style={{ fontWeight: 700, fontSize: "1rem" }}>
                {plan.source === "AI" ? "AI Assisted" : "Structured Rules"}
              </p>
              <span className="stat-hint">{plan.topics.length} Planned Topics</span>
            </div>
          </div>

          {plan.source === "RULES" && plan.rules_reason && plan.rules_reason !== "NOT_REQUESTED" && (
            <p className="notice">{RULES_REASON_TEXT[plan.rules_reason]}</p>
          )}

          {/* Planned Topics in order */}
          <div className="card" style={{ background: "var(--surface-2)" }}>
            <h3 style={{ fontSize: "1rem", marginBottom: "0.5rem" }}>Planned Topics (In Order)</h3>
            <ol className="plain-list numbered" style={{ fontSize: "0.875rem" }}>
              {plan.topics.map((t) => (
                <li key={t.position}>
                  <strong>{t.topic}</strong>
                  <br />
                  <span className="hint">{topicDescription(t)}</span>
                </li>
              ))}
            </ol>
            <p className="hint" style={{ marginTop: "0.75rem", fontSize: "0.8rem" }}>
              Actual interview questions are hidden until presented. Up to two adaptive follow-up questions may be asked per topic.
            </p>
          </div>

          <ExportButtons
            what="plan"
            fileName={exportFileName("plan", new Date(), attempt)}
            build={() => planDocument(plan, { candidateName, date: new Date(), attempt })}
          />
        </>
      )}

      {/* Voice & Camera Setup Checkers */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(18rem, 1fr))", gap: "1rem" }}>
        <VoiceSetup voice={voice} />
        <CameraPanel camera={camera} intro />
      </div>

      <ConsentControl sessionId={sessionId} />

      <p className="hint">{COPY.browserClose}</p>
      {error && <p className="form-error" role="alert">{error}</p>}

      <div className="actions" style={{ marginTop: "1rem" }}>
        <button
          type="button"
          className="primary"
          style={{ padding: "0.8rem 2.25rem", fontSize: "1rem" }}
          onClick={() => run("START_INTERVIEW")}
          disabled={busy || !plan}
        >
          {busy
            ? "Launching Session…"
            : camera.state === "off"
              ? "Start Interview (Without Camera) →"
              : "Start Interview Now →"}
        </button>
      </div>
    </div>
  );
}
