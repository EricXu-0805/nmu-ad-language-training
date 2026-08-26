import assert from "node:assert/strict";
import test from "node:test";
import type { DeviceCapabilityRecord } from "../security/deviceCapability.ts";
import {
  DurableAutopilotAckDelivery,
  parseAutopilotAckReceipt,
  type AutopilotAckStore,
} from "./autopilotAckDelivery.ts";
import {
  createAutopilotAckCheckpoint,
  createAutopilotAckEnvelope,
  fingerprintAutopilotCapability,
  nextAutopilotAckCheckpoint,
  type AutopilotAckCheckpoint,
  type AutopilotAckEnvelope,
} from "./autopilotAckOutbox.ts";
import {
  AutopilotAckPersistenceError,
  type AutopilotAckPersistenceIdentity,
} from "./autopilotController.ts";
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

function failureReceipt(ack: AutopilotAck): unknown {
  return {
    scope_key: "p0a_sim_first_single_v1",
    ack_idempotency_key: ack.idempotency_key,
    ack_type: ack.ack_type,
    replayed: false,
    command_state: "failed",
    command_revision: ack.command_revision + 1,
    status: "paused",
    state_revision: 2,
    command: null,
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
    this.checkpoint = nextAutopilotAckCheckpoint(entry, 1);
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
  // envelope 而不是裸 ack：调用方要靠里面那条 exact command 恢复本地暂停态。
  assert.deepEqual(drained?.ack, sent);
  assert.equal(drained?.commandKey, question().command_key);
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

// ---------------- 持久终态失败 latch ----------------

test("tts_failed complete 后持久 latch，新建 delivery（模拟刷新）读到同一份 exact 证据", async () => {
  const store = new MemoryAckStore();
  const command = question();
  const delivery = await DurableAutopilotAckDelivery.create({
    capability,
    store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => failureReceipt(ack),
    },
  });
  const ack = await delivery.send(command, 0, {
    ack_type: "tts_failed", error_code: "audio_playback_failed",
  });
  assert.ok(delivery.terminalFailureLatch);
  assert.equal(delivery.terminalFailureLatch?.ack.idempotency_key, ack.idempotency_key);
  assert.equal(delivery.terminalFailureLatch?.commandKey, command.command_key);

  const reloaded = await DurableAutopilotAckDelivery.create({ capability, store, transport: { next: async () => null, ack: async () => { throw new Error("不应再次发送"); } } });
  assert.deepEqual(reloaded.terminalFailureLatch, delivery.terminalFailureLatch);
});

test("非失败 ACK complete 会清空此前持久的 latch", async () => {
  const store = new MemoryAckStore();
  const failed = await DurableAutopilotAckDelivery.create({
    capability, store,
    transport: { next: async () => null, ack: async (_sid, _key, ack) => failureReceipt(ack) },
  });
  await failed.send(question(), 0, { ack_type: "tts_failed", error_code: "audio_playback_failed" });
  assert.ok(store.checkpoint && store.checkpoint.schemaVersion === 2 && store.checkpoint.latch);

  // 新命令（不同 key/更高代际）走完 tts_started：非失败 ACK 必须原子清掉旧 latch。
  const nextCommand = {
    ...question(),
    command_key: "cmd-ack-delivery-question-0002",
    command_seq: 2,
    control_generation: 2,
    runner_generation: 2,
  };
  const resumed = await DurableAutopilotAckDelivery.create({
    capability, store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => ({
        ...(receipt(ack, false) as Record<string, unknown>),
        status: "processing_attempt",
      }),
    },
  });
  await resumed.send(nextCommand, 1, { ack_type: "tts_started" });
  assert.equal(resumed.terminalFailureLatch, null);
  assert.ok(store.checkpoint && store.checkpoint.schemaVersion === 2 && store.checkpoint.latch === null);
});

test("latch 已存在时，未证明严格新权威的 send 在任何 stage/音频接管前被拒绝（stage-record 调用 0 次）", async () => {
  const store = new MemoryAckStore();
  const delivery = await DurableAutopilotAckDelivery.create({
    capability, store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => { store.events.push("http"); return failureReceipt(ack); },
    },
  });
  await delivery.send(question(), 0, { ack_type: "tts_failed", error_code: "audio_playback_failed" });
  assert.ok(delivery.terminalFailureLatch);
  const eventsBeforeStaleAttempt = [...store.events];

  // startedRecord() 的 seq 比 latch 高，但代际没有严格双双前进——不构成新权威。
  await assert.rejects(() => delivery.send(startedRecord(), 1, {
    ack_type: "record_stopped",
    stop_reason: "user_done",
    raw_audio_id: "raw-ack-delivery-record-0001",
    receipt_server_seq: 9,
    checksum: "b".repeat(64),
    byte_count: 1024,
    duration_seconds: 1.25,
  }), /严格新权威/);

  assert.deepEqual(store.events, eventsBeforeStaleAttempt);
  assert.equal(store.events.includes("stage-record"), false);
  assert.ok(delivery.terminalFailureLatch);   // latch 原样保留，没被半途清掉
});

test("durable stage 证明之前：transport.ack 调用 0 次、seq 0 推进、第二条 send 被挡", async () => {
  const store = new MemoryAckStore();
  let releaseStage!: () => void;
  const stageGate = new Promise<void>((resolve) => { releaseStage = resolve; });
  let transportCalls = 0;
  const gated: AutopilotAckStore = {
    snapshot: () => store.snapshot(),
    stage: async (entry) => { store.events.push("stage"); await stageGate; store.pending = entry; },
    stageRecordStoppedAndCompleteAudio: (entry) =>
      store.stageRecordStoppedAndCompleteAudio(entry),
    complete: (entry) => store.complete(entry),
  };
  const delivery = await DurableAutopilotAckDelivery.create({
    capability,
    store: gated,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => {
        transportCalls += 1;
        // 非重放的 waiting_tts 收据必须投影这条命令的 started 版本。缺了它严格
        // 解析器会（正确地）拒收——那是假证据，不是"通过"。只改这一处，重放与
        // 非媒体等待状态仍然共用原来的 command:null 助手。
        return {
          ...(receipt(ack, false) as Record<string, unknown>),
          command: { ...question(), state: "started", command_revision: 1 },
        };
      },
    },
  });

  const sending = delivery.send(question(), 0, { ack_type: "tts_started" });
  await Promise.resolve();
  await Promise.resolve();

  // stage 还没落定：网络一次都没碰，checkpoint 序号也一步没推。
  assert.equal(transportCalls, 0);
  assert.equal(delivery.initialDeviceEventSeq, 0);
  assert.equal(delivery.stagingInFlight, true);
  // 这一小段窗口里第二条 send 必须被挡住，否则两条 ACK 会抢同一个事件序号。
  await assert.rejects(
    () => delivery.send(startedRecord(), 0, {
      ack_type: "record_failed", error_code: "recording_runtime_failed",
    }),
    /禁止生成新事件序号/);

  releaseStage();
  await sending;
  assert.equal(transportCalls, 1);
  assert.equal(delivery.stagingInFlight, false);
});

test("确认之后锁存服务器权威 command：绝不由客户端合成 revision", async () => {
  const store = new MemoryAckStore();
  const command = startedRecord();
  const delivery = await DurableAutopilotAckDelivery.create({
    capability, store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => ({
        ...(receipt(ack, false) as Record<string, unknown>),
        command_state: "started",
        command_revision: 2,
        status: "waiting_recording",
        command: { ...command, command_revision: 2 },
      }),
    },
  });
  assert.equal(delivery.lastReceiptAuthority, null);

  // record_started 的 mime_type 是封闭契约里的必填事实，不是可选装饰：漏掉它
  // buildAutopilotAck 会在任何 stage/传输之前就拒绝这条 ACK。
  const ack = await delivery.send(command, 0, {
    ack_type: "record_started", mime_type: "audio/webm",
  });

  const authority = delivery.lastReceiptAuthority;
  assert.equal(authority?.commandKey, command.command_key);
  assert.equal(authority?.ackIdempotencyKey, ack.idempotency_key);
  assert.equal(authority?.ackType, "record_started");
  assert.equal(authority?.commandRevision, 2);
  assert.equal(authority?.command?.kind, "record");
  assert.equal(authority?.command?.state, "started");
  assert.equal(authority?.command?.command_revision, 2);
  // 收据 revision 与投影 revision 必须是服务器返回的同一个数，不是本地拼的。
  assert.equal(authority?.commandRevision, authority?.command?.command_revision);
});

test("exact 重放不投影命令时，权威 command 为 null——调用方只能安全暂停", async () => {
  const store = new MemoryAckStore();
  const delivery = await DurableAutopilotAckDelivery.create({
    capability, store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => receipt(ack, true),
    },
  });
  await delivery.send(question(), 0, { ack_type: "tts_started" });

  assert.equal(delivery.lastReceiptAuthority?.command, null);
  assert.equal(delivery.lastReceiptAuthority?.commandKey, question().command_key);
});

// ---------------- 持久化结算：stage / complete 的 matching / empty / unknown ----------------

/** 深拷贝：模拟 IndexedDB 的结构化克隆，绝不把内存对象原样交回去。 */
function deepClone<T>(value: T): T {
  return value === null || value === undefined
    ? value : JSON.parse(JSON.stringify(value)) as T;
}

type StoreOutcome = "ok" | "commit-then-throw" | "throw";

/**
 * 结算用 store 替身。
 *
 * snapshot 永远交回全新深拷贝，所以任何用引用相等去判 envelope/checkpoint/latch/
 * command/ACK 的实现都会在这些用例里失败。
 */
