import assert from "node:assert/strict";
import test from "node:test";
import type { SessionRuntimeState } from "../types.ts";
import { makeSessionSafeToExit } from "./sessionExitSafety.ts";

function runtime(status: SessionRuntimeState["status"], revision = 1): SessionRuntimeState {
  return {
    sessionId: "S-EXIT",
    status,
    revision,
    cursor: null,
    rapportStep: null,
    updatedAt: "2026-07-18T10:00:00",
  };
}

test("active sessions are server-paused before the console may unload them", async () => {
  const calls: string[] = [];
  const result = await makeSessionSafeToExit({
    getSessionRuntime: async () => { calls.push("get"); return runtime("active"); },
    pauseSession: async () => { calls.push("pause"); return runtime("paused", 2); },
  }, "S-EXIT");
  assert.equal(result.status, "paused");
  assert.deepEqual(calls, ["get", "pause"]);
});

test("paused and terminal sessions never receive a redundant pause write", async () => {
  for (const status of ["paused", "intervention_completed", "completed", "aborted", "failed"] as const) {
    let pauses = 0;
    const result = await makeSessionSafeToExit({
      getSessionRuntime: async () => runtime(status),
      pauseSession: async () => { pauses += 1; return runtime("paused"); },
    }, "S-EXIT");
    assert.equal(result.status, status);
    assert.equal(pauses, 0);
  }
});

test("a lost pause response is reconciled from server truth", async () => {
  let reads = 0;
  const result = await makeSessionSafeToExit({
    getSessionRuntime: async () => runtime(++reads === 1 ? "active" : "paused", reads),
    pauseSession: async () => { throw new Error("timeout after commit"); },
  }, "S-EXIT");
  assert.equal(result.status, "paused");
  assert.equal(reads, 2);
});

test("the console stays mounted when the server still reports active", async () => {
  await assert.rejects(() => makeSessionSafeToExit({
    getSessionRuntime: async () => runtime("active"),
    pauseSession: async () => { throw new Error("network down"); },
  }, "S-EXIT"), /network down/);
});

test("a runtime snapshot for another session can never authorize exit", async () => {
  let pauses = 0;
  await assert.rejects(() => makeSessionSafeToExit({
    getSessionRuntime: async () => ({ ...runtime("paused"), sessionId: "S-OTHER" }),
    pauseSession: async () => { pauses += 1; return runtime("paused", 2); },
  }, "S-EXIT"), /未绑定当前场次/);
  assert.equal(pauses, 0);
});

test("a stale non-active pause response cannot be mistaken for a completed pause", async () => {
  let reads = 0;
  await assert.rejects(() => makeSessionSafeToExit({
    getSessionRuntime: async () => { reads += 1; return runtime("active", 5); },
    pauseSession: async () => runtime("paused", 5),
  }, "S-EXIT"), /高于当前修订号/);
  assert.equal(reads, 2);
});

test("a cross-session pause response needs a fresh bound reconciliation before exit", async () => {
  let reads = 0;
  const result = await makeSessionSafeToExit({
    getSessionRuntime: async () => runtime(++reads === 1 ? "active" : "paused", reads),
    pauseSession: async () => ({ ...runtime("paused", 2), sessionId: "S-OTHER" }),
  }, "S-EXIT");
  assert.equal(result.sessionId, "S-EXIT");
  assert.equal(result.status, "paused");
  assert.equal(result.revision, 2);
});
