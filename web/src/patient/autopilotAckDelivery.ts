import { blobStore, type AutopilotAckStorageSnapshot } from "../audio/blobStore.ts";
import type { DeviceCapabilityRecord } from "../security/deviceCapability.ts";
import {
  buildAutopilotAck,
  parseNextCommandProjection,
  type AutopilotAck,
  type AutopilotAckFacts,
  type NextCommandProjection,
} from "./autopilotProtocol.ts";
import {
  AutopilotAckPersistenceError,
  type AutopilotAckDelivery,
  type AutopilotAckPersistenceIdentity,
  type AutopilotAckPersistenceOutcome,
  type AutopilotAckPersistencePhase,
  type AutopilotTransport,
} from "./autopilotController.ts";
import { pendingAckPermanentlyFenced } from "./autopilotDrainRetryPolicy.ts";
import { commandSupersedesTerminalLatch } from "./autopilotRuntime.ts";
import {
  checkpointTerminalFailureLatch,
  createAutopilotAckEnvelope,
  fingerprintAutopilotCapability,
  nextAutopilotAckLatch,
  sameAutopilotAckEnvelope,
  type AutopilotAckCheckpoint,
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

/**
 * 结构化比较用的规范 JSON。IndexedDB 交回来的永远是结构化克隆，不是内存里那个
 * 对象，所以 checkpoint / latch / command / ACK 一律不能用引用相等去判。
 */
function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

/** 终态 latch 的逐字相等：失败 ACK 必须绑同一条命令与同一条 ACK，非失败恒为 null。 */
function sameTerminalLatch(
  left: AutopilotTerminalFailureLatch | null,
  right: AutopilotTerminalFailureLatch | null,
): boolean {
  if (left === null || right === null) return left === right;
  return left.commandKey === right.commandKey
    && canonicalJson(left.command) === canonicalJson(right.command)
    && canonicalJson(left.ack) === canonicalJson(right.ack);
}

function newIdempotencyKey(ackType: AutopilotAck["ack_type"], seq: number): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (!uuid) throw new Error("当前浏览器不能安全生成 ACK 幂等键");
  return `ack:${ackType}:${seq}:${uuid}`;
}

/**
 * 一条 ACK 被服务器严格确认之后留下的权威事实。
 *
 * `command` 可能是 null：后端在 exact 重放和非媒体等待状态下**故意**不投影命令。
 * 那种情况下调用方没有权威 revision 可用，只能进安全暂停，绝不许本地合成一个。
 */
export interface AutopilotAckReceiptAuthority {
  readonly commandKey: string;
  readonly ackIdempotencyKey: string;
  readonly ackType: AutopilotAck["ack_type"];
  readonly commandRevision: number;
  readonly command: NextCommandProjection | null;
}

export class DurableAutopilotAckDelivery implements AutopilotAckDelivery {
  private lastSeq: number;
  private pending: AutopilotAckEnvelope | null;
  private latch: AutopilotTerminalFailureLatch | null;
  // pending 只有在 durable stage 落定之后才置位。stage 在途的那一小段里它仍是
  // null，若不另设标记，第二条 send 就能挤进来抢同一个 device_event_seq。
  private staging: AutopilotAckEnvelope | null = null;
  // 一次投递尝试的瞬时闸：第一次 transport 之前置位，一直握到严格收据、complete
  // 与结果判定全部收口。它在手时，任何 send/drain 都不许再发第二条 HTTP。
  private attempt: AutopilotAckEnvelope | null = null;
  // 结果不确定时的粘滞闸：同页 send/drain 一律 fail closed，等刷新后由 durable
  // 存储自己收敛，本页绝不猜。
  private unresolved: AutopilotAckPersistenceError | null = null;
  // 本实例造出来的 typed 拒绝。归属证明不落在错误对象上，所以伪造不出来。
  private readonly ownedFailures = new WeakSet<AutopilotAckPersistenceError>();
  // 纯观察面，不参与任何生产判据。生成了却没 staged、也没 transport 的幂等键，
  // 在 store 的 staged 条目和 transport 收到的 ack 上都不留痕——"零新 key"那类
  // 断言因此只证明了"零新 staged key"，杀不掉每次失败都偷偷多生成一个 UUID 的
  // 实现。只暴露计数不暴露值：值已经能从 staged 条目读到，再留一份反而会在内存
  // 里留住一个即使失败也不该被复用的 key。
  private generatedKeyCount = 0;
  private receiptAuthority: AutopilotAckReceiptAuthority | null = null;
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
   * The exact command the server itself returned with the last confirmed ACK.
   *
   * A pagehide landing in the "record_started confirmed, /next refresh still in
   * flight or failed" window has to build its failure ACK from this, because the
   * only other source of the started revision is a refresh that has not
   * returned. Verifiable by command key + ACK idempotency key, so a caller
   * cannot mistake some other command's authority for this one.
   */
  get lastReceiptAuthority(): AutopilotAckReceiptAuthority | null {
    return this.receiptAuthority;
  }

