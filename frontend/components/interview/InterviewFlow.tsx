"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  type Candidate,
  type InterviewSession,
  type InterviewStatus,
  type ReportResult,
  createInterviewSession,
  deleteInterviewSession,
  listInterviewSessions,
} from "@/lib/api";
import { SignedIn } from "@/components/AuthProvider";
import {
  COPY,
  DIFFICULTY_LABEL,
  FINISH_LABEL,
  MODE_LABEL,
  attemptNumbers,
  attemptState,
  errorText,
  isUnfinished,
  screenFor,
} from "@/lib/interview";
import InterviewFeedback from "@/components/interview/InterviewFeedback";
import InterviewReport from "@/components/interview/InterviewReport";
import { PlanForm, ProfileConfirm, ReadyScreen } from "@/components/interview/InterviewSetup";
import InterviewTurn from "@/components/interview/InterviewTurn";
import { useExpressionCamera } from "@/components/interview/useExpressionCamera";
import { useVoice } from "@/components/interview/useVoice";

/** The session on screen: its id and the status the server last reported. */
type Active = { id: string; status: InterviewStatus };

export default function InterviewFlow() {
  return <SignedIn>{(candidate) => <Flow candidate={candidate} />}</SignedIn>;
}

/** The signed-in candidate's interviews. The candidate comes from GET /api/auth/me, never storage. */
function Flow({ candidate }: { candidate: Candidate }) {
  const [sessions, setSessions] = useState<InterviewSession[] | null>(null);
  const [active, setActive] = useState<Active | null>(null);
  const [report, setReport] = useState<ReportResult | null>(null);
  // The unsent answer, in memory only (never browser storage): survives a pause, not a reload.
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const screenRef = useRef<HTMLElement>(null);
  // Optional camera: lives here so a pause keeps it, and is released whenever it is not needed.
  const camera = useExpressionCamera();
  // Optional voice mode: also lives here so a pause keeps its settings; it never holds audio.
  const voice = useVoice();

  const load = useCallback(async () => {
    try {
      const data = await listInterviewSessions(candidate.id);
      setSessions(data);
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
  }, [candidate.id]);

  useEffect(() => {
    let ignore = false;
    const controller = new AbortController();
    listInterviewSessions(candidate.id, controller.signal)
      .then((data) => {
        if (!ignore) {
          setSessions(data);
          setError(null);
        }
      })
      .catch((err) => {
        if (!ignore && !controller.signal.aborted) {
          setError(errorText(err));
        }
      });
    return () => {
      ignore = true;
      controller.abort();
    };
  }, [candidate.id]);

  const screen = active ? screenFor(active.status) : null;
  const cameraScreen = screen === "ready" || screen === "turn" || screen === "paused";
  const { disable: releaseCamera, state: cameraState } = camera;
  useEffect(() => {
    if (!cameraScreen && cameraState !== "off") releaseCamera();
  }, [cameraScreen, cameraState, releaseCamera]);

  const { silence: silenceVoice } = voice;
  useEffect(() => {
    if (!cameraScreen) silenceVoice(); // off the interview: no speaker, no microphone
  }, [cameraScreen, silenceVoice]);

  useEffect(() => {
    screenRef.current?.querySelector<HTMLElement>("[data-screen-heading]")?.focus();
  }, [screen, active?.id]);

  const onStatus = useCallback((status: InterviewStatus) => {
    setActive((current) => (current ? { ...current, status } : current));
  }, []);

  function open(session: { id: string; status: InterviewStatus }) {
    setReport(null);
    setDraft("");
    setError(null);
    setActive({ id: session.id, status: session.status });
  }

  async function startNew() {
    setBusy(true);
    setError(null);
    try {
      const session = await createInterviewSession();
      open(session);
      load();
    } catch (err) {
      setError(errorText(err));
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function abandonAndStartNew() {
    if (!window.confirm("Abandon the active interview session and start a new one?")) return;
    setBusy(true);
    setError(null);
    try {
      const session = await createInterviewSession({ abandon_active: true });
      open(session);
      load();
    } catch (err) {
      setError(errorText(err));
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function discard(id: string) {
    if (!window.confirm(COPY.discardConfirm)) return;
    setBusy(true);
    setError(null);
    try {
      await deleteInterviewSession(id);
    } catch (err) {
      if (!(err && typeof err === "object" && "status" in err && err.status === 404)) {
        setError(errorText(err));
        setBusy(false);
        return;
      }
    }
    setActive(null);
    setReport(null);
    setDraft("");
    setSessions(null);
    setBusy(false);
    await load();
  }

  function leave() {
    setActive(null);
    setReport(null);
    setDraft("");
    setSessions(null);
    load();
  }

  const numbers = attemptNumbers(sessions ?? []);
  const errorBox = error && <p className="form-error" role="alert">{error}</p>;

  if (!active) {
    const unfinished = sessions?.find((s) => isUnfinished(s.status));
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
        <div>
          <Link href="/" className="back-link">
            ← Back to Candidate Dashboard
          </Link>
        </div>
        {/* Active Session Alert / Quick Start */}
        <section className="card" aria-labelledby="sessions-heading">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.5rem" }}>
            <h2 id="sessions-heading" style={{ fontSize: "1.35rem" }}>
              Interview Sessions Hub
            </h2>
            <span className="badge badge-primary">Practice Mode</span>
          </div>

          {errorBox}
          {sessions === null && !error && <p className="hint">Loading your interview attempts…</p>}
          {sessions === null && error && (
            <div className="actions">
              <button type="button" onClick={() => load()}>Retry Loading</button>
            </div>
          )}

          {sessions && unfinished && (
            <div className="active-session-banner" style={{ marginTop: "0.75rem" }}>
              <div>
                <span className="badge badge-warning">Unfinished Session Active</span>
                <h3 style={{ marginTop: "0.25rem", color: "var(--text)" }}>
                  Interview #{numbers.get(unfinished.id)}
                </h3>
                <p className="hint">
                  Started {new Date(unfinished.created_at).toLocaleString()}. You can resume from where you left off or discard it.
                </p>
              </div>
              <div className="actions" style={{ margin: 0 }}>
                <button
                  type="button"
                  className="primary"
                  onClick={() => open(unfinished)}
                  disabled={busy}
                  style={{ padding: "0.6rem 1.25rem" }}
                >
                  Resume Interview →
                </button>
                <button
                  type="button"
                  onClick={abandonAndStartNew}
                  disabled={busy}
                  style={{ padding: "0.6rem 1rem", fontSize: "0.85rem" }}
                >
                  Abandon & Start New
                </button>
                <button
                  type="button"
                  className="danger"
                  onClick={() => discard(unfinished.id)}
                  disabled={busy}
                  style={{ padding: "0.6rem 1rem" }}
                >
                  Discard
                </button>
              </div>
            </div>
          )}

          {sessions && !unfinished && (
            <div style={{ marginTop: "0.5rem" }}>
              <p className="hint" style={{ marginBottom: "1rem" }}>
                {COPY.startNew}
              </p>
              <div className="actions" style={{ margin: 0 }}>
                <button
                  type="button"
                  className="primary"
                  onClick={startNew}
                  disabled={busy}
                  style={{ padding: "0.75rem 1.5rem", fontSize: "0.95rem" }}
                >
                  {busy ? "Starting…" : "+ Start New Practice Interview"}
                </button>
              </div>
            </div>
          )}
        </section>

        {sessions && sessions.length > 0 && (
          <History
            sessions={sessions}
            numbers={numbers}
            busy={busy}
            onOpen={open}
            onDelete={discard}
          />
        )}
      </div>
    );
  }

  const attempt = numbers.get(active.id);
  const listed = sessions?.find((s) => s.id === active.id);
  const retryOf = listed?.retry_of_id ? numbers.get(listed.retry_of_id) : undefined;

  // The Live Interview Room is full-width without an extra wrapping box
  if (screen === "turn" || screen === "paused") {
    return (
      <section ref={screenRef} style={{ width: "100%" }} aria-label="Interview Room">
        <InterviewTurn
          sessionId={active.id}
          paused={screen === "paused"}
          onStatus={onStatus}
          draft={draft}
          setDraft={setDraft}
          camera={camera}
          voice={voice}
          attempt={attempt}
        />
        {errorBox}
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "1rem" }}>
          <button type="button" onClick={leave} disabled={busy} style={{ fontSize: "0.85rem" }}>
            ← Return to Interviews Hub
          </button>
        </div>
      </section>
    );
  }

  // Setup / Feedback / Report screens are wrapped in standard card container
  return (
    <section className="card interview" ref={screenRef} aria-label="Interview">
      {screen === "confirm" && (
        <ProfileConfirm sessionId={active.id} candidateId={candidate.id} onStatus={onStatus} />
      )}
      {screen === "plan" && <PlanForm sessionId={active.id} onStatus={onStatus} />}
      {screen === "ready" && (
        <ReadyScreen
          sessionId={active.id}
          onStatus={onStatus}
          candidateName={candidate.name}
          attempt={attempt}
          camera={camera}
          voice={voice}
        />
      )}
      {screen === "feedback" && (
        <InterviewFeedback
          sessionId={active.id}
          onReport={(result) => {
            setReport(result);
            onStatus(result.status);
          }}
        />
      )}
      {screen === "report" && (
        <InterviewReport
          key={active.id}
          sessionId={active.id}
          initial={report}
          candidateName={candidate.name}
          attempt={attempt}
          retryOf={retryOf}
          onRetry={(view) => {
            open({ id: view.session_id, status: view.status });
            load();
          }}
        />
      )}
      {errorBox}
      <div className="actions secondary">
        {screen !== "report" && (
          <button
            type="button"
            className="danger"
            onClick={() => discard(active.id)}
            disabled={busy}
          >
            {busy ? "Discarding…" : "Discard interview"}
          </button>
        )}
        <button type="button" onClick={leave} disabled={busy}>
          Back to your interviews
        </button>
      </div>
    </section>
  );
}

type HistoryProps = {
  sessions: InterviewSession[];
  numbers: Map<string, number>;
  busy: boolean;
  onOpen: (s: InterviewSession) => void;
  onDelete: (id: string) => void;
};

/** Every attempt, newest first. Each keeps its own report: attempts are never combined. */
function History({ sessions, numbers, busy, onOpen, onDelete }: HistoryProps) {
  return (
    <section className="card" aria-labelledby="history-heading">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: "0.25rem" }}>
        <h2 id="history-heading" style={{ fontSize: "1.25rem" }}>
          Past Interview History
        </h2>
        <span className="hint">{sessions.length} recorded {sessions.length === 1 ? "attempt" : "attempts"}</span>
      </div>
      <p className="hint" style={{ marginBottom: "1rem" }}>
        Each interview preserves its own full report and criteria evaluation. Retrying targets the concepts identified for improvement.
      </p>

      <ul className="history">
        {sessions.map((s) => {
          const n = numbers.get(s.id);
          const source = s.retry_of_id ? numbers.get(s.retry_of_id) : undefined;
          const isDone = s.status === "COMPLETED";

          return (
            <li key={s.id} className="history-item">
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.25rem" }}>
                  <strong style={{ fontSize: "1.05rem" }}>Interview #{n}</strong>
                  <span className={`badge ${isDone ? "badge-success" : "badge-warning"}`}>
                    {attemptState(s.status)}
                  </span>
                  {source && <span className="hint"> · retry of interview #{source}</span>}
                </div>
                <div style={{ fontSize: "0.875rem", color: "var(--text-secondary)" }}>
                  {s.settings
                    ? `${MODE_LABEL[s.settings.mode]} · ${DIFFICULTY_LABEL[s.settings.difficulty]} · ${s.settings.duration_minutes} min`
                    : "Not configured yet"}
                </div>
                <div className="hint" style={{ fontSize: "0.78rem", marginTop: "0.2rem" }}>
                  {new Date(s.created_at).toLocaleString()}
                  {s.finish_reason && ` · ${FINISH_LABEL[s.finish_reason]}`}
                </div>
              </div>

              <div className="actions">
                <button
                  type="button"
                  className={isDone ? "primary" : ""}
                  onClick={() => onOpen(s)}
                  disabled={busy}
                  aria-label={`${isDone ? "View the report of" : "Continue"} interview #${n}`}
                  style={{ padding: "0.45rem 0.95rem", fontSize: "0.85rem" }}
                >
                  {isDone ? "View Report →" : "Continue Session →"}
                </button>
                <button
                  type="button"
                  className="danger"
                  onClick={() => onDelete(s.id)}
                  disabled={busy}
                  aria-label={`Delete interview #${n}`}
                  style={{ padding: "0.45rem 0.75rem", fontSize: "0.85rem" }}
                >
                  Delete
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
