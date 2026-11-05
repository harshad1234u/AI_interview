// Pure helpers for the interview screens. No fetch, no React, no storage: tested with
// `node --test`. Only type imports, which Node strips, so this file runs as-is.
import type {
  AnswerKind,
  Competency,
  Criterion,
  Difficulty,
  EvaluationStatus,
  ExperienceLevel,
  FinishReason,
  InterviewMode,
  InterviewSession,
  InterviewStatus,
  QuestionKind,
  QuestionType,
  QuestionView,
  RulesReason,
  TargetRole,
} from "./api";

/** Which screen shows a session. Chosen from the server's status only. */
export type Screen = "confirm" | "plan" | "ready" | "turn" | "paused" | "feedback" | "report";

export const SCREEN: Record<InterviewStatus, Screen> = {
  // The server applies CREATED and PROFILE_PROCESSING before it stores a session; listed for
  // completeness, they show the same screen as PROFILE_REVIEW.
  CREATED: "confirm",
  PROFILE_PROCESSING: "confirm",
  PROFILE_REVIEW: "confirm",
  INTERVIEW_PLANNING: "plan",
  READY: "ready",
  // Every running state is the turn screen: it asks the server for the current question, which
  // also moves a decided turn on (NEXT_DECISION) or returns the open question again.
  ASKING: "turn",
  WAITING_FOR_ANSWER: "turn",
  ANSWER_PROCESSING: "turn",
  ANSWER_EVALUATION: "turn",
  NEXT_DECISION: "turn",
  PAUSED: "paused",
  REPORT_GENERATION: "feedback",
  COMPLETED: "report",
};

export const screenFor = (status: InterviewStatus): Screen => SCREEN[status];

/** A session the candidate can still continue or discard (the server allows one at a time). */
export const isUnfinished = (status: InterviewStatus) => status !== "COMPLETED";

/** Answers the report still waits for: ANSWER kind, not evaluated yet, in interview order.
 * SKIP, DONT_KNOW and CLARIFY never need an evaluation (the server treats them as not applicable). */
export function pendingEvaluations(questions: QuestionView[]): QuestionView[] {
  return questions
    .filter((q) => q.answer?.kind === "ANSWER" && q.answer.evaluation.status === "NOT_EVALUATED")
    .sort((a, b) => a.seq - b.seq);
}

/** "m:ss" for display only; the server decides when time is up. Never negative. */
export function formatRemaining(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}

export const ANSWER_MAX_LENGTH = 10_000; // the backend limit, in Unicode code points
export const ANSWER_WARN_AT = 9_000;

/** Length as the backend counts it: Unicode code points after trimming. */
export const answerLength = (text: string) => [...text.trim()].length;

/** The text to show for a failed request. ApiError messages already carry the server's detail
 * (or a network message for status 0), so they are shown as they are. */
export function errorText(err: unknown): string {
  if (err && typeof err === "object" && "status" in err && err instanceof Error) return err.message;
  return COPY.unexpected;
}

/** status 0: the request never got a response (offline, server down). */
export const isNetworkError = (err: unknown) =>
  Boolean(err && typeof err === "object" && "status" in err && (err as { status: unknown }).status === 0);

/** 409: the server refused because the interview moved on (time limit, ended, paused…). The
 * page then re-reads the session instead of letting the request be repeated. A 422 is not a
 * conflict: the candidate can edit and send again. */
export const isConflict = (err: unknown) =>
  Boolean(err && typeof err === "object" && "status" in err && (err as { status: unknown }).status === 409);

/** The report decides whether a retry exists; the UI only reads it. */
export const canRetry = (retry: { available: boolean }) => retry.available === true;

/** Question numbers ("#3") by id, from the report's own question list. */
export function seqById(questions: { question_id: string; seq: number }[]): Map<string, number> {
  return new Map(questions.map((q) => [q.question_id, q.seq]));
}

// ---------- labels (display text for server values) ----------

