"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  type AnswerKind,
  type ClientAction,
  type InterviewStatus,
  type QuestionView,
  type TurnResult,
  actOnSession,
  getInterviewSession,
  getQuestions,
  nextQuestion,
  saveExpressionObservation,
  submitAnswer,
} from "@/lib/api";
import type { Camera } from "@/components/interview/useExpressionCamera";
import type { Voice } from "@/components/interview/useVoice";
import { appendTranscript, speechMetrics } from "@/lib/voice";
import {
  COPY,
  errorText,
  isConflict,
  isNetworkError,
  screenFor,
} from "@/lib/interview";
import InterviewHeader from "@/components/interview/InterviewHeader";
import QuestionsPanel from "@/components/interview/QuestionsPanel";
import CandidateCamera from "@/components/interview/CandidateCamera";
import VoiceHub from "@/components/interview/VoiceHub";
import InterviewControls from "@/components/interview/InterviewControls";

type Props = {
  sessionId: string;
  paused: boolean;
  onStatus: (status: InterviewStatus) => void;
  // The unsent answer lives in the parent's memory only, so a pause doesn't lose it. Never stored.
  draft: string;
  setDraft: (text: string) => void;
  camera: Camera;
  voice: Voice;
  attempt?: number;
};

export default function InterviewTurn({
  sessionId,
  paused,
  onStatus,
  draft,
  setDraft,
  camera,
  voice,
  attempt,
}: Props) {
  const [turn, setTurn] = useState<TurnResult | null>(null);
  const [historyQuestions, setHistoryQuestions] = useState<QuestionView[]>([]);
  const [fetchedAt, setFetchedAt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState(false);
  const questionRef = useRef<HTMLParagraphElement>(null);

  // Load session questions history for the left panel
  const loadHistory = useCallback(async () => {
    try {
      const hist = await getQuestions(sessionId);
      if (hist?.questions) setHistoryQuestions(hist.questions);
    } catch {
      // Non-critical background fetch: ignore error
    }
  }, [sessionId]);

  const loadNext = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await nextQuestion(sessionId);
      if (result.question === null) return onStatus(result.status); // finished: feedback next
      setFetchedAt(Date.now());
      setTurn(result);
      await loadHistory();
    } catch (err) {
      setError(errorText(err));
      if (isConflict(err)) setBlocked(true);
    } finally {
      setBusy(false);
    }
  }, [sessionId, onStatus, loadHistory]);

  useEffect(() => {
    if (paused) return;
    let ignore = false;
    nextQuestion(sessionId)
      .then(async (result) => {
        if (ignore) return;
        if (result.question === null) {
          onStatus(result.status);
          return;
        }
        setFetchedAt(Date.now());
        setTurn(result);
        setError(null);
        setBusy(false);
        try {
          const hist = await getQuestions(sessionId);
          if (!ignore && hist?.questions) setHistoryQuestions(hist.questions);
        } catch {
          // Non-critical
        }
      })
      .catch((err) => {
        if (ignore) return;
        setError(errorText(err));
        if (isConflict(err)) setBlocked(true);
        setBusy(false);
      });
    return () => {
      ignore = true;
    };
  }, [sessionId, paused, onStatus]);

  const questionId = turn?.question?.id;
  useEffect(() => {
    if (questionId) questionRef.current?.focus();
  }, [questionId]);

  // Camera samples belong to the open question; none are taken while paused.
  const { setQuestion, setPaused } = camera;
  useEffect(() => {
    setQuestion(paused ? null : questionId ?? null);
    setPaused(paused);
  }, [questionId, paused, setQuestion, setPaused]);

  // Voice mode: a new question is read aloud, and microphone auto-listens if chosen.
  const draftRef = useRef(draft);
  useEffect(() => {
    draftRef.current = draft;
  }, [draft]);

  const questionText = turn?.question?.text ?? null;
  const questionTextRef = useRef(questionText);
  useEffect(() => {
    questionTextRef.current = questionText;
  }, [questionText]);

  const { enabled: voiceOn, autoRead, autoListen, canSpeak, canListen } = voice;
  const { speak, startListening, silence, resetMetrics } = voice;

  const listenInto = useCallback(() => {
    startListening((heard) => setDraft(appendTranscript(draftRef.current, heard)));
  }, [startListening, setDraft]);

  useEffect(() => {
    if (!questionId) return;
    resetMetrics();
    const text = questionTextRef.current;
    if (!voiceOn || !text) return;
    if (autoRead && canSpeak) {
      speak(text, autoListen && canListen ? listenInto : undefined);
    } else if (autoListen && canListen) {
      listenInto();
    }
    return () => silence();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [questionId, voiceOn]);

  useEffect(() => {
    if (paused) silence();
  }, [paused, silence]);

  async function send(kind: AnswerKind) {
    if (!turn?.question) return;
    silence();
    setBusy(true);
    setError(null);
    try {
      const speechDuration = voice.listenedMs > 0 ? voice.listenedMs : undefined;
      const metrics = kind === "ANSWER" && speechDuration ? speechMetrics(draft, speechDuration) : null;
      const speechWpm = metrics?.wordsPerMinute ?? undefined;
      await submitAnswer(
        sessionId,
        turn.question.id,
        kind,
        kind === "ANSWER" ? draft : undefined,
        speechDuration,
        speechWpm,
      );
    } catch (err) {
      setError(isNetworkError(err) ? COPY.networkKept : errorText(err));
      if (isConflict(err)) setBlocked(true);
      setBusy(false);
      return;
    }
    setDraft(""); // dropped only after server saves
    const observation = camera.take(turn.question.id);
    if (observation) {
      saveExpressionObservation(sessionId, turn.question.id, observation).catch(() => {});
    }
    setTurn(null);
    await loadNext();
  }

  async function act(action: ClientAction, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(true);
    setError(null);
    try {
      onStatus((await actOnSession(sessionId, action)).status);
    } catch (err) {
      setError(errorText(err));
      if (isConflict(err)) setBlocked(true);
    } finally {
      setBusy(false);
    }
  }

  async function proceed() {
    setBusy(true);
    setError(null);
    try {
      const session = await getInterviewSession(sessionId);
      setBlocked(false);
      if (screenFor(session.status) === "turn") await loadNext();
      else onStatus(session.status);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  const errorBox = error && (
    <div role="alert" style={{ width: "100%", margin: "0.5rem 0" }}>
      <p className="form-error">{error}</p>
      {blocked ? (
        <div className="actions">
          <button type="button" className="primary" onClick={proceed} disabled={busy}>
            Continue
          </button>
        </div>
      ) : (
        !turn?.question &&
        !paused && (
          <div className="actions">
            <button type="button" className="primary" onClick={loadNext} disabled={busy}>
              Try again
            </button>
          </div>
        )
      )}
    </div>
  );

  // Paused State
  if (paused) {
    return (
      <div className="interview-room" aria-label="Interview paused">
        <InterviewHeader
          attempt={attempt}
          turn={turn}
          fetchedAt={fetchedAt}
          paused={true}
          onPause={() => {}}
          onResume={() => act("RESUME")}
          onEnd={() => act("END_INTERVIEW", COPY.endConfirm)}
          busy={busy}
        />

        <div className="card" style={{ textAlign: "center", padding: "2.5rem 1.5rem" }}>
          <span className="badge badge-warning" style={{ fontSize: "0.85rem", padding: "0.3rem 0.8rem" }}>
            Interview Paused
          </span>
          <h2 style={{ margin: "1rem 0 0.5rem" }} tabIndex={-1} data-screen-heading>
            Your interview is currently on pause
          </h2>
          <p className="hint" style={{ maxWidth: "480px", margin: "0 auto 1.5rem" }}>
            {COPY.paused}
          </p>
          {draft && (
            <p className="hint" style={{ color: "var(--primary)", fontWeight: 500, marginBottom: "1.5rem" }}>
              ✓ Your unsent answer is safely held in memory.
            </p>
          )}

          {errorBox}

          <div className="actions" style={{ justifyContent: "center" }}>
            <button
              type="button"
              className="primary"
              style={{ padding: "0.75rem 2rem", fontSize: "1rem" }}
              onClick={() => act("RESUME")}
              disabled={busy}
            >
              {busy ? "Resuming…" : "▶ Resume Interview"}
            </button>
            <button
              type="button"
              className="danger"
              onClick={() => act("END_INTERVIEW", COPY.endConfirm)}
              disabled={busy}
            >
              End Interview &amp; Generate Report
            </button>
          </div>
        </div>

        <CandidateCamera camera={camera} voiceListening={false} voiceSpeaking={false} />
      </div>
    );
  }

  const q = turn?.question;
  const waiting = turn?.status === "WAITING_FOR_ANSWER";
  const disabledActions = busy || blocked || !waiting;

  return (
    <div className="interview-room" aria-label="Interview Room">
      {/* 1. Header Bar: Attempt, Topic Progress, Isolated Timer, Pause/End */}
      <InterviewHeader
        attempt={attempt}
        turn={turn}
        fetchedAt={fetchedAt}
        paused={false}
        onPause={() => act("PAUSE")}
        onEnd={() => act("END_INTERVIEW", COPY.endConfirm)}
        busy={busy}
      />

      {errorBox}

      {/* 2. Main 3-Panel Experience (Left Questions + Center Camera Feed) */}
      <div className="room-stage">
        {/* LEFT PANEL: Interview Questions & Navigation */}
        <QuestionsPanel
          turn={turn}
          historyQuestions={historyQuestions}
          questionRef={questionRef}
        />

        {/* CENTRE PANEL: Candidate Video Feed & Bottom Voice Hub */}
        <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem", width: "100%" }}>
          {/* Prominent Candidate Camera (Horizontally Centered) */}
          <CandidateCamera
            camera={camera}
            voiceListening={voice.listening}
            voiceSpeaking={voice.speaking}
          />

          {/* 3. BOTTOM SECTION: Voice-First Interaction Hub */}
          {q && (
            <VoiceHub
              voice={voice}
              questionText={q.text}
              disabled={disabledActions}
              draft={draft}
              setDraft={setDraft}
              onListen={listenInto}
              onSubmit={() => send("ANSWER")}
              busy={busy}
            />
          )}

          {/* 4. Controls Toolbar */}
          {q && (
            <InterviewControls
              camera={camera}
              voice={voice}
              onSend={send}
              disabled={disabledActions}
              busy={busy}
              hasDraft={draft.trim().length > 0}
            />
          )}
        </div>
      </div>
    </div>
  );
}