class SettlementStore implements AutopilotAckStore {
  pending: AutopilotAckEnvelope | null = null;
  checkpoint: AutopilotAckCheckpoint | null = null;
  events: string[] = [];
  /**
   * 投递真正交给 stage() 的那个 envelope 的**活引用**（不是深拷贝）。
   *
   * 不可变性 oracle 要拿它去改 command / payload / ACK 的原对象，证明错误里的
   * 那份脱离快照纹丝不动。深拷贝会把这条证据链断掉。
   */
  stagedEntries: AutopilotAckEnvelope[] = [];
  stageOutcome: StoreOutcome = "ok";
  completeOutcome: StoreOutcome = "ok";
  snapshotThrows = false;
  snapshotGate: Promise<void> | null = null;
  completeGate: Promise<void> | null = null;

  async snapshot(): Promise<{
    pending: AutopilotAckEnvelope | null; checkpoint: AutopilotAckCheckpoint | null;
  }> {
    this.events.push("snapshot");
    if (this.snapshotGate) await this.snapshotGate;
    if (this.snapshotThrows) throw new Error("本机 ACK 存储不可读");
    return { pending: deepClone(this.pending), checkpoint: deepClone(this.checkpoint) };
  }

  private applyStage(entry: AutopilotAckEnvelope, label: string): void {
    this.events.push(label);
    this.stagedEntries.push(entry);
    if (this.stageOutcome !== "throw") this.pending = deepClone(entry);
    if (this.stageOutcome !== "ok") throw new Error("暂存事务中止");
  }

  async stage(entry: AutopilotAckEnvelope): Promise<void> {
    this.applyStage(entry, "stage");
  }

  async stageRecordStoppedAndCompleteAudio(entry: AutopilotAckEnvelope): Promise<void> {
    this.applyStage(entry, "stage-record");
  }

  /** 迁移读场景：提交了，但落下的是不记 latch 的 schema v1 checkpoint。 */
  completeWritesLegacyV1 = false;

  /**
   * 落 checkpoint 之后的**单变量**扰动。
   *
   * complete-catch 的矩阵每一行只允许动一个字段，其余全部由
   * `nextAutopilotAckCheckpoint(entry, 1)` 生成——手写整份字面量会让"其余字段是不是
   * 也漂了"变得不可核对，那正是旧用例的毛病。
   */
  completeCheckpointMutate:
    ((checkpoint: AutopilotAckCheckpoint) => AutopilotAckCheckpoint) | null = null;

  async complete(entry: AutopilotAckEnvelope): Promise<void> {
    this.events.push("complete");
    if (this.completeGate) await this.completeGate;
    if (this.completeOutcome !== "throw") {
      const written = this.completeWritesLegacyV1
        ? deepClone({
          schemaVersion: 1,
          ownerKey: entry.ownerKey,
          sessionId: entry.sessionId,
          lastDeviceEventSeq: entry.ack.device_event_seq,
          updatedAtMs: 1,
        } as AutopilotAckCheckpoint)
        : deepClone(nextAutopilotAckCheckpoint(entry, 1));
      this.checkpoint = this.completeCheckpointMutate
        ? deepClone(this.completeCheckpointMutate(written))
        : written;
      this.pending = null;
    }
    if (this.completeOutcome !== "ok") throw new Error("完成事务中止");
  }
}

function ttsStartedReceipt(ack: AutopilotAck): unknown {
  return {
    ...(receipt(ack, false) as Record<string, unknown>),
    command: { ...question(), state: "started", command_revision: 1 },
  };
}

interface SettlementHarness {
  store: SettlementStore;
  delivery: DurableAutopilotAckDelivery;
  httpCalls: number;
  keys: string[];
}

async function settlementHarness(options: {
  respond?: (ack: AutopilotAck) => unknown;
  transportGate?: Promise<void>;
  transportThrows?: boolean;
  /** 在真构造器读初始 snapshot **之前**播种 store：起始 lastSeq/latch 只能这么来。 */
  seed?: (store: SettlementStore) => void;
} = {}): Promise<SettlementHarness> {
  const store = new SettlementStore();
  options.seed?.(store);
  const state = { httpCalls: 0, keys: [] as string[] };
  const delivery = await DurableAutopilotAckDelivery.create({
    capability,
    store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => {
        state.httpCalls += 1;
        state.keys.push(ack.idempotency_key);
        store.events.push("http");
        if (options.transportGate) await options.transportGate;
        if (options.transportThrows) throw new Error("响应丢失");
        return (options.respond ?? ttsStartedReceipt)(ack);
      },
    },
  });
  // 真构造器正确地读了一次 owner-scoped 初始 snapshot，SettlementStore 把它记进了
  // events。结算 oracle 的期望序列是从候选动作开始数的，所以这里只清事件账本：
  // 构造快照照跑不误，pending / checkpoint / stageOutcome / completeOutcome 全不动。
  //
  // 清之前先把被清掉的那一段钉死，否则"构造器到底读了几次 snapshot"就没人看着了：
  // 读两次、或者顺手 stage/complete 一次的构造器，会在这一行翻红而不是被账本清理
  // 掩盖过去。
  assert.deepEqual(store.events, ["snapshot"]);
  store.events.length = 0;
  return {
    store,
    delivery,
    get httpCalls() { return state.httpCalls; },
    get keys() { return state.keys; },
  } as SettlementHarness;
}

async function caught(run: () => Promise<unknown>): Promise<unknown> {
  try { await run(); }
  catch (error) { return error; }
  throw new Error("预期该调用失败，但它成功了");
}

/** 归属先证明再读内容：认领不过就直接判死，绝不拿错误自己的字段自证。 */
function ownedFailure(
  delivery: DurableAutopilotAckDelivery,
  error: unknown,
): AutopilotAckPersistenceError {
  if (!delivery.ownsPersistenceError(error)) {
    throw new Error("这条拒绝不是该 delivery 造出来的 typed 持久化错误");
  }
  return error;
}

test("stage 提交后抛错、snapshot 逐字匹配：同一个 key 继续，restage 0 次", async () => {
  const harness = await settlementHarness();
  harness.store.stageOutcome = "commit-then-throw";

  const ack = await harness.delivery.send(question(), 0, { ack_type: "tts_started" });

  // stage 恰好一次、snapshot 一次、之后照常 transport 与 complete 各一次。
  assert.deepEqual(harness.store.events, ["stage", "snapshot", "http", "complete"]);
  assert.equal(harness.httpCalls, 1);
  assert.deepEqual(harness.keys, [ack.idempotency_key]);
  assert.equal(harness.delivery.initialDeviceEventSeq, 1);
  assert.equal(harness.delivery.lastReceiptAuthority?.ackIdempotencyKey, ack.idempotency_key);
  assert.equal(harness.delivery.unresolvedSettlement, null);
});

test("stage matching 之后 HTTP 失败：exact pending 保留，seq/latch/权威仍是旧值", async () => {
  const harness = await settlementHarness({ transportThrows: true });
  harness.store.stageOutcome = "commit-then-throw";

  await assert.rejects(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }),
    /响应丢失/);

  assert.equal(harness.store.events.includes("complete"), false);
  assert.equal(harness.delivery.initialDeviceEventSeq, 0);
  assert.equal(harness.delivery.terminalFailureLatch, null);
  assert.equal(harness.delivery.lastReceiptAuthority, null);
  assert.equal(harness.delivery.unresolvedSettlement, null);
  assert.ok(harness.store.pending);
});

test("严格收据被拒：complete 0 次、只松瞬时闸，之后 drain 复用同一个 key", async () => {
  // 收据缺少 waiting_tts 该有的命令投影：严格解析器（正确地）拒收。
  const harness = await settlementHarness({ respond: (ack) => receipt(ack, false) });

  await assert.rejects(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }),
    /服务器 ACK 状态与 TTS 命令不一致/);

  assert.equal(harness.store.events.includes("complete"), false);
  assert.equal(harness.delivery.initialDeviceEventSeq, 0);
  assert.equal(harness.delivery.lastReceiptAuthority, null);
  // 这既不是 stage empty 也不是 complete empty：它授权不了任何替换，
  // 之后的 drain 只能重放同一条 envelope 与同一个 key。
  assert.equal(harness.delivery.unresolvedSettlement, null);
  const firstKey = harness.keys[0];
  await assert.rejects(() => harness.delivery.drainPending(),
    /服务器 ACK 状态与 TTS 命令不一致/);
  assert.deepEqual(harness.keys, [firstKey, firstKey]);
});

test("stage 未提交、snapshot 证明为空：typed confirmed_empty，transport/complete 0 次", async () => {
  const harness = await settlementHarness();
  harness.store.stageOutcome = "throw";

  const error = await caught(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }));

  const failure = ownedFailure(harness.delivery, error);
  assert.equal(failure.phase, "stage");
  assert.equal(failure.outcome, "confirmed_empty");
  assert.equal(failure.identity.ackType, "tts_started");
  assert.equal(failure.identity.deviceEventSeq, 1);
  assert.equal(failure.identity.commandKey, question().command_key);
  assert.equal(harness.httpCalls, 0);
  assert.deepEqual(harness.store.events, ["stage", "snapshot"]);
  assert.equal(harness.delivery.initialDeviceEventSeq, 0);
  assert.equal(harness.delivery.terminalFailureLatch, null);
  assert.equal(harness.delivery.lastReceiptAuthority, null);
  // 被丢弃的候选闸已松开：调用方可以决定那条唯一的同 seq 替换。
  assert.equal(harness.delivery.unresolvedSettlement, null);
});

