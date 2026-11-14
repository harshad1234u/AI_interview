"use client";

import React, { useEffect, useState } from "react";
import type { TurnResult } from "@/lib/api";
import { COPY, formatRemaining } from "@/lib/interview";

type HeaderProps = {
  attempt?: number;
  turn: TurnResult | null;
  fetchedAt: number;
  paused: boolean;
  onPause: () => void;
  onResume?: () => void;
  onEnd: () => void;
  busy: boolean;
};

/** Self-contained countdown timer to isolate 1-second ticks from camera and voice trees. */
function CountdownDisplay({ turn, fetchedAt }: { turn: TurnResult | null; fetchedAt: number }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  if (!turn) return null;
  const remaining = turn.progress.time_remaining_seconds - Math.max(0, now - fetchedAt) / 1000;
  const formatted = formatRemaining(remaining);
  const isDanger = remaining > 0 && remaining <= 60;
  const isWarning = remaining > 60 && remaining <= 180;

  return (
    <div
      className={`room-timer ${isDanger ? "danger" : isWarning ? "warning" : ""}`}
      title={COPY.timerNote}
      role="timer"
      aria-label={`Time remaining: ${formatted}`}
    >
      <span aria-hidden="true">⏱</span>
      <span>{formatted}</span>
    </div>
  );
}

function InterviewHeader({
  attempt,
  turn,
  fetchedAt,
  paused,
  onPause,
  onResume,
  onEnd,
  busy,
}: HeaderProps) {
  const q = turn?.question;
  const progress = turn?.progress;

  return (
    <header className="room-header" aria-label="Interview session header">
      <div className="room-meta">
        <span className="badge badge-primary">
          {attempt ? `Interview #${attempt}` : "Live Practice Session"}
        </span>
        {progress && q && (
          <span className="badge">
            Topic {q.topic_position} of {progress.topics_total}: <strong>{q.topic}</strong>
          </span>
        )}
        {paused && <span className="badge badge-warning">Paused</span>}
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
        {!paused && turn && (
          <CountdownDisplay turn={turn} fetchedAt={fetchedAt} />
        )}
        {paused ? (
          <button
            type="button"
            className="primary"
            style={{ padding: "0.35rem 0.85rem", fontSize: "0.85rem" }}
            onClick={onResume}
            disabled={busy}
          >
            ▶ Resume
          </button>
        ) : (
          <button
            type="button"
            style={{ padding: "0.35rem 0.85rem", fontSize: "0.85rem" }}
            onClick={onPause}
            disabled={busy}
            title="Pause the interview timer and media"
          >
            ⏸ Pause
          </button>
        )}
        <button
          type="button"
          className="danger"
          style={{ padding: "0.35rem 0.85rem", fontSize: "0.85rem" }}
          onClick={onEnd}
          disabled={busy}
          title="End interview and proceed to evaluation"
        >
          End Interview
        </button>
      </div>
    </header>
  );
}

export default React.memo(InterviewHeader);
