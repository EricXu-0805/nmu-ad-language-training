import assert from "node:assert/strict";
import test from "node:test";
import { ApiError } from "../apiResponse.ts";
import {
  classifyAutopilotDrainFailure,
  drainRetryDelayMs,
  pendingAckPermanentlyFenced,
} from "./autopilotDrainRetryPolicy.ts";

function canonical(status: number, code: string): ApiError {
  const detail = { code, message: "test" };
  return new ApiError(status, "test", detail, "nested-detail");
}

test("drain retries only uncertainty and transient server failures", () => {
  assert.equal(classifyAutopilotDrainFailure(new TypeError("network")), "retry");
  assert.equal(classifyAutopilotDrainFailure(new ApiError(408, "timeout")), "retry");
  assert.equal(classifyAutopilotDrainFailure(new ApiError(429, "busy")), "retry");
  assert.equal(classifyAutopilotDrainFailure(new ApiError(503, "down")), "retry");
  assert.equal(classifyAutopilotDrainFailure(canonical(
    409, "autopilot_drain_not_current")), "retry");
  assert.deepEqual([0, 1, 2, 3, 4, 10].map(drainRetryDelayMs), [
    1_500, 3_000, 6_000, 12_000, 15_000, 15_000,
  ]);
});

test("released, credential and permanent contract failures do not request-storm", () => {
  assert.equal(classifyAutopilotDrainFailure(canonical(
    409, "autopilot_not_active")), "released");
  assert.equal(classifyAutopilotDrainFailure(new ApiError(401, "expired")),
    "repair-credential");
  assert.equal(classifyAutopilotDrainFailure(new ApiError(403, "wrong device")),
    "repair-credential");
  assert.equal(classifyAutopilotDrainFailure(canonical(
    409, "autopilot_drain_device_mismatch")), "blocked");
  assert.equal(classifyAutopilotDrainFailure(new ApiError(422, "contract")), "blocked");
  assert.equal(classifyAutopilotDrainFailure(new Error("invalid response")), "blocked");
});

test("只有精确的 409 command_not_current 判为代际围栏可丢弃", () => {
  assert.equal(pendingAckPermanentlyFenced(
    canonical(409, "autopilot_command_not_current")), true);
  assert.equal(pendingAckPermanentlyFenced(
    canonical(409, "autopilot_runtime_inactive")), false);
  assert.equal(pendingAckPermanentlyFenced(
    canonical(409, "autopilot_revision_conflict")), false);
  assert.equal(pendingAckPermanentlyFenced(
    canonical(500, "autopilot_command_not_current")), false);
  assert.equal(pendingAckPermanentlyFenced(new TypeError("network")), false);
  assert.equal(pendingAckPermanentlyFenced(new Error("parse")), false);
});