export const ROLE_LABEL: Record<TargetRole, string> = {
  SOFTWARE_ENGINEER: "Software Engineer",
  SOFTWARE_DEVELOPER_INTERN: "Software Developer Intern",
  FRONTEND_DEVELOPER: "Frontend Developer",
  BACKEND_DEVELOPER: "Backend Developer",
  FULL_STACK_DEVELOPER: "Full-Stack Developer",
  PYTHON_DEVELOPER: "Python Developer",
  JAVA_DEVELOPER: "Java Developer",
  ANDROID_DEVELOPER: "Android Developer",
  AI_ML_ENGINEER: "AI/ML Engineer",
  GENERATIVE_AI_ENGINEER: "Generative AI Engineer",
  DATA_ANALYST: "Data Analyst",
  DATA_SCIENTIST: "Data Scientist",
  DATA_ENGINEER: "Data Engineer",
  CYBERSECURITY_ANALYST: "Cybersecurity Analyst",
  SOC_ANALYST: "SOC Analyst",
  PENETRATION_TESTER: "Penetration Tester",
  SECURITY_ENGINEER: "Security Engineer",
  CLOUD_ENGINEER: "Cloud Engineer",
  DEVOPS_ENGINEER: "DevOps Engineer",
  SITE_RELIABILITY_ENGINEER: "Site Reliability Engineer",
  QA_TEST_ENGINEER: "QA / Test Engineer",
  AUTOMATION_TEST_ENGINEER: "Automation Test Engineer",
  DATABASE_ADMINISTRATOR: "Database Administrator",
  NETWORK_ENGINEER: "Network Engineer",
  MOBILE_APP_DEVELOPER: "Mobile App Developer",
  HR_BEHAVIORAL: "HR / Behavioral Interview",
  BUSINESS_ANALYST: "Business Analyst",
  PRODUCT_MANAGER: "Product Manager",
  TECHNICAL_SUPPORT_ENGINEER: "Technical Support Engineer",
};

export const LEVEL_LABEL: Record<ExperienceLevel, string> = {
  STUDENT: "Student",
  FRESH_GRADUATE: "Fresh graduate",
  ENTRY_LEVEL: "Entry level",
  INTERNSHIP_APPLICANT: "Internship applicant",
};

export const MODE_LABEL: Record<InterviewMode, string> = {
  TECHNICAL: "Technical",
  BEHAVIORAL: "Behavioral",
  PROJECT_DEEP_DIVE: "Project deep dive",
  MIXED: "Mixed placement",
};

export const DIFFICULTY_LABEL: Record<Difficulty, string> = {
  EASY: "Easy",
  MEDIUM: "Medium",
  HARD: "Hard",
};

// The planner's own labels (backend LABELS).
export const COMPETENCY_LABEL: Record<Competency, string> = {
  programming_fundamentals: "Programming Fundamentals",
  data_structures: "Data Structures",
  database: "Database",
  web_development: "Web Development",
  project_understanding: "Project Understanding",
  behavioral: "Behavioral",
  hr: "HR",
  api_design: "API Design",
  system_design: "System Design",
  testing_qa: "Testing & QA",
  python_language: "Python",
  java_language: "Java",
  mobile_development: "Mobile Development",
  ml_fundamentals: "ML Fundamentals",
  data_analysis: "Data Analysis",
  data_engineering: "Data Engineering",
  security_fundamentals: "Security Fundamentals",
  network_security: "Network Security",
  cloud_infrastructure: "Cloud Infrastructure",
  devops_practices: "DevOps Practices",
  networking: "Networking",
  business_analysis: "Business Analysis",
  product_management: "Product Management",
  technical_support: "Technical Support",
  professional_skills: "Professional Skills",
};

export const QUESTION_TYPE_LABEL: Record<QuestionType, string> = {
  TECHNICAL: "Technical",
  PROJECT: "Project",
  BEHAVIORAL: "Behavioral",
  HR: "HR",
};

export const CRITERION_LABEL: Record<Criterion, string> = {
  technical_accuracy: "Technical Accuracy",
  relevance: "Relevance",
  completeness: "Completeness",
  reasoning: "Reasoning",
  communication: "Communication",
  structure: "Structure",
  specificity: "Specificity",
  evidence: "Evidence",
  reflection: "Reflection",
  outcome: "Outcome",
};

export const QUESTION_KIND_LABEL: Record<QuestionKind, string> = {
  TOPIC: "Question",
  FOLLOW_UP: "Follow-up question",
  REPHRASE: "The question, put another way",
};

/** The buttons for each answer kind the backend accepts. */
export const ANSWER_KIND_LABEL: Record<AnswerKind, string> = {
  ANSWER: "Submit answer",
  CLARIFY: "Ask to rephrase",
  DONT_KNOW: "I don't know",
  SKIP: "Skip",
};

export const EVALUATION_STATUS_LABEL: Record<EvaluationStatus, string> = {
  NOT_EVALUATED: "Not evaluated yet",
  NOT_APPLICABLE: "Nothing to evaluate",
  OBSERVATIONS_ONLY: "Observations only",
  EVALUATED: "Evaluated with AI",
};

export const FINISH_LABEL: Record<FinishReason, string> = {
  PLAN_COMPLETED: "All planned topics were covered.",
  TIME_LIMIT: "The time limit was reached.",
  CANDIDATE_ENDED: "You ended the interview.",
  NO_ELIGIBLE_TOPIC: "No question could be asked.",
};

