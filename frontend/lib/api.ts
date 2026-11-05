import { LOGIN_PATH, shouldRedirectToLogin } from "@/lib/auth";
import { getApiEndpoint } from "@/lib/endpoint";
import type { Observation } from "@/lib/expressions";

export { getApiEndpoint };

export type Health = {
  status: "ok" | "degraded";
  database: "ok" | "unavailable";
};

export type Candidate = {
  id: string;
  email: string;
  name: string;
  created_at: string;
};

export type CandidateInput = { name: string; email: string };
export type RegisterInput = CandidateInput & { password: string };
export type LoginInput = { email: string; password: string };

export type ResumeStatus = "PROCESSED" | "FAILED" | "UNSUPPORTED";
export type FactCategory = "SKILL" | "PROJECT" | "EXPERIENCE" | "EDUCATION" | "CERTIFICATION";
export type FactStatus = "PENDING_REVIEW" | "APPROVED" | "REJECTED";

export type Resume = {
  id: string;
  candidate_id: string;
  status: ResumeStatus;
  message: string | null;
  page_count: number | null;
  sections: { heading: string; category: FactCategory | null; page: number }[];
  created_at: string;
};

/** A MANUAL fact (typed in by the candidate) has no resume provenance: those fields are null. */
export type Fact = {
  id: string;
  resume_id: string | null;
  category: FactCategory;
  fact: string;
  extracted_fact: string;
  source: "RESUME" | "MANUAL";
  source_section: string | null;
  source_text: string | null;
  status: FactStatus;
  extracted_by: "RULES" | "AI" | null;
  created_at: string;
};

export type AIExtraction = {
  request_id: string;
  requested_provider: string;
  provider: string;
  model: string;
  fallback_used: boolean;
  attempts: number;
  latency_ms: number;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  facts_added: number;
  facts_already_listed: number;
  facts_rejected: number;
};

export type FactReview = { fact?: string; status?: "APPROVED" | "REJECTED" };

/** status 0 = the request never got a response (network down, backend off, misconfigured). */
export class ApiError extends Error {
  status: number;
  fieldErrors: Partial<Record<string, string>>;

  constructor(
    status: number,
    message: string,
    fieldErrors: Partial<Record<string, string>> = {},
  ) {
    super(message);
    this.status = status;
    this.fieldErrors = fieldErrors;
  }
}

type ValidationIssue = { loc?: (string | number)[]; msg?: string };

function toApiError(status: number, body: unknown): ApiError {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (status === 422 && Array.isArray(detail)) {
    const fieldErrors: Record<string, string> = {};
    for (const issue of detail as ValidationIssue[]) {
      const field = issue.loc?.[1];
      if (typeof field === "string" && issue.msg) fieldErrors[field] ??= issue.msg;
    }
    return new ApiError(status, "Please fix the highlighted fields.", fieldErrors);
  }
  // 503 details are written for users (DB down, AI unavailable); other 5xx stay generic.
  if (typeof detail === "string" && (status < 500 || status === 503)) {
    return new ApiError(status, detail);
  }
  if (status === 503) return new ApiError(status, "Service temporarily unavailable. Please try again.");
  return new ApiError(status, "Something went wrong on our side. Please try again.");
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const base = process.env.NEXT_PUBLIC_API_URL?.trim();
  if (!base) throw new ApiError(0, "App is misconfigured: NEXT_PUBLIC_API_URL is not set.");
  let res: Response;
  const endpoint = getApiEndpoint(path);
  try {
    res = await fetch(endpoint, {
      ...init,
      // FormData bodies must let the browser set the multipart Content-Type (with its boundary).
      headers:
        typeof init.body === "string"
          ? { "Content-Type": "application/json", ...init.headers }
          : init.headers,
      cache: "no-store",
      // Sends the HttpOnly session cookie (same-site API). JavaScript never sees the token.
      credentials: "include",
    });
  } catch (err) {
    if (init.signal?.aborted) throw err;
    throw new ApiError(0, "Can't reach the server. Check your connection and try again.");
  }
  if (res.status === 204) return undefined as T;
  const body: unknown = await res.json().catch(() => null);
  // Signed out or the session expired: start again at the login page (unsent drafts are lost).
  if (shouldRedirectToLogin(res.status, path, window.location.pathname)) {
    window.location.assign(LOGIN_PATH);
  }
  if (!res.ok) throw toApiError(res.status, body);
  return body as T;
}

