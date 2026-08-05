import { acquireAudioDeviceLease, type AudioDeviceLease } from "../audio/audioDeviceLease.ts";
import {
  advanceAudioOutbox,
  attachAudioCaptureReceipt,
  attachAutopilotStopReason,
  createAudioOutboxEntry,
  type AudioOutboxEntry,
} from "../audio/audioOutbox.ts";
import { blobStore } from "../audio/blobStore.ts";
import {
  sha256Blob,
  validateAudioCaptureReceiptAck,
  validateAudioUploadReceipt,
} from "../audio/audioUploadReceipt.ts";
import { Recorder } from "../audio/recorder.ts";
import {
  PRE_START_BUDGET_MS,
  admitCaptureBeforeDeadline,
  armCaptureDeadline,
  assertCaptureDurationWithinCommandLimit,
  remainingAnswerWindowMs,
  type CaptureDeadline,
} from "./autopilotCaptureWindow.ts";
import {
  AUTOPILOT_MIME_TYPES,
  type AutopilotMimeType,
  type AutopilotStopReason,
  type NextCommandProjection,
} from "./autopilotProtocol.ts";
import {
  assertEntryMatchesCommand,
  settleCaptureCleanup,
  type AutopilotRecoveryDependencies,
  type AutopilotRecoverySnapshot,
} from "./autopilotRecordingRecovery.ts";
import type {
  AutopilotRecordingCapture,
  AutopilotRecordingExecutor,
} from "./autopilotController.ts";
import {
  createStopLatch,
  finalizeCaptureThenNotify,
  type LocalAutopilotCaptureIdentity,
  type LocalAutopilotCaptureObserver,
} from "./autopilotCapturePresentation.ts";
import { AutopilotMediaError } from "./autopilotMediaError.ts";
import {
  authorizesMicrophoneStart,
  type RecordingAuthorization,
} from "./recordingAuthorization.ts";
import { authorizeExactAutopilotRecording } from "./autopilotMediaTransport.ts";

type RecordCommand = Extract<NextCommandProjection, { kind: "record" }>;
type RecordStoppedFacts = Awaited<AutopilotRecordingCapture["stopped"]>;

function exactAutopilotMime(value: string | null): AutopilotMimeType {
  const normalized = (value ?? "").toLowerCase().replace(/\s+/g, "");
  const match = AUTOPILOT_MIME_TYPES.find((candidate) => candidate === normalized);
  if (!match) throw new Error("录音编码不在服务器自动驾驶白名单");
  return match;
}

/**
 * 上传链的分阶段端口。生产实现就是现有的 api/blobStore 调用，一个都没换；
 * 抽出来只是为了让测试能精确注入 createAudio / PUT / audioSaved / stage 各自的
 * 失败，而不必去动 `api.ts` 或 `blobStore.ts`。
 *
 * 三个 HTTP 方法用窄结构类型声明，不从 `api.ts` 取 `typeof`：那样这个模块的
 * import 图里就一点 `api.ts` 都不剩，Node 直接 import 时不会执行它模块作用域里
 * 的浏览器存储初始化。
 */
export interface ExactCaptureUploadPorts {
  createAudio(body: {
    raw_audio_id: string;
    turn_key: string;
    session_id?: string | null;
    contains_direct_identifier?: boolean;
  }): Promise<{ raw_audio_id: string; registered: boolean }>;
  uploadAudioBlob(rawAudioId: string, blob: Blob, sessionId: string): Promise<unknown>;
  putAudioSaved(payload: object): Promise<unknown>;
  putOutbox(entry: AudioOutboxEntry): Promise<void>;
  stageOutbox(entry: AudioOutboxEntry, blob: Blob): Promise<void>;
}

/**
 * 浏览器默认实现。`api.ts` 只在真正要发这一次请求时才 import：模块初始化不建
 * import Promise、不预热 memo，所以没有真实上传阶段就没有任何加载。
 */
export const browserExactCaptureUploadPorts: ExactCaptureUploadPorts = {
  createAudio: async (input) => (await import("../api.ts")).api.createAudio(input),
  uploadAudioBlob: async (rawAudioId, blob, sessionId) =>
    (await import("../api.ts")).api.uploadAudioBlob(rawAudioId, blob, sessionId),
  putAudioSaved: async (input) => (await import("../api.ts")).api.putAudioSaved(input),
  putOutbox: (entry) => blobStore.putOutbox(entry),
  stageOutbox: (entry, blob) => blobStore.stageOutbox(entry, blob),
};

/**
 * 开麦前置的可注入端口：单调时钟、定时器、设备租约、恢复快照、命令级授权。
 *
 * 生产默认就是现有的 performance/window/Web Locks/IndexedDB/授权调用，一个语义
 * 都没换。抽出来是为了两件事：让唯一的绝对截止时刻、迟到租约与迟到续延可以被
 * 确定性驱动；让浏览器专属依赖只在第一次真实调用时 lazy 加载。
 */
