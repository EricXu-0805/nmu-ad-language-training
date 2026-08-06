import type { DeviceCapabilityRecord } from "../security/deviceCapability.ts";
import {
  buildAutopilotAck,
  type AutopilotAck,
  type AutopilotErrorCode,
  type AutopilotMimeType,
  type AutopilotStopReason,
  type NextCommandProjection,
} from "./autopilotProtocol.ts";
import {
  autopilotRuntimeReducer,
  canOpenAutopilotMicrophone,
  canPlayAutopilotSpeech,
  restoreAutopilotRuntime,
  type AutopilotRuntimeState,
} from "./autopilotRuntime.ts";
import { autopilotMediaErrorCode } from "./autopilotMediaError.ts";

type TtsCommand = Extract<NextCommandProjection, { kind: "tts" }>;
type RecordCommand = Extract<NextCommandProjection, { kind: "record" }>;

export interface AutopilotTransport {
  next(sessionId: string): Promise<unknown | null>;
  ack(sessionId: string, commandKey: string, ack: AutopilotAck): Promise<unknown>;
}

export interface AutopilotSpeechPlayback {
  /** Resolves only after the browser/media element has really begun playback. */
  started: Promise<{ media_duration_ms?: number }>;
  /** Resolves only after the same playback reaches its real ended event. */
  ended: Promise<{ media_duration_ms?: number }>;
  /** Resolves only after playback/fetch teardown can no longer emit audio. */
  closed: Promise<void>;
  cancel(): void;
}

export interface AutopilotSpeechExecutor {
  start(command: TtsCommand): AutopilotSpeechPlayback;
}

export interface AutopilotRecordingCapture {
  /** Resolves only after MediaRecorder is actually recording. */
  started: Promise<{
    mime_type: AutopilotMimeType;
    sample_rate_hz?: number;
    channels?: 1 | 2;
  }>;
  /**
   * Resolves only after durable upload + audioSaved returned the exact receipt
   * tuple. A recorder adapter must never manufacture this tuple locally.
   */
  stopped: Promise<{
    stop_reason: AutopilotStopReason;
    raw_audio_id: string;
    receipt_server_seq: number;
    checksum: string;
    byte_count: number;
    duration_seconds: number;
  }>;
  /**
   * Resolves only after pending getUserMedia, MediaRecorder, persistence and the
   * device lease have all settled. A timeout is not physical microphone proof.
   */
  closed: Promise<void>;
  /** Optional patient "我说好了" boundary; it must still persist the same capture. */
  requestStop?(reason?: "user_done"): void;
  /** True once the browser fired a real `onstart`, regardless of any ACK. */
  readonly locallyStarted?: boolean;
  /**
   * Page-lifecycle interruption. Deliberately separate from `cancel()`, which
   * after a real start signals `stream_end` and uploads the half recording.
   * Returns its phase synchronously so the caller never has to guess whether
   * `started` resolved.
   */
  interrupt?(): "before_start" | "after_start" | "too_late" | "already_interrupted";
  cancel(): void;
}

export interface AutopilotRecordingExecutor {
  start(command: RecordCommand): AutopilotRecordingCapture;
}

/** 一次 ACK 投递在持久化边界上卡住的那个阶段。 */
export type AutopilotAckPersistencePhase = "stage" | "complete";

/**
 * 持久化结果的三分类。`matching_pending` 只会出现在 complete 阶段——stage 阶段
 * 的 matching 意味着那条 exact envelope 其实已经 durable，投递继续走同一个 key，
 * 根本不产生拒绝。
 */
export type AutopilotAckPersistenceOutcome =
  | "confirmed_empty"
  | "matching_pending"
  | "unknown";

/**
 * 这次投递尝试的不可变身份，全部从那条 exact envelope 抄下来。
 *
 * 它必须自带完整事实：调用方要判"这就是我刚才那条候选吗"，靠的是逐字对齐，
 * 而不是回头去读 delivery 上某个随时会被下一次调用改写的 getter。
 */
export interface AutopilotAckPersistenceIdentity {
  readonly ownerKey: string;
  readonly sessionId: string;
  readonly commandKey: string;
  readonly command: NextCommandProjection;
  readonly ack: AutopilotAck;
  readonly ackType: AutopilotAck["ack_type"];
  readonly idempotencyKey: string;
  readonly deviceEventSeq: number;
  readonly commandRevision: number;
  readonly controlGeneration: number;
  readonly runnerGeneration: number;
  /** 仅 `record_started` 带编码；其余 ACK 恒为 null。 */
  readonly mimeType: AutopilotMimeType | null;
}

/**
 * 授权图只收 plain object、数组与原语。
 *
 * `Object.getPrototypeOf` 能挡住 Date / Map / Set / RegExp / 类实例这类普通的非
 * plain 对象，碰到就抛而不是静默展平。它不是通用 Proxy 探测器，所以首要保证是
 * 封闭的调用域：唯一的生产调用者只会传进本仓库自己解析或构造出来的 plain 授权
 * 对象（parseNextCommandProjection / parseAutopilotAck / buildAutopilotAck）。
 */
