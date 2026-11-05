"use client";

import type { AnswerKind } from "@/lib/api";
import { ANSWER_KIND_LABEL } from "@/lib/interview";
import type { Camera } from "@/components/interview/useExpressionCamera";
import type { Voice } from "@/components/interview/useVoice";

type InterviewControlsProps = {
  camera: Camera;
  voice: Voice;
  onSend: (kind: AnswerKind) => void;
  disabled: boolean;
  busy: boolean;
  hasDraft: boolean;
};

const SECONDARY_KINDS: AnswerKind[] = ["CLARIFY", "DONT_KNOW", "SKIP"];

export default function InterviewControls({
  camera,
  voice,
  onSend,
  disabled,
  busy,
  hasDraft,
}: InterviewControlsProps) {
  const isCameraActive =
    camera.state === "starting" || camera.state === "loading" || camera.state === "active";

  return (
    <footer className="interview-controls-bar" aria-label="Interview Actions and Media Bar">
      {/* Quick Answer Kinds */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem", alignItems: "center" }}>
        <button
          type="button"
          className="primary"
          onClick={() => onSend("ANSWER")}
          disabled={disabled || !hasDraft}
          style={{ padding: "0.45rem 1rem", fontSize: "0.85rem" }}
          title={hasDraft ? "Submit your captured answer" : "Speak or type an answer first"}
        >
          {busy ? "Submitting…" : "✓ Submit Answer"}
        </button>

        {SECONDARY_KINDS.map((kind) => (
          <button
            key={kind}
            type="button"
            onClick={() => onSend(kind)}
            disabled={disabled}
            style={{ padding: "0.45rem 0.8rem", fontSize: "0.825rem" }}
          >
            {ANSWER_KIND_LABEL[kind]}
          </button>
        ))}
      </div>

      <div style={{ width: "1px", height: "20px", background: "var(--border)", margin: "0 0.25rem" }} />

      {/* Hardware Toggles */}
      <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
        <button
          type="button"
          onClick={isCameraActive ? camera.disable : () => camera.enable()}
          style={{ padding: "0.45rem 0.8rem", fontSize: "0.825rem" }}
          title={isCameraActive ? "Turn off camera preview" : "Enable camera self-preview"}
        >
          {isCameraActive ? "📹 Camera On" : "📹 Camera Off"}
        </button>

        <button
          type="button"
          onClick={() => {
            if (voice.enabled) {
              voice.silence();
              voice.set({ enabled: false });
            } else {
              voice.set({ enabled: true });
            }
          }}
          style={{ padding: "0.45rem 0.8rem", fontSize: "0.825rem" }}
          title={voice.enabled ? "Turn voice mode off" : "Enable voice conversation"}
        >
          {voice.enabled ? "🎙 Voice Mode On" : "🎙 Voice Mode Off"}
        </button>
      </div>
    </footer>
  );
}