  /** Is an ACK currently between "envelope created" and "durably staged"? */
  get stagingInFlight(): boolean { return this.staging !== null; }

  /** 本实例累计生成过多少个幂等键。只被测试读，不参与任何生产判据。 */
  get generatedIdempotencyKeyCount(): number { return this.generatedKeyCount; }

  /** 未定型的持久化结果。非 null 时本页 send/drain 一律 fail closed。 */
  get unresolvedSettlement(): AutopilotAckPersistenceError | null {
    return this.unresolved;
  }

  /**
   * 归属证明。字段全等但 owner 不同的伪造错误必须被拒，所以判据是本实例私有的
   * WeakSet 加 owner/session，而不是错误对象上的任何自述字段。
   */
  ownsPersistenceError(error: unknown): error is AutopilotAckPersistenceError {
    return error instanceof AutopilotAckPersistenceError
      && this.ownedFailures.has(error)
      && error.identity.ownerKey === this.ownerKey
      && error.identity.sessionId === this.sessionId;
  }

  /**
   * Refresh recovery always drains the exact persisted event before /next.
   *
   * 返回的是整个持久 envelope 而不只是 ack：响应丢失后重启时，调用方要靠
   * envelope 里那条 exact command 才能把本地 runtime 恢复成正确的暂停态。
   * 只给 ack 的话，随后 /next 返回 null 会把状态冲成 waiting_command。
   */
  async drainPending(): Promise<AutopilotAckEnvelope | null> {
    // 未定型的结果是粘滞的：本页绝不据此再发一条 HTTP，也绝不造第二个 key。
    if (this.unresolved) throw this.unresolved;
    if (this.attempt || this.staging) {
      throw new Error("已有未定型的 ACK 投递，禁止重放");
    }
    const entry = this.pending;
    if (!entry) return null;
    try {
      await this.deliver(entry);
    } catch (error) {
      if (pendingAckPermanentlyFenced(error)) {
        // 恢复/接管轮换代际后,这条回执被服务器永久围栏:重放永远 409,不丢弃
        // 就会在每次进场(含刷新)时闩死老人端。持久丢弃并推进本地序号,
        // 调用方按「无待恢复回执」继续走 /next。
        await this.discardFencedPending(entry);
        return null;
      }
      throw error;
    }
    return entry;
  }

  /**
   * 服务器已按代际围栏该命令,回执不可能再被接受:持久移除并消费其事件序号。
   * 序号只需严格递增不需连续,跳过被丢弃的序号对服务端安全;终态失败 latch 照常
   * 落存——它只挡「同代际重发」,新代际命令经 commandSupersedesTerminalLatch 放行。
   */
  private async discardFencedPending(entry: AutopilotAckEnvelope): Promise<void> {
    await this.store.complete(entry);
    this.lastSeq = entry.ack.device_event_seq;
    this.pending = null;
    this.latch = nextAutopilotAckLatch(entry);
  }

  private async readOwnerSnapshot(): Promise<
    { ok: true; value: AutopilotAckStorageSnapshot } | { ok: false; error: unknown }
  > {
    try {
      return { ok: true, value: await this.store.snapshot(this.ownerKey, this.sessionId) };
    } catch (error) {
      return { ok: false, error };
    }
  }

  /** snapshot 的 pending 是不是逐字等于这条候选。畸形与外来一律 false，不外抛。 */
  private isExactPending(
    pending: AutopilotAckEnvelope | null,
    entry: AutopilotAckEnvelope,
  ): boolean {
    if (!pending) return false;
    try { return sameAutopilotAckEnvelope(pending, entry); }
    catch { return false; }
  }

  /** checkpoint 是否仍然证明本实例当前的语义 C：owner/session/lastSeq/latch 全等。 */
  private provesCurrentCheckpoint(checkpoint: AutopilotAckCheckpoint | null): boolean {
    if (checkpoint === null) return this.lastSeq === 0 && this.latch === null;
    return checkpoint.ownerKey === this.ownerKey
      && checkpoint.sessionId === this.sessionId
      && checkpoint.lastDeviceEventSeq === this.lastSeq
      && sameTerminalLatch(checkpointTerminalFailureLatch(checkpoint), this.latch);
  }

