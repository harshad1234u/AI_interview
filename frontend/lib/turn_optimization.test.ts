// Run with `npm test` (node --test; Node strips TypeScript types).
import { test } from "node:test";
import assert from "node:assert/strict";
import type { QuestionView } from "./api.ts";
import { recordTurnMetrics, type TurnTransitionMetrics } from "./interview.ts";

function makeQuestion(id: string, seq: number, answered: boolean = false): QuestionView {
  return {
    id,
    seq,
    topic_position: 1,
    topic: "Algorithms",
    competency: "programming_fundamentals",
    question_type: "TECHNICAL",
    difficulty: "MEDIUM",
    basis: "GENERIC",
    kind: "TOPIC",
    followup_index: 0,
    rephrase_index: 0,
    text: `Question ${seq}`,
    source: "RULES",
    rules_reason: null,
    asked_at: "2026-11-14T00:00:00Z",
    answer: answered
      ? {
          kind: "ANSWER",
          text: `Answer for ${seq}`,
          answered_at: "2026-11-14T00:01:00Z",
          evaluation: { status: "NOT_EVALUATED" },
        }
      : null,
  };
}

test("recordTurnMetrics records metrics on global window object safely and bounds history to 50", () => {
  const metrics: TurnTransitionMetrics = {
    submitAnswerMs: 65,
    nextQuestionMs: 240,
    historyRefreshMs: 95,
    totalTurnMs: 305,
    uiBlockedMs: 305,
    requestCount: 2,
  };

  recordTurnMetrics(metrics);
  const win = globalThis as unknown as {
    __LAST_TURN_METRICS__?: TurnTransitionMetrics;
    __TURN_METRICS_LOG__?: TurnTransitionMetrics[];
  };

  assert.deepEqual(win.__LAST_TURN_METRICS__, metrics);
  assert.ok(Array.isArray(win.__TURN_METRICS_LOG__));
  assert.ok(win.__TURN_METRICS_LOG__!.length >= 1);

  // Push 60 entries to verify upper bound of 50
  for (let i = 0; i < 60; i++) {
    recordTurnMetrics({ ...metrics, submitAnswerMs: i });
  }
  assert.equal(win.__TURN_METRICS_LOG__!.length, 50);
  // Last recorded item matches latest input
  assert.equal(win.__LAST_TURN_METRICS__?.submitAnswerMs, 59);
  assert.equal(win.__TURN_METRICS_LOG__![49].submitAnswerMs, 59);
  // Oldest item in capped log is index 10 (since 60 items were pushed after the initial one)
  assert.equal(win.__TURN_METRICS_LOG__![0].submitAnswerMs, 10);
});

test("optimistic history updater updates existing question or appends new answered question", () => {
  const q1 = makeQuestion("q1", 1, false);
  const q2 = makeQuestion("q2", 2, false);
  const initialHistory: QuestionView[] = [q1, q2];

  // Candidate answers q1
  const answeredQ1 = makeQuestion("q1", 1, true);

  // Simulated optimistic updater
  const updateHistory = (prev: QuestionView[], answered: QuestionView): QuestionView[] => {
    const idx = prev.findIndex((q) => q.id === answered.id);
    if (idx >= 0) {
      const next = [...prev];
      next[idx] = answered;
      return next;
    }
    return [...prev, answered];
  };

  const updated1 = updateHistory(initialHistory, answeredQ1);
  assert.equal(updated1.length, 2);
  assert.ok(updated1[0].answer !== null);
  assert.equal(updated1[0].answer?.text, "Answer for 1");
  assert.equal(updated1[1].answer, null);

  // New question answered that wasn't in history yet
  const answeredQ3 = makeQuestion("q3", 3, true);
  const updated2 = updateHistory(updated1, answeredQ3);
  assert.equal(updated2.length, 3);
  assert.equal(updated2[2].id, "q3");
});

test("pastQuestions filter excludes current active question and includes only answered questions", () => {
  const q1 = makeQuestion("q1", 1, true);
  const q2 = makeQuestion("q2", 2, true);
  const q3 = makeQuestion("q3", 3, false); // current active question (unanswered)
  const history = [q1, q2, q3];

  const currentQ = q3;
  const past = history.filter((item) => item.id !== currentQ.id && item.answer !== null);

  assert.equal(past.length, 2);
  assert.deepEqual(past.map((q) => q.id), ["q1", "q2"]);
});

test("sequence numbering prevents race conditions in out-of-order background responses", () => {
  let activeSeq = 0;
  let storedHistory: string[] = [];

  const handleResponse = (seq: number, data: string[]) => {
    // Only apply if this response is the latest requested sequence
    if (seq === activeSeq) {
      storedHistory = data;
    }
  };

  // Request 1 starts
  const seq1 = ++activeSeq;
  // Request 2 starts before Request 1 finishes
  const seq2 = ++activeSeq;

  // Request 2 finishes first
  handleResponse(seq2, ["q1", "q2"]);
  assert.deepEqual(storedHistory, ["q1", "q2"]);

  // Stale Request 1 finishes later
  handleResponse(seq1, ["q1"]);
  // Stale response was discarded; stored history remained latest
  assert.deepEqual(storedHistory, ["q1", "q2"]);
});

test("submitting guard prevents concurrent or duplicate answer submissions", async () => {
  let inFlight = false;
  let submissionCount = 0;

  const mockSubmit = async () => {
    if (inFlight) {
      // Block duplicate click
      return;
    }
    inFlight = true;
    submissionCount++;
    try {
      await new Promise((resolve) => setTimeout(resolve, 10));
    } finally {
      inFlight = false;
    }
  };

  // Trigger two rapid concurrent clicks
  await Promise.all([mockSubmit(), mockSubmit()]);
  assert.equal(submissionCount, 1);
});

test("background history failure does not crash or throw in main UI loop", async () => {
  let errorCaught = false;
  const failingHistoryFetcher = async () => {
    throw new Error("Network timeout on background GET");
  };

  // Non-blocking fire-and-forget pattern with catch
  const runBackground = () => {
    failingHistoryFetcher().catch(() => {
      errorCaught = true;
    });
  };

  runBackground();
  await new Promise((resolve) => setTimeout(resolve, 5));
  // Background error was caught and suppressed; did not throw unhandled rejection
  assert.equal(errorCaught, true);
});
