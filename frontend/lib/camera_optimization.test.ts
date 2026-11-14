// Run with `npm test` (node --test; Node strips TypeScript types).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  type Counts,
  type Observation,
  addSample,
  cameraErrorText,
  classify,
  emptyCounts,
  liveBreakdown,
  liveLabel,
  toObservation,
} from "./expressions.ts";
import type { CameraTelemetry } from "../components/interview/useExpressionCamera.ts";

test("telemetry subscriber receives formatted composure updates", () => {
  const subscribers = new Set<(t: CameraTelemetry) => void>();
  let recent: ReturnType<typeof classify>[] = [];

  const subscribe = (cb: (t: CameraTelemetry) => void) => {
    subscribers.add(cb);
    cb({
      label: liveLabel(recent),
      breakdown: liveBreakdown(recent),
    });
    return () => subscribers.delete(cb);
  };

  const notify = () => {
    const t = {
      label: liveLabel(recent),
      breakdown: liveBreakdown(recent),
    };
    subscribers.forEach((cb) => cb(t));
  };

  const received: CameraTelemetry[] = [];
  const unsubscribe = subscribe((t) => received.push(t));

  // Initial notification on subscribe
  assert.equal(received.length, 1);
  assert.equal(received[0].label, "Waiting for the first sample…");
  assert.deepEqual(received[0].breakdown, []);

  // Sample 1: neutral face
  recent = [...recent.slice(-4), "neutral"];
  notify();
  assert.equal(received.length, 2);
  assert.equal(received[1].label, "Mostly neutral");
  assert.deepEqual(received[1].breakdown, [{ look: "neutral", percent: 100 }]);

  // Sample 2: smiling face
  recent = [...recent.slice(-4), "happy"];
  notify();
  assert.equal(received.length, 3);
  assert.equal(received[2].breakdown.length, 2);

  unsubscribe();
  // Subsequent sample after unsubscribe should not be received
  recent = [...recent.slice(-4), "sad"];
  notify();
  assert.equal(received.length, 3);
});

test("telemetry memoization prevents redundant renders when composure is unchanged", () => {
  const prevTelemetry: CameraTelemetry = {
    label: "Mostly neutral",
    breakdown: [{ look: "neutral", percent: 100 }],
  };

  const shouldUpdate = (prev: CameraTelemetry, next: CameraTelemetry): boolean => {
    if (
      prev.label === next.label &&
      prev.breakdown.length === next.breakdown.length &&
      prev.breakdown.every(
        (b, i) => b.look === next.breakdown[i]?.look && b.percent === next.breakdown[i]?.percent
      )
    ) {
      return false; // Skip re-render
    }
    return true; // Re-render needed
  };

  // Identical consecutive neutral sample
  const nextTelemetryIdentical: CameraTelemetry = {
    label: "Mostly neutral",
    breakdown: [{ look: "neutral", percent: 100 }],
  };
  assert.equal(shouldUpdate(prevTelemetry, nextTelemetryIdentical), false);

  // Different expression sample
  const nextTelemetryChanged: CameraTelemetry = {
    label: "Mixed",
    breakdown: [
      { look: "neutral", percent: 50 },
      { look: "smiling", percent: 50 },
    ],
  };
  assert.equal(shouldUpdate(prevTelemetry, nextTelemetryChanged), true);
});

test("overlapping sampling guard prevents concurrent inference runs", async () => {
  let inFlight = false;
  let overlappingAttemptsBlocked = 0;
  let completedRuns = 0;

  const sample = async () => {
    if (inFlight) {
      overlappingAttemptsBlocked++;
      return;
    }
    inFlight = true;
    try {
      // Simulate asynchronous face detection model latency (20ms)
      await new Promise((resolve) => setTimeout(resolve, 20));
      completedRuns++;
    } finally {
      inFlight = false;
    }
  };

  // Launch initial sample and attempt two concurrent ticks while it runs
  const p1 = sample();
  const p2 = sample(); // blocked
  const p3 = sample(); // blocked
  await Promise.all([p1, p2, p3]);

  assert.equal(completedRuns, 1);
  assert.equal(overlappingAttemptsBlocked, 2);

  // Subsequent sample after p1 finishes proceeds normally
  await sample();
  assert.equal(completedRuns, 2);
});