function isPlainAuthorityObject(value: object): boolean {
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/** 逐键脱离：复制在前、冻结在后，绝不就地冻结调用方的对象。 */
function detachAuthorityValue<T>(value: T): T {
  if (value === null || typeof value !== "object") {
    if (typeof value === "function") throw new TypeError("授权图不接受函数");
    return value;
  }
  if (Array.isArray(value)) return detachAuthorityArray(value) as unknown as T;
  if (!isPlainAuthorityObject(value)) {
    throw new TypeError("授权图只接受 plain object 与数组");
  }
  return detachAuthorityObject(value as Record<string, unknown>) as unknown as T;
}

/**
 * 数组分支是精确的，不是 `Array.map`。
 *
 * `new Array(length)` 钉住 exact length；`Object.keys` 只交回自有可枚举字符串键，
 * 于是稀疏洞永远不被定义、自有 explicit-undefined 索引被定义成值为 undefined 的
 * 自有数据属性、可枚举额外键原样保留。用 defineProperty 而不是 `copy[key] =`：
 * 字符串键 `__proto__` 走赋值会改克隆的原型，走定义才只成为自有数据属性。
 */
function detachAuthorityArray(source: readonly unknown[]): readonly unknown[] {
  const clone = new Array(source.length) as unknown[];
  for (const key of Object.keys(source)) {
    Object.defineProperty(clone, key, {
      value: detachAuthorityValue((source as unknown as Record<string, unknown>)[key]),
      writable: true,
      enumerable: true,
      configurable: true,
    });
  }
  return Object.freeze(clone);
}

function detachAuthorityObject(
  source: Record<string, unknown>,
): Record<string, unknown> {
  const clone: Record<string, unknown> = {};
  for (const key of Object.keys(source)) {
    Object.defineProperty(clone, key, {
      value: detachAuthorityValue(source[key]),
      writable: true,
      enumerable: true,
      configurable: true,
    });
  }
  return Object.freeze(clone);
}

/**
 * durable stage / complete 在结果可分类之后的 typed 拒绝。
 *
 * 每一次失败都新造一个实例并冻结；后来的调用绝不能改写调用方此刻正在判的那条
 * 事实。归属不能靠错误自己的字段自证——见 `AutopilotAckDelivery.ownsPersistenceError`。
 */
export class AutopilotAckPersistenceError extends Error {
  readonly phase: AutopilotAckPersistencePhase;
  readonly outcome: AutopilotAckPersistenceOutcome;
  readonly identity: AutopilotAckPersistenceIdentity;

  constructor(input: {
    phase: AutopilotAckPersistencePhase;
    outcome: AutopilotAckPersistenceOutcome;
    identity: AutopilotAckPersistenceIdentity;
    message: string;
    cause?: unknown;
  }) {
    super(input.message, { cause: input.cause });
    this.name = "AutopilotAckPersistenceError";
    this.phase = input.phase;
    this.outcome = input.outcome;
    // 复制在前、冻结在后：identity / command / payload / ACK 整条图全部脱离，
    // 之后调用方再怎么改原对象都改不动这份快照。cause 故意留在图外——
    // Error.cause 保留外部引用，但授权判据一次都不读它，也不递归冻结它。
    this.identity = detachAuthorityValue(input.identity);
    // 字段全部落定之后才冻本体；先冻会让上面几行静默失败或抛错。
    Object.freeze(this);
  }
}

/** Production implementations durably stage the exact ACK before HTTP. */
export interface AutopilotAckDelivery {
  readonly initialDeviceEventSeq: number;
  /**
   * 认领：这条 typed 拒绝确实是本 delivery 实例为它自己的 owner/session 造的。
   *
   * 错误上的字段不能自证归属。一份 command/ACK 字段全等、只是 owner 不同的伪造
   * 错误必须被拒，所以判据是 delivery 侧不可变的实例证明，而不是任何"上一次
   * 结果"的可变 getter。
   */
  ownsPersistenceError?(error: unknown): error is AutopilotAckPersistenceError;
  /**
   * The exact command the server returned with the last confirmed ACK, if it
   * returned one. A pagehide that lands after `record_started` was confirmed but
   * before `/next` came back has no other source for the started revision, and
   * synthesising one locally is forbidden — a null authority means safe pause.
   */
  readonly lastReceiptAuthority?: {
    readonly commandKey: string;
    readonly ackType: AutopilotAck["ack_type"];
    /**
     * 这份权威回答的是哪一条 ACK。可选只是为了不逼着旧实现改签名：真要拿它
     * 当 started 权威用，缺一项就不算证明，调用方只能安全暂停。
     */
    readonly ackIdempotencyKey?: string;
    /** 服务器返回的 revision；客户端永远不许自己造一个。 */
    readonly commandRevision?: number;
    readonly command: NextCommandProjection | null;
  } | null;
  send(
    command: NextCommandProjection,
    lastDeviceEventSeq: number,
    facts: Parameters<typeof buildAutopilotAck>[3],
  ): Promise<AutopilotAck>;
}

export interface PatientAutopilotControllerOptions {
  sessionId: string;
  transport: AutopilotTransport;
  speech: AutopilotSpeechExecutor;
  recording: AutopilotRecordingExecutor;
  ackDelivery?: AutopilotAckDelivery;
  initialCommand?: unknown | null;
  /**
   * Full restored runtime seed (e.g. from restoreAutopilotRuntimeAfterRefresh),
   * mutually exclusive with initialCommand. Rebuilding from just a command
   * loses phase/pause_reason — a paused runtime with a null command would
   * silently become waiting_command if only its command were passed through.
   * Its last_device_event_seq must match the controller's own seq origin.
   */
  initialRuntime?: AutopilotRuntimeState;
  initialDeviceEventSeq?: number;
  idempotencyKey?: (
    command: NextCommandProjection,
    ackType: AutopilotAck["ack_type"],
    deviceEventSeq: number,
  ) => string;
  onDeviceEventSeq?: (deviceEventSeq: number) => void;
  onState?: (state: AutopilotRuntimeState) => void;
  /** Resolves only after the exact command's current stimulus is decoded. */
  waitForPresentation?: (
    command: NextCommandProjection,
    signal: AbortSignal,
  ) => Promise<void>;
}

type MediaOutcome<T> = { ok: true; value: T } | { ok: false; error: unknown };

class MediaDeadlineError extends Error {
  constructor() {
    super("媒体命令超时");
    this.name = "MediaDeadlineError";
  }
}

/**
 * Watch a media promise without arming any timer of our own.
 *
 * `capture.started` is bounded by the capture's single pre-start absolute
 * deadline. Arming a second 20s timeout here would make two independent timers
 * race, and which one judged the failure would depend on scheduling.
 */
function settledOutcome<T>(promise: Promise<T>): Promise<MediaOutcome<T>> {
  return new Promise((resolve) => {
    promise.then(
      (value) => resolve({ ok: true, value }),
      (error: unknown) => resolve({ ok: false, error }),
    );
  });
}

function observeMedia<T>(promise: Promise<T>, timeoutMs: number): Promise<MediaOutcome<T>> {
  // Attach both continuations immediately. Some browser media APIs can emit an
  // ended/error event before the started ACK round-trip completes; that must not
  // become an unhandled rejection while we wait for the server revision.
  return new Promise((resolve) => {
    const timeout = globalThis.setTimeout(
      () => resolve({ ok: false, error: new MediaDeadlineError() }), timeoutMs);
    promise.then(
      (value) => {
        globalThis.clearTimeout(timeout);
        resolve({ ok: true, value });
      },
      (error: unknown) => {
        globalThis.clearTimeout(timeout);
        resolve({ ok: false, error });
      },
    );
  });
}

function defaultIdempotencyKey(
  _command: NextCommandProjection,
  ackType: AutopilotAck["ack_type"],
  deviceEventSeq: number,
): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (!uuid) throw new Error("当前浏览器不能安全生成自动驾驶回执标识");
  return `ack:${ackType}:${deviceEventSeq}:${uuid}`;
}

