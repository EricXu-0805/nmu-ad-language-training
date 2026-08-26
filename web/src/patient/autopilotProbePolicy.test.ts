import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { ApiError } from "../apiResponse.ts";
import {
  classifyAutopilotProbeError,
  isRetryableAutopilotProbeError,
  MAX_AUTOPILOT_PROBE_RETRIES,
  planAutopilotProbeFailure,
} from "./autopilotProbePolicy.ts";

function exact(code: string): ApiError {
  return new ApiError(
    409,
    "probe",
    { code, message: "probe" },
    "nested-detail",
  );
}

test("legacy is admitted only by the two exact canonical 409 proofs", () => {
  assert.equal(classifyAutopilotProbeError(exact("autopilot_not_active")), "legacy-inactive");
  assert.equal(classifyAutopilotProbeError(exact("autopilot_p0a_disabled")), "legacy-disabled");
  // D2 分类先行后,真实场次全关的门禁码是 real_sessions_disabled:同样是稳定的
  // 部署开关事实,患者端应挂载人工平面而不是吓人的 blocked 屏。
  assert.equal(
    classifyAutopilotProbeError(exact("autopilot_real_sessions_disabled")),
    "legacy-disabled");
  for (const error of [
    exact("autopilot_state_invalid"),
    new ApiError(500, "down"),
    new ApiError(409, "decorated", {
      code: "autopilot_not_active", message: "probe", request_id: "trace",
    }, "nested-detail"),
    new ApiError(409, "top-level", {
      code: "autopilot_not_active", message: "probe",
    }, "noncanonical-json"),
  ]) {
    assert.equal(classifyAutopilotProbeError(error), "blocked");
  }
});

test("network uncertainty retries while permanent contracts remain blocked", () => {
  assert.equal(isRetryableAutopilotProbeError(new TypeError("network")), true);
  assert.equal(isRetryableAutopilotProbeError(new ApiError(0, "offline")), true);
  assert.equal(isRetryableAutopilotProbeError(new ApiError(408, "timeout")), true);
  assert.equal(isRetryableAutopilotProbeError(new ApiError(503, "down")), true);
  assert.equal(isRetryableAutopilotProbeError(new ApiError(401, "expired")), false);
  assert.equal(isRetryableAutopilotProbeError(new Error("bad contract")), false);
});

test("inactive and disabled gates stop polling for the current paired session", () => {
  assert.deepEqual(planAutopilotProbeFailure(
    exact("autopilot_not_active"), 0,
  ), {
    action: "stop-legacy",
    disposition: "legacy-inactive",
  });
  assert.deepEqual(planAutopilotProbeFailure(
    exact("autopilot_p0a_disabled"), 0,
  ), {
    action: "stop-legacy",
    disposition: "legacy-disabled",
  });
});

test("transient probe failures use finite exponential backoff", () => {
  const transient = new ApiError(503, "down");
  assert.deepEqual(
    Array.from({ length: MAX_AUTOPILOT_PROBE_RETRIES }, (_, attempt) => (
      planAutopilotProbeFailure(transient, attempt)
    )),
    [
      { action: "retry", delayMs: 1_500 },
      { action: "retry", delayMs: 3_000 },
      { action: "retry", delayMs: 6_000 },
      { action: "retry", delayMs: 12_000 },
      { action: "retry", delayMs: 15_000 },
    ],
  );
  assert.deepEqual(
    planAutopilotProbeFailure(transient, MAX_AUTOPILOT_PROBE_RETRIES),
    { action: "stop-blocked", retryExhausted: true },
  );
  assert.deepEqual(
    planAutopilotProbeFailure(exact("autopilot_state_invalid"), 0),
    { action: "stop-blocked", retryExhausted: false },
  );
});

test("patient hook has no inactive standby loop and fences every passive terminal poll", () => {
  const source = readFileSync(new URL("./usePatientAutopilot.ts", import.meta.url), "utf8");
  assert.doesNotMatch(source, /standby probe/);
  assert.doesNotMatch(source, /window\.setTimeout\(check,\s*[\d_]+\)/);
  assert.match(source, /if \(!sessionId \|\| input\.sessionTerminal\)/);
  assert.match(source, /mediaAllowed \|\| !probeAllowed/);
  assert.match(source, /input\.sessionPaused \|\| input\.sessionTerminal/);
});
