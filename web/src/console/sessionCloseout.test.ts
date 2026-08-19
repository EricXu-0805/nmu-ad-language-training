import assert from "node:assert/strict";
import test from "node:test";
import {
  buildSessionCloseoutRequest,
  closeoutFailureNeedsReconciliation,
  emptySessionCloseoutDraft,
  hasStructuredCloseoutObservation,
  parseSessionCloseoutRecord,
  parseSessionOutcomeSummary,
  SESSION_CLOSEOUT_NOTE_MAX_LENGTH,
  sessionCloseoutDraftMatchesRecord,
  sessionCloseoutDraftFromRecord,
  validateSessionCloseoutDraft,
  type SessionCloseoutDraft,
  type SessionCloseoutRecord,
} from "./sessionCloseout.ts";

function draft(overrides: Partial<SessionCloseoutDraft> = {}): SessionCloseoutDraft {
  return { ...emptySessionCloseoutDraft(), ...overrides };
}

test("closeout requires an explicit no-observation or recorded-observation choice", () => {
  const result = buildSessionCloseoutRequest(draft(), null, "closeout-choice");
  assert.equal(result.ok, false);
  if (!result.ok) assert.match(result.errors.join(" "), /请选择/);
});

test("recorded observation requires a note or one directly observable flag", () => {
  const emptyRecorded = validateSessionCloseoutDraft(draft({ report_status: "observation_recorded" }));
  assert.equal(emptyRecorded.valid, false);
  assert.match(emptyRecorded.errors.join(" "), /现场备注|可观察事实/);

  const withNote = buildSessionCloseoutRequest(draft({
    report_status: "observation_recorded",
    note: "  设备重连后继续。  ",
  }), 3, "closeout-note");
  assert.equal(withNote.ok, true);
  if (withNote.ok) {
    assert.equal(withNote.value.expected_revision, 3);
    assert.equal(withNote.value.idempotency_key, "closeout-note");
    assert.equal(withNote.value.note, "设备重连后继续。");
  }

  const withFlagDraft = draft({
    report_status: "observation_recorded",
    device_or_network_interruption_occurred: true,
  });
  assert.equal(hasStructuredCloseoutObservation(withFlagDraft), true);
  const withFlag = buildSessionCloseoutRequest(withFlagDraft, null, "  closeout-flag  ");
  assert.equal(withFlag.ok, true);
  if (withFlag.ok) {
    assert.equal(withFlag.value.expected_revision, 0);
    assert.equal(withFlag.value.idempotency_key, "closeout-flag");
    assert.equal(withFlag.value.device_or_network_interruption_occurred, true);
    assert.equal(withFlag.value.note, null);
  }
});

test("no additional observation produces a contradiction-free request", () => {
  const result = buildSessionCloseoutRequest(draft({
    report_status: "no_additional_observation",
    note: "stale local text",
    fatigue_observed: true,
    distress_or_discomfort_observed: true,
    participant_declined_to_continue: true,
    staff_assistance_occurred: true,
    environment_interruption_occurred: true,
    device_or_network_interruption_occurred: true,
  }), 7, "closeout-none");
  assert.equal(result.ok, true);
  if (!result.ok) return;
  assert.deepEqual(result.value, {
    expected_revision: 7,
    idempotency_key: "closeout-none",
    report_status: "no_additional_observation",
    note: null,
    fatigue_observed: false,
    distress_or_discomfort_observed: false,
    participant_declined_to_continue: false,
    staff_assistance_occurred: false,
    environment_interruption_occurred: false,
    device_or_network_interruption_occurred: false,
  });
});

test("closeout note is capped at 2000 characters", () => {
  const exact = validateSessionCloseoutDraft(draft({
    report_status: "observation_recorded",
    note: "字".repeat(SESSION_CLOSEOUT_NOTE_MAX_LENGTH),
  }));
  assert.equal(exact.valid, true);

  const over = buildSessionCloseoutRequest(draft({
    report_status: "observation_recorded",
    note: "字".repeat(SESSION_CLOSEOUT_NOTE_MAX_LENGTH + 1),
  }), 1, "closeout-overlong");
  assert.equal(over.ok, false);
  if (!over.ok) assert.match(over.errors.join(" "), /2000/);
});

test("an existing record hydrates its revision-independent editable draft", () => {
  const record: SessionCloseoutRecord = {
    session_id: "S-CLOSEOUT",
    schema_version: "session-closeout.v1",
    revision: 4,
    report_status: "observation_recorded",
    note: "现场有短暂环境中断。",
    locked: false,
    fatigue_observed: false,
    distress_or_discomfort_observed: false,
    participant_declined_to_continue: false,
    staff_assistance_occurred: false,
    environment_interruption_occurred: true,
    device_or_network_interruption_occurred: false,
  };
  assert.deepEqual(sessionCloseoutDraftFromRecord(record), {
    report_status: "observation_recorded",
    note: "现场有短暂环境中断。",
    fatigue_observed: false,
    distress_or_discomfort_observed: false,
    participant_declined_to_continue: false,
    staff_assistance_occurred: false,
    environment_interruption_occurred: true,
    device_or_network_interruption_occurred: false,
  });
});