test("stage snapshot 未定型期间：并发 send 与 drain 全部挡住，零 HTTP、零 complete、零新 key", async () => {
  const harness = await settlementHarness();
  harness.store.stageOutcome = "throw";
  let release!: () => void;
  harness.store.snapshotGate = new Promise<void>((resolve) => { release = resolve; });

  const first = harness.delivery.send(question(), 0, { ack_type: "tts_started" });
  first.catch(() => {});
  for (let turn = 0; turn < 20; turn += 1) await Promise.resolve();

  await assert.rejects(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }),
    /禁止生成新事件序号/);
  await assert.rejects(() => harness.delivery.drainPending(), /禁止重放/);
  assert.equal(harness.httpCalls, 0);
  assert.equal(harness.store.events.includes("complete"), false);
  assert.deepEqual(harness.keys, []);

  release();
  await assert.rejects(() => first);
});

for (const negative of [
  { name: "snapshot 不可读", apply: (store: SettlementStore) => { store.snapshotThrows = true; } },
  {
    name: "外来 pending",
    apply: (store: SettlementStore) => {
      store.pending = deepClone(createAutopilotAckEnvelope({
        ownerKey: "a".repeat(64),
        sessionId: "S-ACK",
        command: startedRecord(),
        ack: buildAutopilotAck(startedRecord(), 0, "ack:record_failed:1:foreign", {
          ack_type: "record_failed", error_code: "recording_runtime_failed",
        }),
        nowMs: 1,
      }));
    },
  },
  {
    name: "checkpoint 与当前语义不符（owner 与 seq 都对不上）",
    apply: (store: SettlementStore) => {
      store.checkpoint = deepClone(createAutopilotAckCheckpoint({
        ownerKey: "a".repeat(64), sessionId: "S-ACK", lastDeviceEventSeq: 5, nowMs: 1,
      }));
    },
  },
  {
    name: "checkpoint owner 漂移",
    apply: (store: SettlementStore) => {
      store.checkpoint = deepClone(createAutopilotAckCheckpoint({
        ownerKey: "c".repeat(64), sessionId: "S-ACK", lastDeviceEventSeq: 0, nowMs: 1,
      }));
    },
  },
] as Array<{ name: string; apply: (store: SettlementStore) => void }>) {
  test(`stage unknown（${negative.name}）：粘滞保留 exact identity，之后 send/drain 零网络零新 key`, async () => {
    const harness = await settlementHarness();
    harness.store.stageOutcome = "throw";
    negative.apply(harness.store);

    const error = await caught(
      () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }));

    const failure = ownedFailure(harness.delivery, error);
    assert.equal(failure.phase, "stage");
    assert.equal(failure.outcome, "unknown");
    assert.equal(failure.identity.commandKey, question().command_key);
    assert.equal(harness.httpCalls, 0);
    assert.equal(harness.delivery.unresolvedSettlement, error);

    // 粘滞：同页再 send/drain 一律 fail closed，且不产生第二个 key。
    assert.equal(await caught(
      () => harness.delivery.send(question(), 0, { ack_type: "tts_started" })), error);
    assert.equal(await caught(() => harness.delivery.drainPending()), error);
    assert.equal(harness.httpCalls, 0);
    assert.deepEqual(harness.keys, []);
    assert.equal(harness.store.events.includes("complete"), false);
  });
}

test("record_stopped 提交后抛错：原子接管恰好 1 次、通用 stage 0 次，同一 envelope 走完 transport/complete", async () => {
  const harness = await settlementHarness({
    respond: (ack) => ({
      ...(receipt(ack, false) as Record<string, unknown>),
      command_state: "succeeded",
      command_revision: 2,
      status: "processing_attempt",
      command: null,
    }),
  });
  harness.store.stageOutcome = "commit-then-throw";

  const ack = await harness.delivery.send(startedRecord(), 0, {
    ack_type: "record_stopped",
    stop_reason: "user_done",
    raw_audio_id: "raw-ack-delivery-record-0001",
    receipt_server_seq: 9,
    checksum: "b".repeat(64),
    byte_count: 1024,
    duration_seconds: 1.25,
  });

  assert.deepEqual(harness.store.events, ["stage-record", "snapshot", "http", "complete"]);
  assert.equal(harness.store.events.filter((row) => row === "stage-record").length, 1);
  assert.equal(harness.store.events.includes("stage"), false);
  assert.equal(harness.httpCalls, 1);
  assert.deepEqual(harness.keys, [ack.idempotency_key]);
});

test("record_stopped empty/unknown：原子 stage 恰好 1 次，restage/transport/complete 全 0", async () => {
  for (const mode of ["empty", "unknown"] as const) {
    const harness = await settlementHarness();
    harness.store.stageOutcome = "throw";
    if (mode === "unknown") harness.store.snapshotThrows = true;

    const error = await caught(() => harness.delivery.send(startedRecord(), 0, {
      ack_type: "record_stopped",
      stop_reason: "user_done",
      raw_audio_id: "raw-ack-delivery-record-0001",
      receipt_server_seq: 9,
      checksum: "b".repeat(64),
      byte_count: 1024,
      duration_seconds: 1.25,
    }));

    const failure = ownedFailure(harness.delivery, error);
    assert.equal(failure.outcome, mode === "empty" ? "confirmed_empty" : "unknown");
    assert.equal(harness.store.events.filter((row) => row === "stage-record").length, 1);
    assert.equal(harness.store.events.includes("stage"), false);
    assert.equal(harness.store.events.includes("complete"), false);
    assert.equal(harness.httpCalls, 0);
  }
});

test("complete 提交后抛错（失败 ACK）：原 ACK 正常返回，seq/latch/权威恰好按 exact v2 latch 更新一次", async () => {
  const harness = await settlementHarness({ respond: (ack) => failureReceipt(ack) });
  harness.store.completeOutcome = "commit-then-throw";
  const command = question();

  const ack = await harness.delivery.send(command, 0, {
    ack_type: "tts_failed", error_code: "audio_playback_failed",
  });

  assert.equal(ack.ack_type, "tts_failed");
  assert.deepEqual(harness.store.events, ["stage", "http", "complete", "snapshot"]);
  assert.equal(harness.httpCalls, 1);
  assert.equal(harness.delivery.initialDeviceEventSeq, 1);
  assert.equal(harness.delivery.terminalFailureLatch?.commandKey, command.command_key);
  assert.equal(harness.delivery.terminalFailureLatch?.ack.idempotency_key, ack.idempotency_key);
  assert.equal(harness.delivery.lastReceiptAuthority?.ackIdempotencyKey, ack.idempotency_key);
  assert.equal(harness.delivery.unresolvedSettlement, null);
});

test("complete 提交后抛错（非失败 ACK）：latch 必须为 null，无关 latch 证明不了完成", async () => {
  const harness = await settlementHarness();
  harness.store.completeOutcome = "commit-then-throw";

  const ack = await harness.delivery.send(question(), 0, { ack_type: "tts_started" });
  assert.equal(harness.delivery.terminalFailureLatch, null);
  assert.equal(harness.delivery.initialDeviceEventSeq, 1);
  assert.equal(harness.delivery.lastReceiptAuthority?.ackIdempotencyKey, ack.idempotency_key);

  // 同一场景但 checkpoint 带着一条无关 latch：那不是这条 entry 的完成证明。
  const other = await settlementHarness();
  other.store.completeOutcome = "throw";
  other.store.checkpoint = deepClone(createAutopilotAckCheckpoint({
    ownerKey: "a".repeat(64),
    sessionId: "S-ACK",
    lastDeviceEventSeq: 1,
    latch: {
      commandKey: startedRecord().command_key,
      command: startedRecord(),
      ack: buildAutopilotAck(startedRecord(), 0, "ack:record_failed:1:other", {
        ack_type: "record_failed", error_code: "recording_runtime_failed",
      }),
    },
    nowMs: 1,
  }));
  const error = await caught(
    () => other.delivery.send(question(), 0, { ack_type: "tts_started" }));
  const failure = ownedFailure(other.delivery, error);
  assert.equal(failure.phase, "complete");
  assert.equal(failure.outcome, "unknown");
  assert.equal(other.delivery.initialDeviceEventSeq, 0);
});

test("complete 提交前抛错：exact pending 与旧 checkpoint 保留（快照是深拷贝，不是同一引用），之后 drain 复用同 key 并完成", async () => {
  const harness = await settlementHarness();
  harness.store.completeOutcome = "throw";

  const error = await caught(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }));

  const failure = ownedFailure(harness.delivery, error);
  assert.equal(failure.phase, "complete");
  assert.equal(failure.outcome, "matching_pending");
  assert.equal(harness.delivery.initialDeviceEventSeq, 0);
  assert.equal(harness.delivery.terminalFailureLatch, null);
  assert.equal(harness.delivery.lastReceiptAuthority, null);
  assert.equal(harness.delivery.unresolvedSettlement, null);

  // 显式钉住结构化克隆：store 交回的 pending 与 delivery 内存里的 envelope 绝不是
  // 同一个对象，引用相等的实现在这里必然把 matching 判丢。
  const observed = await harness.store.snapshot();
  assert.notEqual(observed.pending, harness.store.pending);
  assert.deepEqual(observed.pending, harness.store.pending);

  const firstKey = harness.keys[0];
  harness.store.completeOutcome = "ok";
  const drained = await harness.delivery.drainPending();
  assert.equal(drained?.ack.idempotency_key, firstKey);
  assert.deepEqual(harness.keys, [firstKey, firstKey]);
  assert.equal(harness.delivery.initialDeviceEventSeq, 1);
});