/** Why fixed rules were used instead of AI, in the candidate's words. Never hidden. */
export const RULES_REASON_TEXT: Record<RulesReason, string> = {
  NOT_REQUESTED: "AI assistance was off, so fixed rules were used.",
  NOT_CONFIGURED: "AI assistance is not available on this server, so fixed rules were used.",
  PROVIDERS_FAILED: "The AI service could not be used just now, so fixed rules were used.",
  OUTPUT_REJECTED: "The AI suggestion did not pass our checks, so fixed rules were used.",
  CONSENT_WITHDRAWN: "AI assistance was turned off while it was working, so its output was discarded.",
  QUOTA_REACHED: "The daily AI usage limit was reached, so fixed rules were used.",
};

/** A short, honest label for where an attempt stands (history list). */
export function attemptState(status: InterviewStatus): string {
  if (status === "COMPLETED") return "Completed";
  if (status === "REPORT_GENERATION") return "Finished, feedback not built yet";
  if (status === "PAUSED") return "Paused";
  if (status === "PROFILE_REVIEW" || status === "INTERVIEW_PLANNING" || status === "CREATED" ||
      status === "PROFILE_PROCESSING") return "Being set up";
  if (status === "READY") return "Planned, not started";
  return "In progress";
}

/** Attempt numbers (#1 is the oldest), from the server's creation times; ties by id. Display
 * only: attempts are never combined, and the server keeps each one separately. */
export function attemptNumbers(sessions: Pick<InterviewSession, "id" | "created_at">[]): Map<string, number> {
  const ordered = [...sessions].sort((a, b) =>
    Date.parse(a.created_at) - Date.parse(b.created_at) || (a.id < b.id ? -1 : 1));
  return new Map(ordered.map((s, i) => [s.id, i + 1]));
}

// ---------- fixed copy (checked by the wording test) ----------

export const COPY = {
  unexpected: "Something unexpected happened. Please try again.",
  consentLabel: "Use AI assistance for this interview",
  consentDisclosure:
    "Off by default. If you turn it on, your approved profile details, the questions and your " +
    "answers for this interview may be sent to the AI provider this server is configured with, " +
    "to plan topics, word follow-up questions and evaluate your answers. You can turn it off at " +
    "any time; text already sent cannot be recalled. With it off, fixed rules are used instead.",
  consentOn: "AI assistance is on for this interview.",
  consentOff: "AI assistance is off: fixed rules plan the interview and word the questions.",
  consentFeedback:
    "AI evaluation needs AI assistance to be on before you request feedback; it cannot be " +
    "turned on after the report. Without it you get observations such as word counts.",
  confirmIntro:
    "Your interview uses only the resume facts you approved. Pending facts must be reviewed first.",
  pendingFacts: "Review your pending facts on your profile page, then confirm again.",
  readyIntro: "Your interview is planned. The clock starts when you press Start.",
  timerNote: "The server keeps the official time.",
  browserClose:
    "Closing this page does not pause the interview: use Pause if you need a break.",
  networkKept:
    "Your answer is kept on this page but was not saved yet. Check your connection and try again. " +
    "It is lost if you reload the page.",
  lengthWarning: "You are close to the 10,000 character limit.",
  paused: "Your interview is paused. The clock is stopped.",
  endConfirm: "End the interview now? Questions not yet asked will not be asked.",
  discardConfirm:
    "Discard this interview? Its questions, answers and feedback are permanently deleted.",
  feedbackIntro:
    "Your interview has finished. Each answer is evaluated one at a time, then your report is " +
    "built from the stored results.",
  evaluating: "Evaluating answer {i} of {n}…",
  building: "Building your report…",
  reportIntro:
    "Practice feedback for each answer, with the quotes it is based on. Scores belong to one " +
    "criterion of one answer and are never combined.",
  retryWorking: "Creating your retry interview…",
  resumePrompt: "You have an unfinished interview.",
  startNew: "Start a new interview to practise.",
  aiWorded: "Worded with AI assistance",
  cameraTitle: "Facial-expression observation (optional, experimental)",
  cameraExplain:
    "If you turn this on, your camera is used only to look for broad facial-expression patterns " +
    "(such as neutral or smiling) while you answer. The analysis runs on this device: images and " +
    "video never leave your browser and are never stored. Only how many times each expression " +
    "was detected for each question is saved with this interview, to show in your report. It is " +
    "not a measurement of emotion, confidence or honesty and never affects your feedback. It is " +
    "off by default; you can turn it off at any time, and the interview works the same without it.",
} as const;

export const fill = (text: string, values: Record<string, string | number>) =>
  text.replace(/\{(\w+)\}/g, (_, key: string) => String(values[key] ?? `{${key}}`));