export interface AutopilotCaptureBootstrapPorts {
  now(): number;
  setTimer(callback: () => void, delayMs: number): number;
  clearTimer(handle: number): void;
  acquireLease(signal: AbortSignal): Promise<AudioDeviceLease>;
  recoverySnapshot(): Promise<AutopilotRecoverySnapshot>;
  authorize(
    sessionId: string,
    commandKey: string,
    signal: AbortSignal,
  ): Promise<RecordingAuthorization>;
}

export const browserAutopilotCaptureBootstrapPorts: AutopilotCaptureBootstrapPorts = {
  now: () => performance.now(),
  setTimer: (callback, delayMs) => window.setTimeout(callback, delayMs),
  clearTimer: (handle) => window.clearTimeout(handle),
  acquireLease: (signal) => acquireAudioDeviceLease(undefined, signal),
  recoverySnapshot: () => blobStore.recoverySnapshot(),
  authorize: async (sessionId, commandKey, signal) => {
    // 中止后一律不发请求。dynamic import 前后各查一次：权限弹窗期间被判死的
    // 采集，不能因为模块加载晚了一步就补一个请求出去。
    if (signal.aborted) {
      throw signal.reason ?? new DOMException("录音授权已取消", "AbortError");
    }
    const { browserAutopilotMediaDependencies } =
      await import("./autopilotBrowserMediaDependencies.ts");
    if (signal.aborted) {
      throw signal.reason ?? new DOMException("录音授权已取消", "AbortError");
    }
    return authorizeExactAutopilotRecording(
      sessionId, commandKey, signal, browserAutopilotMediaDependencies);
  },
};

export async function finishExactCapture(
  command: RecordCommand,
  entryValue: AudioOutboxEntry,
  blob: Blob,
  ports: ExactCaptureUploadPorts = browserExactCaptureUploadPorts,
): Promise<{ entry: AudioOutboxEntry; facts: RecordStoppedFacts }> {
  let entry = entryValue;
  assertEntryMatchesCommand(entry, command);
  if (blob.size !== entry.blobBytes || (blob.type || "audio/webm") !== entry.mimeType) {
    throw new Error("本机录音字节与 outbox 元数据不一致");
  }
  const checksum = await sha256Blob(blob);

  if (entry.phase === "captured") {
    // POST is an idempotent confirmation of the server-preallocated row. It
    // carries the command-issued raw ID unchanged and never generates a second
    // client ID. A conflicting registration remains a hard failure.
    const registration = await ports.createAudio({
      raw_audio_id: command.payload.raw_audio_id,
      session_id: entry.sessionId,
      turn_key: command.payload.turn_ref,
      contains_direct_identifier: command.payload.contains_direct_identifier,
    });
    if (registration.raw_audio_id !== command.payload.raw_audio_id
        || registration.registered !== true) {
      throw new Error("服务器预分配录音登记收据不一致");
    }
    entry = advanceAudioOutbox(entry, "registered");
    await ports.putOutbox(entry);
  }

  if (entry.phase !== "uploaded") {
    // The exact PUT response proves that the same preallocated row accepted
    // these bytes.
    const uploadReceipt = await ports.uploadAudioBlob(
      command.payload.raw_audio_id,
      blob,
      entry.sessionId,
    );
    validateAudioUploadReceipt(uploadReceipt, {
      rawAudioId: entry.rawAudioId,
      bytes: blob.size,
      checksum,
    });
    entry = advanceAudioOutbox(entry, "uploaded", { checksum });
    await ports.putOutbox(entry);
  } else if (entry.checksum !== checksum) {
    throw new Error("已上传录音的本机 checksum 与字节不一致");
  }

  if (entry.captureReceiptServerSeq === undefined) {
    const response = await ports.putAudioSaved({
      rawAudioId: entry.rawAudioId,
      durationSeconds: entry.durationSeconds,
      byteCount: entry.blobBytes,
      checksum,
      turnKey: entry.turnKey,
      sessionId: entry.sessionId,
      containsDirectIdentifier: entry.containsDirectIdentifier,
    });
    const receipt = validateAudioCaptureReceiptAck(response, entry.rawAudioId);
    entry = attachAudioCaptureReceipt(entry, receipt.serverSeq);
    await ports.putOutbox(entry);
  }

  const receiptServerSeq = entry.captureReceiptServerSeq;
  const stopReason = entry.autopilotStopReason;
  if (receiptServerSeq === undefined || stopReason === undefined || entry.checksum === undefined) {
    throw new Error("录音缺少服务器 audioSaved 采集收据");
  }
  return {
    entry,
    facts: {
      stop_reason: stopReason,
      raw_audio_id: entry.rawAudioId,
      receipt_server_seq: receiptServerSeq,
      checksum: entry.checksum,
      byte_count: entry.blobBytes,
      duration_seconds: entry.durationSeconds,
    },
  };
}