/**
 * 逐字对齐整张 plain 授权图（canonical 命令与 ACK）。
 *
 * 替换分支只认"就是这一条当前 pending 命令 / 就是这一条期望 ACK"。每一层先比
 * 自有可枚举字符串键的精确排序集合，再递归比值，于是缺键、自有 undefined 键、
 * 额外键是三件互相独立的事实。原语用 `Object.is`：prompt_level 从 +0 漂成 -0
 * 会被拒，`===` 会放过。全程零 JSON 序列化、零 subset 比较、零同引用短路。
 *
 * 数组必须双方都是数组、length 相等、`Object.keys()` 排序后精确同集，再逐个自有
 * 键递归。只按下标循环是禁止的：稀疏洞与自有 explicit-undefined 索引读出来都是
 * undefined，下标循环把它们判等，而且完全看不见可枚举额外键。
 */
function sameExactAuthorityGraph(left: unknown, right: unknown): boolean {
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right)) return false;
    if (left.length !== right.length) return false;
    return sameExactOwnKeyGraph(
      left as unknown as Record<string, unknown>,
      right as unknown as Record<string, unknown>,
    );
  }
  if (left !== null && typeof left === "object") {
    if (right === null || typeof right !== "object") return false;
    return sameExactOwnKeyGraph(
      left as Record<string, unknown>,
      right as Record<string, unknown>,
    );
  }
  return Object.is(left, right);
}

/** 先比精确排序键集，再只对每个自有键递归。 */
function sameExactOwnKeyGraph(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): boolean {
  const leftKeys = Object.keys(left).sort();
  const rightKeys = Object.keys(right).sort();
  if (leftKeys.length !== rightKeys.length) return false;
  if (leftKeys.some((key, index) => key !== rightKeys[index])) return false;
  return leftKeys.every((key) => sameExactAuthorityGraph(left[key], right[key]));
}

/**
 * 仅供 autopilotController.test.ts 直接钉住生产比较器；生产代码不得引用。
 *
 * 它指向的就是上面那一个函数，不是第二份实现——今天的生产授权图里没有任何数组
 * 字段，数组分支从生产数据到不了，只能这样直接把它测穿。
 */
export const AUTOPILOT_CONTROLLER_AUTHORITY_TEST_ONLY = Object.freeze({
  sameExactAuthorityGraph,
});

/** Only the exact, unexpired active-session capability may activate polling. */
export function deviceCapabilityAllowsAutopilot(
  capability: DeviceCapabilityRecord | null,
  sessionId: string,
  nowMs = Date.now(),
): capability is DeviceCapabilityRecord {
  return capability !== null
    && capability.sessionId === sessionId
    && Number.isFinite(Date.parse(capability.expiresAt))
    && Date.parse(capability.expiresAt) > nowMs;
}

/**
 * One paired-device media runner. It deliberately has no built-in microphone
 * implementation: the production adapter must first reuse the existing
 * IndexedDB/outbox/receipt chain. Tests inject inert media doubles.
 */