test("complete 悬挂与 complete snapshot 悬挂：并发 send/drain 各自零第二条 HTTP、零 complete、零新 key", async () => {
  for (const stall of ["complete", "snapshot"] as const) {
    const harness = await settlementHarness();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    if (stall === "complete") harness.store.completeGate = gate;
    else {
      harness.store.completeOutcome = "throw";
      harness.store.snapshotGate = gate;
    }

    const first = harness.delivery.send(question(), 0, { ack_type: "tts_started" });
    first.catch(() => {});
    for (let turn = 0; turn < 20; turn += 1) await Promise.resolve();

    await assert.rejects(
      () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }),
      /禁止生成新事件序号/);
    await assert.rejects(() => harness.delivery.drainPending(), /禁止重放/);
    assert.equal(harness.httpCalls, 1);
    assert.equal(harness.keys.length, 1);

    release();
    await first.catch(() => {});
  }
});

test("complete unknown 粘滞：snapshot 不可读之后，同页 send/drain 零网络、零新 key", async () => {
  const harness = await settlementHarness();
  harness.store.completeOutcome = "throw";
  harness.store.snapshotThrows = true;

  const error = await caught(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }));

  const failure = ownedFailure(harness.delivery, error);
  assert.equal(failure.phase, "complete");
  assert.equal(failure.outcome, "unknown");
  assert.equal(harness.delivery.unresolvedSettlement, error);
  assert.equal(harness.delivery.initialDeviceEventSeq, 0);

  const httpBefore = harness.httpCalls;
  assert.equal(await caught(() => harness.delivery.drainPending()), error);
  assert.equal(await caught(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" })), error);
  assert.equal(harness.httpCalls, httpBefore);
  assert.equal(harness.keys.length, 1);
});

test("schema v1 checkpoint 落在目标 seq 上不是完成证明：判 unknown", async () => {
  const harness = await settlementHarness();
  // 事务提交了、pending 也清了，但 checkpoint 是 v1：它根本不记 latch，
  // 拿它当完成证明就等于把"终态失败有没有锁存"猜过去。
  harness.store.completeOutcome = "commit-then-throw";
  harness.store.completeWritesLegacyV1 = true;

  const error = await caught(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }));

  const failure = ownedFailure(harness.delivery, error);
  assert.equal(failure.phase, "complete");
  assert.equal(failure.outcome, "unknown");
  assert.equal(harness.delivery.initialDeviceEventSeq, 0);
  assert.equal(harness.delivery.lastReceiptAuthority, null);
});

test("transport 悬挂：并发 send/drain 零第二条 HTTP、零 complete、零新 key", async () => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const harness = await settlementHarness({ transportGate: gate });

  const first = harness.delivery.send(question(), 0, { ack_type: "tts_started" });
  first.catch(() => {});
  for (let turn = 0; turn < 20; turn += 1) await Promise.resolve();

  await assert.rejects(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }),
    /禁止生成新事件序号/);
  await assert.rejects(() => harness.delivery.drainPending(), /禁止重放/);
  assert.equal(harness.httpCalls, 1);
  assert.equal(harness.store.events.includes("complete"), false);

  release();
  await first;
});

test("归属证明：字段全等但由另一个 delivery 造出来的错误不被认领", async () => {
  const mine = await settlementHarness();
  const theirs = await settlementHarness();
  mine.store.stageOutcome = "throw";
  theirs.store.stageOutcome = "throw";

  const foreign = await caught(
    () => theirs.delivery.send(question(), 0, { ack_type: "tts_started" }));
  const own = await caught(
    () => mine.delivery.send(question(), 0, { ack_type: "tts_started" }));

  const ownFailure = ownedFailure(mine.delivery, own);
  const foreignFailure = ownedFailure(theirs.delivery, foreign);
  // 两条错误的 owner/session/命令/ACK 类型完全一致，唯一的差别是谁造的。
  assert.equal(foreignFailure.identity.ownerKey, ownFailure.identity.ownerKey);
  assert.equal(foreignFailure.identity.sessionId, ownFailure.identity.sessionId);
  assert.equal(foreignFailure.identity.commandKey, ownFailure.identity.commandKey);
  assert.equal(mine.delivery.ownsPersistenceError(foreign), false);
  assert.equal(mine.delivery.ownsPersistenceError(new Error("普通错误")), false);
});

test("record_failed 走通用 stage/complete；record_stopped 专用的音频原子接管方法调用次数为 0", async () => {
  const store = new MemoryAckStore();
  const delivery = await DurableAutopilotAckDelivery.create({
    capability, store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => { store.events.push("http"); return failureReceipt(ack); },
    },
  });
  await delivery.send(startedRecord(), 0, {
    ack_type: "record_failed", error_code: "recording_runtime_failed",
  });
  assert.deepEqual(store.events, ["stage", "http", "complete"]);
  assert.equal(store.events.includes("stage-record"), false);
  assert.ok(delivery.terminalFailureLatch);
  assert.equal(delivery.terminalFailureLatch?.ack.ack_type, "record_failed");
});

// ---------------- 不可变持久化错误：脱离快照、冻结与自有键保真 ----------------

/**
 * 真 Durable 的 stage `confirmed_empty` 失败，本组 oracle 的生产来源。
 *
 * `stageOutcome = "throw"` 让暂存事务未提交，随后 snapshot 证明 pending 为空，
 * 投递才走 `persistenceFailure(entry, "stage", "confirmed_empty", …)`。
 */
async function realStageEmptyFailure(
  command: NextCommandProjection,
  facts: Parameters<typeof buildAutopilotAck>[3],
): Promise<{ harness: SettlementHarness; failure: AutopilotAckPersistenceError }> {
  const harness = await settlementHarness();
  harness.store.stageOutcome = "throw";
  const error = await caught(() => harness.delivery.send(command, 0, facts));
  return { harness, failure: ownedFailure(harness.delivery, error) };
}

/**
 * 直接构造 oracle 用的外层 identity 容器。
 *
 * `persistenceFailure()` 在内部现造这个容器、不交给任何调用方，真 Durable 路径
 * 够不着它，所以外层容器的脱离只能由直接构造补充证明。
 */
function directIdentity(): AutopilotAckPersistenceIdentity {
  const command = startedRecord();
  return {
    ownerKey: "a".repeat(64),
    sessionId: "S-ACK",
    commandKey: command.command_key,
    command,
    ack: buildAutopilotAck(command, 0, "ack:record_started:1:direct-oracle", {
      ack_type: "record_started", mime_type: "audio/webm",
    }),
    ackType: "record_started",
    idempotencyKey: "ack:record_started:1:direct-oracle",
    deviceEventSeq: 1,
    commandRevision: command.command_revision,
    controlGeneration: command.control_generation,
    runnerGeneration: command.runner_generation,
    mimeType: "audio/webm",
  };
}

test("不可变错误（真 Durable 失败）：instanceof AutopilotAckPersistenceError 与 Error，name/message 如实，stack 可用", async () => {
  const { failure } = await realStageEmptyFailure(question(), { ack_type: "tts_started" });

  assert.ok(failure instanceof AutopilotAckPersistenceError);
  assert.ok(failure instanceof Error);
  assert.equal(Object.getPrototypeOf(failure), AutopilotAckPersistenceError.prototype);
  assert.equal(failure.name, "AutopilotAckPersistenceError");
  assert.equal(failure.message, "ACK 暂存事务未提交");
  assert.equal(String(failure), "AutopilotAckPersistenceError: ACK 暂存事务未提交");
  // 冻结之后 stack 仍然可读：Error 子类语义与 typed 字段一并保留。
  const stack = failure.stack;
  assert.equal(typeof stack, "string");
  assert.ok((stack as string).length > 0);
  assert.ok((stack as string).includes("AutopilotAckPersistenceError"));
  assert.equal(failure.phase, "stage");
  assert.equal(failure.outcome, "confirmed_empty");
});

test("不可变错误：cause 保持原始引用，且那个外部 cause 对象没有被冻结", async () => {
  const { failure } = await realStageEmptyFailure(question(), { ack_type: "tts_started" });

  // 真 Durable 路径：cause 就是 store 抛出来的那个对象，构造器一次都不冻它。
  const realCause = failure.cause as Error & { marker?: string };
  assert.ok(realCause instanceof Error);
  assert.equal(realCause.message, "暂存事务中止");
  assert.equal(Object.isFrozen(realCause), false);
  realCause.marker = "外部仍可变";
  assert.equal((failure.cause as Error & { marker?: string }).marker, "外部仍可变");

  // 直接构造：cause 必须是同一个引用，且它下面那一层也没有被递归冻结。
  const externalCause = { detail: { reason: "外部原因" } };
  const direct = new AutopilotAckPersistenceError({
    phase: "stage",
    outcome: "confirmed_empty",
    identity: directIdentity(),
    message: "直接构造的 cause 引用 oracle",
    cause: externalCause,
  });
  assert.equal(direct.cause, externalCause);
  assert.equal(Object.isFrozen(externalCause), false);
  assert.equal(Object.isFrozen(externalCause.detail), false);
});

test("不可变错误：Error 本体与 identity/command/payload/ack 全部 Object.isFrozen", async () => {
  const { failure } = await realStageEmptyFailure(startedRecord(), {
    ack_type: "record_started", mime_type: "audio/webm",
  });

  assert.equal(Object.isFrozen(failure), true);
  assert.equal(Object.isFrozen(failure.identity), true);
  assert.equal(Object.isFrozen(failure.identity.command), true);
  assert.equal(Object.isFrozen(failure.identity.command.payload), true);
  assert.equal(Object.isFrozen(failure.identity.ack), true);
});

