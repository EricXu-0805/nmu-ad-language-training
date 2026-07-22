import assert from "node:assert/strict";
import test from "node:test";
import {
  claimRecordingStartFailure,
  classifyRecordingStartFailure,
  newPatientRecFailureId,
} from "./recordingStartFailure.ts";

test("timeout and authorization failures use distinct closed protocol codes", () => {
  assert.equal(classifyRecordingStartFailure({ kind: "timeout" }), "microphone_start_timeout");
  assert.equal(classifyRecordingStartFailure({ kind: "authorization" }), "recording_authorization_failed");
});

test("getUserMedia DOMException names distinguish permission, missing device and other failures", () => {
  assert.equal(classifyRecordingStartFailure({
    kind: "microphone", error: new DOMException("denied", "NotAllowedError"),
  }), "microphone_permission_denied");
  assert.equal(classifyRecordingStartFailure({
    kind: "microphone", error: new DOMException("missing", "NotFoundError"),
  }), "microphone_not_found");
  assert.equal(classifyRecordingStartFailure({
    kind: "microphone", error: new DOMException("busy", "NotReadableError"),
  }), "microphone_start_failed");
  assert.equal(classifyRecordingStartFailure({ kind: "microphone", error: new Error("boom") }),
    "microphone_start_failed");
});

test("only a current permit can claim one random failure id", () => {
  const stale: { failureId?: string } = {};
  assert.equal(claimRecordingStartFailure(stale, false, "microphone_start_timeout", () => "stale"), null);
  assert.equal(stale.failureId, undefined);

  const permit: { failureId?: string } = {};
  assert.deepEqual(claimRecordingStartFailure(
    permit, true, "microphone_not_found", () => "123e4567-e89b-42d3-a456-426614174000",
  ), {
    failureCode: "microphone_not_found",
    failureId: "123e4567-e89b-42d3-a456-426614174000",
  });
  assert.equal(claimRecordingStartFailure(
    permit, true, "microphone_start_failed", () => "second-id",
  ), null);
});

test("generated failure ids are UUIDs and do not reuse the previous report id", () => {
  const first = newPatientRecFailureId();
  const second = newPatientRecFailureId();
  assert.match(first, /^[0-9a-f-]{36}$/i);
  assert.match(second, /^[0-9a-f-]{36}$/i);
  assert.notEqual(first, second);
});