/**
 * 一次**全新**采集的 stage → finish 收口，生产采集链与行为测试走的是同一个边界。
 *
 * 刷新恢复绝不走这里：那条 outbox entry 已经 durable，再 stage 一次就是拿旧字节
 * 去覆盖一条已经存在的记录。`browserAutopilotRecoveryDependencies.finish` 因此
 * 继续直接调 `finishExactCapture`。
 */
export async function stageAndFinishExactCapture(
  command: RecordCommand,
  entry: AudioOutboxEntry,
  blob: Blob,
  ports: ExactCaptureUploadPorts = browserExactCaptureUploadPorts,
): Promise<{ entry: AudioOutboxEntry; facts: RecordStoppedFacts }> {
  await ports.stageOutbox(entry, blob);
  return finishExactCapture(command, entry, blob, ports);
}

/**
 * 无资源阶段的迟到落地：观察但不消费。
 *
 * snapshot 与两次授权都不交付本地设备资源，所以判死之后 `closed` 不必等它们；
 * 但它们迟早会 settle，没人接就会变成 unhandled rejection。挂一条吞掉的续延，
 * 既不掀翻页面，也绝不让那次迟到的结果恢复任何续延。
 */
function isolateLateSettlement(operation: Promise<unknown>): void {
  operation.catch(() => {});
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
} {
  let resolvePromise!: (value: T) => void;
  let rejectPromise!: (error: unknown) => void;
  const promise = new Promise<T>((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  return { promise, resolve: resolvePromise, reject: rejectPromise };
}

/**
 * `interrupt()` 同步返回的阶段判据。controller 靠它决定发不发失败 ACK，
 * 不许靠猜 `capture.started` 这个 Promise 有没有 resolve——真实 onstart 已经
 * 触发但 continuation 还没跑的那一瞬间，猜法会把 after_start 误判成 before_start。
 */
export type CaptureInterruptDisposition =
  "before_start" | "after_start" | "too_late" | "already_interrupted";

type CaptureOutcome = "successful_stop" | "lifecycle_failure";

class BrowserAutopilotCapture implements AutopilotRecordingCapture {
  readonly started: AutopilotRecordingCapture["started"];
  readonly stopped: AutopilotRecordingCapture["stopped"];
  readonly closed: Promise<void>;
  private readonly startedDeferred = deferred<Awaited<AutopilotRecordingCapture["started"]>>();
  private readonly stoppedDeferred = deferred<RecordStoppedFacts>();
  private readonly closedDeferred = deferred<void>();
  private readonly lifecycleDeferred = deferred<never>();
  private readonly abortController = new AbortController();
  private readonly recorder = new Recorder();
  private lease: AudioDeviceLease | null = null;
  // acquisition 最终交付的那把锁，无论截止时刻有没有先赢。它与 lease 是否同一个
  // 对象，决定释放走正常路径还是迟到路径——两条路互斥，既不漏放也不双放。
  private acquiredLease: AudioDeviceLease | null = null;
  private lateLease: AudioDeviceLease | null = null;
  private maxTimer: number | null = null;
  private preStartDeadline: CaptureDeadline | null = null;
  // prepare() 底下的 getUserMedia 可能在判死之后才交出 MediaStream。留住这条
  // Promise，cleanup 才能在 dispose() 之后等它落地——不等就等于在那条流还在
  // 路上时就宣布"麦克风不可能再出声"。
  private preparePromise: Promise<boolean> | null = null;
  private readonly stopLatch = createStopLatch<AutopilotStopReason>();
  // stopLatch 只裁成功停止的两个理由；成功与生命周期失败之间的胜负由它裁。
  private outcome: CaptureOutcome | null = null;
  private physicalStopCalled = false;
  private lifecycleError: AutopilotMediaError | null = null;
  private didStart = false;
  private startedSettled = false;
  // 真实 onstart 的一次性快照。冻结之后谁都不再回头读 Recorder 的字段——
  // discardActive() 会把起点和 MIME 清掉，回头读只会读到 null。
  private startFact: { startedAtMs: number; mimeType: AutopilotMimeType } | null = null;
  private startFactError: unknown = null;
  private settled = false;
  private readonly sessionId: string;
  private readonly command: RecordCommand;
  private readonly identity: LocalAutopilotCaptureIdentity;
  private readonly isForeground: () => boolean;
  private readonly observe?: LocalAutopilotCaptureObserver;
  private readonly ports: ExactCaptureUploadPorts;
  private readonly bootstrap: AutopilotCaptureBootstrapPorts;

  constructor(sessionId: string, command: RecordCommand,
              identity: LocalAutopilotCaptureIdentity,
              isForeground: () => boolean,
              observe?: LocalAutopilotCaptureObserver,
              ports: ExactCaptureUploadPorts = browserExactCaptureUploadPorts,
              bootstrap: AutopilotCaptureBootstrapPorts
                = browserAutopilotCaptureBootstrapPorts) {
    this.sessionId = sessionId;
    this.command = command;
    this.identity = identity;
    this.isForeground = isForeground;
    this.observe = observe;
    this.ports = ports;
    this.bootstrap = bootstrap;
    this.started = this.startedDeferred.promise;
    this.stopped = this.stoppedDeferred.promise;
    this.closed = this.closedDeferred.promise;
    // 没人 race 它的时候这条拒绝也不能掀翻进程。
    this.lifecycleDeferred.promise.catch(() => {});
    void this.run();
  }

  requestStop(reason: "user_done" = "user_done"): void {
    this.signalStop(reason);
  }

  /** 本地事实：真实 onstart 已经发生，屏幕可以说"正在听您说"。 */
  get locallyStarted(): boolean {
    return this.startFact !== null || this.recorder.startedAtMs !== null;
  }

  /**
   * 真实 onstart 的一次性冻结。正常续延与 `interrupt()` 共用这一个方法。
   *
   * 谁先跑到这里谁负责冻结：真实开录已经发生之后 `discardActive()` 会清掉起点与
   * MIME，另一方稍后再去读设备字段只会读到 null，而那正是把 after_start 误判成
   * before_start、连吞 started 与 failed 两条 ACK 的那条缝。
   */
  private freezeStartFact(): { startedAtMs: number; mimeType: AutopilotMimeType } | null {
    if (this.startFact !== null || this.startFactError !== null) return this.startFact;
    const startedAtMs = this.recorder.startedAtMs;
    if (startedAtMs === null) return null;
    try {
      this.startFact = { startedAtMs, mimeType: exactAutopilotMime(this.recorder.mimeType) };
    } catch (error) {
      // 编码不在服务器白名单：这不是一次可上报的开录，服务器也收不下这条
      // record_started。判一次就定型，两条路径读到的结论完全一致。
      this.startFactError = error;
    }
    return this.startFact;
  }

  private settleStarted(mimeType: AutopilotMimeType): void {
    if (this.startedSettled) return;
    this.startedSettled = true;
    this.startedDeferred.resolve({ mime_type: mimeType });
  }

  private rejectStarted(error: unknown): void {
    if (this.startedSettled) return;
    this.startedSettled = true;
    this.startedDeferred.reject(error);
  }

  /**
   * 页面生命周期中断。**绝不能用 `cancel()` 代替**：`cancel()` 在真实开录之后会
   * signal `stream_end`，那条路会把半段音频 stage、上传、变成一次有效作答。
   */
  interrupt(): CaptureInterruptDisposition {
    // 成功停止一旦同步调用，这条捕获已经在持久化路上，不能被改写成失败。
    if (this.physicalStopCalled || this.outcome === "successful_stop") return "too_late";
    if (this.outcome === "lifecycle_failure") return "already_interrupted";
    this.outcome = "lifecycle_failure";
    // 阶段判据取 Recorder 自己的单调事实，并且在丢弃之前就把它冻结下来。
    const started = this.freezeStartFact();
    const error = new AutopilotMediaError(
      "device_runtime_failed", "页面已离开前台，本次录音已被丢弃");
    this.lifecycleError = error;
    this.abortController.abort(error);
    // 先取快照、先交出所有权，再去碰两个可能抛错的定时器端口。
    //
    // 顺序反了就是这条 P1：clear 抛错会让 interrupt() 在物理拆设备与结算之前就
    // 退出，而 outcome 早已锁死，第二次 interrupt 只会拿到 already_interrupted，
    // 于是麦克风可能一直活着、closed 可能永远不 settle。
    //
    // 交出所有权还有第二个作用：CaptureDeadline.clear() 清失败时不会把自己的
    // handle 置空，外层 cleanup 的 `maxTimer !== null` 那步也会重试同一个已知
    // 失效的端口。这里先置空，"本次采集不再拥有这两个定时器"就成了结构性事实。
    // 没清掉的定时器晚点烧也无害：deadline 的 fire() 有 expiry 闩幂等，作答窗口
    // 那条只会 signalStop，而 run() 后面的 `outcome !== null` 闸会照旧判死。
    const deadline = this.preStartDeadline;
    const answerTimer = this.maxTimer;
    this.preStartDeadline = null;
    this.maxTimer = null;
    this.releaseTimerBestEffort(() => deadline?.clear());
    this.releaseTimerBestEffort(() => {
      if (answerTimer !== null) this.bootstrap.clearTimer(answerTimer);
    });
    this.recorder.cancelPendingStart();
    this.recorder.discardActive();
    if (started) {
      // 真实开录已经发生：exact-once resolve 这条 started，屏幕不改口说"正在听"、
      // 作答窗口也不武装。半段录音物理丢弃，stopped 走生命周期失败。
      this.didStart = true;
      this.settleStarted(started.mimeType);
    } else {
      this.rejectStarted(error);
    }
    this.lifecycleDeferred.reject(error);
    this.publish({ ...this.identity, phase: "cleared" });
    return started ? "after_start" : "before_start";
  }

  /**
   * 释放一个本次采集拥有的定时器。**只做这一件事。**
   *
   * 定时器端口失效不改变生命周期判定：两条 clear 各自独立 best-effort，一条抛错
   * 不得挡住另一条，更不得挡住后面的物理拆设备与结算。刻意不把整个 interrupt()
   * 包进大 catch——那会把真正的编程错误一起吞掉。
   */
  private releaseTimerBestEffort(release: () => void): void {
    try {
      release();
    } catch { /* 定时器端口失效不改变生命周期判定 */ }
  }

  cancel(): void {
    if (this.outcome === "lifecycle_failure") return;
    if (!this.didStart) {
      this.abortController.abort(new DOMException("录音命令已取消", "AbortError"));
      this.recorder.cancelPendingStart();
      this.recorder.discardActive();
      return;
    }
    // Once a real microphone start has occurred, cancellation still closes and
    // stages the captured bytes. It never leaves a hot mic or invents stopped.
    if (this.stopLatch.reason !== null && (this.recorder.active || this.recorder.stopping)) {
      // A stop/onstop that exceeded the controller deadline must be physically
      // torn down as well; Recorder rejects the pending stop so the Web Lock is
      // released in this capture's finally path.
      this.recorder.discardActive({ force: true });
    } else {
      this.signalStop("stream_end");
    }
  }

  private signalStop(reason: AutopilotStopReason): void {
    this.stopLatch.signal(reason);
  }

  /** 每一步开麦前置都要重新证明页面在前台；后台开麦就是隐性采集。 */
  private assertForeground(): void {
    if (this.abortController.signal.aborted) {
      throw this.lifecycleError ?? new Error("录音命令已取消");
    }
    if (!this.isForeground()) {
      throw new AutopilotMediaError(
        "device_runtime_failed", "页面已离开前台，拒绝开启麦克风");
    }
  }

  /** 屏显是纯 UI 信号：observer 抛错不得让这次采集受一点影响。 */
  private publish(event: Parameters<LocalAutopilotCaptureObserver>[0]): void {
    if (!this.observe) return;
    try {
      this.observe(event);
    } catch { /* 屏幕更新坏了不能赔上已经录到的音频 */ }
  }

  /**
   * 同步闸：结局已定或已中止，就不许再往前走一步。
   *
   * 错误取值有先后：生命周期错误优先，其次才是截止时刻已过的稳定超时；两者都
   * 没有时才落到通用取消。这样 controller 拿到的 record_failed 错误码不会因为
   * 谁先落地而漂移。
   */
  private assertStageAdmissible(deadline: CaptureDeadline): void {
    if (this.outcome === null && !this.abortController.signal.aborted) return;
    throw this.lifecycleError ?? deadline.check() ?? new Error("录音命令已取消");
  }

  /**
   * 六段 pre-start 的统一闸。
   *
   * `deadline.race` 只认截止时刻，不认生命周期——而 `interrupt()` 恰恰会把那个
   * 定时器 clear 掉。于是 pagehide 之后，一条不合作的 snapshot 或授权既等不到
   * 截止时刻、也等不到任何人：run()、它的 finally 与 closed 一起悬死。
   *
   * 每一段都必须从这里过：调用 factory **之前**同步查一次结局/中止；拿到底层
   * Promise 之后立刻装上这一段该有的资源/落地观察者；再同时与**同一条** deadline
   * 和**同一条** lifecycle 拒绝赛跑；race 回来之后、把结果交给调用方之前再同步
   * 查一次。后置闸不过就不许调用下一段——下一段的 Promise 因此根本不会被创建，
   * 即使底层已经 settle、续延已经排队，同一事件栈里的 interrupt 仍然挡得住。
   *
   * 这里不新起定时器、不新建截止时刻：deadline 与 lifecycleDeferred 都是本次
   * 采集已经有的那一个。
   */
  private async stage<T>(
    deadline: CaptureDeadline,
    open: () => Promise<T>,
    observe: (operation: Promise<T>) => void,
  ): Promise<T> {
    this.assertStageAdmissible(deadline);
    const operation = open();
    // 观察者必须先于 race 挂上：迟到的 resolve/reject 都要有人接。
    observe(operation);
    const value = await Promise.race([
      deadline.race(operation),
      this.lifecycleDeferred.promise,
    ]);
    this.assertStageAdmissible(deadline);
    return value;
  }

  private async run(): Promise<void> {
    let phase: "starting" | "recording" | "persistence" = "starting";
    // 非合作的 acquisition 可能在判死之后才交出一把真实的 Web Lock。tracker 必须
    // 先于 race 挂上并进入 cleanup，否则那把迟到的锁没人释放，下一个页面永远排
    // 不进来。acquisition 自己失败时它照样 settle，closed 不会因此悬着。
    let leaseDelivery: Promise<void> = Promise.resolve();
    try {
      // 绝对截止时刻在第一项异步 bootstrap（含 acquireLease）之前同步武装，整段
      // pre-start 只有这一个定时器：租约、恢复快照、两次授权、prepare、等待真实
      // onstart 全部共用它，任何一段都不重置预算。controller 不再为
      // capture.started 另起 20 秒超时，只消费这条 Promise 已经 settle 的结果。
      const deadline = armCaptureDeadline({
        deadlineAt: this.bootstrap.now() + PRE_START_BUDGET_MS,
        now: () => this.bootstrap.now(),
        setTimer: (callback, delayMs) => this.bootstrap.setTimer(callback, delayMs),
        clearTimer: (handle) => this.bootstrap.clearTimer(handle),
        failClosed: (reason) => {
          this.abortController.abort(reason);
          this.recorder.cancelPendingStart();
          this.recorder.discardActive();
        },
      });
      this.preStartDeadline = deadline;
      try {
        // 1/6 设备租约。它**可能迟到交付**一把真锁，所以观察者就是 late-lease
        // tracker：acquisition 有结论之前 closed 不许 settle，否则那把将来才出现
        // 的锁永远没人释放，下一个页面再也开不了麦。
        this.lease = await this.stage(
          deadline,
          () => this.bootstrap.acquireLease(this.abortController.signal),
          (acquisition) => {
            leaseDelivery = acquisition.then(
              (lease) => { this.acquiredLease = lease; },
              () => { /* acquisition 自己失败：没有任何需要释放的锁 */ },
            );
          },
        );
        // 2/6 恢复快照。不交付任何本地设备资源：迟到只需隔离，closed 不必等它。
        const snapshot = await this.stage(
          deadline,
          () => this.bootstrap.recoverySnapshot(),
          isolateLateSettlement,
        );
        if (snapshot.invalidBlobKeyCount > 0 || snapshot.legacyOrphans.length > 0
            || snapshot.entries.length > 0) {
          throw new Error("本机存在待恢复录音，禁止覆盖开新麦克风");
        }
        // 3/6 第一次授权。同样是无资源阶段。
        const authorization = await this.stage(
          deadline,
          () => this.bootstrap.authorize(
            this.sessionId,
            this.command.command_key,
            this.abortController.signal,
          ),
          isolateLateSettlement,
        );
        if (!authorizesMicrophoneStart(authorization)) {
          throw new Error("当前场次未授权开启麦克风");
        }
        this.assertForeground();
        // 4/6 只取流、只造 recorder。到这里为止一个录音字节都没有产生。
        // 底层 getUserMedia 可能迟到交付 MediaStream：留住这条 Promise，cleanup
        // 里 dispose() 之后等它，晚到的 tracks 才算真的被关干净。
        const prepared = await this.stage(
          deadline,
          () => this.recorder.prepare(),
          (operation) => {
            this.preparePromise = operation;
            isolateLateSettlement(operation);
          },
        );
        if (!prepared || this.abortController.signal.aborted) {
          throw new Error("麦克风未能完成准备");
        }
        this.assertForeground();
        // 5/6 Permission prompts can outlive the command that authorized opening.
        // Revalidate after the stream really exists; a pause/takeover during the
        // prompt closes tracks before any byte is recorded.
        //
        // 这次复核是一整个网络往返，但麦克风**还没开录**：它耗掉的是开麦前预算，
        // 不再从老人的 14 秒作答窗口里扣，也不会留下一段没人知道的隐性录音。
        const postPermissionAuthorization = await this.stage(
          deadline,
          () => admitCaptureBeforeDeadline(
            this.bootstrap.authorize(
              this.sessionId,
              this.command.command_key,
              this.abortController.signal,
            ),
            deadline,
          ),
          isolateLateSettlement,
        );
        if (!authorizesMicrophoneStart(postPermissionAuthorization)
            || this.abortController.signal.aborted) {
          throw new Error("麦克风权限返回后自动驾驶命令已失效");
        }
        const expired = deadline.check();
        if (expired) throw expired;
        this.assertForeground();
        // 6/6 startPrepared() 在返回 Promise 之前同步下达开录指令；这里 await 的
        // 只是真实 onstart 事件。
        const startedReal = await this.stage(
          deadline,
          () => this.recorder.startPrepared(),
          isolateLateSettlement,
        );
        if (!startedReal) throw new Error("麦克风未真实进入 recording 状态");
      } finally {
        deadline.clear();
        this.preStartDeadline = null;
      }
      const started = this.freezeStartFact();
      if (this.outcome !== null) {
        // 生命周期在这条续延恢复之前就赢了：exact start fact、started 与 stopped
        // 都已经由 interrupt() 收口。这里不重读已清字段、不 publish、不武装作答
        // 窗口，直接沿它那条结局收口。
        throw this.lifecycleError ?? new Error("录音过程未正常结束");
      }
      if (!started) throw this.startFactError ?? new Error("录音缺少真实起点时刻");
      // 从这里到 resolve 之间没有任何网络 await：屏幕说"正在听您说"、作答窗口
      // 武装、capture.started 落地，三件事绑在同一个真实 onstart 上。
      // record_started 的网络 ACK 随后再发，屏显和按钮都不等它。
      this.didStart = true;
      phase = "recording";
      // 真实开录已经发生，先把它 exact-once 结算掉，再做任何**仍可能抛错**的
      // 作答窗口计算与定时器调用。反过来的话，timer port 同步抛错会被 catch 里
      // 的 `!didStart` 判据挡住：started 永久 pending，controller 跟着永久挂起。
      // 已经真实发生的开录不因定时器失败而回滚，半段音频也一个字节都不上传。
      this.settleStarted(started.mimeType);
      this.publish({ ...this.identity, phase: "listening" });
      this.maxTimer = this.bootstrap.setTimer(
        () => this.signalStop("max_duration"),
        remainingAnswerWindowMs(
          this.command.payload.max_duration_seconds,
          this.bootstrap.now() - started.startedAtMs,
        ));

      const stopReason = await Promise.race([
        this.stopLatch.wait(), this.lifecycleDeferred.promise,
      ]);
      if (this.maxTimer !== null) this.bootstrap.clearTimer(this.maxTimer);
      this.maxTimer = null;
      // signal 只是"有人要求停"，还不等于成功。先 claim 成功，claim 到手才在
      // 同一个同步段里调 MediaRecorder.stop()；生命周期若在 claim 之前赢，
      // 这条捕获整个丢弃。
      if (this.outcome !== null) {
        throw this.lifecycleError ?? new Error("录音过程未正常结束");
      }
      this.outcome = "successful_stop";
      // 物理收麦、判活、"已收音"屏显在同一处汇流：max_duration 与 user_done 都
      // 走这里；两项校验先过，屏幕才可以说已收音——空字节或超限的采集马上就会
      // 被判死，先显示保存态就是在骗老人。事件仍然早于 IndexedDB 与任何网络。
      const recording = await finalizeCaptureThenNotify(
        () => {
          this.physicalStopCalled = true;
          return this.recorder.stop();
        },
        (captured) => {
          if (captured.blob.size <= 0) throw new Error("录音没有产生可持久化字节");
          // 顶穿上限的录音在 ACK 阶段必被拒。照实上报、绝不 cap，但也不必先把
          // 它 stage 进 outbox 再上传一遍。
          assertCaptureDurationWithinCommandLimit(
            captured.durationSeconds, this.command.payload.max_duration_seconds);
        },
        stopReason === "user_done" || stopReason === "max_duration"
          ? { ...this.identity, phase: "persisting", stopReason }
          : { ...this.identity, phase: "cleared" },
        this.observe,
      );
      phase = "persistence";
      let entry = createAudioOutboxEntry({
        rawAudioId: this.command.payload.raw_audio_id,
        sessionId: this.sessionId,
        turnKey: this.command.payload.turn_ref,
        containsDirectIdentifier: this.command.payload.contains_direct_identifier,
        durationSeconds: recording.durationSeconds,
        blob: recording.blob,
      });
      entry = attachAutopilotStopReason(entry, stopReason);
      const completed = await stageAndFinishExactCapture(
        this.command, entry, recording.blob, this.ports);
      this.settled = true;
      this.stoppedDeferred.resolve(completed.facts);
    } catch (error) {
      this.publish({ ...this.identity, phase: "cleared" });
      // 生命周期已经判死这条捕获时，对外错误码只能是那一条。`interrupt()` 会同步
      // 丢弃设备，被丢弃的底层操作随后也会拒绝；哪条拒绝先赢 race 取决于调度，
      // 不加这一层，controller 收到的 record_failed 错误码就会跟着漂。
      const mapped = this.lifecycleError
        ?? (error instanceof AutopilotMediaError ? error
        : phase === "starting"
          ? new AutopilotMediaError(
            error instanceof DOMException
              && (error.name === "NotAllowedError" || error.name === "SecurityError")
              ? "microphone_denied"
              : error instanceof DOMException && error.name === "NotFoundError"
                ? "microphone_unavailable" : "recording_start_failed",
            "麦克风未能安全启动",
            { cause: error },
          )
          : phase === "persistence"
            ? new AutopilotMediaError(
              "recording_upload_failed",
              "录音上传或采集收据未完成",
              { cause: error },
            )
            : new AutopilotMediaError(
              "recording_runtime_failed",
              "录音过程未正常结束",
              { cause: error },
            ));
      if (!this.didStart) this.rejectStarted(mapped);
      this.stoppedDeferred.reject(mapped);
      try {
        if (this.recorder.active) this.recorder.discardActive();
      } catch { /* 设备层已不可用；dispose 与 Recorder 内部的断引用仍会跑 */ }
    } finally {
      // closed 是 controller 判断"麦克风物理上已经不可能再出声"的唯一依据。
      // 清理里任何一步抛错都不能让它悬着，否则 controller 永远等在 await
      // capture.closed 上，整台设备卡死。主错误码已经在上面定型，这里吞掉的
      // 只是清理异常。
      const lease = this.lease;
      this.lease = null;
      await settleCaptureCleanup([
        () => {
          if (this.maxTimer !== null) this.bootstrap.clearTimer(this.maxTimer);
          this.maxTimer = null;
        },
        () => this.recorder.dispose(),
        // dispose() 已经推进了代际，迟到的 MediaStream 会被 prepare() 自己关掉
        // 全部 track——但那要等它真的到达。不等这条 Promise 就 resolve closed，
        // 等于在流还在路上时宣布"麦克风不可能再出声"。它 reject 也要被观察，
        // 所以走的是这条被 runBestEffortCleanup 逐步吞错的步骤。
        () => this.preparePromise?.then(() => undefined),
        // 上面那条落定之前，release 调用数必须是 0：先放锁，下一条采集拿到锁
        // 开麦，旧的 getUserMedia 才有机会迟到交出一条没人管的热麦。
        () => { lease?.release(); },
        // release() 抛错也必须继续等 released：Web Lock 真正交还之前，closed 说
        // "麦克风不可能再出声"就是假的。两步分开正是为了这一点。
        () => lease?.released,
        // 截止时刻先赢、acquisition 却还在路上：closed 必须等到它有结论。提前
        // settle 等于把一把将来才出现的锁永远留在那里，下一个页面再也开不了麦。
        () => leaseDelivery,
        () => {
          // 正常接管与迟到路径互斥：同一把锁只会落进其中一条，不会双重释放。
          if (this.acquiredLease !== lease) this.lateLease = this.acquiredLease;
          this.acquiredLease = null;
        },
        () => { this.lateLease?.release(); },
        () => this.lateLease?.released,
        () => {
          if (!this.settled && this.didStart && this.stopLatch.reason === null) {
            // Defensive only; all real starts should either persist or reject stopped.
            this.signalStop("stream_end");
          }
        },
      ], () => this.closedDeferred.resolve(undefined));
    }
  }
}

export interface BrowserAutopilotRecordingExecutorOptions {
  /** 本次 owner lease 代际；屏显事件靠它加 captureGeneration 区分新旧捕获。 */
  ownerGeneration: number;
  isForeground?: () => boolean;
  observe?: LocalAutopilotCaptureObserver;
  ports?: ExactCaptureUploadPorts;
  bootstrap?: AutopilotCaptureBootstrapPorts;
}

export class BrowserAutopilotRecordingExecutor implements AutopilotRecordingExecutor {
  private readonly sessionId: string;
  private readonly ownerGeneration: number;
  private readonly isForeground: () => boolean;
  private readonly observe?: LocalAutopilotCaptureObserver;
  private readonly ports: ExactCaptureUploadPorts;
  private readonly bootstrap: AutopilotCaptureBootstrapPorts;
  private captureGeneration = 0;

  constructor(sessionId: string, options: BrowserAutopilotRecordingExecutorOptions) {
    this.sessionId = sessionId;
    this.ownerGeneration = options.ownerGeneration;
    this.isForeground = options.isForeground
      ?? (() => document.visibilityState === "visible");
    this.observe = options.observe;
    this.ports = options.ports ?? browserExactCaptureUploadPorts;
    this.bootstrap = options.bootstrap ?? browserAutopilotCaptureBootstrapPorts;
  }

  start(command: RecordCommand): AutopilotRecordingCapture {
    if (command.state !== "pending") throw new Error("仅 pending 录音命令可开麦");
    // 页面已隐藏时连造都不造：后台开新麦克风没有任何正当理由。
    if (!this.isForeground()) {
      throw new AutopilotMediaError(
        "device_runtime_failed", "页面已离开前台，拒绝开启麦克风");
    }
    this.captureGeneration += 1;
    return new BrowserAutopilotCapture(
      this.sessionId,
      command,
      {
        sessionId: this.sessionId,
        commandKey: command.command_key,
        ownerGeneration: this.ownerGeneration,
        captureGeneration: this.captureGeneration,
      },
      this.isForeground,
      this.observe,
      this.ports,
      this.bootstrap,
    );
  }
}

export const browserAutopilotRecoveryDependencies: AutopilotRecoveryDependencies = {
  acquireLease: () => acquireAudioDeviceLease(),
  recoverySnapshot: () => blobStore.recoverySnapshot(),
  readBlob: (rawAudioId) => blobStore.get(rawAudioId),
  finish: async (command, entry, blob) =>
    (await finishExactCapture(command, entry, blob)).facts,
};
