"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  type InterviewPlanView,
  type InterviewReport as Report,
  type ReportResult,
  getReport,
  retryInterview,
} from "@/lib/api";
import {
  COPY,
  CRITERION_LABEL,
  DIFFICULTY_LABEL,
  EVALUATION_STATUS_LABEL,
  LEVEL_LABEL,
  MODE_LABEL,
  canRetry,
  errorText,
  formatRemaining,
} from "@/lib/interview";
import { exportFileName, reportDocument } from "@/lib/exportDoc";
import ExportButtons from "@/components/interview/ExportButtons";

type Props = {
  sessionId: string;
  initial: ReportResult | null;
  onRetry: (view: InterviewPlanView) => void;
  candidateName: string;
  attempt?: number;
  /** The attempt this one retries, when known: "Retry of interview #n". */
  retryOf?: number;
};

/** Quotes are the candidate's own words, rendered as clean block quotes. */
function Quotes({ items }: { items: string[] }) {
  if (items.length === 0) return null;
  return (
    <ul className="quotes">
      {items.map((quote, i) => (
        <li key={i}>
          <q>{quote}</q>
        </li>
      ))}
    </ul>
  );
}

/** One criterion score display: score out of 5 with meaning badge. */
function Score({ score, meaning }: { score: number | null; meaning?: string }) {
  if (score === null) {
    return <span className="hint" style={{ fontSize: "0.8rem" }}>Insufficient response to evaluate</span>;
  }
  return (
    <span className="criterion-score-badge">
      {score} / 5{meaning && ` · ${meaning}`}
    </span>
  );
}

