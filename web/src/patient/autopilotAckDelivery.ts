import { blobStore, type AutopilotAckStorageSnapshot } from "../audio/blobStore.ts";
import type { DeviceCapabilityRecord } from "../security/deviceCapability.ts";
import {
  buildAutopilotAck,
  parseNextCommandProjection,
  type AutopilotAck,
  type AutopilotAckFacts,
  type NextCommandProjection,
} from "./autopilotProtocol.ts";
import type {
  AutopilotAckDelivery,
  AutopilotTransport,
} from "./autopilotController.ts";
import { commandSupersedesTerminalLatch } from "./autopilotRuntime.ts";
import {
  checkpointTerminalFailureLatch,
  createAutopilotAckEnvelope,
  fingerprintAutopilotCapability,
  nextAutopilotAckLatch,
  type AutopilotAckEnvelope,
  type AutopilotTerminalFailureLatch,
} from "./autopilotAckOutbox.ts";

const RECEIPT_KEYS = new Set([
  "scope_key", "ack_idempotency_key", "ack_type", "replayed", "command_state",
  "command_revision", "status", "state_revision", "command",
]);
const ACK_TYPES = new Set([
  "tts_started", "tts_ended", "tts_failed",
  "record_started", "record_stopped", "record_failed",
]);
const COMMAND_STATES = new Set(["pending", "started", "succeeded", "failed", "cancelled"]);
const CONTROL_STATUSES = new Set([
  "idle", "running", "waiting_tts", "waiting_recording", "processing_attempt",
  "manual_draining", "paused", "scope_completed", "failed",
]);

export interface AutopilotAckReceipt {
  scope_key: "p0a_sim_first_single_v1";
  ack_idempotency_key: string;
  ack_type: AutopilotAck["ack_type"];
  replayed: boolean;
  command_state: "pending" | "started" | "succeeded" | "failed" | "cancelled";
  command_revision: number;
  status: string;
  state_revision: number;
  command: NextCommandProjection | null;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

/** A generic 2xx is insufficient; bind completion to the exact immutable ACK. */
export function parseAutopilotAckReceipt(
  value: unknown,
  expected: AutopilotAckEnvelope,
): AutopilotAckReceipt {
  const row = record(value);
  if (!row || Object.keys(row).length !== RECEIPT_KEYS.size
      || !Object.keys(row).every((key) => RECEIPT_KEYS.has(key))
      || row.scope_key !== "p0a_sim_first_single_v1"
      || row.ack_idempotency_key !== expected.ack.idempotency_key
      || row.ack_type !== expected.ack.ack_type
      || typeof row.ack_type !== "string" || !ACK_TYPES.has(row.ack_type)
      || typeof row.replayed !== "boolean"
      || typeof row.command_state !== "string" || !COMMAND_STATES.has(row.command_state)
      || !Number.isSafeInteger(row.command_revision) || (row.command_revision as number) < 0
      || typeof row.status !== "string" || !CONTROL_STATUSES.has(row.status)
      || !Number.isSafeInteger(row.state_revision) || (row.state_revision as number) < 0) {
    throw new Error("服务器自动驾驶 ACK 收据与本机事实不一致");
  }
  const command = row.command === null ? null : parseNextCommandProjection(row.command);
  const expectedState = expected.ack.ack_type.endsWith("_started")
    ? "started"
    : expected.ack.ack_type.endsWith("_failed") ? "failed" : "succeeded";
  // A replay must prove the same immutable lifecycle edge, not merely echo an
  // idempotency key after the command has moved somewhere else.
  const minimumRevision = expected.ack.command_revision + 1;
  if (row.command_state !== expectedState
      || (row.replayed === false && row.command_revision !== minimumRevision)
      || (row.replayed === true && (row.command_revision as number) < minimumRevision)) {
    throw new Error("服务器 ACK 收据未证明本次命令转移");
  }
  if (row.replayed === true) {
    // The backend deliberately withholds a possibly newer projection on exact
    // replay. Requiring null here preserves that non-disclosure contract while
    // the immutable ACK tuple above remains strict.
    if (command !== null) throw new Error("服务器 ACK 重放不得投影可变命令");
  } else if (row.status === "waiting_tts") {
    if (command?.kind !== "tts") throw new Error("服务器 ACK 状态与 TTS 命令不一致");
  } else if (row.status === "waiting_recording") {
    if (command?.kind !== "record") throw new Error("服务器 ACK 状态与录音命令不一致");
  } else if (command !== null) {
    throw new Error("服务器 ACK 非媒体等待状态不得携带命令");
  }
  return {
    scope_key: "p0a_sim_first_single_v1",
    ack_idempotency_key: row.ack_idempotency_key as string,
    ack_type: row.ack_type as AutopilotAck["ack_type"],
    replayed: row.replayed,
    command_state: row.command_state as AutopilotAckReceipt["command_state"],
    command_revision: row.command_revision as number,
    status: row.status,
    state_revision: row.state_revision as number,
    command,
  };
}

export interface AutopilotAckStore {
  snapshot(ownerKey: string, sessionId: string): Promise<AutopilotAckStorageSnapshot>;
  stage(entry: AutopilotAckEnvelope): Promise<void>;
  stageRecordStoppedAndCompleteAudio(entry: AutopilotAckEnvelope): Promise<void>;
  complete(entry: AutopilotAckEnvelope): Promise<void>;
}

const indexedDbAckStore: AutopilotAckStore = {
  snapshot: (ownerKey, sessionId) => blobStore.autopilotAckSnapshot(ownerKey, sessionId),
  stage: (entry) => blobStore.stageAutopilotAck(entry),
  stageRecordStoppedAndCompleteAudio: (entry) =>
    blobStore.stageRecordStoppedAckAndCompleteAudio(entry),
  complete: (entry) => blobStore.completeAutopilotAck(entry),
};

function newIdempotencyKey(ackType: AutopilotAck["ack_type"], seq: number): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (!uuid) throw new Error("当前浏览器不能安全生成 ACK 幂等键");
  return `ack:${ackType}:${seq}:${uuid}`;
}

