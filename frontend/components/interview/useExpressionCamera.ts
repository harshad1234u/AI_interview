"use client";

// The optional camera: getUserMedia -> <video> -> a small face-expression model running in this
// browser -> one class per sample -> counts per question. Frames are read from the video element
// by the model and never copied, kept, logged or sent; only counts leave the browser.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  BASE_INTERVAL_MS,
  type Counts,
  type Observation,
  addSample,
  cameraErrorText,
  classify,
  emptyCounts,
  liveBreakdown,
  liveLabel,
  nextPace,
  toObservation,
} from "@/lib/expressions";

export type CameraState = "off" | "starting" | "loading" | "active" | "unavailable";

export type CameraTelemetry = {
  label: string;
  breakdown: { look: string; percent: number }[];
};

export type Camera = {
  state: CameraState;
  /** Why the camera is off or unavailable, in the candidate's words. */
  message: string | null;
  label: string;
  /** The recent samples as shares of looks, for the small live bar. */
  breakdown: { look: string; percent: number }[];
  subscribeTelemetry: (callback: (telemetry: CameraTelemetry) => void) => () => void;
  getTelemetry: () => CameraTelemetry;
  attach: (video: HTMLVideoElement | null) => void;
  enable: () => Promise<void>;
  disable: () => void;
  /** The question the samples belong to (null: none open, nothing is sampled). */
  setQuestion: (id: string | null) => void;
  setPaused: (paused: boolean) => void;
  /** The counts for one question, once: null when the camera was never on during it. */
  take: (questionId: string) => Observation | null;
};

const MODEL_URL = "/models/face-api"; // served by this app: no third-party request
type FaceApi = typeof import("@vladmandic/face-api");
let modelPromise: Promise<FaceApi> | null = null;

/** Loaded on first use only, then shared; a failure can be retried later. */
function loadModel(): Promise<FaceApi> {
  modelPromise ??= (async () => {
    const faceapi = await import("@vladmandic/face-api");
    // The bundled TensorFlow.js; its typings here omit the backend functions.
    const tf = faceapi.tf as unknown as { setBackend(name: string): Promise<boolean>; ready(): Promise<void> };
    if (!(await tf.setBackend("webgl").catch(() => false))) {
      await tf.setBackend("cpu"); // no WebGL: slower, and the pacing adapts
    }
    await tf.ready();
    await Promise.all([faceapi.nets.tinyFaceDetector.loadFromUri(MODEL_URL),
                       faceapi.nets.faceExpressionNet.loadFromUri(MODEL_URL)]);
    return faceapi;
  })().catch((err) => {
    modelPromise = null;
    throw err;
  });
  return modelPromise;
}

type Entry = { counts: Counts; used: boolean; failed: boolean };

