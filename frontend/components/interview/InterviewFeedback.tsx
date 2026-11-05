"use client";

import { useCallback, useEffect, useState } from "react";
import {
  type QuestionHistory,
  type ReportResult,
  batchEvaluateAnswers,
  generateReport,
  getQuestions,
} from "@/lib/api";
import { COPY, FINISH_LABEL, errorText, pendingEvaluations } from "@/lib/interview";
import { ConsentControl } from "@/components/interview/InterviewSetup";

type Props = { sessionId: string; onReport: (result: ReportResult) => void };

/** REPORT_GENERATION: each answer is evaluated sequentially by the server, then
 * the report is compiled. Nothing is evaluated in the browser. */
export default function InterviewFeedback({ sessionId, onReport }: Props) {
  const [history, setHistory] = useState<QuestionHistory | null>(null);
  const [progress, setProgress] = useState<string | null>(null);
  const [notices, setNotices] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await getQuestions(sessionId);
      setHistory(data);
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
  }, [sessionId]);

  useEffect(() => {
    let ignore = false;
    const controller = new AbortController();
    getQuestions(sessionId, controller.signal)
      .then((data) => {
        if (!ignore) {
          setHistory(data);
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
  }, [sessionId]);

  async function run() {
    setError(null);
    try {
      setProgress("Evaluating answers concurrently…");
      const batch = await batchEvaluateAnswers(sessionId);
      if (batch.notices && batch.notices.length > 0) {
        setNotices((all) => Array.from(new Set([...all, ...batch.notices])));
      }
      setProgress(COPY.building);
      onReport(await generateReport(sessionId));
    } catch (err) {
      setError(errorText(err));
      setProgress(null);
      await load();
    }
  }

  const pending = history ? pendingEvaluations(history.questions).length : 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem", padding: "0.5rem 0" }}>
      <div>
        <span className="badge badge-primary">Session Finished</span>
        <h2 tabIndex={-1} data-screen-heading style={{ fontSize: "1.5rem", marginTop: "0.25rem" }}>
          Evaluating Your Interview Performance
        </h2>
        {history?.finish_reason && (
          <p style={{ fontWeight: 600, color: "var(--text)", marginTop: "0.25rem" }}>
            {FINISH_LABEL[history.finish_reason]}
          </p>
        )}
        <p className="hint" style={{ marginTop: "0.25rem" }}>{COPY.feedbackIntro}</p>
      </div>

      <div className="card" style={{ background: "var(--surface-2)" }}>
        {history && (
          <p style={{ fontWeight: 600, fontSize: "1rem" }}>
            {pending === 0
              ? "All answers are ready for report synthesis."
              : `${pending} ${pending === 1 ? "answer is" : "answers are"} queued for criteria evaluation.`}
          </p>
        )}

        {progress && (
          <div style={{ marginTop: "1rem", display: "flex", alignItems: "center", gap: "0.75rem" }}>
            <div className="badge badge-primary" style={{ animation: "pulse 1.5s infinite" }}>
              Evaluating…
            </div>
            <p role="status" aria-live="polite" style={{ fontWeight: 600 }}>
              {progress}
            </p>
          </div>
        )}
      </div>

      {!progress && <ConsentControl sessionId={sessionId} note={COPY.consentFeedback} />}

      {notices.map((n) => (
        <p key={n} className="notice">
          {n}
        </p>
      ))}

      {error && <p className="form-error" role="alert">{error}</p>}

      <div className="actions">
        <button
          type="button"
          className="primary"
          style={{ padding: "0.75rem 2rem", fontSize: "1rem" }}
          onClick={run}
          disabled={!history || progress !== null}
        >
          {progress
            ? "Evaluating…"
            : error
              ? "Try Again"
              : pending
                ? "Evaluate Answers & Generate Report →"
                : "Build My Comprehensive Report →"}
        </button>
      </div>
    </div>
  );
}
