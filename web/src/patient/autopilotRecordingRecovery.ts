/**
 * 刷新恢复的判定与收尾。刻意不 import api / blobStore / 真实录音设备：所有对外
 * 副作用都从 AutopilotRecoveryDependencies 注进来，这样这条链路可以在没有浏览器
 * 的情况下被完整驱动一遍。生产实现在 autopilotRecordingExecutor.ts。
 */
import type { AudioDeviceLease } from "../audio/audioDeviceLease.ts";
import type { AudioOutboxEntry } from "../audio/audioOutbox.ts";
import type { AutopilotTerminalFailureLatch } from "./autopilotAckOutbox.ts";
import { assertCaptureDurationWithinCommandLimit } from "./autopilotCaptureWindow.ts";
import { AutopilotMediaError } from "./autopilotMediaError.ts";
import {
  parseNextCommandProjection,
  type AutopilotAck,
  type AutopilotStopReason,
  type NextCommandProjection,
} from "./autopilotProtocol.ts";
import {
  applyRecoveredRecordFailure,
  autopilotRuntimeReducer,
  commandSupersedesTerminalLatch,
  restoreAutopilotRuntime,
  restoreFailedAckRuntime,
  type AutopilotRuntimeState,
} from "./autopilotRuntime.ts";

type RecordCommand = Extract<NextCommandProjection, { kind: "record" }>;

export interface RecordStoppedFacts {
  stop_reason: AutopilotStopReason;
  raw_audio_id: string;
  receipt_server_seq: number;
  checksum: string;
  byte_count: number;
  duration_seconds: number;
}

export interface AutopilotRecoverySnapshot {
  entries: AudioOutboxEntry[];
  legacyOrphans: readonly unknown[];
  invalidBlobKeyCount: number;
}

export interface AutopilotRecoveryDependencies {
  acquireLease(): Promise<AudioDeviceLease>;
  recoverySnapshot(): Promise<AutopilotRecoverySnapshot>;
  readBlob(rawAudioId: string): Promise<Blob | null | undefined>;
  /** 唯一会 register / upload / putAudioSaved 的一步。 */
  finish(command: RecordCommand, entry: AudioOutboxEntry, blob: Blob):
    Promise<RecordStoppedFacts>;
}

export interface AutopilotRecoveryAckDelivery {
  readonly initialDeviceEventSeq: number;
  readonly terminalFailureLatch: AutopilotTerminalFailureLatch | null;
  send(
    command: NextCommandProjection,
    lastDeviceEventSeq: number,
    facts: Record<string, unknown>,
  ): Promise<AutopilotAck>;
}

/** 逐步 best-effort 清理：任何一步抛错或拒绝都不阻断后面的步骤。 */
export async function runBestEffortCleanup(
  steps: Array<() => void | Promise<void> | undefined>,
): Promise<void> {
  for (const step of steps) {
    try { await step(); } catch { /* 清理尽力而为；主错误已在上游定型 */ }
  }
}

/**
 * capture 最外层收尾：清理逐步 best-effort，最后**无论如何**标记 closed。
 *
 * closed 是 controller 判断"麦克风物理上已经不可能再出声"的唯一依据。清理里任何
 * 一步抛错都不能让它悬着，否则 controller 永远等在 await capture.closed 上。
 */
export async function settleCaptureCleanup(
  steps: Array<() => void | Promise<void> | undefined>,
  markClosed: () => void,
): Promise<void> {
  try {
    await runBestEffortCleanup(steps);
  } finally {
    markClosed();
  }
}

export function assertEntryMatchesCommand(
  entry: AudioOutboxEntry,
  command: RecordCommand,
): void {
  if (entry.rawAudioId !== command.payload.raw_audio_id
      || entry.sessionId.length === 0
      || entry.turnKey !== command.payload.turn_ref
      || entry.containsDirectIdentifier !== command.payload.contains_direct_identifier
      || entry.autopilotStopReason === undefined) {
    throw new Error("本机录音 outbox 与服务器当前录音命令不一致");
  }
}

/**
 * Refresh recovery for a physically stopped capture. It can finish upload and
 * audioSaved only when the server's exact started command matches the durable
 * outbox; otherwise it refuses to reopen or synthesize a recording.
 */
async function recoverAutopilotRecording(
  sessionId: string,
  command: RecordCommand,
  deps: AutopilotRecoveryDependencies,
): Promise<RecordStoppedFacts | null> {
  if (command.state !== "started") return null;
  const lease = await deps.acquireLease();
  try {
    const snapshot = await deps.recoverySnapshot();
    if (snapshot.invalidBlobKeyCount > 0 || snapshot.legacyOrphans.length > 0) {
      throw new Error("本机录音恢复存储状态非法");
    }
    if (snapshot.entries.length === 0) return null;
    if (snapshot.entries.length !== 1) {
      throw new Error("自动驾驶恢复期存在多份待处置录音");
    }
    const entry = snapshot.entries[0];
    assertEntryMatchesCommand(entry, command);
    if (entry.sessionId !== sessionId) throw new Error("恢复录音属于其他场次");
    // 时长闸必须排在读字节 / register / upload / audioSaved 之前。刷新恢复走的
    // 是同一条上限判据：这段录音服务器一定会拒，那就一个字节都不要送上去。
    assertCaptureDurationWithinCommandLimit(
      entry.durationSeconds, command.payload.max_duration_seconds);
    const blob = await deps.readBlob(entry.rawAudioId);
    if (!blob) throw new Error("恢复录音缺少本机原始字节");
    return await deps.finish(command, entry, blob);
  } finally {
    // 租约释放同样 best-effort：清理异常绝不能盖掉上面那个 recording_runtime_failed，
    // 否则超限录音就退化成一个随机错误，稳定的 record_failed 也就发不出去了。
    await runBestEffortCleanup([
      () => { lease.release(); },
      () => lease.released,
    ]);
  }
}