export function useExpressionCamera(): Camera {
  const [state, setState] = useState<CameraState>("off");
  const [message, setMessage] = useState<string | null>(null);

  // Isolate high-frequency recent samples to ref and subscription listeners to prevent
  // parent-level (InterviewFlow / InterviewTurn) re-renders on every 1000ms sample.
  const recentRef = useRef<ReturnType<typeof classify>[]>([]);
  const subscribersRef = useRef<Set<(telemetry: CameraTelemetry) => void>>(new Set());
  const samplingRef = useRef(false);

  const stream = useRef<MediaStream | null>(null);
  const video = useRef<HTMLVideoElement | null>(null);
  const model = useRef<FaceApi | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const running = useRef(false);
  const paused = useRef(false);
  const question = useRef<string | null>(null);
  const entries = useRef(new Map<string, Entry>());
  const strikes = useRef(0);
  const failed = useRef(false); // enabled, then failed: later questions are "unavailable" too
  const tick = useRef<() => void>(() => {}); // the sampler, scheduled by setTimeout

  const entry = (id: string): Entry => {
    let e = entries.current.get(id);
    if (!e) entries.current.set(id, (e = { counts: emptyCounts(), used: false, failed: false }));
    return e;
  };

  const notifySubscribers = useCallback(() => {
    const telemetry: CameraTelemetry = {
      label: liveLabel(recentRef.current),
      breakdown: liveBreakdown(recentRef.current),
    };
    subscribersRef.current.forEach((cb) => {
      try {
        cb(telemetry);
      } catch {
        // Protect sampling loop from subscriber exceptions
      }
    });
  }, []);

  const resetRecent = useCallback(() => {
    recentRef.current = [];
    notifySubscribers();
  }, [notifySubscribers]);

  const stopStream = useCallback(() => {
    running.current = false;
    samplingRef.current = false;
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    stream.current?.getTracks().forEach((t) => t.stop()); // the camera light goes off
    stream.current = null;
    if (video.current) video.current.srcObject = null;
  }, []);

  const fail = useCallback((text: string) => {
    failed.current = true;
    if (question.current) entry(question.current).failed = true;
    stopStream();
    setState("unavailable");
    setMessage(text);
  }, [stopStream]);

  const sample = useCallback(async () => {
    // Prevent overlapping asynchronous analysis requests or execution after shutdown
    if (!running.current || samplingRef.current) return;
    samplingRef.current = true;

    try {
      let delay = BASE_INTERVAL_MS;
      const v = video.current;
      // Nothing is analysed while paused, hidden or before frames arrive. With no question open
      // (before the first, between two) the label previews, and nothing is counted.
      if (model.current && v && v.readyState >= 2 && !paused.current && !document.hidden) {
        const started = performance.now();
        try {
          const faceapi = model.current;
          const found = await faceapi.detectAllFaces(
            v, new faceapi.TinyFaceDetectorOptions({ inputSize: 224, scoreThreshold: 0.5 })).withFaceExpressions();
          const outcome = classify(found.map((f) => ({ expressions: f.expressions as unknown as Record<string, number> })));
          const id = question.current;
          if (id && running.current) {
            const e = entry(id);
            e.used = true;
            e.counts = addSample(e.counts, outcome);
          }
          if (running.current) {
            recentRef.current = [...recentRef.current.slice(-4), outcome];
            notifySubscribers();
          }
        } catch {
          return fail("Facial-expression analysis stopped working. The interview will continue normally.");
        }
        const pace = nextPace(performance.now() - started, strikes.current);
        strikes.current = pace.strikes;
        if (pace.stop) {
          return fail("This device is too slow for facial-expression analysis, so it was turned off. "
                      + "The interview will continue normally.");
        }
        delay = pace.delay;
      }
      if (running.current) timer.current = setTimeout(() => tick.current(), delay);
    } finally {
      samplingRef.current = false;
    }
  }, [fail, notifySubscribers]);

  useEffect(() => {
    tick.current = sample;
  }, [sample]);

  const enable = useCallback(async () => {
    if (running.current || state === "starting" || state === "loading") return;
    failed.current = false;
    setMessage(null);
    if (!navigator.mediaDevices?.getUserMedia) {
      setState("off");
      setMessage("This browser cannot use a camera here. You can continue without facial-expression analysis.");
      return;
    }
    setState("starting");
    try {
      // Video only, small: nothing is recorded, and a smaller frame is cheaper to analyse.
      stream.current = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 320 }, height: { ideal: 240 }, facingMode: "user" }, audio: false });
    } catch (err) {
      const { text, denied } = cameraErrorText((err as DOMException)?.name ?? "");
      stopStream();
      setState(denied ? "off" : "unavailable");
      setMessage(text);
      return;
    }
    stream.current.getVideoTracks()[0]?.addEventListener("ended", () =>
      fail("The camera was disconnected. Facial-expression analysis is unavailable; the interview continues normally."));
    if (video.current) {
      video.current.srcObject = stream.current;
      video.current.play().catch(() => {});
    }
    setState("loading");
    try {
      model.current = await loadModel();
    } catch {
      return fail("Facial-expression analysis is unavailable (the model could not be loaded). "
                  + "The interview will continue normally.");
    }
    if (!stream.current) return; // disabled while loading
    strikes.current = 0;
    running.current = true;
    resetRecent();
    if (question.current) entry(question.current).used = true;
    setState("active");
    timer.current = setTimeout(() => tick.current(), BASE_INTERVAL_MS);
  }, [fail, resetRecent, state, stopStream]);

  const disable = useCallback(() => {
    failed.current = false; // the candidate's choice: from now on, "not enabled"
    stopStream();
    setState("off");
    setMessage(null);
    resetRecent();
  }, [resetRecent, stopStream]);

  const attach = useCallback((el: HTMLVideoElement | null) => {
    video.current = el;
    if (el && stream.current) {
      el.srcObject = stream.current;
      el.play().catch(() => {});
    }
  }, []);

  const setQuestion = useCallback((id: string | null) => {
    question.current = id;
    if (id && failed.current) entry(id).failed = true;
    // Shown while analysis runs: reported even with no sample ("insufficient visual data").
    if (id && running.current) entry(id).used = true;
    resetRecent();
  }, [resetRecent]);

  const setPaused = useCallback((value: boolean) => {
    paused.current = value;
  }, []);

  const take = useCallback((id: string): Observation | null => {
    const e = entries.current.get(id);
    entries.current.delete(id);
    if (!e) return null;
    const seen = Object.values(e.counts).some((n) => n > 0);
    if (e.failed && !seen) return toObservation(e.counts, "UNAVAILABLE");
    return e.used ? toObservation(e.counts, "ANALYZED") : null;
  }, []);

  const subscribeTelemetry = useCallback((cb: (telemetry: CameraTelemetry) => void) => {
    subscribersRef.current.add(cb);
    cb({
      label: liveLabel(recentRef.current),
      breakdown: liveBreakdown(recentRef.current),
    });
    return () => {
      subscribersRef.current.delete(cb);
    };
  }, []);

  const getTelemetry = useCallback((): CameraTelemetry => {
    return {
      label: liveLabel(recentRef.current),
      breakdown: liveBreakdown(recentRef.current),
    };
  }, []);

  // Leaving the interview (or the page) always releases the camera.
  useEffect(() => stopStream, [stopStream]);

  return useMemo<Camera>(
    () => ({
      state,
      message,
      get label() {
        return liveLabel(recentRef.current);
      },
      get breakdown() {
        return liveBreakdown(recentRef.current);
      },
      subscribeTelemetry,
      getTelemetry,
      attach,
      enable,
      disable,
      setQuestion,
      setPaused,
      take,
    }),
    [
      state,
      message,
      subscribeTelemetry,
      getTelemetry,
      attach,
      enable,
      disable,
      setQuestion,
      setPaused,
      take,
    ]
  );
}

/**
 * Lightweight subscriber hook for components rendering composure telemetry badges.
 * Re-renders only the subscriber when label or breakdown values meaningfully change.
 */
export function useCameraTelemetry(camera: Camera): CameraTelemetry {
  const [telemetry, setTelemetry] = useState<CameraTelemetry>(() =>
    camera.getTelemetry ? camera.getTelemetry() : { label: camera.label, breakdown: camera.breakdown }
  );

  useEffect(() => {
    if (!camera.subscribeTelemetry) return;
    return camera.subscribeTelemetry((next) => {
      setTelemetry((prev) => {
        if (
          prev.label === next.label &&
          prev.breakdown.length === next.breakdown.length &&
          prev.breakdown.every(
            (b, i) => b.look === next.breakdown[i]?.look && b.percent === next.breakdown[i]?.percent
          )
        ) {
          return prev;
        }
        return next;
      });
    });
  }, [camera]);

  return telemetry;
}
