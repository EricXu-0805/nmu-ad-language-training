import assert from "node:assert/strict";
import test from "node:test";
import { parseSyncMsg, parseSyncPayload } from "./messages.ts";

const session = {
  type: "session", sessionId: "S-1", weekNo: 2, eventLine: "正式训练",
  mode: "task", itemBankVersionId: "v1", wseq: 1,
};
const cursor = {
  type: "cursor", sessionId: "S-1", screen: "record", itemIdx: 0, turnIdx: 1,
  responseRole: "命名", cueLevel: 1, recording: "armed", recSeq: 2, wseq: 3,
};
const rapport = {
  type: "rapportStep", sessionId: "S-1", sectionKey: "自我介绍",
  questionIdx: 0, recording: "idle", recSeq: 1, containsDirectIdentifier: true, wseq: 4,
};
const audioSaved = {
  type: "audioSaved", rawAudioId: "aud-1", durationSeconds: 1.25,
  byteCount: 321, checksum: "a".repeat(64),
  turnKey: "SE_锚#1", sessionId: "S-1", containsDirectIdentifier: false,
};
const patientRec = {
  type: "patientRec", active: true, turnKey: "关系建立·自我介绍", sessionId: "S-1",
};
const patientRecFailure = {
  type: "patientRec", active: false, turnKey: "SE_锚#1", sessionId: "S-1",
  failureCode: "microphone_permission_denied",
  failureId: "123e4567-e89b-42d3-a456-426614174000",
};
const safetyStop = {
  type: "safetyStop", sessionId: "S-1",
};
const patientPauseStop = {
  type: "patientPauseStop", sessionId: "S-1",
  idempotencyKey: "patient_pause:0123456789abcdef0123456789abcdef",
};

test("every SyncMsg variant passes a strict runtime schema", () => {
  for (const message of [
    session, cursor, rapport, audioSaved, patientRec, safetyStop, patientPauseStop,
  ]) {
    assert.deepEqual(parseSyncMsg(message), message);
  }
});

test("every SyncMsg variant rejects extra properties", () => {
  for (const message of [
    session, cursor, rapport, audioSaved, patientRec, safetyStop, patientPauseStop,
  ]) {
    assert.equal(parseSyncMsg({ ...message, injected: "field" }), null);
  }
});

test("safetyStop is a closed session-bound reduction-only bus signal", () => {
  assert.deepEqual(parseSyncMsg(safetyStop), safetyStop);
  assert.equal(parseSyncMsg({ ...safetyStop, sessionId: "S-1\nother" }), null);
  // It is intentionally absent from server live-state payloads.
  assert.equal(parseSyncPayload("cursor", safetyStop), null);
});

test("patientPauseStop is exact-session, non-secret, and strictly shaped", () => {
  assert.deepEqual(parseSyncMsg(patientPauseStop), patientPauseStop);
  assert.equal(parseSyncMsg({
    ...patientPauseStop, idempotencyKey: "patient_pause:not-hex",
  }), null);
  assert.equal(parseSyncMsg({ ...patientPauseStop, capability: "secret" }), null);
});

test("control characters, oversized identifiers and invalid sequence numbers fail closed", () => {
  assert.equal(parseSyncMsg({ ...session, sessionId: `S-1\u0000other` }), null);
  assert.equal(parseSyncMsg({ ...session, itemBankVersionId: "v".repeat(129) }), null);
  assert.equal(parseSyncMsg({ ...cursor, rawAudioId: "../audio" }), null);
  assert.equal(parseSyncMsg({ ...cursor, responseRole: "命名\n注入" }), null);
  for (const invalid of [-1, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.equal(parseSyncMsg({ ...cursor, wseq: invalid }), null);
    assert.equal(parseSyncMsg({ ...cursor, recSeq: invalid }), null);
  }
});

test("audioSaved requires finite bounded duration plus session and known turn-key structure", () => {
  for (const durationSeconds of [-0.1, 21_601, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.equal(parseSyncMsg({ ...audioSaved, durationSeconds }), null);
  }
  const { sessionId: _removed, ...withoutSession } = audioSaved;
  assert.equal(parseSyncMsg(withoutSession), null);
  assert.equal(parseSyncMsg({ ...audioSaved, sessionId: "S".repeat(129) }), null);
  assert.equal(parseSyncMsg({ ...audioSaved, turnKey: "SE_锚" }), null);
  assert.equal(parseSyncMsg({ ...audioSaved, turnKey: "SE_锚#0" }), null);
  assert.equal(parseSyncMsg({ ...audioSaved, containsDirectIdentifier: "false" }), null);
  assert.equal(parseSyncMsg({ ...audioSaved, byteCount: 0 }), null);
  assert.equal(parseSyncMsg({ ...audioSaved, checksum: "not-a-checksum" }), null);
});

test("server live projections are validated without trusting a cast", () => {
  const { type: _type, ...serverAudio } = audioSaved;
  assert.deepEqual(parseSyncPayload("audioSaved", serverAudio), audioSaved);
  assert.equal(parseSyncPayload("audioSaved", audioSaved), null); // payload-only boundary rejects extras
  const { type: _patientType, ...serverPatientRec } = patientRec;
  assert.deepEqual(parseSyncPayload("patientRec", serverPatientRec), patientRec);
});

test("patientRec accepts the closed device-failure projection and a normal inactive state", () => {
  assert.deepEqual(parseSyncMsg(patientRecFailure), patientRecFailure);
  assert.deepEqual(parseSyncMsg({ ...patientRec, active: false }), { ...patientRec, active: false });
  const { type: _type, ...serverFailure } = patientRecFailure;
  assert.deepEqual(parseSyncPayload("patientRec", serverFailure), patientRecFailure);
});

test("patientRec rejects extra, partial, contradictory or unknown failure fields", () => {
  assert.equal(parseSyncMsg({ ...patientRecFailure, injected: true }), null);
  assert.equal(parseSyncMsg({ ...patientRecFailure, active: true }), null);
  assert.equal(parseSyncMsg({ ...patientRecFailure, failureCode: "unknown_failure" }), null);
  assert.equal(parseSyncMsg({ ...patientRecFailure, failureId: "predictable" }), null);
  const { failureCode: _failureCode, ...withoutCode } = patientRecFailure;
  assert.equal(parseSyncMsg(withoutCode), null);
  const { failureId: _failureId, ...withoutId } = patientRecFailure;
  assert.equal(parseSyncMsg(withoutId), null);
});

test("P0-4 回归:服务端诊断键 sourceWseq 会让 session 槽整槽被拒——console-state 必须在服务端剥掉它", () => {
  // 2026-08-21 审计 P0-4 的真实存储载荷:录音入库、收据在账本里,
  // 但 console-state 原样返回带 sourceWseq 的 session_json,这里解析为 null,
  // useAudioSaved 因此永远不去拉收据账本。服务端读边界(_console_live_projection)
  // 剥掉诊断键之后,同一载荷必须能解析成功。
  const stored = {
    sessionId: "s_q1L5S5INYnz6IXzVO_h6W-xy", weekNo: 2, eventLine: "正式训练",
    mode: "task", itemBankVersionId: "wk2-v1-20260707",
    wseq: 1787249821167, sourceWseq: 1787249791385, paused: true,
  };
  assert.equal(parseSyncPayload("session", stored), null);
  const { sourceWseq: _diagnostic, ...projected } = stored;
  const parsed = parseSyncPayload("session", projected);
  assert.equal(parsed?.sessionId, stored.sessionId);
  assert.equal(parsed?.paused, true);
});