export class DurableAutopilotAckDelivery implements AutopilotAckDelivery {
  private lastSeq: number;
  private pending: AutopilotAckEnvelope | null;
  private latch: AutopilotTerminalFailureLatch | null;
  private readonly ownerKey: string;
  private readonly sessionId: string;
  private readonly transport: AutopilotTransport;
  private readonly store: AutopilotAckStore;

  private constructor(
    ownerKey: string,
    sessionId: string,
    transport: AutopilotTransport,
    store: AutopilotAckStore,
    snapshot: AutopilotAckStorageSnapshot,
  ) {
    this.ownerKey = ownerKey;
    this.sessionId = sessionId;
    this.transport = transport;
    this.store = store;
    this.lastSeq = snapshot.checkpoint?.lastDeviceEventSeq ?? 0;
    this.pending = snapshot.pending;
    this.latch = checkpointTerminalFailureLatch(snapshot.checkpoint);
    if (this.pending && this.pending.ack.device_event_seq !== this.lastSeq + 1) {
      throw new Error("ACK outbox 与 checkpoint 事件序号不连续");
    }
  }

  static async create(input: {
    capability: DeviceCapabilityRecord;
    transport: AutopilotTransport;
    store?: AutopilotAckStore;
  }): Promise<DurableAutopilotAckDelivery> {
    const ownerKey = await fingerprintAutopilotCapability(input.capability);
    const store = input.store ?? indexedDbAckStore;
    const snapshot = await store.snapshot(ownerKey, input.capability.sessionId);
    return new DurableAutopilotAckDelivery(
      ownerKey,
      input.capability.sessionId,
      input.transport,
      store,
      snapshot,
    );
  }

  get initialDeviceEventSeq(): number { return this.lastSeq; }

  /** Persisted exact evidence of the last completed tts_failed/record_failed ACK, if any. */
  get terminalFailureLatch(): AutopilotTerminalFailureLatch | null { return this.latch; }

  /**
   * Refresh recovery always drains the exact persisted event before /next.
   *
   * 返回的是整个持久 envelope 而不只是 ack：响应丢失后重启时，调用方要靠
   * envelope 里那条 exact command 才能把本地 runtime 恢复成正确的暂停态。
   * 只给 ack 的话，随后 /next 返回 null 会把状态冲成 waiting_command。
   */
  async drainPending(): Promise<AutopilotAckEnvelope | null> {
    const entry = this.pending;
    if (!entry) return null;
    await this.deliver(entry);
    return entry;
  }

  async send(
    command: NextCommandProjection,
    lastDeviceEventSeq: number,
    facts: AutopilotAckFacts,
  ): Promise<AutopilotAck> {
    if (lastDeviceEventSeq !== this.lastSeq) {
      throw new Error("控制器与 ACK checkpoint 序号不一致");
    }
    if (this.pending) {
      throw new Error("已有待恢复 ACK，禁止生成新事件序号");
    }
    if (this.latch && !commandSupersedesTerminalLatch(this.latch.command, command)) {
      // 已锁存的终态失败必须在任何 stage/音频原子接管之前挡住：这是
      // Hook 侧终态闸失守时的最后一道防线，绝不能因为调用方传错命令就
      // 悄悄发出一条 ACK 或接管本机录音字节。
      throw new Error("已锁存终态失败，命令未证明严格新权威，拒绝发送");
    }
    const seq = this.lastSeq + 1;
    const ack = buildAutopilotAck(
      command,
      this.lastSeq,
      newIdempotencyKey(facts.ack_type, seq),
      facts,
    );
    const entry = createAutopilotAckEnvelope({
      ownerKey: this.ownerKey,
      sessionId: this.sessionId,
      command,
      ack,
    });
    if (ack.ack_type === "record_stopped") {
      await this.store.stageRecordStoppedAndCompleteAudio(entry);
    } else {
      await this.store.stage(entry);
    }
    this.pending = entry;
    await this.deliver(entry);
    return ack;
  }

  private async deliver(entry: AutopilotAckEnvelope): Promise<void> {
    const response = await this.transport.ack(
      this.sessionId,
      entry.commandKey,
      entry.ack,
    );
    parseAutopilotAckReceipt(response, entry);
    await this.store.complete(entry);
    this.lastSeq = entry.ack.device_event_seq;
    this.pending = null;
    this.latch = nextAutopilotAckLatch(entry);
  }
}
