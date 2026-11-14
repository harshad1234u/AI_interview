"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  type AnswerKind,
  type AnswerResult,
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
  type TurnTransitionMetrics,
  errorText,
  isConflict,
  isNetworkError,
  recordTurnMetrics,
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
  const submittingRef = useRef(false);
  const historySeqRef = useRef(0);

  // Load session questions history for the left panel (non-blocking background sync)
  const loadHistory = useCallback(async (): Promise<number> => {
    const seq = ++historySeqRef.current;
    const t0 = performance.now();
    try {
      const hist = await getQuestions(sessionId);
      if (seq === historySeqRef.current && hist?.questions) {
        setHistoryQuestions(hist.questions);
      }
      return Math.round(performance.now() - t0);
    } catch {
      // Non-critical background fetch: ignore error gracefully
      return Math.round(performance.now() - t0);
    }
  }, [sessionId]);

  const loadNext = useCallback(async () => {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await nextQuestion(sessionId);
      if (result.question === null) {
        onStatus(result.status); // finished: feedback next
        return;
      }
      setFetchedAt(Date.now());
      setTurn(result);
      // Independent non-blocking background history refresh
      loadHistory();
    } catch (err) {
      setError(errorText(err));
      if (isConflict(err)) setBlocked(true);
    } finally {
      submittingRef.current = false;
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
    if (submittingRef.current || busy || blocked || !turn?.question) return;
    submittingRef.current = true;
    silence();
    setBusy(true);
    setError(null);

    const tStart = performance.now();
    const answeredQuestion = turn.question;
    let answerResult: AnswerResult;

    // STEP 1: Submit and persist the answer
    try {
      const speechDuration = voice.listenedMs > 0 ? Math.round(voice.listenedMs) : undefined;
      const metrics = kind === "ANSWER" && speechDuration ? speechMetrics(draft, speechDuration) : null;
      const speechWpm = metrics?.wordsPerMinute ?? undefined;
      answerResult = await submitAnswer(
        sessionId,
        answeredQuestion.id,
        kind,
        kind === "ANSWER" ? draft : undefined,
        speechDuration,
        speechWpm,
      );
    } catch (err) {
      setError(isNetworkError(err) ? COPY.networkKept : errorText(err));
      if (isConflict(err)) setBlocked(true);
      submittingRef.current = false;
      setBusy(false);
      return;
    }

    const tSubmitEnd = performance.now();
    const submitDuration = Math.round(tSubmitEnd - tStart);

    // Answer safely confirmed and persisted on the backend
    setDraft(""); // dropped only after server saves
    const observation = camera.take(answeredQuestion.id);
    if (observation) {
      saveExpressionObservation(sessionId, answeredQuestion.id, observation).catch(() => {});
    }

    // Optimistically update history sidebar immediately with server-confirmed QuestionView
    if (answerResult?.question) {
      setHistoryQuestions((prev) => {
        const idx = prev.findIndex((q) => q.id === answerResult.question.id);
        if (idx >= 0) {
          const next = [...prev];
          next[idx] = answerResult.question;
          return next;
        }
        return [...prev, answerResult.question];
      });
    }

    // STEP 2: Request next question (only after successful answer persistence)
    try {
      const result = await nextQuestion(sessionId);
      const tNextEnd = performance.now();
      const nextDuration = Math.round(tNextEnd - tSubmitEnd);
      const totalTurnMs = Math.round(tNextEnd - tStart);

      if (result.question === null) {
        onStatus(result.status); // finished: feedback next
        submittingRef.current = false;
        setBusy(false);
        return;
      }

      // STEP 3: Display next question immediately and unblock UI!
      setFetchedAt(Date.now());
      setTurn(result);
      submittingRef.current = false;
      setBusy(false);

      // Record performance timing instrumentation
      const turnMetrics: TurnTransitionMetrics = {
        submitAnswerMs: submitDuration,
        nextQuestionMs: nextDuration,
        totalTurnMs,
        uiBlockedMs: totalTurnMs,
        requestCount: 2,
      };
      recordTurnMetrics(turnMetrics);

      // STEP 4: Independent non-blocking background history refresh
      loadHistory().then((histDuration) => {
        turnMetrics.historyRefreshMs = histDuration;
        recordTurnMetrics({ ...turnMetrics });
      });
    } catch (err) {
      // Next question failed after answer was saved: show recoverable error without losing saved answer
      setError(errorText(err));
      if (isConflict(err)) setBlocked(true);
      submittingRef.current = false;
      setBusy(false);
    }
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
