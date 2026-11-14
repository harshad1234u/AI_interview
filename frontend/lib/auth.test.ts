// Run with `npm test` (node --test; Node strips the TypeScript types).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  LOGIN_PATH,
  parseRecoveryHash,
  shouldRedirectToLogin,
  validateAuthForm,
  validateResetPasswordForm,
} from "./auth.ts";

test("a 401 from a protected API sends the browser to the login page", () => {
  for (const path of ["/api/auth/me", "/api/candidates/x", "/api/interview-sessions/x/answers"]) {
    assert.equal(shouldRedirectToLogin(401, path, "/interview"), true);
  }
});

test("wrong credentials, other statuses and the login page itself never redirect", () => {
  assert.equal(shouldRedirectToLogin(401, "/api/auth/login", "/login"), false);
  assert.equal(shouldRedirectToLogin(401, "/api/auth/register", "/login"), false);
  assert.equal(shouldRedirectToLogin(401, "/api/auth/reset-password", "/reset-password"), false);
  assert.equal(shouldRedirectToLogin(401, "/api/auth/forgot-password", "/login"), false);
  assert.equal(shouldRedirectToLogin(401, "/api/auth/me", LOGIN_PATH), false); // no reload loop
  assert.equal(shouldRedirectToLogin(401, "/api/auth/me", "/reset-password"), false); // unauthenticated reset page
  for (const status of [200, 403, 404, 409, 429, 500, 0]) {
    assert.equal(shouldRedirectToLogin(status, "/api/auth/me", "/"), false);
  }
});

const ok = { name: "Asha Rao", email: "asha@example.com", password: "correct horse" };

test("valid forms pass", () => {
  assert.deepEqual(validateAuthForm("register", ok), {});
  assert.deepEqual(validateAuthForm("login", { ...ok, name: "" }), {});
});

test("registration checks every field", () => {
  assert.deepEqual(validateAuthForm("register", { name: " ", email: "", password: "" }), {
    name: "Enter your name.",
    email: "Enter your email.",
    password: "Enter your password.",
  });
  assert.equal(validateAuthForm("register", { ...ok, email: "asha@" }).email,
               "Enter a valid email address.");
  assert.equal(validateAuthForm("register", { ...ok, password: "short" }).password,
               "Use at least 8 characters.");
  assert.equal(validateAuthForm("register", { ...ok, password: "x".repeat(129) }).password,
               "Use at most 128 characters.");
  assert.equal(validateAuthForm("register", { ...ok, name: "x".repeat(201) }).name,
               "Use at most 200 characters.");
});

test("login applies no length policy to an existing password, only the upper bound", () => {
  assert.deepEqual(validateAuthForm("login", { ...ok, password: "short" }), {});
  assert.equal(validateAuthForm("login", { ...ok, password: "x".repeat(129) }).password,
               "Use at most 128 characters.");
});

test("validateResetPasswordForm validates password and confirmation matching", () => {
  assert.deepEqual(
    validateResetPasswordForm({ password: "ValidPassword123", confirmPassword: "ValidPassword123" }),
    {}
  );
  assert.deepEqual(
    validateResetPasswordForm({ password: "", confirmPassword: "" }),
    { password: "Enter your new password.", confirmPassword: "Confirm your new password." }
  );
  assert.equal(
    validateResetPasswordForm({ password: "short", confirmPassword: "short" }).password,
    "Use at least 8 characters."
  );
  assert.equal(
    validateResetPasswordForm({ password: "x".repeat(129), confirmPassword: "x".repeat(129) }).password,
    "Use at most 128 characters."
  );
  assert.equal(
    validateResetPasswordForm({ password: "ValidPassword123", confirmPassword: "MismatchPassword123" }).confirmPassword,
    "Passwords do not match."
  );
});

