import assert from "node:assert/strict";
import test from "node:test";

import {
  newWithdrawalIdempotencyKey,
  parsePatientWithdrawalReceipt,
  parseWithdrawnAudioGovernanceRows,
  withdrawalReceiptMatches,
} from "./subjectWithdrawal.ts";

test("withdrawal retry key is stable for the same random input and high entropy", () => {
  const bytes = Uint8Array.from({ length: 32 }, (_, index) => index);
  const first = newWithdrawalIdempotencyKey(bytes);
  const second = newWithdrawalIdempotencyKey(bytes);
  assert.equal(first, second);
  assert.match(first, /^subject-withdrawal-[0-9a-f]{64}$/);
});

test("unknown-result reconciliation accepts only the exact CAS transition", () => {
  const receipt = {
    schema_version: 1 as const,
    event_id: "withdrawal-event",
    patient_id: "P-001",
    withdrawal_status: "withdrawn" as const,
    consent_status: "withdrawn" as const,
    expected_governance_revision: 4,
    governance_revision: 5,
    reason_code: "participant_request" as const,
    actor_display_id: "R-01",
    actor_role: "admin" as const,
    occurred_at: "2026-07-19T00:00:00Z",
    affected_session_count: 2,
    affected_audio_count: 3,
    request_fingerprint: "a".repeat(64),
    idempotent: true,
  };
  assert.equal(withdrawalReceiptMatches(receipt, "P-001", 4, "participant_request"), true);
  assert.equal(withdrawalReceiptMatches(receipt, "P-002", 4, "participant_request"), false);
  assert.equal(withdrawalReceiptMatches(receipt, "P-001", 3, "participant_request"), false);
  assert.equal(withdrawalReceiptMatches(receipt, "P-001", 4, "clinical_safety"), false);
  assert.equal(parsePatientWithdrawalReceipt(receipt, "P-001").event_id, "withdrawal-event");
  assert.throws(() => parsePatientWithdrawalReceipt({ ...receipt, answer_text: "不得出现" }));
  assert.throws(() => parsePatientWithdrawalReceipt({ ...receipt, governance_revision: 4 }));
  const governance = parseWithdrawnAudioGovernanceRows([{
    raw_audio_id: "raw-one",
    session_id: "S-ONE",
    patient_id: "P-001",
    status: "recorded",
    withdrawn: true,
    withdrawal_status: "isolated_by_subject_withdrawal",
    delete_gate_passed: false,
  }]);
  assert.equal(governance[0].session_id, "S-ONE");
  assert.throws(() => parseWithdrawnAudioGovernanceRows([
    governance[0], governance[0],
  ]));
});
