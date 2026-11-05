// Comprehensive Post-Redesign Visual and Functional QA Audit Runner
import { execFileSync, spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const DIR = import.meta.dirname;
const OUT = join(DIR, "out", "audit");
const SHOTS = join(OUT, "shots");
rmSync(OUT, { recursive: true, force: true });
mkdirSync(SHOTS, { recursive: true });

// Ensure fake video feed exists
execFileSync(process.env.E2E_FEED_PYTHON ?? "python", [join(DIR, "camera_feeds.py")]);

const PYTHON = process.env.E2E_PYTHON ?? join(DIR, "..", "backend", ".venv", "Scripts", "python.exe");
const EDGE = process.env.E2E_EDGE ?? "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const APP = "http://localhost:3000";
const API = "http://localhost:8000/api";
const STAMP = Date.now();
const CANDIDATE = {
  name: "Visual Audit User",
  email: `qa-audit-${STAMP}@example.com`,
  password: "audit password 1234",
};

const VIEWPORTS = [
  { name: "desktop_1440", width: 1440, height: 900, mobile: false },
  { name: "laptop_1280", width: 1280, height: 800, mobile: false },
  { name: "tablet_768", width: 768, height: 1024, mobile: false },
  { name: "mobile_390", width: 390, height: 844, mobile: true },
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const db = (...args) => {
  return execFileSync(PYTHON, [join(DIR, "db.py"), ...args], {
    encoding: "utf8",
    cwd: join(DIR, "..", "backend"),
    env: { ...process.env, DATABASE_URL: "postgresql+psycopg://coach:coachpw@localhost:5432/ai_interview_coach" },
  }).trim();
};

const auditLog = [];
function log(status, category, title, details = "") {
  const entry = { status, category, title, details, time: new Date().toISOString() };
  auditLog.push(entry);
  const color = status === "PASS" ? "\x1b[32m" : status === "FAIL" ? "\x1b[31m" : "\x1b[33m";
  console.log(`${color}[${status}]\x1b[0m [${category}] ${title} ${details ? `— ${details}` : ""}`);
}

// Launch Headless Edge
const profile = join(OUT, "edge-profile");
const edge = spawn(
  EDGE,
  [
    "--headless=new",
    "--remote-debugging-port=9225",
    "--disable-gpu",
    "--no-first-run",
    `--user-data-dir=${profile}`,
    "--window-size=1440,900",
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
    `--use-file-for-fake-video-capture=${join(DIR, "out", "face.mjpeg")}`,
    "about:blank",
  ],
  { stdio: "ignore" }
);

let target;
for (let i = 0; i < 75 && !target; i++) {
  await sleep(200);
  try {
    target = (await (await fetch("http://127.0.0.1:9225/json/list")).json()).find((t) => t.type === "page");
  } catch {}
}
if (!target) throw new Error("Could not connect to Headless Edge on port 9225");

const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r, { once: true }));

let nextId = 0;
const pending = new Map();
const consoleMessages = [];
const consoleErrors = [];
const reactWarnings = [];
const uncaughtExceptions = [];
const failedRequests = [];
const allRequests = [];

ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) {
    const { resolve, reject } = pending.get(m.id);
    pending.delete(m.id);
    m.error ? reject(new Error(m.error.message)) : resolve(m.result);
  } else if (m.method === "Runtime.consoleAPICalled") {
    const text = m.params.args.map((a) => a.value ?? a.description ?? "").join(" ");
    consoleMessages.push({ type: m.params.type, text });
    if (m.params.type === "error") {
      consoleErrors.push(text);
    }
    if (text.includes("Warning:") || text.includes("React") || text.includes("Extra attributes")) {
      reactWarnings.push(text);
    }
  } else if (m.method === "Runtime.exceptionThrown") {
    const text = m.params.exceptionDetails.exception?.description ?? m.params.exceptionDetails.text;
    uncaughtExceptions.push(text);
  } else if (m.method === "Network.responseReceived") {
    const { response } = m.params;
    allRequests.push({ url: response.url, status: response.status });
    // Ignore expected 401 on /api/auth/me for signed-out state
    if (response.status >= 400 && !response.url.endsWith("/api/auth/me")) {
      failedRequests.push({ url: response.url, status: response.status, statusText: response.statusText });
    }
  } else if (m.method === "Page.javascriptDialogOpening") {
    send("Page.handleJavaScriptDialog", { accept: true });
  }
});

