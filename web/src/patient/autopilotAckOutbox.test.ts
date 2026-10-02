import assert from "node:assert/strict";
import test from "node:test";
import {
  checkpointTerminalFailureLatch,
  createAutopilotAckCheckpoint,
  createAutopilotAckEnvelope,
  nextAutopilotAckCheckpoint,
  nextAutopilotAckLatch,
  parseAutopilotAckCheckpoint,
  parseAutopilotAckEnvelope,
  sameAutopilotAckEnvelope,
  type AutopilotAckEnvelope,
} from "./autopilotAckOutbox.ts";
import { buildAutopilotAck, type NextCommandProjection } from "./autopilotProtocol.ts";

const OWNER_KEY = "a".repeat(64);
const SESSION_ID = "S-OUTBOX";

function question(): NextCommandProjection {
  return {
    schema_version: 1,
    command_key: "cmd-outbox-question-0001",
    command_seq: 1,
    kind: "tts",
    state: "pending",
    command_revision: 0,
    control_generation: 1,
    runner_generation: 1,
    item_ref: "itm-0001",
    turn_seq: 1,
    attempt_seq: 1,
    prompt_level: 0,
    payload: {
      speech_key: "wk2.01.question",
      speech_text: "请说出图片中的物品。",
      purpose: "question",
    },
  };
}

function ttsFailedEnvelope(): AutopilotAckEnvelope {
  const command = question();
  const ack = buildAutopilotAck(
    command, 0, "ack:tts_failed:1:00000000-0000-4000-8000-000000000001",
    { ack_type: "tts_failed", error_code: "audio_playback_failed" },
  );
  return createAutopilotAckEnvelope({ ownerKey: OWNER_KEY, sessionId: SESSION_ID, command, ack, nowMs: 1 });
}

function ttsStartedEnvelope(): AutopilotAckEnvelope {
  const command = question();
  const ack = buildAutopilotAck(
    command, 0, "ack:tts_started:1:00000000-0000-4000-8000-000000000002",
    { ack_type: "tts_started" },
  );
  return createAutopilotAckEnvelope({ ownerKey: OWNER_KEY, sessionId: SESSION_ID, command, ack, nowMs: 1 });
}

test("v1 checkpoint（无 latch 字段）继续可读，且迁移读出的 latch 恒为 null", () => {
  const v1 = {
    schemaVersion: 1,
    ownerKey: OWNER_KEY,
    sessionId: SESSION_ID,
    lastDeviceEventSeq: 3,
    updatedAtMs: 100,
  };
  const parsed = parseAutopilotAckCheckpoint(v1);
  assert.equal(parsed.schemaVersion, 1);
  assert.equal(checkpointTerminalFailureLatch(parsed), null);
});

test("v2 checkpoint 往返：latch 完整保留", () => {
  const entry = ttsFailedEnvelope();
  const checkpoint = nextAutopilotAckCheckpoint(entry, 200);
  const roundTripped = parseAutopilotAckCheckpoint(JSON.parse(JSON.stringify(checkpoint)));
  assert.equal(roundTripped.schemaVersion, 2);
  const latch = checkpointTerminalFailureLatch(roundTripped);
  assert.ok(latch);
  assert.equal(latch.commandKey, entry.command.command_key);
  assert.equal(latch.ack.ack_type, "tts_failed");
  assert.equal(latch.ack.device_event_seq, entry.ack.device_event_seq);
});

test("v2 checkpoint 多余字段拒绝", () => {
  const entry = ttsFailedEnvelope();
  const checkpoint = nextAutopilotAckCheckpoint(entry, 200);
  assert.throws(() => parseAutopilotAckCheckpoint({ ...checkpoint, extra: "x" }));
});

test("v2 checkpoint 缺 latch 字段拒绝（v2 必须显式携带，哪怕是 null）", () => {
  const entry = ttsFailedEnvelope();
  const checkpoint = nextAutopilotAckCheckpoint(entry, 200) as Record<string, unknown>;
  const { latch: _latch, ...withoutLatch } = checkpoint;
  assert.throws(() => parseAutopilotAckCheckpoint(withoutLatch));
});

test("latch 的 device_event_seq 必须与 checkpoint.lastDeviceEventSeq 一致，否则拒绝", () => {
  const entry = ttsFailedEnvelope();
  const checkpoint = nextAutopilotAckCheckpoint(entry, 200) as Record<string, unknown>;
  const tampered = { ...checkpoint, lastDeviceEventSeq: (checkpoint.lastDeviceEventSeq as number) + 1 };
  assert.throws(() => parseAutopilotAckCheckpoint(tampered));
});