  /**
   * checkpoint 是否证明这条 entry 已经 complete。
   *
   * schema v1 落在目标 seq 上**不是**完成证明：v1 根本不记 latch，拿它当证据就等于
   * 把"终态失败有没有锁存"这件事猜过去。
   */
  private provesCompleted(
    checkpoint: AutopilotAckCheckpoint | null,
    entry: AutopilotAckEnvelope,
  ): boolean {
    if (checkpoint === null || checkpoint.schemaVersion !== 2) return false;
    return checkpoint.ownerKey === this.ownerKey
      && checkpoint.sessionId === this.sessionId
      && checkpoint.lastDeviceEventSeq === entry.ack.device_event_seq
      && sameTerminalLatch(checkpoint.latch, nextAutopilotAckLatch(entry));
  }

  /** 每一次失败都新造一个，调用方正在判的那条事实不会被后来的调用改写。 */
  private persistenceFailure(
    entry: AutopilotAckEnvelope,
    phase: AutopilotAckPersistencePhase,
    outcome: AutopilotAckPersistenceOutcome,
    message: string,
    cause: unknown,
  ): AutopilotAckPersistenceError {
    const identity: AutopilotAckPersistenceIdentity = {
      ownerKey: entry.ownerKey,
      sessionId: entry.sessionId,
      commandKey: entry.commandKey,
      command: entry.command,
      ack: entry.ack,
      ackType: entry.ack.ack_type,
      idempotencyKey: entry.ack.idempotency_key,
      deviceEventSeq: entry.ack.device_event_seq,
      commandRevision: entry.ack.command_revision,
      controlGeneration: entry.ack.control_generation,
      runnerGeneration: entry.ack.runner_generation,
      mimeType: entry.ack.ack_type === "record_started" ? entry.ack.mime_type : null,
    };
    const failure = new AutopilotAckPersistenceError({
      phase, outcome, identity, message, cause,
    });
    this.ownedFailures.add(failure);
    return failure;
  }

  /** unknown：保留 exact identity，转成粘滞闸，之后同页零网络、零新 key。 */
  private retainUnresolved(
    entry: AutopilotAckEnvelope,
    phase: AutopilotAckPersistencePhase,
    message: string,
    cause: unknown,
  ): AutopilotAckPersistenceError {
    const failure = this.persistenceFailure(entry, phase, "unknown", message, cause);
    this.staging = null;
    this.attempt = null;
    this.unresolved = failure;
    return failure;
  }

  /**
   * 正常 complete 成功与 snapshot 证明已完成，必须收敛到这一个内存提交点，
   * 否则 lastSeq、pending、latch 与回执权威迟早会各走各的。
   */
  private commitCompleted(entry: AutopilotAckEnvelope, receipt: AutopilotAckReceipt): void {
    this.lastSeq = entry.ack.device_event_seq;
    this.pending = null;
    this.latch = nextAutopilotAckLatch(entry);
    this.receiptAuthority = {
      commandKey: entry.commandKey,
      ackIdempotencyKey: entry.ack.idempotency_key,
      ackType: receipt.ack_type,
      commandRevision: receipt.command_revision,
      command: receipt.command,
    };
  }