export type AutopilotRecoverySettlement =
  | { outcome: "none" }
  | { outcome: "stopped"; ack: AutopilotAck }
  | { outcome: "failed"; ack: AutopilotAck };

/**
 * 刷新后把一条 started 录音命令收口成服务器可见的终态，只有两种出口：
 *
 * - 本机 outbox 完整且时长过得了这条命令的上限 → 补一条 record_stopped。
 * - 时长顶穿上限（或读数本身是 NaN/Infinity/负数）→ 补一条**同 started revision**
 *   的 record_failed(recording_runtime_failed)，服务器安全暂停。本机 outbox 和
 *   字节原样留着等人工处置，绝不上传、绝不伪造一条 record_stopped。
 */
export async function settleAutopilotRecordingRecovery(
  sessionId: string,
  command: RecordCommand,
  delivery: AutopilotRecoveryAckDelivery,
  deps: AutopilotRecoveryDependencies,
): Promise<AutopilotRecoverySettlement> {
  let recovered: RecordStoppedFacts | null;
  try {
    recovered = await recoverAutopilotRecording(sessionId, command, deps);
  } catch (error) {
    if (!(error instanceof AutopilotMediaError)
        || error.errorCode !== "recording_runtime_failed") throw error;
    // 回执要交回调用方落进本地 runtime：服务器已经暂停，而随后的 /next 返回
    // null 不得把这个暂停事实冲回 waiting_command。
    const ack = await delivery.send(command, delivery.initialDeviceEventSeq, {
      ack_type: "record_failed",
      error_code: "recording_runtime_failed",
    });
    return { outcome: "failed", ack };
  }
  if (!recovered) return { outcome: "none" };
  const ack = await delivery.send(command, delivery.initialDeviceEventSeq, {
    ack_type: "record_stopped",
    ...recovered,
  });
  return { outcome: "stopped", ack };
}

/** drainPending 交回来的持久 envelope：exact command + exact ack，缺一不可。 */
export interface AutopilotDrainedAck {
  command: unknown;
  ack: AutopilotAck;
}

export interface AutopilotRefreshPorts {
  sessionId: string;
  next(sessionId: string): Promise<unknown | null>;
  delivery: AutopilotRecoveryAckDelivery;
  deps: AutopilotRecoveryDependencies;
  /** 本次启动重放掉的持久 ACK；轮询路径没有这一步，传 null。 */
  drained?: AutopilotDrainedAck | null;
}

function pausedWithoutCommand(
  lastDeviceEventSeq: number,
  pauseReason: "inflight_command_recovery_required" | "technical_failure",
): AutopilotRuntimeState {
  return {
    phase: "paused",
    command: null,
    last_device_event_seq: lastDeviceEventSeq,
    last_ack: null,
    pause_reason: pauseReason,
  };
}

/**
 * /next 说"当前没有命令"时，本机还得自己看一眼有没有没收口的录音。
 *
 * record_failed 第一次就发成功的话，ACK envelope 会被 complete 掉，但音频 outbox
 * 按设计仍然留着等人工处置。再刷新一次就既没有 pending ACK、服务器也还是 null——
 * 这时候恢复成 waiting_command 等于宣布"一切正常，继续轮询"，而本机那段录音还在。
 * 所以只要本会话还有待处置 outbox（或本机恢复状态根本读不出/非法），一律 hydrate
 * 成 paused：不开媒体、不上传。没有 exact ACK 就绝不伪造一条 record_failed。
 */
async function hydrateIdleRuntime(
  sessionId: string,
  lastDeviceEventSeq: number,
  deps: AutopilotRecoveryDependencies,
): Promise<AutopilotRuntimeState> {
  let snapshot: AutopilotRecoverySnapshot;
  try {
    snapshot = await deps.recoverySnapshot();
  } catch {
    // 读不出本机状态就不敢说"没事"。
    return pausedWithoutCommand(lastDeviceEventSeq, "technical_failure");
  }
  if (snapshot.invalidBlobKeyCount > 0 || snapshot.legacyOrphans.length > 0) {
    return pausedWithoutCommand(lastDeviceEventSeq, "technical_failure");
  }
  if (snapshot.entries.some((entry) => entry.sessionId === sessionId)) {
    return pausedWithoutCommand(
      lastDeviceEventSeq, "inflight_command_recovery_required");
  }
  return restoreAutopilotRuntime(null, lastDeviceEventSeq);
}

