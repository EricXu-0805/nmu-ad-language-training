import assert from "node:assert/strict";
import test from "node:test";

import {
  newWithdrawalIdempotencyKey,
  parsePatientWithdrawalReceipt,
  parseSupersededAudioGovernanceRows,
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
  const baseRow = {
    raw_audio_id: "raw-one",
    session_id: "S-ONE",
    patient_id: "P-001",
    status: "recorded",
    withdrawn: true,
    withdrawal_status: "isolated_by_subject_withdrawal",
    delete_gate_passed: false,
    local_copy_disposal_device_count: 0,
    local_copy_disposal_last_at: null,
  };
  const governance = parseWithdrawnAudioGovernanceRows([baseRow]);
  assert.equal(governance[0].session_id, "S-ONE");
  assert.equal(governance[0].local_copy_disposal_device_count, 0);
  assert.throws(() => parseWithdrawnAudioGovernanceRows([
    governance[0], governance[0],
  ]));
  const reported = parseWithdrawnAudioGovernanceRows([{
    ...baseRow,
    local_copy_disposal_device_count: 2,
    local_copy_disposal_last_at: "2026-08-08T00:15:00",
  }]);
  assert.equal(reported[0].local_copy_disposal_device_count, 2);
  // 缺字段、负数、半空组合(有计数没时间/有时间没计数)都必须拒绝。
  const legacyShape = { ...baseRow } as Record<string, unknown>;
  delete legacyShape.local_copy_disposal_device_count;
  delete legacyShape.local_copy_disposal_last_at;
  assert.throws(() => parseWithdrawnAudioGovernanceRows([legacyShape]));
  assert.throws(() => parseWithdrawnAudioGovernanceRows([
    { ...baseRow, local_copy_disposal_device_count: -1 }]));
  assert.throws(() => parseWithdrawnAudioGovernanceRows([
    { ...baseRow, local_copy_disposal_device_count: 1 }]));
  assert.throws(() => parseWithdrawnAudioGovernanceRows([
    { ...baseRow, local_copy_disposal_last_at: "2026-08-08T00:15:00" }]));
});

test("superseded audio slot list accepts only the exact server projection", () => {
  const ruleRow = {
    raw_audio_id: "raw-orphan-1",
    session_id: "S-ONE",
    patient_id: "P-001",
    marked_by_rule: true,
    last_marked_at: "2026-09-13T06:30:00+00:00",
    local_copy_disposal_device_count: 1,
    local_copy_disposal_last_at: "2026-09-13T06:31:00+00:00",
  };
  const handRow = {
    ...ruleRow,
    raw_audio_id: "raw-hand-voided",
    marked_by_rule: false,
    last_marked_at: null,
    local_copy_disposal_device_count: 0,
    local_copy_disposal_last_at: null,
  };
  const parsed = parseSupersededAudioGovernanceRows([ruleRow, handRow]);
  assert.deepEqual(parsed.map((row) => row.marked_by_rule), [true, false]);
  assert.deepEqual(parseSupersededAudioGovernanceRows([]), []);
  assert.throws(() => parseSupersededAudioGovernanceRows({ rows: [] }));
  assert.throws(() => parseSupersededAudioGovernanceRows([ruleRow, ruleRow]));
  // 多一个字段(哪怕是撤回列表里合法的字段)、少一个字段,都拒绝。
  assert.throws(() => parseSupersededAudioGovernanceRows([
    { ...ruleRow, withdrawn: false }]));
  assert.throws(() => parseSupersededAudioGovernanceRows([
    { ...ruleRow, asr_text: "不得出现" }]));
  const missing = { ...ruleRow } as Record<string, unknown>;
  delete missing.last_marked_at;
  assert.throws(() => parseSupersededAudioGovernanceRows([missing]));
  // 规则标记与作废时间必须成对;时间必须带显式时区。
  assert.throws(() => parseSupersededAudioGovernanceRows([
    { ...ruleRow, last_marked_at: null }]));
  assert.throws(() => parseSupersededAudioGovernanceRows([
    { ...handRow, last_marked_at: "2026-09-13T06:30:00+00:00" }]));
  assert.throws(() => parseSupersededAudioGovernanceRows([
    { ...ruleRow, last_marked_at: "2026-09-13T06:30:00" }]));
  assert.throws(() => parseSupersededAudioGovernanceRows([
    { ...ruleRow, marked_by_rule: "true" }]));
  assert.throws(() => parseSupersededAudioGovernanceRows([
    { ...ruleRow, local_copy_disposal_last_at: "2026-09-13T06:31:00" }]));
  assert.throws(() => parseSupersededAudioGovernanceRows([
    { ...ruleRow, local_copy_disposal_last_at: null }]));
  assert.throws(() => parseSupersededAudioGovernanceRows([
    { ...handRow, local_copy_disposal_last_at: "2026-09-13T06:31:00+00:00" }]));
  assert.throws(() => parseSupersededAudioGovernanceRows([
    { ...handRow, local_copy_disposal_device_count: -1 }]));
  assert.throws(() => parseSupersededAudioGovernanceRows([
    { ...handRow, raw_audio_id: "../escape" }]));
  assert.throws(() => parseSupersededAudioGovernanceRows([
    { ...handRow, patient_id: "" }]));
});