export class PatientAutopilotController {
  private stateValue: AutopilotRuntimeState;
  private readonly sessionId: string;
  private readonly transport: AutopilotTransport;
  private readonly speech: AutopilotSpeechExecutor;
  private readonly recording: AutopilotRecordingExecutor;
  private readonly ackDelivery?: AutopilotAckDelivery;
  private readonly makeIdempotencyKey: NonNullable<PatientAutopilotControllerOptions["idempotencyKey"]>;
  private readonly onDeviceEventSeq?: (deviceEventSeq: number) => void;
  private readonly onState?: (state: AutopilotRuntimeState) => void;
  private readonly waitForPresentation?: NonNullable<
    PatientAutopilotControllerOptions["waitForPresentation"]
  >;
  private readonly presentationAbort = new AbortController();
  private inFlight: Promise<AutopilotRuntimeState> | null = null;
  private activeMedia: { cancel(): void; closed: Promise<void> } | null = null;
  private activeRecording: AutopilotRecordingCapture | null = null;
  private lifecycleShutdown: Promise<void> | null = null;
  // 生命周期判据必须绑在**那一条** capture 上：只记一个布尔的话，上一条捕获的
  // 中断会让下一条捕获跳过它该做的 cancel。
  private lifecycleCapture: AutopilotRecordingCapture | null = null;
  private lifecycleDisposition:
    "before_start" | "after_start" | "too_late" | "already_interrupted" | null = null;
  // 权威不足时被物理丢弃的那条捕获：外层 catch 绝不能再去 cancel 它。
  private discardedCapture: AutopilotRecordingCapture | null = null;
  private stopped = false;

  constructor(options: PatientAutopilotControllerOptions) {
    this.sessionId = options.sessionId;
    this.transport = options.transport;
    this.speech = options.speech;
    this.recording = options.recording;
    this.ackDelivery = options.ackDelivery;
    this.makeIdempotencyKey = options.idempotencyKey ?? defaultIdempotencyKey;
    this.onDeviceEventSeq = options.onDeviceEventSeq;
    this.onState = options.onState;
    this.waitForPresentation = options.waitForPresentation;
    const initialSeq = options.ackDelivery?.initialDeviceEventSeq
      ?? options.initialDeviceEventSeq ?? 0;
    if (options.ackDelivery && options.initialDeviceEventSeq !== undefined
        && options.initialDeviceEventSeq !== initialSeq) {
      throw new Error("ACK delivery 与控制器序号起点不一致");
    }
    if (options.initialRuntime && options.initialCommand !== undefined) {
      throw new Error("initialRuntime 与 initialCommand 不得同时提供");
    }
    if (options.initialRuntime) {
      if (options.initialRuntime.last_device_event_seq !== initialSeq) {
        throw new Error("initialRuntime 与控制器序号起点不一致");
      }
      this.stateValue = options.initialRuntime;
    } else {
      this.stateValue = restoreAutopilotRuntime(
        options.initialCommand ?? null,
        initialSeq,
      );
    }
    this.emitState();
  }

  get state(): AutopilotRuntimeState { return this.stateValue; }

  stop(): void {
    this.stopped = true;
    this.presentationAbort.abort(new DOMException("自动驾驶媒体已停止", "AbortError"));
    this.activeMedia?.cancel();
    this.activeMedia = null;
    if (this.stateValue.phase !== "paused"
        && this.stateValue.phase !== "scope_completed") {
      this.stateValue = autopilotRuntimeReducer(this.stateValue, {
        type: "technical_failure",
      });
    }
    this.emitState();
  }

  /**
   * Stop media synchronously, then wait until the serialized runner has left
   * every ACK/media continuation. The cross-tab owner lease is released only
   * after this promise settles, so the next tab cannot overlap speech or ACKs.
   */
  async stopAndWait(): Promise<void> {
    const running = this.inFlight;
    const media = this.activeMedia;
    this.stop();
    const pending: Promise<unknown>[] = [];
    if (running) pending.push(running);
    if (media) pending.push(media.closed);
    if (pending.length > 0) await Promise.allSettled(pending);
  }

  /**
   * The patient's optional "说完了可以点这里".
   *
   * Gated on the local capture, never on `runtime.phase === "recording"`: the
   * server only reaches that phase after `record_started` is ACKed, and a tap
   * while that round trip is still pending must still stop the microphone
   * instead of silently doing nothing.
   */
  stopRecordingNow(): void {
    const capture = this.activeRecording;
    if (!capture || capture.locallyStarted === false) return;
    capture.requestStop?.("user_done");
  }

  /**
   * Page lifecycle said the microphone must close now.
   *
   * Physical teardown is synchronous and happens first; everything after it only
   * decides which ACK, if any, this capture still owes the server. `stopped` is
   * deliberately NOT set here — `sendAck` refuses to send once it is true, so
   * setting it before the failure ACK would swallow the very ACK we owe.
   */
  handleRecordingLifecycleInterruption(): "before_start" | "after_start" | "none" {
    const capture = this.activeRecording ?? (this.activeMedia as AutopilotRecordingCapture | null);
    const disposition = capture?.interrupt?.();
    if (capture && disposition) {
      // first-writer-wins：第二次 pagehide/freeze 只会拿到 already_interrupted，
      // 它绝不能盖掉最初那次 before/after/too_late——判据一变，这条捕获欠不欠
      // ACK、能不能 cancel 就全变了。
      // 原样留着 too_late/already_interrupted：对外仍然报 "none"（调用方只关心
      // 还欠不欠 ACK），但内部要靠它决定"绝不 cancel"和"必须 join 同一条链"。
      if (this.lifecycleCapture !== capture || this.lifecycleDisposition === null) {
        this.lifecycleCapture = capture;
        this.lifecycleDisposition = disposition;
      }
    }
    const retained = capture ? this.lifecycleDispositionFor(capture) : null;
    if (retained === "before_start") return "before_start";
    if (retained === "after_start") return "after_start";
    return "none";
  }

  /** 这条 capture 自己的生命周期判据；别的捕获的判据一律不认。 */
  private lifecycleDispositionFor(
    capture: unknown,
  ): "before_start" | "after_start" | "too_late" | "already_interrupted" | null {
    return this.lifecycleCapture !== null && this.lifecycleCapture === capture
      ? this.lifecycleDisposition
      : null;
  }

