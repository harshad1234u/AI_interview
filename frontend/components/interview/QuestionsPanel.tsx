"use client";

import React, { type RefObject, useMemo, useState } from "react";
import type { QuestionView, TurnResult } from "@/lib/api";
import {
  ANSWER_KIND_LABEL,
  COPY,
  QUESTION_KIND_LABEL,
  RULES_REASON_TEXT,
} from "@/lib/interview";

type QuestionsPanelProps = {
  turn: TurnResult | null;
  historyQuestions: QuestionView[];
  questionRef: RefObject<HTMLParagraphElement | null>;
};

function QuestionsPanel({
  turn,
  historyQuestions,
  questionRef,
}: QuestionsPanelProps) {
  const [collapsedMobile, setCollapsedMobile] = useState(false);
  const q = turn?.question;
  const progress = turn?.progress;

  const pastQuestions = useMemo(
    () => historyQuestions.filter((item) => item.id !== q?.id && item.answer !== null),
    [historyQuestions, q?.id]
  );

  return (
    <aside className="questions-panel" aria-label="Interview questions">
      <div className="questions-panel-header">
        <div>
          <span className="eyebrow" style={{ fontSize: "0.72rem" }}>Curriculum</span>
          <h3 style={{ fontSize: "1.05rem" }}>Interview Questions</h3>
        </div>
        <button
          type="button"
          className="link"
          style={{ fontSize: "0.8rem", display: "none" }}
          onClick={() => setCollapsedMobile(!collapsedMobile)}
          aria-expanded={!collapsedMobile}
        >
          {collapsedMobile ? "Show" : "Hide"}
        </button>
      </div>

      {/* Active Question Card */}
      {q ? (
        <div className="active-question-card" aria-live="polite">
          <div style={{ display: "flex", flexWrap: "wrap", gap: "0.4rem", alignItems: "center" }}>
            <span className="badge badge-primary" style={{ fontSize: "0.72rem" }}>
              Current Question
            </span>
            <span className="badge" style={{ fontSize: "0.72rem" }}>
              {QUESTION_KIND_LABEL[q.kind]}
            </span>
            {progress && (
              <span className="hint" style={{ fontSize: "0.75rem", marginLeft: "auto" }}>
                Topic {q.topic_position}/{progress.topics_total}
              </span>
            )}
          </div>

          <p
            className="question-text"
            ref={questionRef}
            tabIndex={-1}
            style={{ fontSize: "1.05rem", lineHeight: 1.5, marginTop: "0.25rem" }}
          >
            {q.text}
          </p>

          <div style={{ marginTop: "0.25rem" }}>
            {q.source === "AI" && <span className="hint" style={{ fontSize: "0.75rem" }}>{COPY.aiWorded}</span>}
            {q.source === "RULES" &&
              q.kind !== "TOPIC" &&
              q.rules_reason &&
              q.rules_reason !== "NOT_REQUESTED" &&
              q.rules_reason !== "NOT_CONFIGURED" && (
                <span className="hint" style={{ fontSize: "0.75rem" }}>
                  {RULES_REASON_TEXT[q.rules_reason]}
                </span>
              )}
          </div>
        </div>
      ) : (
        <div className="card" style={{ padding: "1.25rem", textAlign: "center" }}>
          <p className="hint">Loading question…</p>
        </div>
      )}

      {/* Past Questions History in this session */}
      {pastQuestions.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", marginTop: "0.5rem" }}>
          <span className="hint" style={{ fontSize: "0.75rem", textTransform: "uppercase", letterSpacing: "0.05em", fontWeight: 600 }}>
            Answered Questions ({pastQuestions.length})
          </span>
          <div className="past-questions-list">
            {pastQuestions.map((item) => (
              <div key={item.id} className="past-question-item">
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span style={{ fontWeight: 600, fontSize: "0.75rem", color: "var(--text)" }}>
                    #{item.seq} · {item.topic}
                  </span>
                  {item.answer && (
                    <span
                      className={`badge ${item.answer.kind === "ANSWER" ? "badge-success" : ""}`}
                      style={{ fontSize: "0.68rem", padding: "0.1rem 0.4rem" }}
                    >
                      {ANSWER_KIND_LABEL[item.answer.kind]}
                    </span>
                  )}
                </div>
                <p
                  style={{
                    fontSize: "0.78rem",
                    color: "var(--muted)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                  title={item.text}
                >
                  {item.text}
                </p>
              </div>
            ))}
          </div>
        </div>
      )}
    </aside>
  );
}

export default React.memo(QuestionsPanel);