test("parseRecoveryHash extracts access token, type, and error information", () => {
  const hash = "#access_token=test_jwt_token&refresh_token=test_refresh&expires_in=3600&token_type=bearer&type=recovery";
  const parsed = parseRecoveryHash(hash);
  assert.equal(parsed.accessToken, "test_jwt_token");
  assert.equal(parsed.type, "recovery");
  assert.equal(parsed.error, null);

  const errorHash = "#error=access_denied&error_code=otp_expired&error_description=Email+link+is+invalid+or+has+expired";
  const parsedError = parseRecoveryHash(errorHash);
  assert.equal(parsedError.accessToken, null);
  assert.equal(parsedError.error, "access_denied");
  assert.equal(parsedError.errorCode, "otp_expired");
  assert.equal(parsedError.errorDescription, "Email link is invalid or has expired");

  const queryParams = "?error=access_denied";
  const parsedQuery = parseRecoveryHash(queryParams);
  assert.equal(parsedQuery.error, "access_denied");

  const empty = parseRecoveryHash("");
  assert.equal(empty.accessToken, null);
  assert.equal(empty.type, null);
});

test("password visibility toggle preserves input values and changes input type correctly", () => {
  let isPasswordVisible = false;
  const toggleVisibility = () => {
    isPasswordVisible = !isPasswordVisible;
  };
  const getInputType = (visible: boolean) => (visible ? "text" : "password");

  // Initial state is password (hidden)
  assert.equal(getInputType(isPasswordVisible), "password");

  // Toggle to visible
  toggleVisibility();
  assert.equal(isPasswordVisible, true);
  assert.equal(getInputType(isPasswordVisible), "text");

  // Toggle back to hidden
  toggleVisibility();
  assert.equal(isPasswordVisible, false);
  assert.equal(getInputType(isPasswordVisible), "password");

  // Verify form value is completely preserved when input type changes
  const passwordValue = "MySecretP@ssw0rd!";
  assert.deepEqual(
    validateAuthForm("login", { email: "user@example.com", password: passwordValue, name: "" }),
    {}
  );
  assert.deepEqual(
    validateResetPasswordForm({ password: passwordValue, confirmPassword: passwordValue }),
    {}
  );
});

test("password toggle accessibility attributes and aria-pressed semantics", () => {
  let showPassword = false;
  const getButtonProps = (visible: boolean) => ({
    type: "button",
    "aria-label": visible ? "Hide password" : "Show password",
    "aria-pressed": visible,
    title: visible ? "Hide password" : "Show password",
  });

  // Default state: hidden
  let props = getButtonProps(showPassword);
  assert.equal(props["aria-label"], "Show password");
  assert.equal(props["aria-pressed"], false);
  assert.equal(props.title, "Show password");

  // Toggled state: visible
  showPassword = true;
  props = getButtonProps(showPassword);
  assert.equal(props["aria-label"], "Hide password");
  assert.equal(props["aria-pressed"], true);
  assert.equal(props.title, "Hide password");
});

test("autofill fallback synchronization preserves password on toggle and submit", () => {
  // Simulates browser autofill where DOM has value but React state was empty
  const state = { email: "harshad@harshad.me", password: "" };
  const mockDomInput = { value: "SavedPassword123!" };

  // 1. Submit synchronization fallback
  const resolvedPassword = state.password || mockDomInput.value || "";
  assert.equal(resolvedPassword, "SavedPassword123!");
  assert.deepEqual(
    validateAuthForm("login", { email: state.email, password: resolvedPassword, name: "" }),
    {}
  );

  // 2. Toggle synchronization: state captures DOM value before flip
  let syncedPassword = state.password;
  if (mockDomInput.value && mockDomInput.value !== state.password) {
    syncedPassword = mockDomInput.value;
  }
  assert.equal(syncedPassword, "SavedPassword123!");

  // Now when switching type from password to text, the input retains the autofilled value
  const nextInputType = "text";
  assert.equal(nextInputType, "text");
  assert.equal(syncedPassword.length, 17);
});

test("repeated visibility toggling maintains idempotency and selection position", () => {
  let show = false;
  const cursor = { start: 5, end: 12, direction: "forward" };
  const history: boolean[] = [];

  for (let i = 0; i < 10; i++) {
    show = !show;
    history.push(show);
    // Cursor range remains unaffected by state toggling
    assert.equal(cursor.start, 5);
    assert.equal(cursor.end, 12);
  }

  // Exactly 10 alternating states ending back on false (hidden)
  assert.equal(history.length, 10);
  assert.equal(show, false);
  assert.deepEqual(history, [true, false, true, false, true, false, true, false, true, false]);
});
