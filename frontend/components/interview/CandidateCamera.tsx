"use client";

import React from "react";
import type { Camera } from "@/components/interview/useExpressionCamera";
import { useCameraTelemetry } from "@/components/interview/useExpressionCamera";

type CandidateCameraProps = {
  camera: Camera;
  voiceListening: boolean;
  voiceSpeaking: boolean;
};

function CandidateCamera({
  camera,
  voiceListening,
  voiceSpeaking,
}: CandidateCameraProps) {
  const { state, message, attach, enable } = camera;
  const { label, breakdown } = useCameraTelemetry(camera);
  const isVideoOn = state === "starting" || state === "loading" || state === "active";

  return (
    <section className="camera-center-panel" aria-label="Candidate Video Feed">
      <div className="candidate-camera-frame" data-active={state === "active"}>
        {/* Top Badges Overlay */}
        <div className="camera-badge-overlay">
          {state === "active" ? (
            <span className="live-indicator">● Live Video</span>
          ) : (
            <span
              className="badge"
              style={{
                background: "rgba(15, 23, 42, 0.75)",
                color: "#cbd5e1",
                backdropFilter: "blur(4px)",
                border: "1px solid rgba(255, 255, 255, 0.15)",
              }}
            >
              Camera: {state === "starting" ? "Starting…" : state === "loading" ? "Loading…" : "Off"}
            </span>
          )}

          {/* Real-Time Microphone Status Overlay */}
          <span
            className="badge"
            style={{
              background: voiceListening
                ? "rgba(220, 38, 38, 0.85)"
                : voiceSpeaking
                  ? "rgba(79, 70, 229, 0.85)"
                  : "rgba(15, 23, 42, 0.75)",
              color: "#fff",
              backdropFilter: "blur(4px)",
              border: "1px solid rgba(255, 255, 255, 0.15)",
            }}
          >
            {voiceListening
              ? "🎙 Mic: Listening…"
              : voiceSpeaking
                ? "🔊 AI Speaking"
                : "🎙 Mic: Ready"}
          </span>
        </div>

        {/* Video Preview Feed */}
        {isVideoOn ? (
          <video
            ref={attach}
            className="camera-preview"
            muted
            playsInline
            autoPlay
            aria-label="Your live camera preview (remains 100% on your device)"
          />
        ) : (
          <div className="camera-off-placeholder">
            <div className="placeholder-avatar">👤</div>
            <p style={{ fontWeight: 600, color: "#f8fafc", fontSize: "0.95rem" }}>
              {state === "unavailable"
                ? "Camera or expression model unavailable"
                : "Candidate camera is currently off"}
            </p>
            <p className="hint" style={{ color: "#94a3b8", fontSize: "0.8rem", maxWidth: "340px" }}>
              Enabling your camera provides local facial composure feedback during practice.
              Video frames are never saved or sent to any server.
            </p>
            <button
              type="button"
              className="primary"
              style={{ padding: "0.5rem 1.25rem", marginTop: "0.25rem" }}
              onClick={() => enable()}
            >
              {state === "unavailable" ? "Try Camera Again" : "Turn On Camera"}
            </button>
          </div>
        )}

        {/* Bottom Expression Telemetry Overlay */}
        {state === "active" && (
          <div className="camera-telemetry-overlay">
            <div>
              Composure: <strong style={{ color: "#38bdf8", textTransform: "capitalize" }}>{label}</strong>
            </div>
            {breakdown.length > 0 && (
              <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                {breakdown.slice(0, 3).map(({ look, percent }) => (
                  <span
                    key={look}
                    style={{
                      background: "rgba(255, 255, 255, 0.12)",
                      padding: "2px 6px",
                      borderRadius: "4px",
                      fontSize: "0.72rem",
                    }}
                  >
                    {look}: {percent}%
                  </span>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Camera Status Notification */}
      {message && (
        <p className="notice" style={{ width: "100%", maxWidth: "760px", fontSize: "0.825rem" }}>
          {message}
        </p>
      )}
    </section>
  );
}

export default React.memo(CandidateCamera);