export async function getHealth(signal?: AbortSignal): Promise<Health> {
  const base = process.env.NEXT_PUBLIC_API_URL?.trim();
  if (!base) throw new Error("NEXT_PUBLIC_API_URL is not configured");
  const endpoint = getApiEndpoint("/api/health");
  const res = await fetch(endpoint, { signal, cache: "no-store" });
  // 503 still carries a well-formed Health body (database unavailable).
  if (!res.ok && res.status !== 503) throw new Error(`Unexpected status ${res.status}`);
  return (await res.json()) as Health;
}

// ---------- authentication (backend Phase 12) ----------

export type RegisterResponse = {
  email: string;
  verification_required: boolean;
  message: string;
};

/** Creates the Supabase Auth profile and sends verification email. */
export const register = (input: RegisterInput) =>
  request<RegisterResponse>("/api/auth/register", { method: "POST", body: JSON.stringify(input) });

/** Confirms email verification via POST to complete account activation without prefetch consumption. */
export const confirmEmailVerification = (payload: { token_hash: string; type?: string; csrf_token?: string }) =>
  request<{ status: string; redirect_url: string }>("/api/auth/verify", {
    method: "POST",
    body: JSON.stringify(payload),
  });

export const login = (input: LoginInput) =>
  request<Candidate>("/api/auth/login", { method: "POST", body: JSON.stringify(input) });

export const logout = () => request<void>("/api/auth/logout", { method: "POST" });

export const forgotPassword = (email: string) =>
  request<{ message: string }>("/api/auth/forgot-password", {
    method: "POST",
    body: JSON.stringify({ email }),
  });

export const resetPassword = (accessToken: string, newPassword: string) =>
  request<{ message: string }>("/api/auth/reset-password", {
    method: "POST",
    body: JSON.stringify({ access_token: accessToken, new_password: newPassword }),
  });

/** Who is signed in: the only source of the candidate's identity. 401 when nobody is. */
export const getMe = (signal?: AbortSignal) => request<Candidate>("/api/auth/me", { signal });