test("不可变错误：改 phase/outcome/identity 与每一层嵌套授权对象都改不动已捕获的事实", async () => {
  const { failure } = await realStageEmptyFailure(startedRecord(), {
    ack_type: "record_started", mime_type: "audio/webm",
  });
  const identity = failure.identity;
  const command = identity.command;
  if (command.kind !== "record") throw new Error("本 oracle 需要 record 命令");
  const ack = identity.ack;
  if (ack.ack_type !== "record_started") throw new Error("本 oracle 需要 record_started ACK");
  const ownerKeyBefore = identity.ownerKey;
  const commandKeyBefore = command.command_key;
  const turnRefBefore = command.payload.turn_ref;
  const mimeBefore = ack.mime_type;

  // ESM 恒为严格模式：写冻结属性必须响亮地抛，而不是静默失败之后被误当成"改不动"。
  assert.throws(() => {
    (failure as unknown as { phase: string }).phase = "complete";
  }, TypeError);
  assert.throws(() => {
    (failure as unknown as { outcome: string }).outcome = "unknown";
  }, TypeError);
  assert.throws(() => {
    (failure as unknown as { identity: unknown }).identity = null;
  }, TypeError);
  assert.throws(() => {
    (identity as unknown as { ownerKey: string }).ownerKey = "f".repeat(64);
  }, TypeError);
  assert.throws(() => {
    (identity as unknown as { injected?: number }).injected = 1;
  }, TypeError);
  assert.throws(() => {
    delete (identity as unknown as { ownerKey?: string }).ownerKey;
  }, TypeError);
  assert.throws(() => {
    (command as unknown as { command_key: string }).command_key = "cmd-forged-0001";
  }, TypeError);
  assert.throws(() => {
    (command.payload as unknown as { turn_ref: string }).turn_ref = "itm-0001#9";
  }, TypeError);
  assert.throws(() => {
    (ack as unknown as { mime_type: string }).mime_type = "audio/mp4";
  }, TypeError);

  assert.equal(failure.phase, "stage");
  assert.equal(failure.outcome, "confirmed_empty");
  assert.equal(failure.identity, identity);
  assert.equal(identity.ownerKey, ownerKeyBefore);
  assert.equal(command.command_key, commandKeyBefore);
  assert.equal(command.payload.turn_ref, turnRefBefore);
  assert.equal(ack.mime_type, mimeBefore);
});

test("不可变错误：真 Durable 失败之后改原始 command/payload/ACK 快照纹丝不动；外层 identity 容器由直接构造补充证明", async () => {
  const { harness, failure } = await realStageEmptyFailure(startedRecord(), {
    ack_type: "record_started", mime_type: "audio/webm",
  });

  // stagedEntries 交出的是投递真正递给 stage() 的那个 envelope 活引用：
  // parseRecordPayload 与 parseAutopilotAck 都原样返回传入对象，所以下面这几个
  // 引用就是构造 identity 时用的那几个原对象。
  const entry = harness.store.stagedEntries[0];
  assert.ok(entry);
  const stagedCommand = entry.command;
  if (stagedCommand.kind !== "record") throw new Error("本 oracle 需要 record 命令");
  const stagedAck = entry.ack;
  if (stagedAck.ack_type !== "record_started") throw new Error("本 oracle 需要 record_started ACK");
  const snapshotCommand = failure.identity.command;
  if (snapshotCommand.kind !== "record") throw new Error("快照命令类型异常");
  const snapshotAck = failure.identity.ack;
  if (snapshotAck.ack_type !== "record_started") throw new Error("快照 ACK 类型异常");

  stagedCommand.command_key = "cmd-ack-delivery-record-9999";
  stagedCommand.payload.turn_ref = "itm-0001#9";
  stagedAck.mime_type = "audio/mp4";

  assert.equal(snapshotCommand.command_key, "cmd-ack-delivery-record-0002");
  assert.equal(snapshotCommand.payload.turn_ref, "itm-0001#1");
  assert.equal(snapshotAck.mime_type, "audio/webm");
  assert.notEqual(snapshotCommand, stagedCommand);
  assert.notEqual(snapshotCommand.payload, stagedCommand.payload);
  assert.notEqual(snapshotAck, stagedAck);

  // 外层容器那一段不声称跑过真 Durable 路径：投递根本不把它交出来。
  const outer = directIdentity();
  const mutableOuter = outer as unknown as { sessionId: string; mimeType: string | null };
  const direct = new AutopilotAckPersistenceError({
    phase: "stage", outcome: "confirmed_empty", identity: outer,
    message: "外层容器脱离 oracle",
  });
  mutableOuter.sessionId = "S-OTHER";
  mutableOuter.mimeType = "audio/mp4";
  assert.equal(direct.identity.sessionId, "S-ACK");
  assert.equal(direct.identity.mimeType, "audio/webm");
  assert.notEqual(direct.identity, outer);
});

test("不可变错误：构造器不冻结调用方传进来的原始对象", async () => {
  const { harness, failure } = await realStageEmptyFailure(startedRecord(), {
    ack_type: "record_started", mime_type: "audio/webm",
  });

  const entry = harness.store.stagedEntries[0];
  assert.ok(entry);
  assert.equal(Object.isFrozen(entry), false);
  assert.equal(Object.isFrozen(entry.command), false);
  assert.equal(Object.isFrozen(entry.command.payload), false);
  assert.equal(Object.isFrozen(entry.ack), false);
  // 快照那一侧全冻：复制在前、冻结在后，两侧互不影响。
  assert.equal(Object.isFrozen(failure.identity.command), true);

  const outer = directIdentity();
  const constructed = new AutopilotAckPersistenceError({
    phase: "stage", outcome: "confirmed_empty", identity: outer,
    message: "不冻结入参 oracle",
  });
  assert.ok(constructed instanceof AutopilotAckPersistenceError);
  assert.equal(Object.isFrozen(outer), false);
  assert.equal(Object.isFrozen(outer.command), false);
  assert.equal(Object.isFrozen(outer.ack), false);
});

test("不可变错误：自有 explicit-undefined 键与额外键原样留在快照里，供后续 exact 比较器判负", () => {
  const outer = directIdentity();
  const command = outer.command as unknown as Record<string, unknown>;
  Object.defineProperty(command, "__extra__", {
    value: 7, writable: true, enumerable: true, configurable: true,
  });
  Object.defineProperty(command, "__own_undefined__", {
    value: undefined, writable: true, enumerable: true, configurable: true,
  });
  const payload = command.payload as Record<string, unknown>;
  Object.defineProperty(payload, "__payload_undefined__", {
    value: undefined, writable: true, enumerable: true, configurable: true,
  });

  const failure = new AutopilotAckPersistenceError({
    phase: "stage", outcome: "confirmed_empty", identity: outer,
    message: "自有键保真 oracle",
  });
  const snapshot = failure.identity.command as unknown as Record<string, unknown>;
  const snapshotPayload = snapshot.payload as Record<string, unknown>;

  // 缺键、自有 undefined 键、额外键在快照里是三件互相独立、后续 exact 比较器可以
  // 分别判负的事实：只要键集被 JSON clone 或 hasOwn-only 实现抹平，这里就翻红。
  assert.equal(Object.hasOwn(snapshot, "__extra__"), true);
  assert.equal(snapshot.__extra__, 7);
  assert.equal(Object.hasOwn(snapshot, "__own_undefined__"), true);
  assert.equal(snapshot.__own_undefined__, undefined);
  // 原来这里断言 `hasOwn(snapshot, "response_path") === false`。那是恒真的：
  // response_path 是 TTS payload 的可选键，不是顶层命令键，而这份快照来自 record
  // 命令，任何实现都不会凭空长出它。换成"顶层键集逐字相等"——少一个自有 undefined
  // 键、或多一个克隆过程中掺进来的键，这一行都会翻红。
  assert.deepEqual(Object.keys(snapshot).sort(), Object.keys(command).sort());
  assert.equal(Object.hasOwn(snapshotPayload, "__payload_undefined__"), true);
  assert.equal(snapshotPayload.__payload_undefined__, undefined);
  assert.deepEqual(Object.keys(snapshotPayload).sort(), Object.keys(payload).sort());
});

