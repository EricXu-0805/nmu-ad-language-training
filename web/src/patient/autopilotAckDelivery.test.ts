import assert from "node:assert/strict";
import test from "node:test";
import type { DeviceCapabilityRecord } from "../security/deviceCapability.ts";
import {
  DurableAutopilotAckDelivery,
  parseAutopilotAckReceipt,
  type AutopilotAckStore,
} from "./autopilotAckDelivery.ts";
import {
  createAutopilotAckEnvelope,
  type AutopilotAckCheckpoint,
  type AutopilotAckEnvelope,
} from "./autopilotAckOutbox.ts";
import { buildAutopilotAck, type AutopilotAck, type NextCommandProjection } from "./autopilotProtocol.ts";

const capability: DeviceCapabilityRecord = {
  capability: "x".repeat(43),
  sessionId: "S-ACK",
  expiresAt: "2026-07-19T12:00:00Z",
};

function question(): NextCommandProjection {
  return {
    schema_version: 1,
    command_key: "cmd-ack-delivery-question-0001",
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

function startedRecord(): NextCommandProjection {
  return {
    schema_version: 1,
    command_key: "cmd-ack-delivery-record-0002",
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
      raw_audio_id: "raw-ack-delivery-record-0001",
      turn_ref: "itm-0001#1",
      max_duration_seconds: 12,
      contains_direct_identifier: false,
      presentation_speech_key: "ack.question",
      presentation_speech_text: "请说出图片中的物品。",
      presentation_purpose: "question",
    },
  };
}

function receipt(ack: AutopilotAck, replayed: boolean): unknown {
  return {
    scope_key: "p0a_sim_first_single_v1",
    ack_idempotency_key: ack.idempotency_key,
    ack_type: ack.ack_type,
    replayed,
    command_state: "started",
    command_revision: 1,
    status: "waiting_tts",
    state_revision: 2,
    command: null,
  };
}

class MemoryAckStore implements AutopilotAckStore {
  pending: AutopilotAckEnvelope | null = null;
  checkpoint: AutopilotAckCheckpoint | null = null;
  events: string[] = [];

  async snapshot() { return { pending: this.pending, checkpoint: this.checkpoint }; }
  async stage(entry: AutopilotAckEnvelope) {
    this.events.push("stage");
    if (this.pending && JSON.stringify(this.pending) !== JSON.stringify(entry)) throw new Error("conflict");
    this.pending = entry;
  }
  async stageRecordStoppedAndCompleteAudio(entry: AutopilotAckEnvelope) {
    this.events.push("stage-record");
    this.pending = entry;
  }
  async complete(entry: AutopilotAckEnvelope) {
    this.events.push("complete");
    assert.deepEqual(this.pending, entry);
    this.checkpoint = {
      schemaVersion: 1,
      ownerKey: entry.ownerKey,
      sessionId: entry.sessionId,
      lastDeviceEventSeq: entry.ack.device_event_seq,
      updatedAtMs: Date.now(),
    };
    this.pending = null;
  }
}

test("response loss is recovered by retransmitting the exact persisted ACK", async () => {
  const store = new MemoryAckStore();
  let sent: AutopilotAck | null = null;
  const first = await DurableAutopilotAckDelivery.create({
    capability,
    store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => {
        store.events.push("http-lost");
        sent = ack;
        throw new Error("response lost");
      },
    },
  });
  await assert.rejects(() => first.send(question(), 0, { ack_type: "tts_started" }));
  assert.ok(sent);
  assert.equal(store.pending?.ack.idempotency_key, sent.idempotency_key);
  assert.equal(store.pending?.ack.device_event_seq, 1);
  assert.deepEqual(store.events, ["stage", "http-lost"]);

  const recovered = await DurableAutopilotAckDelivery.create({
    capability,
    store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => {
        store.events.push("http-retry");
        assert.deepEqual(ack, sent);
        return receipt(ack, true);
      },
    },
  });
  const drained = await recovered.drainPending();
  assert.deepEqual(drained, sent);
  assert.equal(recovered.initialDeviceEventSeq, 1);
  assert.equal(store.pending, null);
  assert.deepEqual(store.events, ["stage", "http-lost", "http-retry", "complete"]);
});

test("receipt validation rejects a successful-looking response for another ACK", () => {
  const command = question();
  const ack = buildAutopilotAck(
    command, 0, "ack:tts_started:1:00000000-0000-4000-8000-000000000001",
    { ack_type: "tts_started" },
  );
  const envelope = createAutopilotAckEnvelope({
    ownerKey: "a".repeat(64),
    sessionId: "S-ACK",
    command,
    ack,
    nowMs: 1,
  });
  assert.throws(() => parseAutopilotAckReceipt({
    ...(receipt(ack, false) as Record<string, unknown>),
    ack_idempotency_key: "ack:other:1:00000000-0000-4000-8000-000000000001",
  }, envelope));
});

test("receipt validation binds lifecycle state and status to the exact command", () => {
  const command = question();
  const ack = buildAutopilotAck(
    command, 0, "ack:tts_started:1:00000000-0000-4000-8000-000000000001",
    { ack_type: "tts_started" },
  );
  const envelope = createAutopilotAckEnvelope({
    ownerKey: "a".repeat(64),
    sessionId: "S-ACK",
    command,
    ack,
    nowMs: 1,
  });
  const valid = {
    ...(receipt(ack, false) as Record<string, unknown>),
    command,
  };
  assert.equal(parseAutopilotAckReceipt(valid, envelope).command?.kind, "tts");
  assert.throws(() => parseAutopilotAckReceipt({
    ...valid,
    command_state: "succeeded",
    command_revision: 2,
  }, envelope), /本次命令转移/);
  assert.throws(() => parseAutopilotAckReceipt({
    ...valid,
    status: "processing_attempt",
  }, envelope), /不得携带命令/);
  assert.throws(() => parseAutopilotAckReceipt({
    ...valid,
    replayed: true,
  }, envelope), /重放不得投影/);
});

test("record_stopped uses the atomic audio handoff before HTTP", async () => {
  const store = new MemoryAckStore();
  const delivery = await DurableAutopilotAckDelivery.create({
    capability,
    store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => {
        store.events.push("http");
        return {
          ...(receipt(ack, false) as Record<string, unknown>),
          command_state: "succeeded",
          command_revision: 2,
          status: "processing_attempt",
        };
      },
    },
  });
  await delivery.send(startedRecord(), 0, {
    ack_type: "record_stopped",
    stop_reason: "user_done",
    raw_audio_id: "raw-ack-delivery-record-0001",
    receipt_server_seq: 9,
    checksum: "b".repeat(64),
    byte_count: 1024,
    duration_seconds: 1.25,
  });
  assert.deepEqual(store.events, ["stage-record", "http", "complete"]);
});
