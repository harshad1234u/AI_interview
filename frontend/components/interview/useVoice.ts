"use client";

// Optional voice mode, built on the browser's own speech engines (no extra service of ours):
//   speechSynthesis      reads the interviewer's question aloud
//   SpeechRecognition    turns the candidate's speech into text for the answer box
// Audio is never recorded or stored by this app and never sent to our server; the recogniser is
// the browser vendor's service (disclosed before the microphone is used). Everything degrades to
// plain typing when an engine is missing, blocked or fails.
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  DEFAULT_LANG,
  DEFAULT_RATE,
  type VoiceLang,
  isVoiceLang,
  pickVoice,
  recognitionErrorText,
  splitForSpeech,
} from "@/lib/voice";

type RecognitionResult = { isFinal: boolean; 0: { transcript: string } };
type RecognitionEvent = { resultIndex: number; results: ArrayLike<RecognitionResult> };
type Recognition = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onstart: (() => void) | null;
  onresult: ((e: RecognitionEvent) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
};
type RecognitionCtor = new () => Recognition;

const recognitionCtor = (): RecognitionCtor | null => {
  if (typeof window === "undefined") return null;
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
};
const canSpeak = () => typeof window !== "undefined" && "speechSynthesis" in window
  && typeof SpeechSynthesisUtterance !== "undefined";

// Preferences are a per-device convenience (never identity or answers); storage may be blocked.
const KEY = "coach.voice.v1";
type Prefs = { enabled: boolean; autoRead: boolean; autoListen: boolean; lang: VoiceLang; rate: number };
const DEFAULT_PREFS: Prefs = { enabled: false, autoRead: true, autoListen: false, lang: DEFAULT_LANG, rate: DEFAULT_RATE };

function loadPrefs(): Prefs {
  try {
    const raw = JSON.parse(window.localStorage.getItem(KEY) ?? "null") as Partial<Prefs> | null;
    if (!raw || typeof raw !== "object") return DEFAULT_PREFS;
    return {
      enabled: raw.enabled === true,
      autoRead: raw.autoRead !== false,
      autoListen: raw.autoListen === true,
      lang: isVoiceLang(raw.lang) ? raw.lang : DEFAULT_LANG,
      rate: typeof raw.rate === "number" && raw.rate >= 0.5 && raw.rate <= 2 ? raw.rate : DEFAULT_RATE,
    };
  } catch {
    return DEFAULT_PREFS;
  }
}

const noopSubscribe = () => () => {};

export type Voice = {
  canSpeak: boolean;
  canListen: boolean;
  enabled: boolean;
  autoRead: boolean;
  autoListen: boolean;
  lang: VoiceLang;
  rate: number;
  speaking: boolean;
  listening: boolean;
  /** Words recognised but not final yet: shown greyed under the answer box. */
  interim: string;
  /** Time the microphone has listened for the current answer (for the speech measurements). */
  listenedMs: number;
  error: string | null;
  set: (prefs: Partial<Prefs>) => void;
  /** Read `text` aloud; `then` runs when the whole text was read (not when it was interrupted). */
  speak: (text: string, then?: () => void) => void;
  stopSpeaking: () => void;
  /** Listen; each finished phrase goes to `onText`. */
  startListening: (onText: (text: string) => void) => void;
  stopListening: () => void;
  resetMetrics: () => void;
  /** Stop everything (pause, send, leave). */
  silence: () => void;
};