test("latch 里的 command 与 ack 三元组（generation/revision）对不上时拒绝", () => {
  const entry = ttsFailedEnvelope();
  const checkpoint = nextAutopilotAckCheckpoint(entry, 200) as Record<string, unknown>;
  const latch = checkpoint.latch as Record<string, unknown>;
  const tamperedAck = { ...(latch.ack as Record<string, unknown>), command_revision: 99 };
  const tampered = { ...checkpoint, latch: { ...latch, ack: tamperedAck } };
  assert.throws(() => parseAutopilotAckCheckpoint(tampered));
});

test("latch 里的 ack_type 若不是 tts_failed/record_failed 一律拒绝", () => {
  const startedEntry = ttsStartedEnvelope();
  const checkpoint = createAutopilotAckCheckpoint({
    ownerKey: OWNER_KEY, sessionId: SESSION_ID, lastDeviceEventSeq: startedEntry.ack.device_event_seq,
  }) as Record<string, unknown>;
  const forged = {
    ...checkpoint,
    latch: { commandKey: startedEntry.commandKey, command: startedEntry.command, ack: startedEntry.ack },
  };
  assert.throws(() => parseAutopilotAckCheckpoint(forged));
});

test("跨 owner/session 的 checkpoint 由 blobStore 层拒绝（此处验证字段本身各自合法但不互相校验归属）", () => {
  // parseAutopilotAckCheckpoint 只校验形状；owner/session 与调用方期望值的比对属于
  // blobStore.ts 的调用点职责（autopilotAckSnapshot / stageAutopilotAck 等）。
  const parsed = parseAutopilotAckCheckpoint({
    schemaVersion: 1, ownerKey: OWNER_KEY, sessionId: SESSION_ID, lastDeviceEventSeq: 1, updatedAtMs: 1,
  });
  assert.equal(parsed.ownerKey, OWNER_KEY);
  assert.equal(parsed.sessionId, SESSION_ID);
});

test("nextAutopilotAckLatch：tts_failed/record_failed 落 latch，其余全部清空", () => {
  const failed = ttsFailedEnvelope();
  const latch = nextAutopilotAckLatch(failed);
  assert.ok(latch);
  assert.equal(latch.ack.ack_type, "tts_failed");

  const started = ttsStartedEnvelope();
  assert.equal(nextAutopilotAckLatch(started), null);
});

function startedRecord(): NextCommandProjection {
  return {
    schema_version: 1,
    command_key: "cmd-outbox-record-0002",
    command_seq: 2,
    kind: "record",
    state: "started",
    command_revision: 1,
    control_generation: 1,
    runner_generation: 1,
    item_ref: "itm-0001",
    turn_seq: 1,
    attempt_seq: 1,
    prompt_level: 0,
    payload: {
      raw_audio_id: "raw-outbox-0001",
      turn_ref: "itm-0001#1",
      max_duration_seconds: 15,
      contains_direct_identifier: false,
      presentation_speech_key: "wk2.01.question",
      presentation_speech_text: "请说出图片中的物品。",
      presentation_purpose: "question",
    },
  };
}

test("IndexedDB 里升级前的 record_stopped envelope(无 VAD 两键)照样能重放;新 envelope 带上它们也合法", () => {
  const command = startedRecord();
  const receipt = {
    ack_type: "record_stopped",
    stop_reason: "user_done",
    raw_audio_id: "raw-outbox-0001",
    receipt_server_seq: 12,
    checksum: "c".repeat(64),
    byte_count: 4_096,
    duration_seconds: 3.5,
  } as const;
  const historicalAck = {
    idempotency_key: "ack:record_stopped:5:00000000-0000-4000-8000-000000000005",
    control_generation: 1,
    runner_generation: 1,
    command_revision: 1,
    device_event_seq: 5,
    ...receipt,
  };
  // 结构化克隆读回来的就是这样一份 plain 对象,没有任何新键。
  const stored = JSON.parse(JSON.stringify({
    schemaVersion: 1, ownerKey: OWNER_KEY, sessionId: SESSION_ID,
    commandKey: command.command_key, command, ack: historicalAck, createdAtMs: 7,
  }));
  const replayed = parseAutopilotAckEnvelope(stored);
  assert.deepEqual(replayed.ack, historicalAck);
  assert.equal(sameAutopilotAckEnvelope(stored, replayed), true);

  const current = createAutopilotAckEnvelope({
    ownerKey: OWNER_KEY, sessionId: SESSION_ID, command, nowMs: 7,
    ack: buildAutopilotAck(command, 4, historicalAck.idempotency_key, {
      ...receipt, vad_armed: true, vad_voiced_ms: 1_280,
    }),
  });
  assert.deepEqual(parseAutopilotAckEnvelope(JSON.parse(JSON.stringify(current))), current);
  // 多了两个事实就是另一份 envelope:同 key 重放时逐字对齐会把它们区分开。
  assert.equal(sameAutopilotAckEnvelope(stored, current), false);
});
