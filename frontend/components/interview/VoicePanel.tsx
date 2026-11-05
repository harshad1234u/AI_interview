"use client";

import type { Voice } from "@/components/interview/useVoice";
import {
  RATES,
  VOICE_COPY,
  VOICE_LANGUAGES,
  type VoiceLang,
  isVoiceLang,
} from "@/lib/voice";

function Settings({ voice }: { voice: Voice }) {
  return (
    <details className="voice-settings">
      <summary>Voice Settings &amp; Preferences</summary>
      <div className="voice-settings-grid">
        <div className="field">
          <label htmlFor="voice-lang">Speech Language</label>
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
        <div className="field">
          <label htmlFor="voice-rate">Reading Speed</label>
          <select
            id="voice-rate"
            value={voice.rate}
            onChange={(e) => voice.set({ rate: Number(e.target.value) })}
          >
            {RATES.map((r) => (
              <option key={r} value={r}>
                {r < 1 ? "Slower" : r > 1 ? "Faster" : "Normal"}
              </option>
            ))}
          </select>
        </div>
        <div className="field checkbox">
          <input
            id="voice-autoread"
            type="checkbox"
            checked={voice.autoRead}
            disabled={!voice.canSpeak}
            onChange={(e) => voice.set({ autoRead: e.target.checked })}
          />
          <label htmlFor="voice-autoread">Read each question aloud automatically</label>
        </div>
        <div className="field checkbox">
          <input
            id="voice-autolisten"
            type="checkbox"
            checked={voice.autoListen}
            disabled={!voice.canListen}
            onChange={(e) => voice.set({ autoListen: e.target.checked })}
          />
          <label htmlFor="voice-autolisten">Start listening when the question has been read</label>
        </div>
      </div>
    </details>
  );
}

/** Before the interview starts: what voice mode is, the privacy note, a way to try it. */
export function VoiceSetup({ voice }: { voice: Voice }) {
  const supported = voice.canSpeak || voice.canListen;
  return (
    <section className="card voice-setup" aria-labelledby="voice-heading">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h3 id="voice-heading">{VOICE_COPY.title}</h3>
        <span className="badge badge-primary">Voice-First</span>
      </div>
      <p className="hint">{VOICE_COPY.intro}</p>
      {!voice.canSpeak && <p className="notice">{VOICE_COPY.unsupportedSpeak}</p>}
      {!voice.canListen && <p className="notice">{VOICE_COPY.unsupportedListen}</p>}
      {supported && (
        <>
          <div className="field checkbox" style={{ margin: "0.5rem 0" }}>
            <input
              id="voice-enabled"
              type="checkbox"
              checked={voice.enabled}
              onChange={(e) => voice.set({ enabled: e.target.checked })}
            />
            <label htmlFor="voice-enabled" style={{ fontWeight: 600 }}>
              Enable Voice Mode for this interview
            </label>
          </div>
          {voice.enabled && (
            <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
              {voice.canListen && <p className="hint">{VOICE_COPY.micPrivacy}</p>}
              {voice.canSpeak && <p className="hint">{VOICE_COPY.voicePrivacy}</p>}
              <div className="actions" style={{ margin: "0.25rem 0" }}>
                {voice.canSpeak && (
                  <button
                    type="button"
                    disabled={voice.speaking}
                    onClick={() =>
                      voice.speak("Hello. I am your AI interviewer. Tell me when you are ready to begin.")
                    }
                  >
                    {voice.speaking ? "Speaking…" : "🔊 Hear a sample audio"}
                  </button>
                )}
              </div>
              <Settings voice={voice} />
            </div>
          )}
        </>
      )}
      {voice.error && <p className="notice" role="alert">{voice.error}</p>}
    </section>
  );
}

type StageProps = {
  voice: Voice;
  questionText: string | null;
  /** Nothing can be sent right now (busy, blocked, not waiting): the microphone is not offered. */
  disabled: boolean;
  onListen: () => void;
};

/** During a question: the interviewer audio indicator, whose turn it is, and the controls. */
export function VoiceStage({ voice, questionText, disabled, onListen }: StageProps) {
  if (!voice.enabled) {
    if (!voice.canSpeak && !voice.canListen) return null;
    return (
      <div className="voice-toggle">
        <button type="button" onClick={() => voice.set({ enabled: true })}>
          🎙 Turn on voice mode
        </button>
        <span className="hint">The interviewer reads questions aloud and you can answer by speaking.</span>
      </div>
    );
  }

  const isSpeaking = voice.speaking;
  const isListening = voice.listening;
  const status = isSpeaking
    ? "The interviewer is speaking…"
    : isListening
      ? "Listening… speak your answer, then press Stop & review."
      : "Your turn. Speak, or type below.";

  return (
    <div className="voice-stage" style={{ background: "var(--surface-2)" }}>
      <div
        className={`waveform-container ${isSpeaking || isListening ? "waveform-active" : ""}`}
        aria-hidden="true"
        style={{ padding: "0.5rem" }}
      >
        <span className="waveform-bar" />
        <span className="waveform-bar" />
        <span className="waveform-bar" />
        <span className="waveform-bar" />
        <span className="waveform-bar" />
      </div>

      <div className="voice-body">
        <p className="voice-status" role="status" aria-live="polite">
          {status}
        </p>
        <div className="actions">
          {voice.canSpeak && questionText && (
            isSpeaking ? (
              <button type="button" onClick={voice.stopSpeaking}>
                Stop reading
              </button>
            ) : (
              <button type="button" onClick={() => voice.speak(questionText)} disabled={isListening}>
                ↻ Read question again
              </button>
            )
          )}
          {voice.canListen && (
            isListening ? (
              <button type="button" className="primary" onClick={voice.stopListening}>
                ■ Stop &amp; review
              </button>
            ) : (
              <button type="button" className="primary" onClick={onListen} disabled={disabled}>
                🎙 {voice.listenedMs > 0 ? "Keep speaking" : "Start speaking"}
              </button>
            )
          )}
          <button
            type="button"
            className="link"
            onClick={() => {
              voice.silence();
              voice.set({ enabled: false });
            }}
          >
            Turn off voice mode
          </button>
        </div>
        {!voice.canListen && <p className="hint">{VOICE_COPY.unsupportedListen}</p>}
        {voice.error && <p className="notice" role="alert">{voice.error}</p>}
        <Settings voice={voice} />
      </div>
    </div>
  );
}
