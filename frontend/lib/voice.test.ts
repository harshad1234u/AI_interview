import assert from "node:assert/strict";
import { test } from "node:test";
import {
  appendTranscript,
  cleanForSpeech,
  countWords,
  describeSpeech,
  isVoiceLang,
  pickVoice,
  recognitionErrorText,
  speechMetrics,
  splitForSpeech,
} from "./voice.ts";

test("appendTranscript joins with one space and ignores empty speech", () => {
  assert.equal(appendTranscript("", "  hello   world "), "hello world");
  assert.equal(appendTranscript("I used React.  ", "and Redux"), "I used React. and Redux");
  assert.equal(appendTranscript("kept", "   "), "kept");
});

test("cleanForSpeech drops markup and links", () => {
  assert.equal(cleanForSpeech("What is `useState`?\n\nSee https://x.dev/a now"), "What is useState? See a link now");
});

test("splitForSpeech keeps whole sentences within the limit", () => {
  assert.deepEqual(splitForSpeech("One. Two. Three.", 200), ["One. Two. Three."]);
  assert.deepEqual(splitForSpeech("Aaaa bbbb. Cccc dddd.", 12), ["Aaaa bbbb.", "Cccc dddd."]);
  assert.deepEqual(splitForSpeech("   "), []);
});

test("splitForSpeech cuts a very long sentence at spaces and loses no words", () => {
  const long = Array.from({ length: 80 }, (_, i) => `word${i}`).join(" ");
  const chunks = splitForSpeech(long, 60);
  assert.ok(chunks.length > 1);
  assert.ok(chunks.every((c) => c.length <= 60));
  assert.equal(chunks.join(" "), long);
});

test("pickVoice prefers the language, then an on-device voice, never a non-English one", () => {
  const voices = [
    { name: "Hindi", lang: "hi-IN", localService: true },
    { name: "Online US", lang: "en-US", localService: false },
    { name: "Local US", lang: "en-US", localService: true },
    { name: "Online India Natural", lang: "en-IN", localService: false },
  ];
  assert.equal(pickVoice(voices, "en-IN")?.name, "Online India Natural");
  assert.equal(pickVoice(voices, "en-US")?.name, "Local US");
  assert.equal(pickVoice([voices[0]], "en-IN"), null);
  assert.equal(pickVoice([], "en-IN"), null);
  assert.equal(pickVoice([{ name: "x", lang: "en_GB", localService: true }], "en-GB")?.name, "x");
});

test("language codes are a closed set", () => {
  assert.ok(isVoiceLang("en-IN"));
  assert.ok(!isVoiceLang("xx-XX"));
  assert.ok(!isVoiceLang(undefined));
});

test("speechMetrics: words, pace and filler sounds are measured", () => {
  const text = "um I built a small web app uh with React and you know a FastAPI backend today";
  const m = speechMetrics(text, 20_000);
  assert.equal(m.words, countWords(text));
  assert.equal(m.seconds, 20);
  assert.equal(m.fillers, 3);
  assert.equal(m.wordsPerMinute, Math.round((m.words / 20_000) * 60000));
  assert.equal(m.tooShort, false);
});

test("speechMetrics: a very short answer is 'too short', never 'poor'", () => {
  const m = speechMetrics("yes I did", 3000);
  assert.equal(m.tooShort, true);
  assert.equal(m.wordsPerMinute, null);
  assert.match(describeSpeech(m), /too short for meaningful speech measurements/);
});

test("describeSpeech never judges: no confidence, nervous or good/bad wording", () => {
  const text = "this is a reasonably long spoken answer about my project and what I learned from it";
  const out = describeSpeech(speechMetrics(text, 15_000));
  assert.match(out, /words a minute/);
  assert.doesNotMatch(out, /nervous|confident|confidence|anxious|good|bad|poor|weak/i);
  assert.equal(describeSpeech(speechMetrics("typed only", 0)), "");
});

test("speechMetrics: handles floating point milliseconds accurately", () => {
  const text = "I built an innovative cloud platform using modern React and FastAPI microservices";
  const m = speechMetrics(text, 23145.789);
  assert.equal(m.words, countWords(text));
  assert.equal(m.seconds, 23);
  assert.equal(m.tooShort, false);
  assert.equal(typeof m.wordsPerMinute, "number");
  assert.equal(m.wordsPerMinute, Math.round((m.words / 23145.789) * 60000));
});

test("recognition errors: a missing mic is final; no-speech is not a judgement", () => {
  assert.equal(recognitionErrorText("not-allowed").fatal, true);
  assert.equal(recognitionErrorText("audio-capture").fatal, true);
  const quiet = recognitionErrorText("no-speech");
  assert.equal(quiet.fatal, false);
  assert.match(quiet.text, /not a judgement/);
  assert.equal(recognitionErrorText("something-new").fatal, true);
});
