"use client";

import React from "react";
import type { Camera } from "@/components/interview/useExpressionCamera";
import { useCameraTelemetry } from "@/components/interview/useExpressionCamera";
import { COPY } from "@/lib/interview";

const STATUS: Record<Camera["state"], string> = {
  off: "Camera: Off",
  starting: "Camera: Asking for permission…",
  loading: "Camera: Active · Loading expression model…",
  active: "Camera: Active · Expression analysis active",
  unavailable: "Camera: Off · Expression analysis unavailable",
};

/** The optional camera for tech check and previews. */
function CameraPanel({
  camera,
  intro = false,
}: {
  camera: Camera;
  intro?: boolean;
}) {
  const { state, message, attach, enable, disable } = camera;
  const { label, breakdown } = useCameraTelemetry(camera);
  const on = state === "starting" || state === "loading" || state === "active";

  return (
    <section className="card camera" aria-labelledby="camera-heading">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h3 id="camera-heading">{COPY.cameraTitle}</h3>
        <span className={`badge ${on ? "badge-success" : ""}`}>{STATUS[state]}</span>
      </div>

      {(intro || !on) && <p className="hint">{COPY.cameraExplain}</p>}

      <div className="camera-row" style={{ marginTop: "0.5rem" }}>
        {on && (
          <div className="camera-frame" data-live={state === "active"} style={{ borderRadius: "10px", overflow: "hidden" }}>
            <video
              ref={attach}
              className="camera-preview"
              muted
              playsInline
              autoPlay
              aria-label="Your camera preview (stays 100% on this device)"
              style={{ width: "220px", aspectRatio: "4/3", objectFit: "cover", display: "block" }}
            />
            {state === "active" && <span className="camera-live">● Live</span>}
          </div>
        )}

        <div className="camera-info">
          <p className="camera-status" role="status" aria-live="polite">
            {state === "active" && (
              <>
                Composure / Look: <strong>{label}</strong>
              </>
            )}
          </p>

          {state === "active" && breakdown.length > 0 && (
            <ul className="look-bars" aria-label="Recent face samples breakdown">
              {breakdown.map(({ look, percent }) => (
                <li key={look}>
                  <span className="look-name">{look}</span>
                  <span className="look-track" aria-hidden="true">
                    <span className="look-fill" style={{ width: `${percent}%` }} />
                  </span>
                  <span className="look-pct">{percent}%</span>
                </li>
              ))}
            </ul>
          )}

          {message && <p className="notice">{message}</p>}

          <div className="actions" style={{ marginTop: "0.5rem" }}>
            {on ? (
              <button type="button" onClick={disable}>
                Disable camera
              </button>
            ) : (
              <button type="button" className="primary" onClick={() => enable()}>
                {state === "unavailable" ? "Try camera again" : "Enable camera"}
              </button>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

export default React.memo(CameraPanel);