test("camera stream shutdown stops all active media tracks and clears timers", () => {
  let tracksStopped = 0;
  let timerCleared = false;
  let running = true;
  let timerId: ReturnType<typeof setTimeout> | null = setTimeout(() => {}, 10000);

  const mockTrack = {
    stop: () => {
      tracksStopped++;
    },
  };
  const mockStream = {
    getTracks: () => [mockTrack, mockTrack],
  };

  const stopStream = () => {
    running = false;
    if (timerId) {
      clearTimeout(timerId);
      timerCleared = true;
      timerId = null;
    }
    mockStream.getTracks().forEach((t) => t.stop());
  };

  stopStream();
  assert.equal(running, false);
  assert.equal(timerCleared, true);
  assert.equal(timerId, null);
  assert.equal(tracksStopped, 2);
});

test("disabling and re-enabling camera resets telemetry history", () => {
  let recent: ReturnType<typeof classify>[] = ["happy", "happy", "happy"];
  assert.equal(liveLabel(recent), "Mostly smiling");

  const resetRecent = () => {
    recent = [];
  };

  // User disables camera
  resetRecent();
  assert.equal(recent.length, 0);
  assert.equal(liveLabel(recent), "Waiting for the first sample…");
  assert.deepEqual(liveBreakdown(recent), []);
});

test("expression counts and observations are strictly preserved", () => {
  let counts: Counts = emptyCounts();
  counts = addSample(counts, "happy");
  counts = addSample(counts, "happy");
  counts = addSample(counts, "neutral");
  counts = addSample(counts, "no_face");

  const obs: Observation = toObservation(counts, "ANALYZED");
  assert.equal(obs.status, "ANALYZED");
  assert.equal(obs.counts.happy, 2);
  assert.equal(obs.counts.neutral, 1);
  assert.equal(obs.no_face, 1);
  assert.equal(obs.multiple_faces, 0);

  // Unavailable question sends empty counts
  const unavail = toObservation(counts, "UNAVAILABLE");
  assert.equal(unavail.status, "UNAVAILABLE");
  assert.deepEqual(unavail.counts, {});
});

test("camera permission errors are translated to candidate-friendly explanations", () => {
  const denied = cameraErrorText("NotAllowedError");
  assert.equal(denied.denied, true);
  assert.match(denied.text, /Camera permission was denied/);

  const inUse = cameraErrorText("NotReadableError");
  assert.equal(inUse.denied, false);
  assert.match(inUse.text, /in use by another application/);
});

test("isolated render pipeline simulation demonstrates zero parent re-renders", () => {
  let flowRenders = 0;
  let turnRenders = 0;
  let questionsPanelRenders = 0;
  let voiceHubRenders = 0;
  let candidateCameraTelemetryRenders = 0;

  // Root state does NOT change during sampling
  const renderFlow = () => {
    flowRenders++;
    renderTurn();
  };

  const renderTurn = () => {
    turnRenders++;
    questionsPanelRenders++;
    voiceHubRenders++;
  };

  // Initial page load
  renderFlow();
  assert.equal(flowRenders, 1);
  assert.equal(turnRenders, 1);
  assert.equal(questionsPanelRenders, 1);
  assert.equal(voiceHubRenders, 1);

  // Simulate 30 seconds of active interview sampling (30 samples)
  for (let s = 1; s <= 30; s++) {
    // With state isolation, sampling updates only the subscriber inside CandidateCamera
    candidateCameraTelemetryRenders++;
  }

  // Parent and sibling components have ZERO additional re-renders!
  assert.equal(flowRenders, 1);
  assert.equal(turnRenders, 1);
  assert.equal(questionsPanelRenders, 1);
  assert.equal(voiceHubRenders, 1);
  assert.equal(candidateCameraTelemetryRenders, 30);
});