export function useVoice(): Voice {
  const speakOk = useSyncExternalStore(noopSubscribe, canSpeak, () => false);
  const listenOk = useSyncExternalStore(noopSubscribe, () => recognitionCtor() !== null, () => false);
  const [prefs, setPrefs] = useState<Prefs>(loadPrefs);
  const [speaking, setSpeaking] = useState(false);
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const [listenedMs, setListenedMs] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const prefsRef = useRef(prefs);
  const token = useRef(0); // bumped whenever speech is interrupted, so a stale chunk stops chaining
  const utterance = useRef<SpeechSynthesisUtterance | null>(null); // held: some engines drop it otherwise
  const rec = useRef<Recognition | null>(null);
  const want = useRef(false); // the candidate wants the microphone on (restart when it times out)
  const onText = useRef<(text: string) => void>(() => {});
  const startedAt = useRef(0);
  const listened = useRef(0);
  const quiet = useRef(0); // consecutive "no speech" endings
  const begin = useRef<() => void>(() => {});
  useEffect(() => {
    prefsRef.current = prefs;
  }, [prefs]);

  const set = useCallback((next: Partial<Prefs>) => {
    setPrefs((current) => {
      const merged = { ...current, ...next };
      try {
        window.localStorage.setItem(KEY, JSON.stringify(merged));
      } catch {
        /* storage blocked: the choice still applies for this visit */
      }
      return merged;
    });
  }, []);

  const stopSpeaking = useCallback(() => {
    token.current += 1;
    utterance.current = null;
    if (canSpeak()) window.speechSynthesis.cancel();
    setSpeaking(false);
  }, []);

  const speak = useCallback((text: string, then?: () => void) => {
    if (!canSpeak()) return;
    const synth = window.speechSynthesis;
    synth.cancel();
    const mine = ++token.current;
    const chunks = splitForSpeech(text);
    if (chunks.length === 0) return then?.();
    const voice = pickVoice(synth.getVoices(), prefsRef.current.lang);
    let i = 0;
    const next = () => {
      if (mine !== token.current) return;
      if (i >= chunks.length) {
        utterance.current = null;
        setSpeaking(false);
        then?.();
        return;
      }
      try {
        const u = new SpeechSynthesisUtterance(chunks[i++]);
        u.lang = voice?.lang ?? prefsRef.current.lang;
        if (voice) u.voice = voice;
        u.rate = prefsRef.current.rate;
        u.onend = next;
        u.onerror = (e) => {
          if (mine !== token.current) return;
          setSpeaking(false);
          if (e.error !== "interrupted" && e.error !== "canceled") {
            setError("The question could not be read aloud. You can read it on the page.");
          }
        };
        utterance.current = u;
        synth.speak(u);
      } catch {
        // An engine that refuses must never break the interview: the question is on the page.
        setSpeaking(false);
        setError("The question could not be read aloud. You can read it on the page.");
      }
    };
    setError(null);
    setSpeaking(true);
    next();
  }, []);

  const finishListening = useCallback(() => {
    if (startedAt.current) {
      listened.current += performance.now() - startedAt.current;
      startedAt.current = 0;
      setListenedMs(Math.round(listened.current));
    }
  }, []);

  const startNow = useCallback(() => {
    const Ctor = recognitionCtor();
    if (!Ctor) return;
    const r = new Ctor();
    r.lang = prefsRef.current.lang;
    r.continuous = true;
    r.interimResults = true;
    r.onstart = () => {
      startedAt.current = performance.now();
      setListening(true);
    };
    r.onresult = (e) => {
      let pending = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const result = e.results[i];
        if (result.isFinal) {
          quiet.current = 0;
          onText.current(result[0].transcript);
        } else {
          pending += result[0].transcript;
        }
      }
      setInterim(pending);
    };
    r.onerror = (e) => {
      if (e.error === "aborted") return;
      const { text, fatal } = recognitionErrorText(e.error);
      if (e.error === "no-speech") {
        quiet.current += 1;
        if (quiet.current < 2) return; // one quiet stretch is normal: listen again quietly
        want.current = false; // two in a row: stop and say so
      }
      if (fatal) want.current = false;
      setError(text);
    };
    r.onend = () => {
      setInterim("");
      finishListening();
      if (rec.current === r) rec.current = null;
      if (want.current) {
        setTimeout(() => {
          if (want.current && !rec.current) begin.current();
        }, 200);
      } else {
        setListening(false);
      }
    };
    rec.current = r;
    try {
      r.start();
    } catch {
      rec.current = null;
      want.current = false;
      setListening(false);
      setError("Voice input could not start. You can type your answer instead.");
    }
  }, [finishListening]);
  useEffect(() => {
    begin.current = startNow;
  }, [startNow]);

  const startListening = useCallback((handler: (text: string) => void) => {
    if (!recognitionCtor() || want.current) return;
    stopSpeaking(); // never listen to the interviewer's own voice
    onText.current = handler;
    want.current = true;
    quiet.current = 0;
    setError(null);
    startNow();
  }, [startNow, stopSpeaking]);

  const stopListening = useCallback(() => {
    want.current = false;
    rec.current?.stop(); // delivers what was heard so far, then onend
    if (!rec.current) setListening(false);
  }, []);

  const resetMetrics = useCallback(() => {
    listened.current = 0;
    startedAt.current = 0;
    setListenedMs(0);
  }, []);

  const silence = useCallback(() => {
    stopSpeaking();
    want.current = false;
    rec.current?.abort();
    rec.current = null;
    finishListening();
    setInterim("");
    setListening(false);
  }, [finishListening, stopSpeaking]);

  // Leaving the interview always releases the speaker and the microphone.
  useEffect(() => silence, [silence]);

  // Some engines need voices loaded before the first utterance picks one.
  useEffect(() => {
    if (!canSpeak()) return;
    const synth = window.speechSynthesis;
    synth.getVoices();
    const refresh = () => void synth.getVoices();
    synth.addEventListener("voiceschanged", refresh);
    return () => synth.removeEventListener("voiceschanged", refresh);
  }, []);

  return {
    canSpeak: speakOk,
    canListen: listenOk,
    ...prefs,
    speaking,
    listening,
    interim,
    listenedMs,
    error,
    set,
    speak,
    stopSpeaking,
    startListening,
    stopListening,
    resetMetrics,
    silence,
  };
}