/**
 * "reconcile" 闸：checkpoint 里持久化的终态失败 latch 是否被这次刷新读到的
 * /next 投影解闩。null、畸形、同一条命令、代际不严格双双前进、seq 回退，
 * 一律返回锁存的 paused 状态本身（调用方直接用它，不再读 hydrateIdleRuntime
 * 的启发式）；只有 commandSupersedesTerminalLatch 判定真正新权威时才返回
 * null，交回调用方按 restoreAutopilotRuntime 走一般新鲜恢复。
 */
export function reconcileAutopilotTerminalLatch(
  latch: AutopilotTerminalFailureLatch,
  current: unknown | null,
): AutopilotRuntimeState | null {
  if (current !== null) {
    try {
      const candidate = parseNextCommandProjection(current);
      if (commandSupersedesTerminalLatch(latch.command, candidate)) return null;
    } catch { /* 畸形投影不构成新权威，维持锁存 */ }
  }
  return {
    phase: "paused",
    command: latch.command,
    last_device_event_seq: latch.ack.device_event_seq,
    last_ack: latch.ack,
    pause_reason: latch.ack.ack_type,
  };
}

/**
 * 刷新/重连后把服务器投影与本机持久事实合成一个本地 runtime。
 *
 * 恢复入口按优先级 fail closed：
 * 0. checkpoint 里持久化着一条已完成的 tts_failed/record_failed latch，且这次
 *    /next 没能证明真正新权威 → 直接落成锁存的 paused，不再往下走。latch 判定
 *    必须发生在 restoreAutopilotRuntime(current) 之前——后者一读到畸形投影就
 *    会抛错，而"畸形投影不解闩"要求这里绝不能因为解析失败而丢掉暂停事实。
 * 1. latch 判定为严格新权威（或本来就没有 latch）时，若本次启动还重放了一条
 *    drained 失败回执——生产环境下 drainPending 成功后 delivery.latch 必然已经
 *    反映同一条事实，所以这里只在没有 latch 时才信 drained（兼容旧版/测试替身
 *    尚未同步 latch 的场景）；latch 判定为严格新权威时，这条 drained 回执已经
 *    是过时证据，必须忽略，改用这次 /next 的新投影继续恢复。
 * 2. /next 给的是一条 started 录音命令 → 交给恢复链补 record_stopped 或
 *    同 revision 的 record_failed。
 * 3. /next 是 null 但本机还有待处置录音 → paused，不再开媒体。
 */
export async function restoreAutopilotRuntimeAfterRefresh(
  ports: AutopilotRefreshPorts,
): Promise<AutopilotRuntimeState> {
  const drained = ports.drained ?? null;
  let current = await ports.next(ports.sessionId);

  const latch = ports.delivery.terminalFailureLatch;
  if (latch) {
    const held = reconcileAutopilotTerminalLatch(latch, current);
    if (held) return held;
    // 严格新权威已证明：这条 latch 不再持有，同一次刷新里重放的 drained 失败
    // 回执（如果有）针对的是同一条已被取代的旧命令，绝不能再用它盖过新投影。
  } else if (drained) {
    // 没有持久 latch 时才信 drained：重放只是补收据，这条失败其实早被服务器
    // 接受、场次已经暂停，/next 自然是 null。用 envelope 里那条 exact 命令把
    // 本地状态摆回去，别让 null 冲掉它。tts_failed 与 record_failed、pending
    // 与 started 都走这一条；非失败回执返回 null，照原路径走。
    const replayed = restoreFailedAckRuntime(drained);
    if (replayed) {
      return autopilotRuntimeReducer(
        replayed, { type: "server_command", command: current });
    }
  }

  let runtime = restoreAutopilotRuntime(
    current, ports.delivery.initialDeviceEventSeq);

  if (runtime.command?.kind === "record" && runtime.command.state === "started") {
    // 补 record_stopped 还是补同 revision 的 record_failed 由恢复链自己判：
    // 本机时长过不了这条命令的上限时它只发失败回执，不上传、不伪造停止。
    const settled = await settleAutopilotRecordingRecovery(
      ports.sessionId, runtime.command, ports.delivery, ports.deps);
    if (settled.outcome === "failed") {
      // 先把这条 exact 失败回执落进本地 runtime（paused/record_failed + 保留命令
      // 证据），再让 /next 做一致性核对。reducer 在 paused 状态下不接受服务端
      // 投影，所以 null 冲不掉这个暂停事实；本机字节仍留着等人工处置。
      runtime = applyRecoveredRecordFailure(runtime, settled.ack);
      current = await ports.next(ports.sessionId);
      return autopilotRuntimeReducer(runtime, {
        type: "server_command", command: current });
    }
    if (settled.outcome === "stopped") {
      current = await ports.next(ports.sessionId);
      runtime = restoreAutopilotRuntime(
        current, ports.delivery.initialDeviceEventSeq);
    }
  }

  if (runtime.phase === "waiting_command" && runtime.command === null) {
    return hydrateIdleRuntime(
      ports.sessionId, runtime.last_device_event_seq, ports.deps);
  }
  return runtime;
}