export const updateCandidate = (id: string, changes: Partial<CandidateInput>) =>
  request<Candidate>(`/api/candidates/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  });

export const deleteCandidate = (id: string) =>
  request<void>(`/api/candidates/${encodeURIComponent(id)}`, { method: "DELETE" });

/** For the signed-in candidate: the server takes the identity from the session, not the form. */
export function uploadResume(file: File) {
  const body = new FormData();
  body.append("file", file);
  return request<Resume>("/api/resumes", { method: "POST", body });
}

export const listResumes = (candidateId: string, signal?: AbortSignal) =>
  request<Resume[]>(`/api/candidates/${encodeURIComponent(candidateId)}/resumes`, { signal });

export const getResumeFacts = (resumeId: string, signal?: AbortSignal) =>
  request<Fact[]>(`/api/resumes/${encodeURIComponent(resumeId)}/facts`, { signal });

export const reviewFact = (resumeId: string, factId: string, review: FactReview) =>
  request<Fact>(
    `/api/resumes/${encodeURIComponent(resumeId)}/facts/${encodeURIComponent(factId)}`,
    { method: "PATCH", body: JSON.stringify(review) },
  );

// ---------- manual facts (backend Phase 13): approved as soon as they are added ----------

const manualFactsPath = (candidateId: string) =>
  `/api/candidates/${encodeURIComponent(candidateId)}/manual-facts`;

export const listManualFacts = (candidateId: string, signal?: AbortSignal) =>
  request<Fact[]>(manualFactsPath(candidateId), { signal });

export const createManualFact = (candidateId: string, category: FactCategory, fact: string) =>
  request<Fact>(manualFactsPath(candidateId), {
    method: "POST",
    body: JSON.stringify({ category, fact }),
  });

/** Only the text can change: never the category, source or status. */
export const editManualFact = (factId: string, fact: string) =>
  request<Fact>(`/api/manual-facts/${encodeURIComponent(factId)}`, {
    method: "PATCH",
    body: JSON.stringify({ fact }),
  });

export const deleteManualFact = (factId: string) =>
  request<void>(`/api/manual-facts/${encodeURIComponent(factId)}`, { method: "DELETE" });

/** Approve (or reject) every fact of this resume that still needs review. */
export const reviewPendingFacts = (resumeId: string, status: "APPROVED" | "REJECTED") =>
  request<Fact[]>(`/api/resumes/${encodeURIComponent(resumeId)}/facts/review-pending`, {
    method: "POST",
    body: JSON.stringify({ status }),
  });

export const runAiExtraction = (resumeId: string) =>
  request<AIExtraction>(`/api/resumes/${encodeURIComponent(resumeId)}/ai-extraction`, {
    method: "POST",
  });

export const deleteResume = (resumeId: string) =>
  request<void>(`/api/resumes/${encodeURIComponent(resumeId)}`, { method: "DELETE" });

// ---------- interview sessions (backend Phases 5-10) ----------
// Field names and value sets mirror the backend Pydantic schemas. Every decision about the
// interview (state, questions, timing, evaluation, report) is the server's.

export type InterviewStatus =
  | "CREATED"
  | "PROFILE_PROCESSING"
  | "PROFILE_REVIEW"
  | "INTERVIEW_PLANNING"
  | "READY"
  | "ASKING"
  | "WAITING_FOR_ANSWER"
  | "PAUSED"
  | "ANSWER_PROCESSING"
  | "ANSWER_EVALUATION"
  | "NEXT_DECISION"
  | "REPORT_GENERATION"
  | "COMPLETED";

export type ClientAction =
  | "CONFIRM_PROFILE"
  | "START_INTERVIEW"
  | "PAUSE"
  | "RESUME"
  | "END_INTERVIEW";

export type FinishReason = "PLAN_COMPLETED" | "TIME_LIMIT" | "CANDIDATE_ENDED" | "NO_ELIGIBLE_TOPIC";
export type RulesReason =
  | "NOT_REQUESTED"
  | "NOT_CONFIGURED"
  | "PROVIDERS_FAILED"
  | "OUTPUT_REJECTED"
  | "CONSENT_WITHDRAWN"
  | "QUOTA_REACHED";

/** One interview attempt as listed in the candidate's history: a summary, never its content. */
export type InterviewSession = {
  id: string;
  candidate_id: string;
  status: InterviewStatus;
  created_at: string;
  updated_at: string;
  finish_reason: FinishReason | null;
  /** The completed interview this one retries; null for a first attempt or a deleted source. */
  retry_of_id: string | null;
  settings: PlanSettings | null; // null until planned
  plan_source: Source | null;
};

export type TargetRole =
  // Software Development
  | "SOFTWARE_ENGINEER"
  | "SOFTWARE_DEVELOPER_INTERN"
  | "FRONTEND_DEVELOPER"
  | "BACKEND_DEVELOPER"
  | "FULL_STACK_DEVELOPER"
  | "PYTHON_DEVELOPER"
  | "JAVA_DEVELOPER"
  | "ANDROID_DEVELOPER"
  // AI and Data
  | "AI_ML_ENGINEER"
  | "GENERATIVE_AI_ENGINEER"
  | "DATA_ANALYST"
  | "DATA_SCIENTIST"
  | "DATA_ENGINEER"
  // Cybersecurity
  | "CYBERSECURITY_ANALYST"
  | "SOC_ANALYST"
  | "PENETRATION_TESTER"
  | "SECURITY_ENGINEER"
  // Cloud and DevOps
  | "CLOUD_ENGINEER"
  | "DEVOPS_ENGINEER"
  | "SITE_RELIABILITY_ENGINEER"
  // Other Technical Roles
  | "QA_TEST_ENGINEER"
  | "AUTOMATION_TEST_ENGINEER"
  | "DATABASE_ADMINISTRATOR"
  | "NETWORK_ENGINEER"
  | "MOBILE_APP_DEVELOPER"
  // General and Professional
  | "HR_BEHAVIORAL"
  | "BUSINESS_ANALYST"
  | "PRODUCT_MANAGER"
  | "TECHNICAL_SUPPORT_ENGINEER";
export type ExperienceLevel = "STUDENT" | "FRESH_GRADUATE" | "ENTRY_LEVEL" | "INTERNSHIP_APPLICANT";
export type InterviewMode = "TECHNICAL" | "BEHAVIORAL" | "PROJECT_DEEP_DIVE" | "MIXED";
export type Difficulty = "EASY" | "MEDIUM" | "HARD";
export type Duration = 15 | 30 | 45;
export type Competency =
  | "programming_fundamentals"
  | "data_structures"
  | "database"
  | "web_development"
  | "project_understanding"
  | "behavioral"
  | "hr"
  | "api_design"
  | "system_design"
  | "testing_qa"
  | "python_language"
  | "java_language"
  | "mobile_development"
  | "ml_fundamentals"
  | "data_analysis"
  | "data_engineering"
  | "security_fundamentals"
  | "network_security"
  | "cloud_infrastructure"
  | "devops_practices"
  | "networking"
  | "business_analysis"
  | "product_management"
  | "technical_support"
  | "professional_skills";
export type QuestionType = "TECHNICAL" | "PROJECT" | "BEHAVIORAL" | "HR";
export type Source = "AI" | "RULES";

export type PlanSettings = {
  target_role: TargetRole;
  experience_level: ExperienceLevel;
  mode: InterviewMode;
  difficulty: Difficulty;
  duration_minutes: Duration;
};

export type PlannedTopic = {
  position: number;
  competency: Competency;
  question_type: QuestionType;
  difficulty: Difficulty;
  basis: "GENERIC" | "CANDIDATE_FACT";
  topic: string;
  question_templates: string[];
  expected_concepts: string[];
  facts: { fact_id: string; category: FactCategory; fact: string }[];
};

export type InterviewPlan = {
  version: 1;
  source: Source;
  rules_reason: RulesReason | null;
  settings: PlanSettings;
  competencies: Competency[];
  omitted_groups: QuestionType[];
  topics: PlannedTopic[];
};

/** GET/POST .../plan and POST .../retry (the new session). */
export type InterviewPlanView = { session_id: string; status: InterviewStatus; plan: InterviewPlan };

export type ConsentState = { session_id: string; granted: boolean; changed_at: string | null };
export type ConsentHistory = {
  session_id: string;
  granted: boolean;
  history: { granted: boolean; changed_at: string }[];
};

export type AnswerKind = "ANSWER" | "CLARIFY" | "DONT_KNOW" | "SKIP";
export type QuestionKind = "TOPIC" | "FOLLOW_UP" | "REPHRASE";
export type EvaluationStatus = "NOT_EVALUATED" | "NOT_APPLICABLE" | "OBSERVATIONS_ONLY" | "EVALUATED";
export type Criterion =
  | "technical_accuracy"
  | "relevance"
  | "completeness"
  | "reasoning"
  | "communication"
  | "structure"
  | "specificity"
  | "evidence"
  | "reflection"
  | "outcome";

export type EvaluationView = {
  rubric: "TECHNICAL_PROJECT" | "BEHAVIORAL_HR";
  rubric_version: number;
  source: Source;
  rules_reason: RulesReason | null;
  criteria: { criterion: Criterion; score: number | null; evidence: string[] }[];
  missing_concepts: string[];
  claims_to_verify: { quote: string; status: "NEEDS_VERIFICATION" }[];
  feedback: string | null;
  observations: { word_count: number; planned_concepts: string[]; concepts_mentioned: string[] };
  created_at: string;
  updated_at: string;
};

export type QuestionView = {
  id: string;
  seq: number;
  topic_position: number;
  topic: string;
  competency: Competency;
  question_type: QuestionType;
  difficulty: Difficulty;
  basis: "GENERIC" | "CANDIDATE_FACT";
  kind: QuestionKind;
  followup_index: number;
  rephrase_index: number;
  text: string;
  source: Source;
  rules_reason: RulesReason | null;
  asked_at: string;
  answer: {
    kind: AnswerKind;
    text: string | null;
    answered_at: string;
    evaluation: { status: EvaluationStatus; detail?: EvaluationView };
  } | null;
};

export type Progress = {
  topics_total: number;
  time_remaining_seconds: number;
  deadline_reached: boolean;
};

export type TurnResult = {
  session_id: string;
  status: InterviewStatus;
  finish_reason: FinishReason | null;
  ai_consent: boolean;
  question: QuestionView | null;
  progress: Progress;
};

export type AnswerResult = { session_id: string; status: InterviewStatus; question: QuestionView };

export type QuestionHistory = {
  session_id: string;
  status: InterviewStatus;
  finish_reason: FinishReason | null;
  progress: Progress | null;
  questions: QuestionView[];
};

export type EvaluationResult = {
  session_id: string;
  question_id: string;
  status: EvaluationStatus;
  evaluation: EvaluationView | null;
  notice: string | null;
};

type ItemRef = { question_id: string; seq: number; topic_position: number };
type CriterionItem = ItemRef & {
  criterion: Criterion;
  score: number;
  score_meaning: string;
  evidence: string[];
};
export type Unavailable = { available: false; note: string };
export type AvailableSpeechMetrics = {
  available: true;
  note: string;
  total_spoken_seconds: number;
  average_wpm: number | null;
  spoken_answers_count: number;
};
export type SpeechMetrics = Unavailable | AvailableSpeechMetrics;

export type BatchEvaluationResult = {
  session_id: string;
  total_evaluated: number;
  evaluations: EvaluationResult[];
  notices: string[];
};

export type InterviewReport = {
  summary: {
    settings: PlanSettings;
    plan_source: Source;
    finish_reason: FinishReason | null;
    finish_reason_text: string;
    active_seconds: number;
    questions_asked: number;
    answers: { answer: number; skip: number; dont_know: number; clarify: number; unanswered: number };
    evaluations: {
      evaluated: number;
      observations_only: number;
      not_applicable: number;
      not_evaluated: number;
    };
  };
  questions: (ItemRef & {
    kind: QuestionKind;
    text: string;
    answer_kind: AnswerKind | null;
    answer_note: string;
    evaluation_status: EvaluationStatus | null;
  })[];
  coverage: {
    topics: {
      topic_position: number;
      topic: string;
      competency: Competency;
      question_type: QuestionType;
      questions_asked: number;
      note: string | null;
    }[];
    competencies: { competency: Competency; planned_topics: number; asked_topics: number }[];
  };
  shown_clearly: CriterionItem[];
  to_practise: {
    criteria: (CriterionItem & { practice: string })[];
    missing_concepts: (ItemRef & { concept: string; practice: string })[];
  };
  answer_evidence: (ItemRef & {
    status: EvaluationStatus;
    evaluation: EvaluationView;
    notice: string | null;
  })[];
  communication_observations: {
    answers: { question_id: string; seq: number; word_count: number; concepts_mentioned: string[] }[];
    speech_metrics: SpeechMetrics;
  };
  topics_to_practise: {
    topic_position: number;
    topic: string;
    competency: Competency;
    criteria: Criterion[];
    missing_concepts: string[];
  }[];
  recommended_practice: { practice: string; question_ids: string[] }[];
  retry: { available: boolean; note: string };
  score_scale: Record<string, string>;
  limitations: string;
  /** Optional, experimental camera observations: never part of any score. */
  expression_signals: {
    available: boolean;
    note: string;
    questions: { question_id: string; seq: number; summary: string }[];
  };
};

export type ReportResult = { session_id: string; status: InterviewStatus; report: InterviewReport };

const sessionPath = (id: string) => `/api/interview-sessions/${encodeURIComponent(id)}`;
const post = (body: unknown = {}): RequestInit => ({ method: "POST", body: JSON.stringify(body) });

export const listInterviewSessions = (candidateId: string, signal?: AbortSignal) =>
  request<InterviewSession[]>(
    `/api/candidates/${encodeURIComponent(candidateId)}/interview-sessions`,
    { signal },
  );

/** For the signed-in candidate (the server refuses a candidate_id in the body). */
export const createInterviewSession = (options?: { abandon_active?: boolean }) =>
  request<InterviewSession>("/api/interview-sessions", post(options?.abandon_active ? { abandon_active: true } : {}));

export const abandonInterviewSession = (id: string) =>
  request<InterviewSession>(`${sessionPath(id)}/abandon`, post());

export const getInterviewSession = (id: string, signal?: AbortSignal) =>
  request<InterviewSession>(sessionPath(id), { signal });

export const actOnSession = (id: string, action: ClientAction) =>
  request<InterviewSession>(`${sessionPath(id)}/actions`, post({ action }));

export const deleteInterviewSession = (id: string) =>
  request<void>(sessionPath(id), { method: "DELETE" });

export const planInterview = (id: string, settings: PlanSettings) =>
  request<InterviewPlanView>(`${sessionPath(id)}/plan`, post(settings));

export const getInterviewPlan = (id: string, signal?: AbortSignal) =>
  request<InterviewPlanView>(`${sessionPath(id)}/plan`, { signal });

export const getConsent = (id: string, signal?: AbortSignal) =>
  request<ConsentHistory>(`${sessionPath(id)}/ai-consent`, { signal });

export const setConsent = (id: string, granted: boolean) =>
  request<ConsentState>(`${sessionPath(id)}/ai-consent`, {
    method: "PUT",
    body: JSON.stringify({ granted }),
  });

export const nextQuestion = (id: string) =>
  request<TurnResult>(`${sessionPath(id)}/questions/next`, post());

// Only ANSWER carries text; the other kinds send none (the backend's contract).
export const submitAnswer = (
  id: string,
  questionId: string,
  kind: AnswerKind,
  text?: string,
  speech_duration_ms?: number,
  speech_wpm?: number,
) =>
  request<AnswerResult>(
    `${sessionPath(id)}/answers`,
    post({
      question_id: questionId,
      kind,
      ...(kind === "ANSWER" ? { text } : {}),
      ...(speech_duration_ms !== undefined && speech_duration_ms > 0 ? { speech_duration_ms } : {}),
      ...(speech_wpm !== undefined && speech_wpm > 0 ? { speech_wpm } : {}),
    }),
  );

export const getQuestions = (id: string, signal?: AbortSignal) =>
  request<QuestionHistory>(`${sessionPath(id)}/questions`, { signal });

export const evaluateAnswer = (id: string, questionId: string) =>
  request<EvaluationResult>(
    `${sessionPath(id)}/questions/${encodeURIComponent(questionId)}/evaluation`,
    post(),
  );

export const batchEvaluateAnswers = (id: string) =>
  request<BatchEvaluationResult>(`${sessionPath(id)}/evaluations/batch`, post());

/** Counts only, from this browser's expression model, for one answered question. */
export const saveExpressionObservation = (id: string, questionId: string, observation: Observation) =>
  request<void>(`${sessionPath(id)}/questions/${encodeURIComponent(questionId)}/expressions`, {
    method: "PUT",
    body: JSON.stringify(observation),
  });

export const generateReport = (id: string) =>
  request<ReportResult>(`${sessionPath(id)}/report`, post());

export const getReport = (id: string, signal?: AbortSignal) =>
  request<ReportResult>(`${sessionPath(id)}/report`, { signal });

export const retryInterview = (id: string) =>
  request<InterviewPlanView>(`${sessionPath(id)}/retry`, post());
