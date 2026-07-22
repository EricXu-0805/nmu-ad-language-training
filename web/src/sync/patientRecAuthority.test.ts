import assert from "node:assert/strict";
import test from "node:test";
import { readAuthoritativePatientRec } from "./patientRecAuthority.ts";

const failure = {
  active: false,
  turnKey: "SE_锚#1",
  sessionId: "S-1",
  failureCode: "microphone_permission_denied",
  failureId: "123e4567-e89b-42d3-a456-426614174000",
};

test("a strict matching server failure projection is authoritative", () => {
  assert.deepEqual(readAuthoritativePatientRec(failure, "S-1"), { type: "patientRec", ...failure });
});

test("BroadcastChannel-shaped failures and cross-session projections cannot become authority", () => {
  assert.equal(readAuthoritativePatientRec({ type: "patientRec", ...failure }, "S-1"), null);
  assert.equal(readAuthoritativePatientRec(failure, "S-2"), null);
});

test("invalid active/failure combinations fail closed", () => {
  assert.equal(readAuthoritativePatientRec({ ...failure, active: true }, "S-1"), null);
  assert.equal(readAuthoritativePatientRec({ ...failure, failureCode: "unknown" }, "S-1"), null);
  const { failureId: _failureId, ...withoutFailureId } = failure;
  assert.equal(readAuthoritativePatientRec(withoutFailureId, "S-1"), null);
});
