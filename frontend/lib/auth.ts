// Pure authentication rules, kept free of React and fetch so `node --test` can run them.
// Identity lives in the backend's HttpOnly session cookie: nothing here stores a token or an id.

export const LOGIN_PATH = "/login";
export const PASSWORD_MIN_LENGTH = 8;
export const PASSWORD_MAX_LENGTH = 128;
const NAME_MAX_LENGTH = 200;

// A 401 from these means "wrong email, password or token", not "your session ended".
const CREDENTIAL_PATHS = [
  "/api/auth/login",
  "/api/auth/register",
  "/api/auth/reset-password",
  "/api/auth/forgot-password",
];

/** Whether a response should send the browser to the login page.
 * Unauthenticated calls to /api/auth/me on public pages ("/" or "/reset-password") do not redirect,
 * allowing public views to render cleanly. */
export function shouldRedirectToLogin(status: number, apiPath: string, pagePath: string): boolean {
  if (status !== 401) return false;
  if (CREDENTIAL_PATHS.includes(apiPath)) return false;
  if (apiPath === "/api/auth/me" && (pagePath === "/" || pagePath === "/reset-password")) return false;
  return pagePath !== LOGIN_PATH && pagePath !== "/reset-password";
}

export type AuthMode = "login" | "register";
export type AuthForm = { name: string; email: string; password: string };
export type AuthFieldErrors = Partial<Record<keyof AuthForm, string>>;

/** Client-side checks before sending; the server validates again and has the last word. */
export function validateAuthForm(mode: AuthMode, form: AuthForm): AuthFieldErrors {
  const errors: AuthFieldErrors = {};
  if (mode === "register") {
    const name = form.name.trim();
    if (!name) errors.name = "Enter your name.";
    else if (name.length > NAME_MAX_LENGTH) errors.name = `Use at most ${NAME_MAX_LENGTH} characters.`;
  }
  const email = form.email.trim();
  if (!email) errors.email = "Enter your email.";
  else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) errors.email = "Enter a valid email address.";
  if (!form.password) errors.password = "Enter your password.";
  else if (form.password.length > PASSWORD_MAX_LENGTH)
    errors.password = `Use at most ${PASSWORD_MAX_LENGTH} characters.`;
  else if (mode === "register" && form.password.length < PASSWORD_MIN_LENGTH)
    errors.password = `Use at least ${PASSWORD_MIN_LENGTH} characters.`;
  return errors;
}

export type ResetPasswordForm = { password: string; confirmPassword: string };
export type ResetPasswordFieldErrors = Partial<Record<keyof ResetPasswordForm, string>>;

export function validateResetPasswordForm(form: ResetPasswordForm): ResetPasswordFieldErrors {
  const errors: ResetPasswordFieldErrors = {};
  const pwd = form.password;
  const confirm = form.confirmPassword;

  if (!pwd) {
    errors.password = "Enter your new password.";
  } else if (pwd.length < PASSWORD_MIN_LENGTH) {
    errors.password = `Use at least ${PASSWORD_MIN_LENGTH} characters.`;
  } else if (pwd.length > PASSWORD_MAX_LENGTH) {
    errors.password = `Use at most ${PASSWORD_MAX_LENGTH} characters.`;
  }

  if (!confirm) {
    errors.confirmPassword = "Confirm your new password.";
  } else if (pwd && pwd !== confirm) {
    errors.confirmPassword = "Passwords do not match.";
  }

  return errors;
}

export type ParsedRecoveryTokens = {
  accessToken: string | null;
  type: string | null;
  error: string | null;
  errorCode: string | null;
  errorDescription: string | null;
};

export function parseRecoveryHash(hashOrQuery: string): ParsedRecoveryTokens {
  const clean = hashOrQuery.startsWith("#") || hashOrQuery.startsWith("?")
    ? hashOrQuery.substring(1)
    : hashOrQuery;
  const params = new URLSearchParams(clean);
  return {
    accessToken: params.get("access_token"),
    type: params.get("type"),
    error: params.get("error"),
    errorCode: params.get("error_code"),
    errorDescription: params.get("error_description"),
  };
}