test("脱离克隆 oracle（真 Error 构造器）：exact length、稀疏洞、自有 undefined 索引、可枚举额外键全部保真，脱离数组被冻结、输入数组未被冻结，可枚举 __proto__ 只成为自有数据属性且不改克隆原型，Date 值让构造器抛 TypeError", () => {
  const source = new Array(5) as unknown[];
  source[0] = 1;
  // 自有 explicit-undefined 索引必须用 defineProperty 建：`source[2] = undefined`
  // 同样建自有键，但这样写更能钉住"洞与自有 undefined 是两件事"。
  Object.defineProperty(source, "2", {
    value: undefined, writable: true, enumerable: true, configurable: true,
  });
  source[3] = 4;
  Object.defineProperty(source, "extra", {
    value: 9, writable: true, enumerable: true, configurable: true,
  });
  // 对象字面量 `{ __proto__: x }` 是原型 setter 语法，根本不产生自有键，拿它当
  // oracle 不成立；只有 defineProperty 才建得出可枚举的自有 `__proto__` 数据属性。
  Object.defineProperty(source, "__proto__", {
    value: { marker: "数组自有键" }, writable: true, enumerable: true, configurable: true,
  });

  const nested: Record<string, unknown> = {};
  Object.defineProperty(nested, "__proto__", {
    value: { marker: "对象自有键" }, writable: true, enumerable: true, configurable: true,
  });

  const outer = directIdentity();
  const carrier = outer.command as unknown as Record<string, unknown>;
  carrier.__array__ = source;
  carrier.__nested__ = nested;

  const failure = new AutopilotAckPersistenceError({
    phase: "stage", outcome: "confirmed_empty", identity: outer,
    message: "脱离克隆 oracle",
  });
  const snapshot = failure.identity.command as unknown as Record<string, unknown>;
  const clone = snapshot.__array__ as unknown[];

  assert.equal(Array.isArray(clone), true);
  assert.notEqual(clone, source);
  assert.equal(clone.length, 5);                                  // exact length，尾部洞不被填平
  assert.equal(clone[0], 1);
  assert.equal(Object.hasOwn(clone, "1"), false);                 // 稀疏洞仍是洞
  assert.equal(Object.hasOwn(clone, "2"), true);                  // 自有 explicit undefined 索引保留
  assert.equal(clone[2], undefined);
  assert.equal(clone[3], 4);
  assert.equal(Object.hasOwn(clone, "4"), false);
  assert.equal(Object.hasOwn(clone, "extra"), true);              // Array.map 会把它丢掉
  assert.equal((clone as unknown as Record<string, unknown>).extra, 9);
  assert.deepEqual(Object.keys(clone), ["0", "2", "3", "extra", "__proto__"]);
  assert.equal(Object.isFrozen(clone), true);
  assert.equal(Object.isFrozen(source), false);
  // `copy[key] = …` 会让 "__proto__" 走 Object.prototype 的 setter、改掉克隆原型；
  // defineProperty 恒建自有数据属性。
  assert.equal(Object.hasOwn(clone, "__proto__"), true);
  assert.equal(Object.getPrototypeOf(clone), Array.prototype);
  assert.deepEqual(
    Object.getOwnPropertyDescriptor(clone, "__proto__")?.value, { marker: "数组自有键" });

  const nestedClone = snapshot.__nested__ as Record<string, unknown>;
  assert.equal(Object.hasOwn(nestedClone, "__proto__"), true);
  assert.equal(Object.getPrototypeOf(nestedClone), Object.prototype);
  const nestedProtoValue = Object.getOwnPropertyDescriptor(
    nestedClone, "__proto__")?.value as Record<string, unknown>;
  assert.deepEqual(nestedProtoValue, { marker: "对象自有键" });
  assert.equal(Object.isFrozen(nestedProtoValue), true);
  assert.equal(Object.isFrozen(nested), false);

  // 普通的非 plain 对象必须响亮地抛，而不是被静默展平成一份看起来正常的快照。
  const dateIdentity = directIdentity();
  (dateIdentity.command as unknown as Record<string, unknown>).__date__ = new Date(0);
  assert.throws(() => new AutopilotAckPersistenceError({
    phase: "stage", outcome: "confirmed_empty", identity: dateIdentity,
    message: "Date 守卫 oracle",
  }), /授权图只接受 plain object 与数组/);
});

test("不可变错误：两次失败是两个新实例，冻结之后仍各自被产生它的 delivery 私有 WeakSet 认领", async () => {
  const mine = await settlementHarness();
  const theirs = await settlementHarness();
  mine.store.stageOutcome = "throw";
  theirs.store.stageOutcome = "throw";

  const first = ownedFailure(mine.delivery, await caught(
    () => mine.delivery.send(question(), 0, { ack_type: "tts_started" })));
  const second = ownedFailure(mine.delivery, await caught(
    () => mine.delivery.send(question(), 0, { ack_type: "tts_started" })));
  const foreign = ownedFailure(theirs.delivery, await caught(
    () => theirs.delivery.send(question(), 0, { ack_type: "tts_started" })));

  assert.notEqual(first, second);
  assert.notEqual(first.identity, second.identity);
  assert.equal(Object.isFrozen(first), true);
  assert.equal(Object.isFrozen(second), true);
  // owner/session/命令全等，差别只在"谁造的"与各自新造的幂等键。
  assert.equal(first.identity.ownerKey, second.identity.ownerKey);
  assert.equal(first.identity.commandKey, second.identity.commandKey);
  assert.notEqual(first.identity.idempotencyKey, second.identity.idempotencyKey);

  assert.equal(mine.delivery.ownsPersistenceError(first), true);
  assert.equal(mine.delivery.ownsPersistenceError(second), true);
  assert.equal(mine.delivery.ownsPersistenceError(foreign), false);
  assert.equal(theirs.delivery.ownsPersistenceError(foreign), true);
  assert.equal(theirs.delivery.ownsPersistenceError(first), false);
});


// ---------------- R8B：幂等键生成 seam 与单变量结算矩阵 ----------------

const SESSION_ID = "S-ACK";

/**
 * 真 owner key，不是手写常量。
 *
 * 它是 capability 的指纹，不是 `"a".repeat(64)`。夹具若用手写值当"当前 owner"，
 * 整份 checkpoint 会从第一个字段起就判负——那样每一行都因为同一个原因返回
 * unknown，"只动一个变量"的结论一条都成立不了。
 */
let cachedOwnerKey: string | null = null;
async function ownerKey(): Promise<string> {
  cachedOwnerKey ??= await fingerprintAutopilotCapability(capability);
  return cachedOwnerKey;
}

/** 比 question() 严格更新的命令：key/seq/两代都更大，足以解开 question() 的闩。 */
function supersedingQuestion(): NextCommandProjection {
  return {
    ...question(),
    command_key: "cmd-ack-delivery-question-0002",
    command_seq: 2,
    control_generation: 2,
    runner_generation: 2,
  };
}

/** 以某条命令为准的终态失败闩。失败类型必须跟命令种类对上，否则 checkpoint 解析拒收。 */
function latchOn(command: NextCommandProjection) {
  const facts = command.kind === "record"
    ? { ack_type: "record_failed", error_code: "recording_runtime_failed" } as const
    : { ack_type: "tts_failed", error_code: "audio_playback_failed" } as const;
  return {
    commandKey: command.command_key,
    command,
    ack: buildAutopilotAck(command, 0, `ack:${facts.ack_type}:1:seeded`, facts),
  };
}

/** 当前语义下完全正确的 v2 checkpoint。每一行只允许覆写其中一个字段。 */
function currentCheckpoint(owner: string, overrides: {
  ownerKey?: string;
  sessionId?: string;
  lastDeviceEventSeq?: number;
  latch?: ReturnType<typeof latchOn> | null;
} = {}, seq = 0): AutopilotAckCheckpoint {
  return deepClone(createAutopilotAckCheckpoint({
    ownerKey: overrides.ownerKey ?? owner,
    sessionId: overrides.sessionId ?? SESSION_ID,
    lastDeviceEventSeq: overrides.lastDeviceEventSeq ?? seq,
    latch: overrides.latch ?? null,
    nowMs: 1,
  }));
}

/**
 * 播种一个"已锁存终态失败"的起始状态：lastSeq=1、latch 落在 question() 上。
 *
 * seq 只能是 1 不能是 0：`parseAutopilotAckCheckpoint` 强制
 * `latch.ack.device_event_seq === lastDeviceEventSeq`，而失败 ACK 的
 * device_event_seq 至少是 1。这条不变量顺带说明了一件事——
 * `provesCurrentCheckpoint(null)` 里 `this.latch === null` 那一半**无法**被单独
 * 触发：任何带 latch 的 delivery 都已经 lastSeq ≥ 1，前一个合取项先判负。它是
 * 纵深防御而非活判据，所以本文件不给它造一行"只能靠伪造非法 checkpoint"的用例。
 */
function seedLatched(owner: string): (store: SettlementStore) => void {
  return (store) => {
    store.checkpoint = currentCheckpoint(owner, { latch: latchOn(question()) }, 1);
  };
}

test("幂等键生成计数（K1 终态闩先于生成点）：闸拒绝时计数为 0", async () => {
  // 已锁存终态失败、命令又证明不了新权威——这一格必须在生成点之前就挡住。
  // 计数点若被挪到闸后面，这一行会翻红。
  const harness = await settlementHarness({ seed: seedLatched(await ownerKey()) });

  await assert.rejects(
    () => harness.delivery.send(question(), 1, { ack_type: "tts_started" }),
    /已锁存终态失败/);

  assert.equal(harness.delivery.generatedIdempotencyKeyCount, 0);
  assert.equal(harness.httpCalls, 0);
  assert.deepEqual(harness.store.events, []);
});

test("幂等键生成计数（K2 confirmed_empty 之后的唯一替换）：1 → 2，两个 staged key 不等，transport 全程 0", async () => {
  const harness = await settlementHarness();
  harness.store.stageOutcome = "throw";

  const first = await caught(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }));
  ownedFailure(harness.delivery, first);
  assert.equal(harness.delivery.generatedIdempotencyKeyCount, 1);

  const second = await caught(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }));
  ownedFailure(harness.delivery, second);

  assert.equal(harness.delivery.generatedIdempotencyKeyCount, 2);
  assert.equal(harness.store.stagedEntries.length, 2);
  assert.notEqual(
    harness.store.stagedEntries[0].ack.idempotency_key,
    harness.store.stagedEntries[1].ack.idempotency_key);
  assert.equal(harness.httpCalls, 0);
  assert.deepEqual(harness.keys, []);
});

test("幂等键生成计数（K3 粘滞 unknown 之后）：同页 send/drain 先抛粘滞错误，计数不变", async () => {
  const harness = await settlementHarness();
  harness.store.stageOutcome = "throw";
  harness.store.snapshotThrows = true;

  const sticky = await caught(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }));
  ownedFailure(harness.delivery, sticky);
  assert.equal(harness.delivery.generatedIdempotencyKeyCount, 1);

  assert.equal(await caught(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" })), sticky);
  assert.equal(await caught(() => harness.delivery.drainPending()), sticky);

  assert.equal(harness.delivery.generatedIdempotencyKeyCount, 1);
});