  /**
   * The only lifecycle shutdown path. Repeated pagehide/freeze/unmount events
   * reuse this one promise instead of each starting their own teardown, and it
   * never routes through `stopAndWait()`, whose first act is to set `stopped`.
   */
  interruptRecordingForLifecycleAndWait(): Promise<void> {
    if (this.lifecycleShutdown) return this.lifecycleShutdown;
    const capture = this.activeRecording ?? (this.activeMedia as AutopilotRecordingCapture | null);
    this.handleRecordingLifecycleInterruption();
    const disposition = this.lifecycleDispositionFor(capture);
    this.lifecycleShutdown = (async () => {
      // before_start owes nothing: no bytes, no record_started, so the server
      // command stays pending for a freshly activated page to re-verify.
      // after_start lets the in-flight runRecording reach its own failure branch,
      // which is where the single record_failed is sent.
      //
      // too_late 同样要 join：那条捕获已经在成功持久化的路上，它自己的终态成功
      // ACK 还没发出去。这里若抢先把 stopped 置位，sendAck 会拒绝发送，一次
      // 已经保存下来的作答就被生命周期吞掉了。
      if (disposition !== null && this.inFlight) {
        await this.inFlight.catch(() => {});
      }
      if (capture) await capture.closed.catch(() => {});
      this.stopped = true;
      this.presentationAbort.abort(
        new DOMException("自动驾驶媒体已停止", "AbortError"));
      this.activeMedia = null;
      this.activeRecording = null;
      if (this.stateValue.phase !== "paused"
          && this.stateValue.phase !== "scope_completed") {
        this.stateValue = autopilotRuntimeReducer(this.stateValue, {
          type: "technical_failure",
        });
        this.emitState();
      }
    })();
    return this.lifecycleShutdown;
  }

  private emitState(): void { this.onState?.(this.stateValue); }

  /** Serialize timer/visibility/manual refresh calls into one media owner. */
  pollOnce(): Promise<AutopilotRuntimeState> {
    if (this.inFlight) return this.inFlight;
    this.inFlight = this.runOnce().finally(() => { this.inFlight = null; });
    return this.inFlight;
  }

  private async runOnce(): Promise<AutopilotRuntimeState> {
    if (this.stopped || this.stateValue.phase === "paused"
        || this.stateValue.phase === "scope_completed") return this.stateValue;
    try {
      await this.refreshCommand();
      // refreshCommand mutates the property through a method; widen the prior
      // control-flow narrowing before examining the newly reduced state.
      if ((this.stateValue as AutopilotRuntimeState).phase === "paused") return this.stateValue;
      const canPlay = canPlayAutopilotSpeech(this.stateValue);
      const canRecord = canOpenAutopilotMicrophone(this.stateValue);
      if ((canPlay || canRecord) && this.waitForPresentation) {
        const command = this.stateValue.command;
        if (!command) throw new Error("媒体命令缺少当前题目投影");
        await this.waitForPresentation(command, this.presentationAbort.signal);
        if (this.stopped) return this.stateValue;
        if (this.stateValue.command?.command_key !== command.command_key) {
          throw new Error("图片门禁与当前媒体命令不一致");
        }
      }
      if (canPlay) await this.runSpeech();
      else if (canRecord) await this.runRecording();
      return this.stateValue;
    } catch {
      // 生命周期中断和"权威不足"两条路都已经物理拆掉过设备，这里绝不能再
      // cancel 一次：真实开录之后 cancel 会 signal stream_end，把那半段音频
      // stage、上传，变成一次有效作答。
      const media = this.activeMedia;
      if (media && !this.lifecycleDispositionFor(media) && media !== this.discardedCapture) {
        media.cancel();
      }
      this.activeMedia = null;
      // Preserve the reducer's more specific fail-closed reason (protocol,
      // TTS, recording). Only an unclassified transport/runtime exception is
      // promoted to technical_failure.
      const caughtPhase = (this.stateValue as AutopilotRuntimeState).phase;
      if (caughtPhase !== "paused" && caughtPhase !== "scope_completed") {
        this.stateValue = autopilotRuntimeReducer(this.stateValue, {
          type: "technical_failure",
        });
      }
      this.emitState();
      return this.stateValue;
    }
  }

  private async refreshCommand(): Promise<void> {
    if (this.stopped) return;
    const current = await this.transport.next(this.sessionId);
    this.stateValue = autopilotRuntimeReducer(this.stateValue, {
      type: "server_command",
      command: current,
    });
    this.emitState();
  }

  private currentCommand<K extends NextCommandProjection["kind"]>(
    kind: K,
  ): Extract<NextCommandProjection, { kind: K }> {
    const command = this.stateValue.command;
    if (!command || command.kind !== kind) throw new Error("当前媒体命令已失效");
    return command as Extract<NextCommandProjection, { kind: K }>;
  }