export default function InterviewReport({
  sessionId,
  initial,
  onRetry,
  candidateName,
  attempt,
  retryOf,
}: Props) {
  const [result, setResult] = useState<ReportResult | null>(initial);
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);

  useEffect(() => {
    if (initial) return;
    const controller = new AbortController();
    getReport(sessionId, controller.signal)
      .then(setResult)
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorText(err));
      });
    return () => controller.abort();
  }, [sessionId, initial]);

  async function retry() {
    setRetrying(true);
    setRetryError(null);
    try {
      onRetry(await retryInterview(sessionId));
    } catch (err) {
      setRetryError(errorText(err));
      setRetrying(false);
    }
  }

  const heading = (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", justifyContent: "space-between", alignItems: "center", gap: "1rem" }}>
        <div>
          <span className="eyebrow">Post-Session Evaluation</span>
          <h2 tabIndex={-1} data-screen-heading style={{ fontSize: "1.75rem", marginTop: "0.25rem" }}>
            Interview Performance Report{attempt ? ` (#${attempt})` : ""}
          </h2>
        </div>
        {result && (
          <ExportButtons
            what="report"
            fileName={exportFileName("report", new Date(), attempt)}
            build={() => reportDocument(result.report, { candidateName, date: new Date(), attempt })}
          />
        )}
      </div>
      {retryOf && <p className="hint" style={{ marginTop: "0.25rem" }}>A retry of interview #{retryOf}.</p>}
      <p className="hint" style={{ marginTop: "0.25rem" }}>{COPY.reportIntro}</p>
    </div>
  );

  if (error) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
        {heading}
        <p className="form-error" role="alert">{error}</p>
      </div>
    );
  }

  if (!result) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
        {heading}
        <div className="card" style={{ textAlign: "center", padding: "2rem" }}>
          <p className="hint">Synthesizing criteria evaluation and generating report…</p>
        </div>
      </div>
    );
  }

  const r: Report = result.report;
  const s = r.summary;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
      {heading}

      {/* 1. Summary Hero Metrics */}
      <section className="report-summary-hero" aria-labelledby="r-summary">
        <div>
          <span className="stat-label">Session Format</span>
          <p style={{ fontWeight: 700, fontSize: "1.1rem" }}>{MODE_LABEL[s.settings.mode]}</p>
          <span className="stat-hint">
            {DIFFICULTY_LABEL[s.settings.difficulty]} · {s.settings.duration_minutes} min ({LEVEL_LABEL[s.settings.experience_level]})
          </span>
        </div>
        <div>
          <span className="stat-label">Active Time</span>
          <p style={{ fontWeight: 700, fontSize: "1.1rem" }}>{formatRemaining(s.active_seconds)}</p>
          <span className="stat-hint">{s.finish_reason_text}</span>
        </div>
        <div>
          <span className="stat-label">Questions &amp; Completion</span>
          <p style={{ fontWeight: 700, fontSize: "1.1rem" }}>
            {s.answers.answer} of {s.questions_asked} Answered
          </p>
          <span className="stat-hint">
            {s.answers.skip > 0 && `${s.answers.skip} skipped · `}
            {s.answers.dont_know > 0 && `${s.answers.dont_know} unknown · `}
            {s.evaluations.evaluated} evaluated with AI
          </span>
        </div>
        <div>
          <span className="stat-label">Planning Origin</span>
          <p style={{ fontWeight: 700, fontSize: "1.1rem" }}>
            {s.plan_source === "AI" ? "AI Assisted" : "Structured Rules"}
          </p>
          <span className="stat-hint">Checked against role rubrics</span>
        </div>
      </section>

      {/* 2. Retry Next Steps (High Priority) */}
      <section className="card active-session-banner" aria-labelledby="r-retry" style={{ background: "var(--surface)" }}>
        <div>
          <span className="badge badge-primary">Recommended Action</span>
          <h3 id="r-retry" style={{ fontSize: "1.2rem", marginTop: "0.25rem", color: "var(--text)" }}>
            Reinforce Your Weaker Topics
          </h3>
          <p className="hint" style={{ marginTop: "0.25rem", maxWidth: "600px" }}>{r.retry.note}</p>
        </div>

        {retryError && <p className="form-error" role="alert">{retryError}</p>}
        {retrying && <p role="status">{COPY.retryWorking}</p>}

        {canRetry(r.retry) && (
          <button
            type="button"
            className="primary"
            style={{ padding: "0.75rem 1.5rem", whiteSpace: "nowrap" }}
            onClick={retry}
            disabled={retrying}
          >
            {retrying ? "Preparing Retry…" : "Retry Topics to Practise →"}
          </button>
        )}
      </section>

      {/* 3. Key Strengths ("Shown Clearly") */}
      <section className="card" aria-labelledby="r-shown">
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.5rem" }}>
          <span style={{ fontSize: "1.25rem" }}>🌟</span>
          <h3 id="r-shown" style={{ fontSize: "1.2rem" }}>Key Strengths Demonstrated</h3>
        </div>
        <p className="hint">Answers where your explanations met or exceeded rubric expectations.</p>

        {r.shown_clearly.length === 0 ? (
          <p className="hint" style={{ marginTop: "0.5rem" }}>No clear high-scoring criteria identified in this session.</p>
        ) : (
          <div className="criteria-grid">
            {r.shown_clearly.map((i) => (
              <div key={`${i.question_id}-${i.criterion}`} className="criterion-card">
                <div className="criterion-header">
                  <strong>{CRITERION_LABEL[i.criterion]}</strong>
                  <Score score={i.score} meaning={i.score_meaning} />
                </div>
                <span className="hint" style={{ fontSize: "0.8rem" }}>Answer #{i.seq}</span>
                <Quotes items={i.evidence} />
              </div>
            ))}
          </div>
        )}
      </section>

      {/* 4. Areas for Growth ("To Practise") */}
      <section className="card" aria-labelledby="r-practise">
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.5rem" }}>
          <span style={{ fontSize: "1.25rem" }}>🎯</span>
          <h3 id="r-practise" style={{ fontSize: "1.2rem" }}>Areas for Growth &amp; Missing Concepts</h3>
        </div>
        <p className="hint">Topics and rubrics where scores indicated opportunity for more depth or structure.</p>

        {r.to_practise.criteria.length + r.to_practise.missing_concepts.length === 0 ? (
          <p className="hint" style={{ marginTop: "0.5rem" }}>All evaluated criteria met expectations with no major gaps.</p>
        ) : (
          <div className="criteria-grid">
            {r.to_practise.criteria.map((i) => (
              <div key={`${i.question_id}-${i.criterion}`} className="practice-card">
                <div className="criterion-header">
                  <strong>{CRITERION_LABEL[i.criterion]}</strong>
                  <Score score={i.score} meaning={i.score_meaning} />
                </div>
                <span className="hint" style={{ fontSize: "0.8rem" }}>In Answer #{i.seq}</span>
                <p style={{ fontSize: "0.875rem", marginTop: "0.25rem" }}>{i.practice}</p>
                <Quotes items={i.evidence} />
              </div>
            ))}
            {r.to_practise.missing_concepts.map((m) => (
              <div key={`${m.question_id}-${m.concept}`} className="practice-card" style={{ borderLeftColor: "var(--warn)" }}>
                <div className="criterion-header">
                  <strong>Missing Concept: {m.concept}</strong>
                  <span className="badge badge-warning" style={{ fontSize: "0.72rem" }}>Omitted</span>
                </div>
                <span className="hint" style={{ fontSize: "0.8rem" }}>In Answer #{m.seq}</span>
                <p style={{ fontSize: "0.875rem", marginTop: "0.25rem" }}>{m.practice}</p>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* 5. Detailed Question-by-Question Rubrics */}
      <section className="card" aria-labelledby="r-evidence">
        <h3 id="r-evidence" style={{ fontSize: "1.2rem", marginBottom: "0.5rem" }}>
          Full Question-by-Question Rubrics &amp; Transcripts
        </h3>
        <p className="hint" style={{ marginBottom: "1rem" }}>
          Detailed examination of how each answer was scored against standard rubrics.
        </p>

        {r.answer_evidence.length === 0 ? (
          <p className="hint">No evaluated answers recorded.</p>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
            {r.answer_evidence.map((a) => (
              <article key={a.question_id} className="card" style={{ background: "var(--surface-2)" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: "0.5rem" }}>
                  <h4 style={{ fontSize: "1.05rem" }}>
                    Answer #{a.seq}{" "}
                    <span className="badge" style={{ marginLeft: "0.5rem" }}>
                      {EVALUATION_STATUS_LABEL[a.status]}
                    </span>
                  </h4>
                </div>

                {a.notice && <p className="notice" style={{ marginBottom: "0.5rem" }}>{a.notice}</p>}

                {a.evaluation.criteria.length > 0 && (
                  <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", margin: "0.5rem 0" }}>
                    {a.evaluation.criteria.map((c) => (
                      <div key={c.criterion} style={{ padding: "0.5rem 0", borderTop: "1px solid var(--border)" }}>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                          <span style={{ fontWeight: 600 }}>{CRITERION_LABEL[c.criterion]}</span>
                          <Score score={c.score} />
                        </div>
                        <Quotes items={c.evidence} />
                      </div>
                    ))}
                  </div>
                )}

                {a.evaluation.missing_concepts.length > 0 && (
                  <p className="hint" style={{ marginTop: "0.5rem" }}>
                    <strong>Expected concepts not covered:</strong> {a.evaluation.missing_concepts.join(", ")}
                  </p>
                )}

                {a.evaluation.claims_to_verify.map((c, i) => (
                  <p key={i} style={{ marginTop: "0.4rem", fontSize: "0.85rem" }}>
                    <q>{c.quote}</q> <span className="badge badge-warning">Needs Verification</span>
                  </p>
                ))}

                {a.evaluation.feedback && (
                  <p style={{ marginTop: "0.75rem", fontStyle: "italic", color: "var(--text)" }}>
                    Feedback: {a.evaluation.feedback}
                  </p>
                )}
              </article>
            ))}
          </div>
        )}
      </section>

      {/* 6. Communication & Composure Signals */}
      <section className="card" aria-labelledby="r-observations">
        <h3 id="r-observations" style={{ fontSize: "1.2rem", marginBottom: "0.5rem" }}>
          Communication &amp; Composure Telemetry
        </h3>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(18rem, 1fr))", gap: "1rem" }}>
          <div style={{ background: "var(--surface-2)", padding: "1rem", borderRadius: "8px" }}>
            <h4 style={{ fontSize: "0.95rem", marginBottom: "0.5rem" }}>Answer Lengths</h4>
            <ul className="plain-list" style={{ fontSize: "0.875rem" }}>
              {r.communication_observations.answers.map((o) => (
                <li key={o.question_id}>
                  Answer #{o.seq}: <strong>{o.word_count} words</strong>
                  {o.concepts_mentioned.length > 0 && (
                    <span className="hint"> · concepts: {o.concepts_mentioned.join(", ")}</span>
                  )}
                </li>
              ))}
            </ul>
            <p className="hint" style={{ marginTop: "0.5rem", fontSize: "0.8rem" }}>
              {r.communication_observations.speech_metrics.note}
            </p>
          </div>

          <div style={{ background: "var(--surface-2)", padding: "1rem", borderRadius: "8px" }}>
            <h4 style={{ fontSize: "0.95rem", marginBottom: "0.5rem" }}>Facial Composure Signals</h4>
            <p className="hint" style={{ fontSize: "0.85rem" }}>{r.expression_signals.note}</p>
            {r.expression_signals.questions.length > 0 && (
              <ul className="plain-list" style={{ marginTop: "0.5rem", fontSize: "0.875rem" }}>
                {r.expression_signals.questions.map((q) => (
                  <li key={q.question_id}>
                    Question #{q.seq}: <strong>{q.summary}</strong>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </section>

      {/* 7. Score Scale & Rubric Methodology */}
      <section className="card" aria-labelledby="r-scale" style={{ background: "var(--surface-2)" }}>
        <h3 id="r-scale" style={{ fontSize: "1rem", marginBottom: "0.5rem" }}>
          Evaluation Rubric Scale &amp; Disclaimer
        </h3>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(14rem, 1fr))", gap: "0.75rem", fontSize: "0.85rem" }}>
          {Object.entries(r.score_scale).map(([k, v]) => (
            <div key={k}>
              <span className="badge" style={{ marginRight: "0.4rem" }}>
                {k === "null" ? "No Score" : `${k} / 5`}
              </span>
              <span>{v}</span>
            </div>
          ))}
        </div>
        <p className="notice" style={{ marginTop: "1rem", fontSize: "0.825rem" }}>
          {r.limitations}
        </p>
      </section>

      {/* Navigation Return */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", paddingTop: "0.5rem" }}>
        <Link href="/" className="back-link">
          ← Back to Candidate Dashboard
        </Link>
        <button
          type="button"
          onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}
          style={{ fontSize: "0.85rem" }}
        >
          ↑ Back to Top
        </button>
      </div>
    </div>
  );
}
