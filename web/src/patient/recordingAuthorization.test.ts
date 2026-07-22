import assert from "node:assert/strict";
import test from "node:test";
import {
  authorizesMicrophoneStart,
  recordingRevocationAction,
  type RecordingAuthorization,
} from "./recordingAuthorization.ts";

test("microphone preflight only accepts an explicitly allowed active session", () => {
  const authorization: RecordingAuthorization = { allowed: true, runtime_status: "active", is_simulation: false };
  assert.equal(authorizesMicrophoneStart(authorization), true);
  for (const runtime_status of ["paused", "intervention_completed", "completed", "aborted", "failed"] as const) {
    assert.equal(authorizesMicrophoneStart({ ...authorization, runtime_status }), false);
  }
  assert.equal(authorizesMicrophoneStart({ ...authorization, allowed: false }), false);
});

test("self-start revocation invalidates pending permission and saves an active self recording", () => {
  assert.equal(recordingRevocationAction({
    stopRequested: false,
    selfStartAllowed: false,
    pendingKind: "self",
    activeKind: null,
    microphoneActive: false,
  }), "invalidate-start");
  assert.equal(recordingRevocationAction({
    stopRequested: false,
    selfStartAllowed: false,
    pendingKind: null,
    activeKind: "self",
    microphoneActive: true,
  }), "stop-and-save");
});

test("self-start revocation leaves remote recording alone, while a terminal stop closes every kind", () => {
  assert.equal(recordingRevocationAction({
    stopRequested: false,
    selfStartAllowed: false,
    pendingKind: null,
    activeKind: "remote",
    microphoneActive: true,
  }), "none");
  assert.equal(recordingRevocationAction({
    stopRequested: true,
    selfStartAllowed: true,
    pendingKind: null,
    activeKind: "remote",
    microphoneActive: true,
  }), "stop-and-save");
});