  private async sendAck(command: NextCommandProjection, facts: Parameters<typeof buildAutopilotAck>[3]): Promise<AutopilotAck> {
    if (this.stopped) throw new Error("自动驾驶已停止");
    const nextSeq = this.stateValue.last_device_event_seq + 1;
    const ack = this.ackDelivery
      ? await this.ackDelivery.send(command, this.stateValue.last_device_event_seq, facts)
      : buildAutopilotAck(
        command,
        this.stateValue.last_device_event_seq,
        this.makeIdempotencyKey(command, facts.ack_type, nextSeq),
        facts,
      );
    // Local state advances only after the server has durably accepted the ACK.
    if (!this.ackDelivery) await this.transport.ack(this.sessionId, command.command_key, ack);
    this.stateValue = autopilotRuntimeReducer(this.stateValue, {
      type: "device_ack",
      ack,
    });
    const expectedFailurePause = facts.ack_type === "tts_failed"
      ? this.stateValue.pause_reason === "tts_failed"
      : facts.ack_type === "record_failed"
        ? this.stateValue.pause_reason === "record_failed"
        : false;
    if (this.stateValue.phase === "paused" && !expectedFailurePause) {
      throw new Error("自动驾驶回执未通过本地状态校验");
    }
    this.onDeviceEventSeq?.(this.stateValue.last_device_event_seq);
    this.emitState();
    return ack;
  }

  private async mediaFailure(
    kind: "tts" | "record",
    errorCode: AutopilotErrorCode,
  ): Promise<void> {
    const command = this.currentCommand(kind);
    await this.sendAck(command, {
      ack_type: kind === "tts" ? "tts_failed" : "record_failed",
      error_code: errorCode,
    });
  }

  private async runSpeech(): Promise<void> {
    const initial = this.currentCommand("tts");
    let playback: AutopilotSpeechPlayback;
    try { playback = this.speech.start(initial); }
    catch {
      await this.mediaFailure("tts", "audio_playback_failed");
      return;
    }
    this.activeMedia = playback;
    const startedOutcome = observeMedia(playback.started, 15_000);
    const endedOutcome = observeMedia(playback.ended, 120_000);
    try {
      const observedStart = await startedOutcome;
      if (!observedStart.ok) {
        playback.cancel();
        await playback.closed;
        await this.mediaFailure("tts", observedStart.error instanceof MediaDeadlineError
          ? "device_command_timeout" : "audio_playback_failed");
        return;
      }
      await this.sendAck(initial, { ack_type: "tts_started", ...observedStart.value });

      // The terminal ACK must echo the server's started revision, never the old
      // pending revision cached before playback began.
      await this.refreshCommand();
      const startedCommand = this.currentCommand("tts");
      if (startedCommand.state !== "started") throw new Error("TTS started revision 未确认");

      const observedEnd = await endedOutcome;
      if (!observedEnd.ok) {
        playback.cancel();
        await playback.closed;
        await this.mediaFailure("tts", observedEnd.error instanceof MediaDeadlineError
          ? "device_command_timeout" : "audio_playback_failed");
        return;
      }
      await playback.closed;
      await this.sendAck(startedCommand, {
        ack_type: "tts_ended",
        media_ended: true,
        ...observedEnd.value,
      });
      await this.refreshCommand();
    } finally {
      await playback.closed;
      if (this.activeMedia === playback) this.activeMedia = null;
    }
  }