test("builder rejects a missing idempotency key", () => {
  const result = buildSessionCloseoutRequest(draft({
    report_status: "no_additional_observation",
  }), 2, "   ");
  assert.equal(result.ok, false);
  if (!result.ok) assert.match(result.errors.join(" "), /防重复标识/);
});

test("network parsers bind summary and closeout to the requested session", () => {
  const summary = parseSessionOutcomeSummary({
    session_id: "S-CLOSEOUT",
    schema_version: "session-outcome-summary.v1",
    generator_version: "generator-v1",
    item_bank_version_id: "bank-v1",
    is_simulation: true,
    data_classification: "simulation",
    expected_turns: 1,
    matched_turns: 1,
    completed_attempt_turns: 1,
    audio_evidenced_turns: 1,
    total_attempts: 1,
    completed_attempts: 1,
    needs_review_attempts: 0,
    technical_failure_attempts: 0,
    technical_pause_count: 0,
    researcher_takeover_count: 0,
    prompt_level_0_count: 1,
    prompt_level_1_count: 0,
    prompt_level_2_count: 0,
    prompt_level_3_count: 0,
    source_digest: "a".repeat(64),
    generated_at: "2026-07-19T00:00:00",
  }, "S-CLOSEOUT");
  assert.equal(summary.completed_attempt_turns, 1);

  const closeout = parseSessionCloseoutRecord({
    session_id: "S-CLOSEOUT",
    schema_version: "session-closeout.v1",
    revision: 1,
    report_status: "no_additional_observation",
    note: null,
    locked: false,
    locked_by: null,
    locked_at: null,
    fatigue_observed: false,
    distress_or_discomfort_observed: false,
    participant_declined_to_continue: false,
    staff_assistance_occurred: false,
    environment_interruption_occurred: false,
    device_or_network_interruption_occurred: false,
  }, "S-CLOSEOUT");
  assert.equal(closeout.report_status, "no_additional_observation");

  assert.throws(() => parseSessionOutcomeSummary({ ...summary, session_id: "S-OTHER" }, "S-CLOSEOUT"), /其他场次/);
});

test("network parsers reject contradictory closeout and summary facts", () => {
  assert.throws(() => parseSessionCloseoutRecord({
    session_id: "S-CLOSEOUT",
    schema_version: "session-closeout.v1",
    revision: 1,
    report_status: "no_additional_observation",
    note: "不应存在",
    locked: false,
    locked_by: null,
    locked_at: null,
    fatigue_observed: false,
    distress_or_discomfort_observed: false,
    participant_declined_to_continue: false,
    staff_assistance_occurred: false,
    environment_interruption_occurred: false,
    device_or_network_interruption_occurred: false,
  }, "S-CLOSEOUT"), /矛盾/);
});

test("closeout editor stays dirty until its exact normalized draft is server-saved", () => {
  const record: SessionCloseoutRecord = {
    session_id: "S-CLOSEOUT",
    schema_version: "session-closeout.v1",
    revision: 2,
    report_status: "observation_recorded",
    note: "环境短暂中断。",
    locked: false,
    fatigue_observed: false,
    distress_or_discomfort_observed: false,
    participant_declined_to_continue: false,
    staff_assistance_occurred: false,
    environment_interruption_occurred: true,
    device_or_network_interruption_occurred: false,
  };
  assert.equal(sessionCloseoutDraftMatchesRecord(
    sessionCloseoutDraftFromRecord(record), record,
  ), true);
  assert.equal(sessionCloseoutDraftMatchesRecord({
    ...sessionCloseoutDraftFromRecord(record), note: "已修改。",
  }, record), false);
  assert.equal(sessionCloseoutDraftMatchesRecord(emptySessionCloseoutDraft(), null), false);
});

test("unknown, timeout, conflict and server failures require authoritative reconciliation", () => {
  assert.equal(closeoutFailureNeedsReconciliation(new Error("response lost")), true);
  assert.equal(closeoutFailureNeedsReconciliation({ status: 0 }), true);
  assert.equal(closeoutFailureNeedsReconciliation({ status: 409 }), true);
  assert.equal(closeoutFailureNeedsReconciliation({ status: 503 }), true);
  assert.equal(closeoutFailureNeedsReconciliation({ status: 422 }), false);
  assert.equal(closeoutFailureNeedsReconciliation({ status: 403 }), false);
});