test("幂等键生成计数（K4 buildAutopilotAck 因非法 facts 抛错）：计数 +1 而 staged 0", async () => {
  // 整个 seam 的存在理由。这个 key 生成了，却既没进 envelope 也没 staged、更没
  // transport——在 store 与 transport 两个面上都不留痕，计数是它唯一的观察面。
  const harness = await settlementHarness();

  await assert.rejects(() => harness.delivery.send(startedRecord(), 0, {
    ack_type: "record_stopped",
    stop_reason: "user_done",
    raw_audio_id: "raw-ack-delivery-record-0001",
    receipt_server_seq: 9,
    checksum: "not-a-sha256",
    byte_count: 1024,
    duration_seconds: 1.25,
  }));

  assert.equal(harness.delivery.generatedIdempotencyKeyCount, 1);
  assert.equal(harness.store.stagedEntries.length, 0);
  assert.deepEqual(harness.store.events, []);
  assert.equal(harness.httpCalls, 0);
  assert.equal(harness.delivery.initialDeviceEventSeq, 0);
});

test("幂等键生成计数（K5 in-flight 时的并发 send）：被闸挡住，计数不变", async () => {
  const harness = await settlementHarness();
  harness.store.stageOutcome = "throw";
  let release!: () => void;
  harness.store.snapshotGate = new Promise<void>((resolve) => { release = resolve; });

  const first = harness.delivery.send(question(), 0, { ack_type: "tts_started" });
  first.catch(() => {});
  for (let turn = 0; turn < 20; turn += 1) await Promise.resolve();
  assert.equal(harness.delivery.generatedIdempotencyKeyCount, 1);

  await assert.rejects(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }),
    /禁止生成新事件序号/);

  assert.equal(harness.delivery.generatedIdempotencyKeyCount, 1);
  release();
  await assert.rejects(() => first);
  assert.equal(harness.delivery.generatedIdempotencyKeyCount, 1);
});

test("幂等键生成计数（K6 drainPending 重放）：重放不生成新 key，计数不变", async () => {
  const harness = await settlementHarness();
  harness.store.completeOutcome = "throw";

  const failure = await caught(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }));
  ownedFailure(harness.delivery, failure);
  assert.equal(harness.delivery.generatedIdempotencyKeyCount, 1);
  const firstKey = harness.keys[0];

  harness.store.completeOutcome = "ok";
  const drained = await harness.delivery.drainPending();

  assert.equal(drained?.ack.idempotency_key, firstKey);
  assert.equal(harness.delivery.generatedIdempotencyKeyCount, 1);
  assert.deepEqual(harness.keys, [firstKey, firstKey]);
});

test("夹具自证：未经扰动的当前 checkpoint 必须被判成当前语义", async () => {
  // 这条不测生产判据，测的是下面那张矩阵有没有资格叫"单变量"。基线本身若判负，
  // 每一行都会因为同一个原因返回 unknown，"只动了 X 才失守"的结论一条都不成立。
  const owner = await ownerKey();
  const harness = await settlementHarness();
  harness.store.stageOutcome = "throw";
  harness.store.checkpoint = currentCheckpoint(owner);

  const error = await caught(
    () => harness.delivery.send(question(), 0, { ack_type: "tts_started" }));

  assert.equal(ownedFailure(harness.delivery, error).outcome, "confirmed_empty");
});

interface StageRow {
  name: string;
  outcome: "unknown" | "confirmed_empty";
  kills: string;
  seed?: (owner: string) => (store: SettlementStore) => void;
  arrange: (store: SettlementStore, owner: string) => void;
  command?: () => NextCommandProjection;
  lastSeq?: number;
  /** 默认 throw（未提交）。exact pending 的那一行要用 commit-then-throw。 */
  stageOutcome?: StoreOutcome;
}

const STAGE_ROWS: StageRow[] = [
  {
    name: "S5 只动 ownerKey",
    outcome: "unknown",
    kills: "把 owner 从当前语义判据里删掉",
    arrange: (store, owner) => {
      store.checkpoint = currentCheckpoint(owner, { ownerKey: "c".repeat(64) });
    },
  },
  {
    name: "S6 只动 sessionId",
    outcome: "unknown",
    kills: "只比 owner 不比 session，同一台设备换场次即被误判为当前",
    arrange: (store, owner) => {
      store.checkpoint = currentCheckpoint(owner, { sessionId: "S-OTHER" });
    },
  },
  {
    name: "S7 只动 lastDeviceEventSeq",
    outcome: "unknown",
    kills: "不比 seq，拿一个落在别的序号上的 checkpoint 当当前证明",
    arrange: (store, owner) => {
      store.checkpoint = currentCheckpoint(owner, { lastDeviceEventSeq: 5 });
    },
  },
  {
    name: "S8 只动 latch，owner/session/seq 全对",
    outcome: "unknown",
    kills: "把 latch 从当前语义判据里删掉",
    seed: seedLatched,
    arrange: (store, owner) => {
      store.checkpoint = currentCheckpoint(
        owner, { latch: latchOn(startedRecord()) }, 1);
    },
    command: supersedingQuestion,
    lastSeq: 1,
  },
  {
    name: "S9 v1 checkpoint，owner/session/seq 全对",
    outcome: "confirmed_empty",
    kills: "把 v1 checkpoint 一律判成不可证明的过严实现",
    arrange: (store, owner) => {
      // v1 不记 latch，checkpointTerminalFailureLatch 对它恒返回 null；本实例的
      // latch 也是 null，所以它**能**证明当前语义。
      store.checkpoint = deepClone({
        schemaVersion: 1,
        ownerKey: owner,
        sessionId: SESSION_ID,
        lastDeviceEventSeq: 0,
        updatedAtMs: 1,
      } as AutopilotAckCheckpoint);
    },
  },
  {
    name: "S11 pending 畸形",
    outcome: "unknown",
    kills: "让 envelope 解析异常穿透到调用方，而不是判 false",
    arrange: (store, owner) => {
      store.checkpoint = currentCheckpoint(owner);
      store.pending = { not: "an envelope" } as unknown as AutopilotAckEnvelope;
    },
  },
  {
    name: "S12 checkpoint 为 null 而 lastSeq 已推进",
    outcome: "unknown",
    kills: "null-checkpoint 分支不看 lastSeq，把空存储当成当前证明",
    seed: seedLatched,
    arrange: (store) => { store.checkpoint = null; },
    command: supersedingQuestion,
    lastSeq: 1,
  },
  {
    name: "S10 exact pending 但只动 latch",
    outcome: "unknown",
    kills: "以为 exact pending 就够，救得回一个不当前的 checkpoint",
    seed: seedLatched,
    stageOutcome: "commit-then-throw",
    arrange: (store, owner) => {
      store.checkpoint = currentCheckpoint(
        owner, { latch: latchOn(startedRecord()) }, 1);
    },
    command: supersedingQuestion,
    lastSeq: 1,
  },
];

for (const row of STAGE_ROWS) {
  test(`stage 结算（${row.name}）：零 HTTP、零 complete、零新 key，粘滞保留 exact identity`, async () => {
    const owner = await ownerKey();
    const harness = await settlementHarness({ seed: row.seed?.(owner) });
    const beforeSeq = harness.delivery.initialDeviceEventSeq;
    const beforeLatch = harness.delivery.terminalFailureLatch;
    const beforeAuthority = harness.delivery.lastReceiptAuthority;
    harness.store.stageOutcome = row.stageOutcome ?? "throw";
    row.arrange(harness.store, owner);
    const command = (row.command ?? question)();

    const error = await caught(() => harness.delivery.send(
      command, row.lastSeq ?? 0, { ack_type: "tts_started" }));

    const failure = ownedFailure(harness.delivery, error);
    assert.equal(failure.phase, "stage");
    assert.equal(failure.outcome, row.outcome, `本行杀的是：${row.kills}`);
    assert.equal(failure.identity.commandKey, command.command_key);
    assert.equal(harness.httpCalls, 0);
    assert.deepEqual(harness.keys, []);
    assert.equal(harness.store.events.includes("complete"), false);
    assert.equal(harness.delivery.initialDeviceEventSeq, beforeSeq);
    assert.deepEqual(harness.delivery.terminalFailureLatch, beforeLatch);
    assert.equal(harness.delivery.lastReceiptAuthority, beforeAuthority);
    assert.equal(harness.delivery.generatedIdempotencyKeyCount, 1);

    if (row.outcome === "unknown") {
      // 粘滞：同页再 send/drain 一律返回同一条错误，且不再生成第二个 key。
      assert.equal(harness.delivery.unresolvedSettlement, error);
      assert.equal(await caught(() => harness.delivery.send(
        command, row.lastSeq ?? 0, { ack_type: "tts_started" })), error);
      assert.equal(await caught(() => harness.delivery.drainPending()), error);
      assert.equal(harness.delivery.generatedIdempotencyKeyCount, 1);
      assert.equal(harness.httpCalls, 0);
    } else {
      // confirmed_empty 是唯一能授权同 seq 替换的结果：闸松开，不粘滞。
      assert.equal(harness.delivery.unresolvedSettlement, null);
    }
  });
}

// ---------------- R8B：complete-catch 单变量矩阵 ----------------

type CompleteExpectation = "unknown" | "commit";

interface CompleteRow {
  name: string;
  expect: CompleteExpectation;
  kills: string;
  /** 失败 ACK 的行要用 failureReceipt，否则严格收据解析先一步拒收。 */
  failureAck?: boolean;
  seed?: (owner: string) => (store: SettlementStore) => void;
  completeOutcome?: StoreOutcome;
  /** commit-then-throw 之后对已写入 checkpoint 的单变量扰动。 */
  mutate?: (owner: string) => (checkpoint: AutopilotAckCheckpoint) => AutopilotAckCheckpoint;
  /** completeOutcome=throw 的行自己摆 checkpoint。 */
  arrange?: (store: SettlementStore, owner: string) => void;
  command?: () => NextCommandProjection;
  lastSeq?: number;
}