  private async runRecording(): Promise<void> {
    // This method is the sole microphone admission point. The reducer guard in
    // runOnce proves an exact, pending server record command exists first.
    const initial = this.currentCommand("record");
    let capture: AutopilotRecordingCapture;
    try { capture = this.recording.start(initial); }
    catch (error) {
      await this.mediaFailure("record", autopilotMediaErrorCode(error, "recording_start_failed"));
      return;
    }
    this.activeMedia = capture;
    // No timer here: the capture owns the single pre-start absolute deadline and
    // always settles `started` by it, with device_command_timeout as its code.
    const startedOutcome = settledOutcome(capture.started);
    const stoppedOutcome = observeMedia(
      capture.stopped,
      (initial.payload.max_duration_seconds * 1_000) + 90_000,
    );
    try {
      const observedStart = await startedOutcome;
      if (!observedStart.ok) {
        // 生命周期已经同步物理关麦。原子 start fact 落地之后，能走到这里的只有
        // before_start（零字节、零 ACK：那条服务器命令仍然 pending，留给重新激活
        // 的页面自己核对）；too_late/already_interrupted 与 started 拒绝同时成立
        // 本身就自相矛盾，同样 fail closed——一次 cancel 都不调、一条 ACK 都不发。
        const lifecycle = this.lifecycleDispositionFor(capture);
        if (lifecycle !== null) {
          await capture.closed;
          return;
        }
        capture.cancel();
        await capture.closed;
        await this.mediaFailure("record",
          autopilotMediaErrorCode(observedStart.error, "recording_start_failed"));
        return;
      }
      this.activeRecording = capture;
      let startedAck: AutopilotAck;
      try {
        startedAck = await this.sendAck(
          initial, { ack_type: "record_started", ...observedStart.value });
      } catch (error) {
        // 唯一允许的替换分支，只包住这第一次 record_started 尝试。证明成立就发
        // 那条同 seq 的生命周期失败并返回；否则不再原样重抛——异常会先撞上
        // finally 的 `await capture.closed`，而活跃捕获没人 settle closed：外层
        // catch 永远到不了，自动驾驶整个挂死，麦克风保持物理打开。收口只能在
        // 这里做，且只能 interrupt（真实开录之后 cancel 会 signal stream_end，
        // 把半段音频 stage 成一次有效作答）。生命周期已经收口过的捕获不再碰，
        // 保持「恰好一次物理收口」。
        if (await this.replaceDiscardedStartedCandidate(
          capture, initial, observedStart.value, error)) return;
        this.discardedCapture = capture;
        if (!this.lifecycleDispositionFor(capture)) capture.interrupt?.();
        await capture.closed;
        this.safePauseWithoutAck();
        return;
      }
      // record_started 已被服务器接受，服务器自己那条 started command 就在回执
      // 权威里。先把它验实并经 reducer 采纳，再谈停止/生命周期——这一步之前
      // 绝不 await /next：刷新失败或永不返回都不该拖累一次已经录下来的作答。
      // 采纳自身抛错（legacy /next 刷新失败、「录音 started revision 未确认」）
      // 与权威不足同归下面这条收口路：原样抛出同样会死在 finally 的
      // `await capture.closed` 上。
      const startedCommand = await this.adoptStartedRecordCommand(
        initial, startedAck, observedStart.value.mime_type).catch(() => null);
      if (!startedCommand) {
        // 权威不足（exact 重放、ACK 身份不符、key/revision/state/MIME 不对或不可变
        // 字段漂移）：本地不合成 revision，也不发终态/失败 ACK。
        //
        // interrupt() 的返回值在这里同归一处，理由却不同：before/after 是物理
        // 丢弃后等 closed 再安全暂停；too_late 说明这条捕获已经在持久化路上，只
        // 能 join 它那条链——绝不 cancel（真实开录之后 cancel 会 signal
        // stream_end，把半段音频 stage、上传、变成一次有效作答）、绝不破坏已经
        // durable 的字节、也绝不另开第二条处理链。三条路都是零终态、零第二 ACK。
        // 已确认的 started ACK 事实原样保留，不重建、不重发。
        this.discardedCapture = capture;
        capture.interrupt?.();
        await capture.closed;
        this.safePauseWithoutAck();
        return;
      }

      const observedStop = await stoppedOutcome;
      if (!observedStop.ok) {
        // 判据必须 exact。旧写法在这里做 truthiness，把 too_late 一起拖进
        // device_runtime_failed：一次真实的上传/持久化失败被改标成生命周期失败，
        // 服务器看到的就是假的。
        const lifecycle = this.lifecycleDispositionFor(capture);
        const ordinaryFailure = {
          ack_type: "record_failed",
          error_code: observedStop.error instanceof MediaDeadlineError
            ? "device_command_timeout"
            : autopilotMediaErrorCode(observedStop.error, "recording_runtime_failed"),
        } as const;
        if (lifecycle === "after_start") {
          // 半段音频已经被 interrupt() 物理丢弃。绝不走 cancel（真实开录之后它会
          // signal stream_end，把这半段 stage、上传、变成一次有效作答），只补
          // 这一条绑在 started 权威上的失败回执。
          await capture.closed;
          await this.sendAck(startedCommand, {
            ack_type: "record_failed", error_code: "device_runtime_failed",
          });
          return;
        }
        if (lifecycle === "too_late") {
          // 成功停止已经 claim 了这条捕获，结局归它自己那条持久化链。零 cancel，
          // 照实上报真实的 non-lifecycle 失败，绝不改标成生命周期失败。
          await capture.closed;
          await this.sendAck(startedCommand, ordinaryFailure);
          return;
        }
        if (lifecycle !== null) {
          // before_start / already_interrupted 与"started 已经确认"自相矛盾：最初
          // 那次判据已经不可知，fail closed——零 cancel、零第二 ACK。
          await capture.closed;
          return;
        }
        capture.cancel();
        await capture.closed;
        await this.sendAck(startedCommand, ordinaryFailure);
        return;
      }
      await capture.closed;
      await this.sendAck(startedCommand, { ack_type: "record_stopped", ...observedStop.value });
      // 终态 ACK 之后不再等 /next：刷新抛错或永不 settle 都不得把一次已保存的
      // 作答改判成技术故障，也不得把 pollOnce 挂住。下一拍轮询自己会取下一条
      // 命令。legacy/无 delivery 适配器保留原来那次刷新。
      if (!this.ackDelivery) await this.refreshCommand();
    } finally {
      await capture.closed;
      if (this.activeMedia === capture) this.activeMedia = null;
      if (this.activeRecording === capture) this.activeRecording = null;
    }
  }