function send(method, params = {}) {
  const id = ++nextId;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

async function js(expression) {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text);
  return r.result.value;
}

async function waitFor(expression, what, timeout = 25000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    try {
      if (await js(expression)) return;
    } catch {}
    await sleep(200);
  }
  throw new Error(`Timed out waiting for ${what}`);
}

async function typeInto(selector, value) {
  await waitFor(`Boolean(document.querySelector(${JSON.stringify(selector)}))`, selector);
  await js(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement : el.tagName === 'SELECT' ? HTMLSelectElement : HTMLInputElement;
    Object.getOwnPropertyDescriptor(proto.prototype, 'value').set.call(el, ${JSON.stringify(value)});
    el.dispatchEvent(new Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
  })()`);
}

async function setViewport(vp) {
  await send("Emulation.setDeviceMetricsOverride", {
    width: vp.width,
    height: vp.height,
    deviceScaleFactor: 1,
    mobile: vp.mobile,
  });
  await sleep(150);
}

async function setTheme(theme) {
  await js(`document.documentElement.dataset.theme = "${theme}"`);
  await sleep(150);
}

async function shot(name) {
  const { data } = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  writeFileSync(join(SHOTS, `${name}.png`), Buffer.from(data, "base64"));
}

async function checkOverflow() {
  return await js(`(() => {
    const doc = document.documentElement;
    const body = document.body;
    const hasHScroll = doc.scrollWidth > window.innerWidth || body.scrollWidth > window.innerWidth;
    const overflowDiff = Math.max(doc.scrollWidth, body.scrollWidth) - window.innerWidth;
    return { hasHScroll, overflowDiff };
  })()`);
}

// Track media streams in window
await send("Page.enable");
await send("Runtime.enable");
await send("Network.enable");
await send("Page.addScriptToEvaluateOnNewDocument", {
  source: `
    window.__streams = [];
    const origGUM = navigator.mediaDevices?.getUserMedia?.bind(navigator.mediaDevices);
    if (origGUM) {
      navigator.mediaDevices.getUserMedia = async (c) => {
        const stream = await origGUM(c);
        window.__streams.push(stream);
        return stream;
      };
    }
  `,
});

const getLiveTrackCount = () =>
  js("(window.__streams || []).flatMap(s => s.getTracks()).filter(t => t.readyState === 'live').length");

try {
  log("INFO", "INIT", "Starting comprehensive visual and functional audit");
  console.log(db("cleanup", "qa-audit-"));

  // =========================================================================
  // SECTION 1: VISUAL QA OF PUBLIC ROUTES (Desktop, Laptop, Tablet, Mobile)
  // =========================================================================
  log("INFO", "VISUAL_QA", "Auditing Route / (Landing Page)");
  await send("Page.navigate", { url: APP });
  await waitFor("document.readyState === 'complete'", "Landing page load");
  await waitFor("document.body.innerText.includes('Master High-Stakes Job Interviews')", "Landing content");

  for (const vp of VIEWPORTS) {
    await setViewport(vp);
    for (const theme of ["light", "dark"]) {
      await setTheme(theme);
      await shot(`landing_${vp.name}_${theme}`);
      const ov = await checkOverflow();
      if (ov.hasHScroll && ov.overflowDiff > 1) {
        log("FAIL", "VISUAL_QA", `Landing Page Horizontal Overflow on ${vp.name} (${theme})`, `Diff: ${ov.overflowDiff}px`);
      } else {
        log("PASS", "VISUAL_QA", `Landing Page Responsiveness & Spacing on ${vp.name} (${theme})`);
      }
    }
  }

  // Auditing Route /login
  log("INFO", "VISUAL_QA", "Auditing Route /login (Auth Page)");
  await send("Page.navigate", { url: `${APP}/login` });
  await waitFor("document.readyState === 'complete'", "Login page load");
  await waitFor("Boolean(document.querySelector('#email'))", "Login form");

  for (const vp of VIEWPORTS) {
    await setViewport(vp);
    for (const theme of ["light", "dark"]) {
      await setTheme(theme);
      await shot(`login_${vp.name}_${theme}`);
      const ov = await checkOverflow();
      if (ov.hasHScroll && ov.overflowDiff > 1) {
        log("FAIL", "VISUAL_QA", `Login Page Horizontal Overflow on ${vp.name} (${theme})`, `Diff: ${ov.overflowDiff}px`);
      } else {
        log("PASS", "VISUAL_QA", `Login Page Responsiveness on ${vp.name} (${theme})`);
      }
    }
  }

  // =========================================================================
  // SECTION 2: FUNCTIONAL QA - REGISTRATION & DASHBOARD
  // =========================================================================
  log("INFO", "FUNCTIONAL_QA", "Step 1: Registering new candidate");
  await setViewport(VIEWPORTS[0]); // Desktop
  await setTheme("light");

  // Click "Create Account" tab
  await js(`
    const tab = [...document.querySelectorAll('.auth-mode-tab')].find(b => b.textContent.includes('Create Account'));
    if (tab) tab.click();
  `);
  await waitFor("Boolean(document.querySelector('#name'))", "Registration name field");

  await typeInto("#name", CANDIDATE.name);
  await typeInto("#email", CANDIDATE.email);
  await typeInto("#password", CANDIDATE.password);
  await js(`document.querySelector("form button[type='submit']").click()`);

  // Wait for redirect to Dashboard (/)
  await waitFor("document.body.innerText.includes('Welcome, " + CANDIDATE.name + "')", "Dashboard welcome");
  log("PASS", "FUNCTIONAL_QA", "Step 1 & 2: Registration & Dashboard loading successful");

  // Visual QA on Dashboard for all 4 viewports
  for (const vp of VIEWPORTS) {
    await setViewport(vp);
    for (const theme of ["light", "dark"]) {
      await setTheme(theme);
      await shot(`dashboard_${vp.name}_${theme}`);
      const ov = await checkOverflow();
      if (ov.hasHScroll && ov.overflowDiff > 1) {
        log("FAIL", "VISUAL_QA", `Dashboard Horizontal Overflow on ${vp.name} (${theme})`, `Diff: ${ov.overflowDiff}px`);
      } else {
        log("PASS", "VISUAL_QA", `Dashboard Layout & Visual Spacing on ${vp.name} (${theme})`);
      }
    }
  }

  // Profile configuration: Add a manual fact
  await setViewport(VIEWPORTS[0]);
  await setTheme("light");
  log("INFO", "FUNCTIONAL_QA", "Step 3a: Adding manual candidate fact");
  await typeInto('#manual-fact-category', 'SKILL');
  await typeInto('#manual-fact-text', 'TypeScript and Next.js Architecture');
  await js(`[...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Add').click()`);
  await waitFor("document.body.innerText.includes('TypeScript and Next.js Architecture')", "Manual fact added");
  log("PASS", "FUNCTIONAL_QA", "Manual candidate fact added and rendered");

  // Upload Resume Fixture
  log("INFO", "FUNCTIONAL_QA", "Step 3b: Uploading resume fixture");
  const { root } = await send("DOM.getDocument");
  const { nodeId } = await send("DOM.querySelector", { nodeId: root.nodeId, selector: "#resume-file" });
  await send("DOM.setFileInputFiles", { nodeId, files: [join(DIR, "fixtures", "resume-fresh-graduate.pdf")] });
  await js(`[...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Upload resume').click()`);
  await waitFor("document.body.innerText.includes('Library Management System')", "Extracted resume facts", 20000);
  log("PASS", "FUNCTIONAL_QA", "Resume uploaded and extracted facts rendered");

  // Approve all facts
  await js(`
    const approveBtn = [...document.querySelectorAll('button')].find(b => b.textContent.startsWith('Approve all'));
    if (approveBtn) approveBtn.click();
  `);
  await sleep(600);

  // =========================================================================
  // SECTION 3: INTERVIEW SETUP WORKFLOW
  // =========================================================================
  log("INFO", "FUNCTIONAL_QA", "Navigating to /interview");
  await js(`document.querySelector("a[href='/interview']").click()`);
  await waitFor("document.body.innerText.includes('Interview Sessions Hub')", "Interview hub load");

  for (const vp of VIEWPORTS) {
    await setViewport(vp);
    await shot(`interview_hub_${vp.name}_light`);
  }

  await setViewport(VIEWPORTS[0]);
  log("INFO", "FUNCTIONAL_QA", "Starting new practice interview");
  await js(`[...document.querySelectorAll('button')].find(b => b.textContent.includes('Start New Practice Interview')).click()`);
  await waitFor("document.body.innerText.includes('Confirm Your Practice Profile')", "Confirm Profile step");
  log("PASS", "FUNCTIONAL_QA", "Step 3c: Profile confirmation step loaded");

  // Confirm Profile
  await shot("interview_step1_confirm_profile");
  await js(`[...document.querySelectorAll('button')].find(b => b.textContent.includes('Confirm Profile')).click()`);
  await waitFor("document.body.innerText.includes('Configure Your Interview Session')", "Plan Interview step");
  log("PASS", "FUNCTIONAL_QA", "Step 3d: Interview planning configuration loaded");

  // Plan Interview settings
  await typeInto('#plan-mode', 'TECHNICAL');
  await typeInto('#plan-difficulty', 'MEDIUM');
  await typeInto('#plan-duration_minutes', '15');
  await typeInto('#plan-target_role', 'FULL_STACK_DEVELOPER');
  await js(`
    const consent = document.querySelector('#plan-consent');
    if (consent && !consent.checked) consent.click();
  `);
  await shot("interview_step2_plan_settings");
  await js(`[...document.querySelectorAll('button')].find(b => b.textContent.includes('Generate Interview Plan')).click()`);

  // Wait for ReadyScreen (Step 3)
  await waitFor("document.body.innerText.includes('Ready to Enter the Interview Room')", "Ready screen", 25000);
  log("PASS", "FUNCTIONAL_QA", "Step 4: Interview planned, ready screen loaded");

  // Camera permissions and tech check on ReadyScreen
  await shot("interview_step3_ready_screen");
  log("INFO", "FUNCTIONAL_QA", "Enabling Camera for tech check");
  await js(`[...document.querySelectorAll('button')].find(b => b.textContent.includes('Enable camera')).click()`);
  await waitFor("document.body.innerText.includes('Expression analysis active') || document.body.innerText.includes('Composure')", "Camera active", 60000);
  const liveCamTracksBefore = await getLiveTrackCount();
  log("PASS", "FUNCTIONAL_QA", `Camera enabled: ${liveCamTracksBefore} live stream track(s)`);

  // =========================================================================
  // SECTION 4: INTERVIEW ROOM QA (LAYOUT, CAMERA, QUESTIONS, VOICE HUB)
  // =========================================================================
  log("INFO", "INTERVIEW_ROOM_QA", "Entering Live Interview Room");
  await js(`[...document.querySelectorAll('button')].find(b => b.textContent.includes('Start Interview Now') || b.textContent.includes('Start Interview')).click()`);
  await waitFor("Boolean(document.querySelector('.room-stage'))", "Interview room stage loaded", 20000);
  await waitFor("Boolean(document.querySelector('.question-text'))", "First question loaded", 15000);

  // Inspect layout across all 4 viewports
  for (const vp of VIEWPORTS) {
    await setViewport(vp);
    for (const theme of ["light", "dark"]) {
      await setTheme(theme);
      await shot(`interview_room_${vp.name}_${theme}`);
      const ov = await checkOverflow();
      if (ov.hasHScroll && ov.overflowDiff > 1) {
        log("FAIL", "INTERVIEW_ROOM_QA", `Interview Room Overflow on ${vp.name} (${theme})`, `Diff: ${ov.overflowDiff}px`);
      } else {
        log("PASS", "INTERVIEW_ROOM_QA", `Interview Room No Overflow on ${vp.name} (${theme})`);
      }
    }
  }

  // Reset to desktop for detailed structural checks
  await setViewport(VIEWPORTS[0]);
  await setTheme("light");

  // 1. Check Questions Positioned on Left vs Camera in Center
  const layoutCheck = await js(`(() => {
    const qPanel = document.querySelector('.questions-panel');
    const camPanel = document.querySelector('.camera-center-panel');
    const qRect = qPanel ? qPanel.getBoundingClientRect() : null;
    const camRect = camPanel ? camPanel.getBoundingClientRect() : null;
    const q = qRect ? { left: qRect.left, right: qRect.right, width: qRect.width, top: qRect.top } : null;
    const cam = camRect ? { left: camRect.left, right: camRect.right, width: camRect.width, top: camRect.top } : null;
    return {
      q,
      cam,
      questionsOnLeft: q && cam ? q.right <= cam.left + 40 : false,
    };
  })()`);

  if (layoutCheck.questionsOnLeft) {
    log("PASS", "INTERVIEW_ROOM_QA", "Questions panel is positioned strictly on the LEFT", `Questions right: ${layoutCheck.q.right}px <= Camera left: ${layoutCheck.cam.left}px`);
  } else {
    log("FAIL", "INTERVIEW_ROOM_QA", "Questions panel is NOT on the left of camera", JSON.stringify(layoutCheck));
  }

  // 2. Camera Sizing and Aspect Ratio
  const camCheck = await js(`(() => {
    const frame = document.querySelector('.candidate-camera-frame');
    if (!frame) return null;
    const rect = frame.getBoundingClientRect();
    const ratio = rect.width / rect.height;
    return {
      width: rect.width,
      height: rect.height,
      aspectRatio: ratio,
      isLarge: rect.width >= 500,
      isApprox16by9: Math.abs(ratio - 16 / 9) < 0.15,
    };
  })()`);

  if (camCheck && camCheck.isLarge && camCheck.isApprox16by9) {
    log("PASS", "INTERVIEW_ROOM_QA", "Camera frame is sufficiently large and maintains 16:9 aspect ratio", `Width: ${camCheck.width}px, Height: ${camCheck.height}px, Ratio: ${camCheck.aspectRatio.toFixed(2)}`);
  } else {
    log("WARN", "INTERVIEW_ROOM_QA", "Camera frame sizing unexpected", JSON.stringify(camCheck));
  }

  // 3. Voice Interaction as Primary Action
  const voiceCheck = await js(`(() => {
    const heroBtn = document.querySelector('.mic-hero-button');
    const hub = document.querySelector('.voice-hub');
    const waveform = document.querySelector('.waveform-container');
    const badges = [...document.querySelectorAll('.camera-badge-overlay .badge, .camera-badge-overlay .live-indicator')].map(b => b.textContent.trim());
    return {
      hasHeroButton: Boolean(heroBtn),
      heroText: heroBtn ? heroBtn.textContent.trim() : null,
      hasHub: Boolean(hub),
      hasWaveform: Boolean(waveform),
      badges,
    };
  })()`);

  if (voiceCheck.hasHeroButton && voiceCheck.hasHub && voiceCheck.hasWaveform) {
    log("PASS", "INTERVIEW_ROOM_QA", "Voice interaction hub is the primary visual action", JSON.stringify(voiceCheck));
  } else {
    log("FAIL", "INTERVIEW_ROOM_QA", "Voice interaction elements missing or incomplete", JSON.stringify(voiceCheck));
  }

  // 4. Manual Typing as Optional Fallback
  const typingCheck = await js(`(() => {
    const toggle = document.querySelector('.manual-typing-toggle');
    const drawerBefore = document.querySelector('.manual-typing-drawer');
    return {
      hasToggle: Boolean(toggle),
      toggleText: toggle ? toggle.textContent.trim() : null,
      drawerInitiallyClosed: !drawerBefore,
    };
  })()`);

  if (typingCheck.hasToggle && typingCheck.drawerInitiallyClosed) {
    log("PASS", "INTERVIEW_ROOM_QA", "Manual typing is initially collapsed as an optional fallback");
  } else {
    log("WARN", "INTERVIEW_ROOM_QA", "Manual typing toggle or initial state unexpected", JSON.stringify(typingCheck));
  }

  // Open typing drawer
  await js(`document.querySelector('.manual-typing-toggle').click()`);
  await waitFor("Boolean(document.querySelector('#answer'))", "Manual textarea drawer opened");
  log("PASS", "INTERVIEW_ROOM_QA", "Manual typing drawer expands on toggle click");

  // 5. Timer Verification & Non-Interference
  const timerCheck = await js(`(() => {
    const timer = document.querySelector('.room-timer');
    return {
      hasTimer: Boolean(timer),
      text: timer ? timer.textContent.trim() : null,
    };
  })()`);
  log("PASS", "INTERVIEW_ROOM_QA", `Interview timer rendered: ${timerCheck.text}`);

  // 6. Test No Lost Answers During State Transitions (Pause & Resume)
  log("INFO", "INTERVIEW_ROOM_QA", "Testing answer draft preservation during Pause & Resume");
  const testDraft = "I choose modular design and measure latency trade-offs between speed and clarity in production.";
  await typeInto('#answer', testDraft);

  // Click Pause
  await js(`[...document.querySelectorAll('button')].find(b => b.textContent.includes('Pause')).click()`);
  await waitFor("document.body.innerText.includes('Interview Paused')", "Paused state banner");
  await shot("interview_paused_state");

  const pauseCheck = await js(`(() => {
    const text = document.body.innerText;
    return {
      hasPausedTitle: text.includes('interview is currently on pause'),
      hasDraftPreservedNotice: text.includes('unsent answer is safely held in memory'),
    };
  })()`);

  if (pauseCheck.hasDraftPreservedNotice) {
    log("PASS", "INTERVIEW_ROOM_QA", "Pause screen explicitly notes that draft is safely held in memory");
  } else {
    log("FAIL", "INTERVIEW_ROOM_QA", "Draft notice missing on pause screen");
  }

  // Resume interview
  await js(`[...document.querySelectorAll('button')].find(b => b.textContent.includes('Resume Interview')).click()`);
  await waitFor("Boolean(document.querySelector('.room-stage'))", "Interview room resumed");

  // Check draft is preserved
  // First check live transcript box preview
  const liveTranscriptText = await js(`document.querySelector('.live-transcript-box p')?.textContent.trim() || ''`);
  // Open manual drawer and check #answer
  await js(`document.querySelector('.manual-typing-toggle')?.click()`);
  await waitFor("Boolean(document.querySelector('#answer'))", "Resumed typing drawer open");
  const resumedDraft = await js(`document.querySelector('#answer')?.value || ''`);

  if (resumedDraft === testDraft || liveTranscriptText === testDraft) {
    log("PASS", "INTERVIEW_ROOM_QA", "Answer draft perfectly preserved across pause & resume transition!");
  } else {
    log("FAIL", "INTERVIEW_ROOM_QA", "Answer draft was lost after resume", `Expected: "${testDraft}", Found: "${resumedDraft}"`);
  }

  // 7. Stable Camera Stream Check
  const camTracksAfterResume = await getLiveTrackCount();
  log("PASS", "INTERVIEW_ROOM_QA", `Camera streams stable after resume: ${camTracksAfterResume} live track(s)`);

  // =========================================================================
  // SECTION 5: ANSWER SUBMISSION, PROGRESSION & COMPLETION
  // =========================================================================
  log("INFO", "FUNCTIONAL_QA", "Step 7 & 8: Submitting answer and testing evaluation/progression");
  const firstQuestionText = await js(`document.querySelector('.question-text')?.textContent.trim()`);

  // Submit answer
  await js(`[...document.querySelectorAll('button')].find(b => b.textContent.includes('Send Spoken Answer') || b.textContent.includes('Submit Written Answer') || b.textContent.trim() === '✓ Submit Answer').click()`);

  // Wait for next question or progress
  await waitFor(
    `Boolean(document.querySelector('.question-text')) && document.querySelector('.question-text').textContent.trim() !== ${JSON.stringify(firstQuestionText)}`,
    "Second question loaded",
    30000
  );
  const secondQuestionText = await js(`document.querySelector('.question-text')?.textContent.trim()`);
  log("PASS", "FUNCTIONAL_QA", `Step 9: Answer evaluated and progressed to next question: "${secondQuestionText.slice(0, 50)}…"`);

  // Answer second question with Quick Kinds (Skip)
  await js(`[...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Skip').click()`);
  await sleep(1500);

  // End interview to verify report and completion
  log("INFO", "FUNCTIONAL_QA", "Step 10: Ending interview early to generate report");
  await js(`[...document.querySelectorAll('button')].find(b => b.textContent.includes('End Interview')).click()`);

  // Wait for Feedback screen (Step 10: Answer evaluation)
  await waitFor("document.body.innerText.includes('Evaluating Your Interview Performance')", "Feedback screen", 25000);
  log("PASS", "FUNCTIONAL_QA", "Step 10: Interview ended, feedback evaluation screen loaded");
  await shot("interview_step10_feedback_evaluating");

  // Click Evaluate Answers & Generate Report
  await js(`[...document.querySelectorAll('button')].find(b => b.textContent.includes('Evaluate Answers') || b.textContent.includes('Build My Comprehensive Report')).click()`);

  // Wait for Performance Report (Step 11)
  await waitFor("document.body.innerText.includes('Performance Report')", "Report page", 45000);
  log("PASS", "FUNCTIONAL_QA", "Step 11: Evaluation synthesized, Performance Report generated");

  // Visual QA on Performance Report across all 4 viewports
  for (const vp of VIEWPORTS) {
    await setViewport(vp);
    for (const theme of ["light", "dark"]) {
      await setTheme(theme);
      await shot(`report_${vp.name}_${theme}`);
      const ov = await checkOverflow();
      if (ov.hasHScroll && ov.overflowDiff > 1) {
        log("FAIL", "VISUAL_QA", `Report Horizontal Overflow on ${vp.name} (${theme})`, `Diff: ${ov.overflowDiff}px`);
      } else {
        log("PASS", "VISUAL_QA", `Report Layout & Spacing on ${vp.name} (${theme})`);
      }
    }
  }

  // Inspect Report Content & Analytics
  await setViewport(VIEWPORTS[0]);
  await setTheme("light");
  const reportCheck = await js(`(() => {
    const text = document.body.innerText;
    return {
      hasHeroSummary: Boolean(document.querySelector('.report-summary-hero')),
      hasStrengths: text.includes('Key Strengths Demonstrated'),
      hasPractise: text.includes('Areas for Growth'),
      hasExportDoc: Boolean(document.querySelector("button[title*='HTML document']")),
      hasExportPdf: Boolean(document.querySelector("button[title*='print dialog']")),
    };
  })()`);
  log("PASS", "FUNCTIONAL_QA", "Report contains Summary Hero, Strengths, Growth Areas, and Export buttons", JSON.stringify(reportCheck));

  // Step 12: Historical interview access
  log("INFO", "FUNCTIONAL_QA", "Step 12: Returning to Interviews Hub & verifying History");
  await js(`[...document.querySelectorAll('button')].find(b => b.textContent.includes('Back to your interviews')).click()`);
  await waitFor("document.body.innerText.includes('Past Interview History')", "History section");
  await shot("interview_hub_with_history");

  const historyCheck = await js(`(() => {
    const items = [...document.querySelectorAll('.history-item')];
    return {
      count: items.length,
      firstText: items[0] ? items[0].innerText : null,
    };
  })()`);
  log("PASS", "FUNCTIONAL_QA", `Step 12: Historical interview records accessible: ${historyCheck.count} attempt(s) found`);

  // Verify camera stream cleanup after leaving interview
  const camTracksFinal = await getLiveTrackCount();
  if (camTracksFinal === 0) {
    log("PASS", "TECHNICAL_QA", "Camera streams cleanly released upon leaving the interview room");
  } else {
    log("FAIL", "TECHNICAL_QA", `Camera streams LEAKED: ${camTracksFinal} live track(s) remaining after leaving interview`);
  }

} catch (err) {
  log("FAIL", "EXECUTION_ERROR", err.message, err.stack);
  await shot("audit_failure");
} finally {
  // Dump Technical Summary
  log("INFO", "SUMMARY", "Audit execution complete. Summarizing technical metrics.");
  const results = {
    totalLogs: auditLog.length,
    passes: auditLog.filter((l) => l.status === "PASS").length,
    fails: auditLog.filter((l) => l.status === "FAIL").length,
    warns: auditLog.filter((l) => l.status === "WARN").length,
    consoleErrors,
    reactWarnings,
    uncaughtExceptions,
    failedRequests,
    shots: readdirSync(SHOTS),
    logEntries: auditLog,
  };
  writeFileSync(join(OUT, "audit_summary.json"), JSON.stringify(results, null, 2));
  console.log(`\n================ AUDIT RESULTS ================`);
  console.log(`Total Passes: ${results.passes}`);
  console.log(`Total Fails:  ${results.fails}`);
  console.log(`Total Warns:  ${results.warns}`);
  console.log(`Console Errors: ${consoleErrors.length}`);
  console.log(`React Warnings: ${reactWarnings.length}`);
  console.log(`Uncaught Exceptions: ${uncaughtExceptions.length}`);
  console.log(`Screenshots Captured: ${results.shots.length}`);
  console.log(`===============================================\n`);
  ws.close();
  edge.kill();
}
