import assert from "node:assert/strict";
import test from "node:test";
import { csrfHeader, readCookie } from "./csrf.ts";

test("cookie parser is exact and rejects malformed encoding", () => {
  assert.equal(readCookie("nmu_csrf=abc123; other=x", "nmu_csrf"), "abc123");
  assert.equal(readCookie("prefix_nmu_csrf=wrong; nmu_csrf=right", "nmu_csrf"), "right");
  assert.equal(readCookie("nmu_csrf=%ZZ", "nmu_csrf"), null);
});

test("only unsafe methods receive the session-bound csrf header", () => {
  assert.deepEqual(csrfHeader("GET", "nmu_csrf=secret"), {});
  assert.deepEqual(csrfHeader("HEAD", "nmu_csrf=secret"), {});
  assert.deepEqual(csrfHeader("POST", "other=x"), {});
  assert.deepEqual(csrfHeader("PATCH", "nmu_csrf=proof"), { "X-CSRF-Token": "proof" });
});