  /**
   * 被丢弃的 started 候选 → 同 seq 唯一 replacement。这是控制器**唯一**的替换分支。
   *
   * 只有 stage 阶段被证明"事务未提交"才可能走到这里：那条候选既没成为 durable
   * 事实，也一次都没上过网，所以这个 device event seq 上还没有任何事件，替换是
   * 该 seq 的第一条也是唯一一条。两者幂等键不同，但只有替换能变成事件。
   *
   * 归属先于内容：先让 delivery 认领这条 typed 拒绝，再逐字核对它就是本条 pending
   * 命令的候选，最后才看这条 capture 自己保留的生命周期判据。任何一项不成立都
   * 返回 false，交回既有 fail-closed 路径，零替换。
   */
  private async replaceDiscardedStartedCandidate(
    capture: AutopilotRecordingCapture,
    pending: RecordCommand,
    observedStart: Awaited<AutopilotRecordingCapture["started"]>,
    error: unknown,
  ): Promise<boolean> {
    const delivery = this.ackDelivery;
    // 归属先于内容，顺序不可换：认领只由 delivery 自持的实例证明裁决，
    // identity.ownerKey 永远不作为归属证据。
    if (!delivery || !delivery.ownsPersistenceError) return false;
    if (!delivery.ownsPersistenceError(error)) return false;
    if (error.phase !== "stage" || error.outcome !== "confirmed_empty") return false;
    const identity = error.identity;
    // 先把内层 ACK 收窄到 record_started，之后才读得到 mime_type 与两个可选事实。
    const candidateAck = identity.ack;
    if (candidateAck.ack_type !== "record_started") return false;
    // 用错误的顶层幂等键、当前 pending、控制器当前的旧 seq 与本机那份**完整**
    // observedStart 重建期望 ACK。不传 clientObservedMs，于是 client_observed_ms
    // 在期望侧根本不作为键存在，内层 ACK 上任何该键（合法值或自有 undefined）
    // 都会被键集判负；sample_rate_hz / channels 的在场与否也被逐字带进来。
    const expectedAck = buildAutopilotAck(
      pending,
      this.stateValue.last_device_event_seq,
      identity.idempotencyKey,
      { ack_type: "record_started", ...observedStart },
    );
    // 每一项的锚点都在错误之外：本控制器 session、当前 pending、当前旧 seq、
    // 以及原始发送用的同一份 observedStart。错误自带的两个字段互相吻合，
    // 一次都不作为独立证明。
    if (identity.sessionId !== this.sessionId
        || identity.ackType !== "record_started"
        || identity.ackType !== candidateAck.ack_type
        || identity.commandKey !== pending.command_key
        || identity.commandKey !== identity.command.command_key
        || identity.idempotencyKey !== candidateAck.idempotency_key
        || identity.deviceEventSeq !== candidateAck.device_event_seq
        || identity.deviceEventSeq !== this.stateValue.last_device_event_seq + 1
        || identity.commandRevision !== candidateAck.command_revision
        || identity.commandRevision !== pending.command_revision
        || identity.controlGeneration !== candidateAck.control_generation
        || identity.controlGeneration !== pending.control_generation
        || identity.runnerGeneration !== candidateAck.runner_generation
        || identity.runnerGeneration !== pending.runner_generation
        || identity.mimeType !== candidateAck.mime_type
        || identity.mimeType !== observedStart.mime_type
        || !sameExactAuthorityGraph(identity.command, pending)
        || !sameExactAuthorityGraph(candidateAck, expectedAck)) {
      return false;
    }
    // 只有这一条 capture 自己同步保留下来的 after_start 才算数。
    if (this.lifecycleDispositionFor(capture) !== "after_start") return false;
    // 物理丢弃已经赢了；等它彻底收口再谈发 ACK。
    await capture.closed;
    // 故意不包 try/catch：替换自己的 stage/complete 失败必须落回既有 fail-closed
    // 路径，绝不能在这里递归造出第三条 envelope。
    await this.sendAck(pending, {
      ack_type: "record_failed", error_code: "device_runtime_failed",
    });
    return true;
  }

  /**
   * 拿到可以绑终态 ACK 的那条 started 命令。
   *
   * 有持久 delivery 时权威只能来自服务器随这条 ACK 返回的回执，而且必须逐项
   * 证明：ACK 类型、命令键、ACK 幂等键、投影的 kind/state、收据 revision 等于
   * 投影 revision，以及**服务器实际返回的那条 record_started ACK 的 mime_type
   * 等于本机 observedStart 的 MIME**——只核对 type/key/revision 不够，编码对不上
   * 就说明这条回执讲的不是本机真正开的那次录音。少一项都不算证明。
   * legacy/无 delivery 适配器保留原来那条严格 /next 路径，但同样不许自己合成一个
   * revision。
   */
  private async adoptStartedRecordCommand(
    pending: RecordCommand,
    ack: AutopilotAck,
    observedMimeType: AutopilotMimeType,
  ): Promise<RecordCommand | null> {
    if (!this.ackDelivery) {
      await this.refreshCommand();
      return this.startedRecordCommand();
    }
    const authority = this.ackDelivery.lastReceiptAuthority ?? null;
    const projected = authority?.command ?? null;
    const proven = authority !== null
      && authority.ackType === "record_started"
      && authority.commandKey === pending.command_key
      && authority.ackIdempotencyKey === ack.idempotency_key
      && authority.commandRevision !== undefined
      && ack.ack_type === "record_started"
      && ack.mime_type === observedMimeType
      && projected !== null
      && projected.kind === "record"
      && projected.state === "started"
      && authority.commandRevision === projected.command_revision;
    if (!proven || projected === null) return null;
    // 采纳只走既有 server_command reducer：命令同一性、每个不可变字段、revision
    // 必须恰好 +1、本机确实观察到过开录，全部由它再证一次。任何漂移都会落成
    // protocol_violation 的 paused，而不是从这里放过去。
    this.stateValue = autopilotRuntimeReducer(this.stateValue, {
      type: "server_command", command: projected,
    });
    this.emitState();
    const adopted = this.stateValue.command;
    return this.stateValue.phase !== "paused" && adopted?.kind === "record"
      && adopted.state === "started" ? adopted : null;
  }

  /** 权威不足时的安全暂停：零终态 ACK、零失败 ACK、零本地合成 revision。 */
  private safePauseWithoutAck(): void {
    if (this.stateValue.phase === "paused"
        || this.stateValue.phase === "scope_completed") return;
    this.stateValue = autopilotRuntimeReducer(this.stateValue, {
      type: "technical_failure",
    });
    this.emitState();
  }

  /**
   * The started record command to bind a terminal ACK to.
   *
   * `/next` is the normal source. When a lifecycle interruption cut the refresh
   * short, the server's own receipt authority stands in — it is the exact
   * command the backend returned when it accepted `record_started`. If neither
   * exists there is no authoritative revision, and the client must not invent
   * one; the caller fails closed instead.
   */
  private startedRecordCommand(): RecordCommand {
    const projected = this.stateValue.command;
    if (projected?.kind === "record" && projected.state === "started") return projected;
    const authority = this.ackDelivery?.lastReceiptAuthority;
    const fallback = authority?.ackType === "record_started" ? authority.command : null;
    if (fallback?.kind === "record" && fallback.state === "started") return fallback;
    throw new Error("录音 started revision 未确认");
  }
}
