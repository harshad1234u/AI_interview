import assert from "node:assert/strict";
import test from "node:test";
import { getApiEndpoint } from "./endpoint.ts";

test("getApiEndpoint strips trailing slashes from base URL", () => {
  const orig = process.env.NEXT_PUBLIC_API_URL;
  try {
    process.env.NEXT_PUBLIC_API_URL = "https://ai-interview-coach-api-w0zj.onrender.com/";
    assert.equal(
      getApiEndpoint("/api/health"),
      "https://ai-interview-coach-api-w0zj.onrender.com/api/health",
    );
  } finally {
    process.env.NEXT_PUBLIC_API_URL = orig;
  }
});

test("getApiEndpoint handles base URLs with multiple trailing slashes", () => {
  const orig = process.env.NEXT_PUBLIC_API_URL;
  try {
    process.env.NEXT_PUBLIC_API_URL = "https://ai-interview-coach-api-w0zj.onrender.com///";
    assert.equal(
      getApiEndpoint("/api/auth/me"),
      "https://ai-interview-coach-api-w0zj.onrender.com/api/auth/me",
    );
  } finally {
    process.env.NEXT_PUBLIC_API_URL = orig;
  }
});

test("getApiEndpoint normalizes paths with duplicate slashes", () => {
  const orig = process.env.NEXT_PUBLIC_API_URL;
  try {
    process.env.NEXT_PUBLIC_API_URL = "https://ai-interview-coach-api-w0zj.onrender.com";
    assert.equal(
      getApiEndpoint("//api//health"),
      "https://ai-interview-coach-api-w0zj.onrender.com/api/health",
    );
    assert.equal(
      getApiEndpoint("api/health"),
      "https://ai-interview-coach-api-w0zj.onrender.com/api/health",
    );
  } finally {
    process.env.NEXT_PUBLIC_API_URL = orig;
  }
});

test("getApiEndpoint returns relative path when NEXT_PUBLIC_API_URL is unset", () => {
  const orig = process.env.NEXT_PUBLIC_API_URL;
  try {
    delete process.env.NEXT_PUBLIC_API_URL;
    assert.equal(getApiEndpoint("/api/health"), "/api/health");
    assert.equal(getApiEndpoint("//api//auth/me"), "/api/auth/me");
  } finally {
    process.env.NEXT_PUBLIC_API_URL = orig;
  }
});