  async send(
    command: NextCommandProjection,
    lastDeviceEventSeq: number,
    facts: AutopilotAckFacts,
  ): Promise<AutopilotAck> {
    if (lastDeviceEventSeq !== this.lastSeq) {
      throw new Error("控制器与 ACK checkpoint 序号不一致");
    }
    if (this.unresolved) throw this.unresolved;
    if (this.pending || this.staging || this.attempt) {
      throw new Error("已有待恢复 ACK，禁止生成新事件序号");
    }
    if (this.latch && !commandSupersedesTerminalLatch(this.latch.command, command)) {
      // 已锁存的终态失败必须在任何 stage/音频原子接管之前挡住：这是
      // Hook 侧终态闸失守时的最后一道防线，绝不能因为调用方传错命令就
      // 悄悄发出一条 ACK 或接管本机录音字节。
      throw new Error("已锁存终态失败，命令未证明严格新权威，拒绝发送");
    }
    const seq = this.lastSeq + 1;
    const idempotencyKey = newIdempotencyKey(facts.ack_type, seq);
    // 计数点必须紧跟生成、排在 buildAutopilotAck 之前：后者会因非法 facts 抛错，
    // 而"生成了却没进 envelope"正是这个 seam 唯一看得见的地方。放到它之后就漏了。
    this.generatedKeyCount += 1;
    const ack = buildAutopilotAck(
      command,
      this.lastSeq,
      idempotencyKey,
      facts,
    );
    const entry = createAutopilotAckEnvelope({
      ownerKey: this.ownerKey,
      sessionId: this.sessionId,
      command,
      ack,
    });
    // 顺序是冻结的：唯一 envelope → 标记 in-flight（挡住任何新 send）→ durable
    // stage → 证明 durable 之后才允许 transport。stage 证明前 transport.ack、
    // /next 与 device event seq 推进都必须是 0 次。
    this.staging = entry;
    try {
      if (ack.ack_type === "record_stopped") {
        await this.store.stageRecordStoppedAndCompleteAudio(entry);
      } else {
        await this.store.stage(entry);
      }
    } catch (stageError) {
      // 闸绝不在这里无条件松开：整段 snapshot 判定都必须握着它，否则并发的
      // drain/send 能挤进来发第二条 HTTP，或者给同一个 seq 造第二个 key。
      const snapshot = await this.readOwnerSnapshot();
      if (!snapshot.ok) {
        throw this.retainUnresolved(entry, "stage",
          "ACK 暂存结果不可读，已保留原 envelope 并安全暂停", snapshot.error);
      }
      const { pending, checkpoint } = snapshot.value;
      if (this.isExactPending(pending, entry) && this.provesCurrentCheckpoint(checkpoint)) {
        // matching：这条 exact envelope 其实已经 durable（record_stopped 则连音频
        // 原子接管一起提交了）。只松开瞬时暂存标记，绝不再 stage 一次，继续同一个
        // key 走 transport → 严格收据 → complete。
        this.staging = null;
        this.pending = entry;
        await this.deliver(entry);
        return ack;
      }
      if (pending === null && this.provesCurrentCheckpoint(checkpoint)) {
        // 证明未提交：stage 证明之前 transport 一次都没跑过，这条候选既不是
        // durable 事实也不是 server 事件。松开被丢弃的候选闸，由调用方决定那条
        // 唯一的同 seq 替换——这是唯一能授权替换的持久化结果。
        this.staging = null;
        throw this.persistenceFailure(entry, "stage", "confirmed_empty",
          "ACK 暂存事务未提交", stageError);
      }
      throw this.retainUnresolved(entry, "stage",
        "ACK 暂存结果不确定，已保留原 envelope 并安全暂停", stageError);
    }
    this.staging = null;
    this.pending = entry;
    await this.deliver(entry);
    return ack;
  }

  private async deliver(entry: AutopilotAckEnvelope): Promise<void> {
    // 投递尝试闸：第一次 transport 之前置位，一直握到严格收据、complete 与结果
    // 判定全部收口。并发的 send/drain 在此期间零 HTTP、零 complete、零新 key。
    this.attempt = entry;
    let response: unknown;
    try {
      response = await this.transport.ack(this.sessionId, entry.commandKey, entry.ack);
    } catch (error) {
      // 只松开这一层瞬时闸：exact pending 原样保留，之后 drain 只能重放同一个 key。
      this.attempt = null;
      throw error;
    }
    let receipt: AutopilotAckReceipt;
    try {
      receipt = parseAutopilotAckReceipt(response, entry);
    } catch (error) {
      // 严格解析被拒同理：complete 一次都不调，seq/latch/权威都不推进，只松开
      // 瞬时闸。它绝不算作 stage/complete 的 empty，也授权不了任何替换。
      this.attempt = null;
      throw error;
    }
    try {
      await this.store.complete(entry);
    } catch (completeError) {
      // 收据已经严格验过，所以这里没有任何结果可以授权替换：只能证明它到底
      // 提交没有。闸继续握着，snapshot 也读在闸内。
      const snapshot = await this.readOwnerSnapshot();
      if (!snapshot.ok) {
        throw this.retainUnresolved(entry, "complete",
          "ACK 完成结果不可读，已保留原 envelope 并安全暂停", snapshot.error);
      }
      const { pending, checkpoint } = snapshot.value;
      if (pending === null && this.provesCompleted(checkpoint, entry)) {
        // 已提交：把那条已经严格校验过的收据一次性落进内存，原 ACK 正常返回。
        this.commitCompleted(entry, receipt);
        this.attempt = null;
        return;
      }
      if (this.isExactPending(pending, entry) && this.provesCurrentCheckpoint(checkpoint)) {
        // 仍然 pending：保留 exact envelope，seq/latch/权威一步不推，之后的恢复
        // 只能重放同一条 envelope 与同一个 key。
        this.attempt = null;
        throw this.persistenceFailure(entry, "complete", "matching_pending",
          "ACK 完成事务未提交，保留原 envelope 待重放", completeError);
      }
      throw this.retainUnresolved(entry, "complete",
        "ACK 完成结果不确定，已保留原 envelope 并安全暂停", completeError);
    }
    this.commitCompleted(entry, receipt);
    this.attempt = null;
  }
}
