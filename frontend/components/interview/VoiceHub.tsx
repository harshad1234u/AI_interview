"use client";

import { useState } from "react";
import type { Voice } from "@/components/interview/useVoice";
import {
  RATES,
  VOICE_COPY,
  VOICE_LANGUAGES,
  type VoiceLang,
  describeSpeech,
  isVoiceLang,
  speechMetrics,
} from "@/lib/voice";
import { ANSWER_MAX_LENGTH, ANSWER_WARN_AT, COPY, answerLength } from "@/lib/interview";

type VoiceHubProps = {
  voice: Voice;
  questionText: string | null;
  disabled: boolean;
  draft: string;
  setDraft: (text: string) => void;
  onListen: () => void;
  onSubmit: () => void;
  busy: boolean;
};

export default function VoiceHub({
  voice,
  questionText,
  disabled,
  draft,
  setDraft,
  onListen,
  onSubmit,
  busy,
}: VoiceHubProps) {
  const [showTextFallback, setShowTextFallback] = useState(false);
  const length = answerLength(draft);

  // Derive Voice Interaction State (1: READY, 2: AI SPEAKING, 3: LISTENING, 4: PROCESSING, 5: ERROR)
  const isSpeaking = voice.speaking;
  const isListening = voice.listening;
  const hasError = Boolean(voice.error);

  let statusText = "Ready for your answer. Press Start Speaking below.";
  if (busy) {
    statusText = "AI is processing your response…";
  } else if (isSpeaking) {
    statusText = "The interviewer is asking the question…";
  } else if (isListening) {
    statusText = "Listening to you… speak your answer, then press Stop & Review.";
  } else if (draft.trim().length > 0) {
    statusText = "Answer captured. You can speak more or submit your response.";
  }

  return (
    <section className="voice-hub" aria-label="Voice Answer Hub">
      {/* 1. Status Indicator & Waveforms */}
      <div className="voice-status-bar">
        <div
          className={`waveform-container ${isSpeaking || isListening ? "waveform-active" : ""}`}
          aria-hidden="true"
        >
          <span className="waveform-bar" />
          <span className="waveform-bar" />
          <span className="waveform-bar" />
          <span className="waveform-bar" />
          <span className="waveform-bar" />
        </div>
        <p className="voice-status-text" role="status" aria-live="polite">
          {statusText}
        </p>
      </div>

      {/* 2. Hero Voice Button Cluster */}
      <div className="voice-action-cluster">
        {!voice.canListen && (
          <div className="voice-unsupported-notice" role="status" style={{ padding: "0.5rem 1rem", background: "var(--surface-2)", borderRadius: "8px", fontSize: "0.85rem" }}>
            <p>{VOICE_COPY.unsupportedListen}</p>
          </div>
        )}
        {voice.canListen && (
          isListening ? (
            <button
              type="button"
              className="mic-hero-button listening"
              onClick={voice.stopListening}
              aria-label="Stop recording and review answer"
            >
              <span>■</span> Stop &amp; Review Answer
            </button>
          ) : (
            <button
              type="button"
              className="mic-hero-button"
              onClick={onListen}
              disabled={disabled || isSpeaking}
              aria-label="Start recording your spoken answer"
            >
              <span>🎙</span>
              {draft.trim().length > 0 ? "Add More to Speech" : "Start Speaking"}
            </button>
          )
        )}

        {voice.canSpeak && questionText && (
          isSpeaking ? (
            <button
              type="button"
              onClick={voice.stopSpeaking}
              style={{ padding: "0.75rem 1.25rem", borderRadius: "9999px" }}
            >
              ⏹ Stop Reading
            </button>
          ) : (
            <button
              type="button"
              onClick={() => voice.speak(questionText)}
              disabled={isListening || busy}
              style={{ padding: "0.75rem 1.25rem", borderRadius: "9999px" }}
              title="Hear the interviewer re-read the question aloud"
            >
              ↻ Read Question Aloud
            </button>
          )
        )}

        {draft.trim().length > 0 && !isListening && (
          <button
            type="button"
            className="primary"
            onClick={onSubmit}
            disabled={disabled || length === 0}
            style={{ padding: "0.75rem 1.5rem", borderRadius: "9999px", fontWeight: 700 }}
          >
            ✓ Send Spoken Answer
          </button>
        )}
      </div>

      {/* Real-time speech transcript preview */}
      {(voice.interim || draft) && (
        <div className="live-transcript-box" aria-live="polite">
          <span className="hint" style={{ fontSize: "0.75rem", display: "block", marginBottom: "0.25rem" }}>
            {isListening ? "Live Transcription (Speaking…):" : "Recorded Transcript:"}
          </span>
          <p style={{ fontStyle: isListening ? "italic" : "normal", fontSize: "0.95rem" }}>
            {draft || voice.interim}
            {voice.interim && !draft && "…"}
          </p>
        </div>
      )}

      {/* Speech cadence metrics when listening finishes */}
      {voice.listenedMs > 0 && !voice.listening && draft && (
        <span className="hint voice-metrics">
          Cadence: {describeSpeech(speechMetrics(draft, voice.listenedMs))}
        </span>
      )}

      {/* Error Banner */}
      {hasError && (
        <p className="notice" role="alert" style={{ fontSize: "0.85rem" }}>
          {voice.error}
        </p>
      )}

      {/* 3. Secondary Manual Typing Alternative */}
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", width: "100%" }}>
        <button
          type="button"
          className="manual-typing-toggle"
          onClick={() => setShowTextFallback(!showTextFallback)}
          aria-expanded={showTextFallback}
        >
          <span>⌨</span>
          {showTextFallback ? "Hide typing box ▲" : "Prefer to type your answer instead? ▾"}
        </button>

        {showTextFallback && (
          <div className="manual-typing-drawer" style={{ marginTop: "0.75rem" }}>
            <div className="field" style={{ margin: 0 }}>
              <label htmlFor="answer" style={{ fontSize: "0.85rem" }}>
                Type or edit your response:
              </label>
              <textarea
                id="answer"
                rows={5}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                disabled={disabled}
                placeholder="Type your answer here, or review words captured from your microphone above…"
                aria-describedby="answer-count"
              />
            </div>

            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "0.5rem" }}>
              <span id="answer-count" className={length >= ANSWER_WARN_AT ? "field-error" : "hint"}>
                {length.toLocaleString()} / {ANSWER_MAX_LENGTH.toLocaleString()} characters
                {length >= ANSWER_WARN_AT && ` — ${COPY.lengthWarning}`}
              </span>

              <div style={{ display: "flex", gap: "0.5rem" }}>
                <button
                  type="button"
                  className="primary"
                  onClick={onSubmit}
                  disabled={disabled || length === 0}
                  style={{ padding: "0.5rem 1rem", fontSize: "0.85rem" }}
                >
                  Submit Written Answer
                </button>
                <button
                  type="button"
                  onClick={() => setShowTextFallback(false)}
                  style={{ padding: "0.5rem 0.85rem", fontSize: "0.85rem" }}
                >
                  Close
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Voice Settings Drawer */}
      <details className="voice-settings">
        <summary>Voice &amp; Audio Preferences</summary>
        <div className="voice-settings-grid">
          <div className="field" style={{ margin: 0 }}>
            <label htmlFor="voice-lang">Language</label>
            <select
              id="voice-lang"
              value={voice.lang}
              onChange={(e) => isVoiceLang(e.target.value) && voice.set({ lang: e.target.value as VoiceLang })}
            >
              {VOICE_LANGUAGES.map((l) => (
                <option key={l.code} value={l.code}>
                  {l.label}
                </option>
              ))}
            </select>
          </div>
          <div className="field" style={{ margin: 0 }}>
            <label htmlFor="voice-rate">Interviewer Reading Speed</label>
            <select
              id="voice-rate"
              value={voice.rate}
              onChange={(e) => voice.set({ rate: Number(e.target.value) })}
            >
              {RATES.map((r) => (
                <option key={r} value={r}>
                  {r < 1 ? "Slower (0.8x)" : r > 1 ? "Faster (1.2x)" : "Normal (1.0x)"}
                </option>
              ))}
            </select>
          </div>
          <div className="field checkbox" style={{ margin: 0 }}>
            <input
              id="voice-autoread"
              type="checkbox"
              checked={voice.autoRead}
              disabled={!voice.canSpeak}
              onChange={(e) => voice.set({ autoRead: e.target.checked })}
            />
            <label htmlFor="voice-autoread">Read questions aloud automatically</label>
          </div>
          <div className="field checkbox" style={{ margin: 0 }}>
            <input
              id="voice-autolisten"
              type="checkbox"
              checked={voice.autoListen}
              disabled={!voice.canListen}
              onChange={(e) => voice.set({ autoListen: e.target.checked })}
            />
            <label htmlFor="voice-autolisten">Auto-open microphone after reading</label>
          </div>
        </div>
      </details>
    </section>
  );
}
