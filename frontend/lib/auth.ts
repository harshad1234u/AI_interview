// Pure authentication rules, kept free of React and fetch so `node --test` can run them.
// Identity lives in the backend's HttpOnly session cookie: nothing here stores a token or an id.

export const LOGIN_PATH = "/login";
export const PASSWORD_MIN_LENGTH = 8;
export const PASSWORD_MAX_LENGTH = 128;
const NAME_MAX_LENGTH = 200;

// A 401 from these means "wrong email or password", not "your session ended".
const CREDENTIAL_PATHS = ["/api/auth/login", "/api/auth/register"];

/** Whether a response should send the browser to the login page.
 * Unauthenticated calls to /api/auth/me on the home landing page ("/") do not redirect,
 * allowing the public product showcase to render. */
export function shouldRedirectToLogin(status: number, apiPath: string, pagePath: string): boolean {
  if (status !== 401) return false;
  if (CREDENTIAL_PATHS.includes(apiPath)) return false;
  if (apiPath === "/api/auth/me" && pagePath === "/") return false;
  return pagePath !== LOGIN_PATH;
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
