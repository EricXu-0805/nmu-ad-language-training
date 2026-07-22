import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, decodeJsonApiResponse } from "./apiResponse.ts";

test("non-JSON proxy errors become readable ApiError values", () => {
  assert.throws(
    () => decodeJsonApiResponse({
      status: 502,
      ok: false,
      statusText: "Bad Gateway",
      text: "<!doctype html><title>Bad Gateway</title><h1>upstream unavailable</h1>",
    }),
    (error: unknown) => error instanceof ApiError
      && error.status === 502
      && error.detail === "Bad Gateway upstream unavailable",
  );
});

test("JSON API details preserve structured error data", () => {
  assert.throws(
    () => decodeJsonApiResponse({
      status: 409,
      ok: false,
      text: JSON.stringify({ detail: { issue: "classification mismatch" } }),
    }),
    (error: unknown) => error instanceof ApiError
      && error.detail === '{"issue":"classification mismatch"}'
      && error.detailEnvelope === "nested-detail"
      && (error.detailData as { issue?: string }).issue === "classification mismatch",
  );
});

test("top-level or decorated JSON errors are not canonical nested-detail envelopes", () => {
  for (const body of [
    { code: "audio_terminal_disposition" },
    { detail: { code: "audio_terminal_disposition" }, requestId: "trace" },
  ]) {
    assert.throws(
      () => decodeJsonApiResponse({ status: 410, ok: false, text: JSON.stringify(body) }),
      (error: unknown) => error instanceof ApiError
        && error.detailEnvelope === "noncanonical-json",
    );
  }
});

test("a non-JSON success is also rejected at the typed API boundary", () => {
  assert.throws(
    () => decodeJsonApiResponse({ status: 200, ok: true, text: "not-json" }),
    (error: unknown) => error instanceof ApiError && error.detail.includes("格式错误"),
  );
});

test("empty and valid JSON success bodies decode without throwing SyntaxError", () => {
  assert.equal(decodeJsonApiResponse({ status: 204, ok: true, text: "" }), null);
  assert.deepEqual(decodeJsonApiResponse({ status: 200, ok: true, text: '{"ok":true}' }), { ok: true });
});