const COMPLETE_ROWS: CompleteRow[] = [
  {
    name: "C5 只动 ownerKey",
    expect: "unknown",
    kills: "把 owner 从完成证明里删掉",
    mutate: () => (checkpoint) => ({ ...checkpoint, ownerKey: "c".repeat(64) }),
  },
  {
    name: "C6 只动 sessionId",
    expect: "unknown",
    kills: "把 session 从完成证明里删掉",
    mutate: () => (checkpoint) => ({ ...checkpoint, sessionId: "S-OTHER" }),
  },
  {
    name: "C7 lastDeviceEventSeq 不等于本条 ACK 的 seq",
    expect: "unknown",
    kills: "用 this.lastSeq 而不是 entry.ack.device_event_seq 做完成证明",
    mutate: () => (checkpoint) => ({ ...checkpoint, lastDeviceEventSeq: 0 }),
  },
  {
    name: "C8 失败 ACK，只动 latch",
    expect: "unknown",
    kills: "把 latch 从完成证明里删掉",
    failureAck: true,
    mutate: () => (checkpoint) => ({
      ...checkpoint, latch: latchOn(startedRecord()),
    } as AutopilotAckCheckpoint),
  },
  {
    name: "C10 非失败 ACK 完成后 checkpoint 却落了 latch",
    expect: "unknown",
    kills: "非失败 ACK 完成之后没有原子清掉旧 latch",
    mutate: () => (checkpoint) => ({
      ...checkpoint, latch: latchOn(question()),
    } as AutopilotAckCheckpoint),
  },
  {
    name: "C11 失败 ACK，checkpoint 的 latch 逐字等于该 entry 的 latch",
    expect: "commit",
    kills: "把逐字相等的完成证明也判负（正例对照，防止上面几行靠恒判负通过）",
    failureAck: true,
  },
  {
    name: "C9 exact pending 但只动 latch",
    expect: "unknown",
    kills: "以为 exact pending 就够，救得回一个不当前的 checkpoint",
    seed: seedLatched,
    completeOutcome: "throw",
    arrange: (store, owner) => {
      store.checkpoint = currentCheckpoint(
        owner, { latch: latchOn(startedRecord()) }, 1);
    },
    command: supersedingQuestion,
    lastSeq: 1,
  },
];

for (const row of COMPLETE_ROWS) {
  test(`complete 结算（${row.name}）：零第二条 HTTP、零新 key，seq/latch/权威按判据一步不多推`, async () => {
    const owner = await ownerKey();
    const harness = await settlementHarness({
      seed: row.seed?.(owner),
      respond: row.failureAck ? (ack) => failureReceipt(ack) : undefined,
    });
    const beforeSeq = harness.delivery.initialDeviceEventSeq;
    const beforeLatch = harness.delivery.terminalFailureLatch;
    harness.store.completeOutcome = row.completeOutcome ?? "commit-then-throw";
    if (row.mutate) harness.store.completeCheckpointMutate = row.mutate(owner);
    row.arrange?.(harness.store, owner);
    const command = (row.command ?? question)();
    const facts = row.failureAck
      ? { ack_type: "tts_failed", error_code: "audio_playback_failed" } as const
      : { ack_type: "tts_started" } as const;

    if (row.expect === "commit") {
      const ack = await harness.delivery.send(command, row.lastSeq ?? 0, facts);

      assert.equal(harness.httpCalls, 1);
      assert.deepEqual(harness.keys, [ack.idempotency_key]);
      assert.equal(harness.delivery.initialDeviceEventSeq, beforeSeq + 1);
      assert.equal(
        harness.delivery.terminalFailureLatch?.ack.idempotency_key,
        ack.idempotency_key);
      assert.equal(harness.delivery.unresolvedSettlement, null);
      assert.equal(harness.delivery.generatedIdempotencyKeyCount, 1);
      return;
    }

    const error = await caught(
      () => harness.delivery.send(command, row.lastSeq ?? 0, facts));

    const failure = ownedFailure(harness.delivery, error);
    assert.equal(failure.phase, "complete");
    assert.equal(failure.outcome, "unknown", `本行杀的是：${row.kills}`);
    assert.equal(harness.httpCalls, 1);
    assert.equal(harness.keys.length, 1);
    assert.equal(harness.delivery.initialDeviceEventSeq, beforeSeq);
    assert.deepEqual(harness.delivery.terminalFailureLatch, beforeLatch);
    assert.equal(harness.delivery.lastReceiptAuthority, null);
    assert.equal(harness.delivery.unresolvedSettlement, error);
    assert.equal(harness.delivery.generatedIdempotencyKeyCount, 1);

    // 粘滞：同页 send/drain 零第二条 HTTP、零新 key。
    assert.equal(await caught(() => harness.delivery.drainPending()), error);
    assert.equal(await caught(
      () => harness.delivery.send(command, row.lastSeq ?? 0, facts)), error);
    assert.equal(harness.httpCalls, 1);
    assert.equal(harness.delivery.generatedIdempotencyKeyCount, 1);
  });
}

test("脱离克隆守卫：函数值抛 TypeError；原型为 null 的对象是被接受的 plain 授权对象", () => {
  // W1C 留下的两条未覆盖分支。函数守卫排在 typeof !== "object" 的早退里，
  // 原型为 null 那条则是 isPlainAuthorityObject 里 `proto === null` 的接受路径——
  // 把它写成"只接受 Object.prototype"的实现会在第二段翻红。
  const withFunction = directIdentity();
  (withFunction.command as unknown as Record<string, unknown>).__fn__ = () => 1;

  assert.throws(() => new AutopilotAckPersistenceError({
    phase: "stage", outcome: "confirmed_empty", identity: withFunction,
    message: "函数守卫",
  }), /授权图不接受函数/);

  const nullProto = Object.create(null) as Record<string, unknown>;
  nullProto.marker = "无原型对象";
  const accepted = directIdentity();
  (accepted.command as unknown as Record<string, unknown>).__null_proto__ = nullProto;

  const failure = new AutopilotAckPersistenceError({
    phase: "stage", outcome: "confirmed_empty", identity: accepted,
    message: "无原型对象守卫",
  });

  const snapshot = failure.identity.command as unknown as Record<string, unknown>;
  const clone = snapshot.__null_proto__ as Record<string, unknown>;
  assert.notEqual(clone, nullProto);
  assert.equal(clone.marker, "无原型对象");
  assert.equal(Object.isFrozen(clone), true);
  assert.equal(Object.isFrozen(nullProto), false);
});

test("代际围栏的滞留回执被持久丢弃,重放 409 command_not_current 不再闩死老人端", async () => {
  const { ApiError } = await import("../apiResponse.ts");
  const fenced = new ApiError(409, "test", {
    code: "autopilot_command_not_current",
    message: "设备回执命令已不是当前自主命令",
  }, "nested-detail");
  const store = new MemoryAckStore();
  const first = await DurableAutopilotAckDelivery.create({
    capability,
    store,
    transport: {
      next: async () => null,
      ack: async () => {
        store.events.push("http-lost");
        throw new Error("response lost");
      },
    },
  });
  await assert.rejects(() => first.send(question(), 0, { ack_type: "tts_started" }));
  assert.equal(store.pending?.ack.device_event_seq, 1);

  const recovered = await DurableAutopilotAckDelivery.create({
    capability,
    store,
    transport: {
      next: async () => null,
      ack: async () => {
        store.events.push("http-fenced");
        throw fenced;
      },
    },
  });
  // 按「无待恢复回执」继续:不抛错、不进 blocked、随后照常 /next。
  assert.equal(await recovered.drainPending(), null);
  assert.equal(store.pending, null);
  assert.equal(recovered.unresolvedSettlement, null);
  // 被围栏的序号已持久消费(服务器只要求严格递增,跳号安全)。
  assert.equal(store.checkpoint?.lastDeviceEventSeq, 1);
  assert.deepEqual(store.events, ["stage", "http-lost", "http-fenced", "complete"]);

  // 整页刷新重入:死回执不复活,一条 HTTP 都不发。
  const reloaded = await DurableAutopilotAckDelivery.create({
    capability,
    store,
    transport: {
      next: async () => null,
      ack: async () => { throw new Error("刷新后不得重放已丢弃回执"); },
    },
  });
  assert.equal(await reloaded.drainPending(), null);
  assert.equal(reloaded.initialDeviceEventSeq, 1);
});

test("非围栏拒因(如暂停窗口的 runtime_inactive)照旧保留待重放", async () => {
  const { ApiError } = await import("../apiResponse.ts");
  const pausedWindow = new ApiError(409, "test", {
    code: "autopilot_runtime_inactive",
    message: "P0a 要求显式 active 的场次运行状态",
  }, "nested-detail");
  const store = new MemoryAckStore();
  const first = await DurableAutopilotAckDelivery.create({
    capability,
    store,
    transport: {
      next: async () => null,
      ack: async () => { throw new Error("response lost"); },
    },
  });
  await assert.rejects(() => first.send(question(), 0, { ack_type: "tts_started" }));

  const recovered = await DurableAutopilotAckDelivery.create({
    capability,
    store,
    transport: {
      next: async () => null,
      ack: async () => { throw pausedWindow; },
    },
  });
  await assert.rejects(() => recovered.drainPending(), (error: unknown) => error === pausedWindow);
  // exact envelope 原样保留:恢复后重放才可能拿到围栏拒因并被安全丢弃。
  assert.equal(store.pending?.ack.device_event_seq, 1);
});
