import assert from "node:assert/strict";
import test from "node:test";
import { parseSessionRuntimeState } from "./sessionRuntime.ts";

const SID = "S-RUNTIME-01";

function runtime(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    sessionId: SID,
    status: "active",
    revision: 0,
    cursor: null,
    rapportStep: null,
    pausedAt: null,
    resumedAt: null,
    interventionCompletedAt: null,
    interventionEndedBy: null,
    completedAt: null,
    abortedAt: null,
    endedBy: null,
    endReason: null,
    updatedAt: "2026-07-19T01:00:00.123456",
    ...overrides,
  };
}

test("runtime parser binds the exact path session and normalizes safe recovery cursors", () => {
  const parsed = parseSessionRuntimeState(runtime({
    revision: 8,
    cursor: {
      sessionId: SID,
      screen: "present",
      itemIdx: 2,
      turnIdx: 1,
      responseRole: "命名",
      cueLevel: 1,
      recording: "idle",
      recSeq: 3,
      selfStart: false,
      fbKey: "self",
      fbItemId: "ITEM-2",
      fbSeq: 4,
      wseq: 20,
      sourceWseq: 19,
    },
    rapportStep: {
      sessionId: SID,
      sectionKey: "认识机器人",
      questionIdx: 1,
      recording: "idle",
      recSeq: 2,
      assentGate: true,
      containsDirectIdentifier: false,
      wseq: 21,
      sourceWseq: 18,
    },
  }), SID);
  assert.equal(parsed.sessionId, SID);
  assert.equal(parsed.cursor?.itemIdx, 2);
  assert.equal(parsed.cursor?.recording, "idle");
  assert.equal(parsed.rapportStep?.sectionKey, "认识机器人");
  assert.equal(Object.hasOwn(parsed.cursor ?? {}, "sourceWseq"), false);
  assert.equal(Object.hasOwn(parsed.rapportStep ?? {}, "sourceWseq"), false);
});

test("runtime parser rejects cross-session, unknown, unsafe-microphone and contradictory data", () => {
  assert.throws(() => parseSessionRuntimeState(runtime({ sessionId: "S-OTHER" }), SID), /严格契约/);
  assert.throws(() => parseSessionRuntimeState({ ...runtime(), injected: true }, SID), /严格契约/);
  assert.throws(() => parseSessionRuntimeState(runtime({ revision: -1 }), SID), /严格契约/);
  assert.throws(() => parseSessionRuntimeState(runtime({ updatedAt: "2026-02-30T01:00:00" }), SID), /严格契约/);
  assert.throws(() => parseSessionRuntimeState(runtime({ updatedAt: "2026-07-19T01:00:00+14:30" }), SID), /严格契约/);
  assert.throws(() => parseSessionRuntimeState(runtime({
    revision: 1,
    cursor: {
      sessionId: SID,
      screen: "record",
      itemIdx: 0,
      turnIdx: 0,
      responseRole: "命名",
      cueLevel: 0,
      recording: "armed",
      selfStart: true,
    },
  }), SID), /安全恢复态/);
  assert.throws(() => parseSessionRuntimeState(runtime({
    status: "paused", revision: 1, pausedAt: null,
  }), SID), /暂停事实/);
  assert.throws(() => parseSessionRuntimeState(runtime({
    status: "active", revision: 3, completedAt: "2026-07-19T01:01:00",
  }), SID), /终态事实/);
});

test("runtime parser validates successful lifecycle assessment receipts before discarding them", () => {
  const intervention = parseSessionRuntimeState(runtime({
    status: "intervention_completed",
    revision: 2,
    interventionCompletedAt: "2026-07-19T01:02:00",
    interventionEndedBy: "R-1",
    interventionAssessment: {
      ready: true,
      expected_turns: 2,
      matched_turns: 2,
      completed_attempt_turns: 2,
      audio_evidenced_turns: 2,
      issues: [],
    },
  }), SID);
  assert.equal(intervention.status, "intervention_completed");
  assert.equal(Object.hasOwn(intervention, "interventionAssessment"), false);

  const completed = parseSessionRuntimeState(runtime({
    status: "completed",
    revision: 3,
    interventionCompletedAt: "2026-07-19T01:02:00",
    interventionEndedBy: "R-1",
    completedAt: "2026-07-19T01:03:00",
    endedBy: "R-2",
    endReason: "completion_gate_passed",
    completionAssessment: {
      ready: true,
      expected_turns: 2,
      matched_turns: 2,
      locked_turns: 2,
      audio_evidenced_turns: 2,
      issues: [],
    },
  }), SID);
  assert.equal(completed.status, "completed");

  assert.throws(() => parseSessionRuntimeState(runtime({
    status: "intervention_completed",
    revision: 2,
    interventionCompletedAt: "2026-07-19T01:02:00",
    interventionEndedBy: "R-1",
    interventionAssessment: {
      ready: true,
      expected_turns: 2,
      matched_turns: 2,
      completed_attempt_turns: 1,
      audio_evidenced_turns: 2,
      issues: [],
    },
  }), SID), /成功状态矛盾/);
});

test("runtime parser accepts the week-1 rapport success receipt and stays strict", () => {
  const rapportSuccess = {
    ready: true,
    protocol: "rapport",
    at_farewell: true,
    recording_idle: true,
    audio_total: 3,
    audio_verified: 3,
    issues: [],
  };
  const intervention = parseSessionRuntimeState(runtime({
    status: "intervention_completed",
    revision: 2,
    interventionCompletedAt: "2026-07-19T01:02:00",
    interventionEndedBy: "R-1",
    interventionAssessment: rapportSuccess,
  }), SID);
  assert.equal(intervention.status, "intervention_completed");

  const completed = parseSessionRuntimeState(runtime({
    status: "completed",
    revision: 3,
    interventionCompletedAt: "2026-07-19T01:02:00",
    interventionEndedBy: "R-1",
    completedAt: "2026-07-19T01:03:00",
    endedBy: "R-2",
    endReason: "completion_gate_passed",
    completionAssessment: rapportSuccess,
  }), SID);
  assert.equal(completed.status, "completed");

  // ready=true 却未停道别/未收麦/核验数不齐 → 拒绝。
  for (const broken of [
    { ...rapportSuccess, at_farewell: false },
    { ...rapportSuccess, recording_idle: false },
    { ...rapportSuccess, audio_verified: 2 },
    { ...rapportSuccess, issues: [{ code: "x", detail: "y", item_id: null, turn_seq: null, response_role: null }] },
  ]) {
    assert.throws(() => parseSessionRuntimeState(runtime({
      status: "intervention_completed",
      revision: 2,
      interventionCompletedAt: "2026-07-19T01:02:00",
      interventionEndedBy: "R-1",
      interventionAssessment: broken,
    }), SID), /关系建立完成门禁响应与成功状态矛盾/);
  }
});
