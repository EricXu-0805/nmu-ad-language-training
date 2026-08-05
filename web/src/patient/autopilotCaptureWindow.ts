/**
 * 自动收麦的截止时刻怎么算。
 *
 * 服务器下发的 max_duration_seconds 是**上报时长的硬上限**：record_stopped 的
 * duration_seconds 必须严格 <= 它（后端 apply_device_ack 与 ledger 各校一次），
 * 超一点整条录音证据被拒、场次停在 record_failed。所以老人不点"我说好了"、
 * 完全靠自动截止收麦这条路，截止时刻必须从**真实录音起点**往回算，还得预留
 * 一段余量——setTimeout 只保证不早于，从不保证不晚于。
 *
 * 两个量分开命名，别混成一个"缓冲"：
 * - **作答窗口** answerWindowMs：老人真正能说话的那段时间。
 * - **设备停麦抖动容差** CAPTURE_STOP_JITTER_TOLERANCE_MS：从截止时刻触发到
 *   Recorder.stop() 读数之间，定时器迟到 + 事件派发耗时的预算上限。
 *
 * 作答窗口 = 服务器上限 − 抖动容差。容差是常量、有严格上限，不随重试或失败
 * 次数放大；真实时长照实上报，绝不 cap、绝不改写。余量被真实抖动吃穿时，
 * 后端照旧拒绝那条 ACK——这是有意的 fail-closed，不是可以调松的旋钮。
 */

import { AutopilotMediaError } from "./autopilotMediaError.ts";

/** 设备停麦抖动容差。只覆盖定时器迟到与停麦派发，不是给网络往返用的。 */
export const CAPTURE_STOP_JITTER_TOLERANCE_MS = 1_000;

/** 容差不得把作答窗口吃光：窗口至少留这么长，且永不超过服务器上限本身。 */
export const MIN_ANSWER_WINDOW_MS = 1_000;

function hardLimitMs(maxDurationSeconds: number): number {
  if (!Number.isFinite(maxDurationSeconds) || maxDurationSeconds <= 0) {
    throw new Error("录音命令的最长时长非法");
  }
  return Math.round(maxDurationSeconds * 1_000);
}

/**
 * 一次录音允许的作答窗口。P0a 实际下发 max_duration_seconds = silence_seconds + 5
 * = 15 秒，窗口 14 秒，仍然长过 10 秒的冻结沉默阈值。
 *
 * 上限本身短于容差时（契约允许 max_duration_seconds 低到 1 秒，P0a 不会这么发），
 * 窗口退化成上限，此时真实抖动一定会把时长顶出上限、由后端拒绝——这里不假装
 * 还有余量。
 */
export function answerWindowMs(maxDurationSeconds: number): number {
  const hard = hardLimitMs(maxDurationSeconds);
  return Math.max(
    Math.min(hard, MIN_ANSWER_WINDOW_MS),
    hard - CAPTURE_STOP_JITTER_TOLERANCE_MS,
  );
}

/**
 * 从真实录音起点起算，还剩多少作答窗口。
 *
 * elapsedSinceStartMs 是**已经录进去**的那一段（构造 MediaRecorder 之后到武装
 * 定时器之间的耗时）。不扣掉它，这段就整段加在服务器上限之外——权限返回后那次
 * 授权复核是一整个网络往返，正是 T5 里自动收麦必被拒的主因。
 */
export function remainingAnswerWindowMs(
  maxDurationSeconds: number,
  elapsedSinceStartMs: number,
): number {
  if (!Number.isFinite(elapsedSinceStartMs) || elapsedSinceStartMs < 0) {
    throw new Error("录音起点耗时非法");
  }
  return Math.max(0, answerWindowMs(maxDurationSeconds) - elapsedSinceStartMs);
}

/**
 * 开麦前整段准备的绝对预算。这是 pre-start 窗口里**唯一**的一个数：prepare、
 * 权限返回后的第二次服务端授权、等待真实 onstart 全部受它约束，也只武装一个
 * 定时器。控制器不得再为 capture.started 另起一个超时——两个 owner 就是两条
 * 互相竞争的截止时刻，谁先烧取决于调度，判定就不可复现了。
 */
export const PRE_START_BUDGET_MS = 20_000;

export interface CaptureAdmissionDeadlinePorts {
  /** 绝对截止时刻，与 now() 同一条单调时钟。 */
  deadlineAt: number;
  /** 单调时钟读数（生产是 performance.now）。 */
  now(): number;
  setTimer(callback: () => void, delayMs: number): number;
  clearTimer(handle: number): void;
  /**
   * 截止时刻先赢时执行：中止在途授权请求 + 物理关掉麦克风。
   * 必须同步、可重入。语义是 **best-effort 清理**：它抛错不改变判定，
   * 三条判死路径都会把它的异常吞掉（物理关麦还有 capture 的 catch/finally 兜底）。
   */
  failClosed(reason: Error): void;
}

function captureDeadlineError(): AutopilotMediaError {
  return new AutopilotMediaError(
    "device_command_timeout", "麦克风权限复核晚于录音截止时刻");
}

/**
 * 判死时的清理一律 best-effort：清理失败不能把稳定的 device_command_timeout
 * 换成一个随机的清理异常，否则 controller 那边的 record_failed 错误码就飘了。
 */
function failClosedBestEffort(
  ports: CaptureAdmissionDeadlinePorts,
  reason: AutopilotMediaError,
): void {
  try {
    ports.failClosed(reason);
  } catch {
    // 清理本身失败已经无处可报，判定不受影响。
  }
}

/**
 * 一个已武装的绝对截止时刻，供整段 pre-start 共用。
 *
 * `race` 可以调用任意多次，`setTimer` 只发生一次；`clear` 也只撤那一次。
 */
export interface CaptureDeadline {
  readonly deadlineAt: number;
  /**
   * 按**绝对单调时钟**判定是否已过期，过期则就地判死并返回稳定错误。
   * 判据不是"定时器烧没烧"：后台标签页被节流时，定时器可以晚到几秒才跑。
   */
  check(): AutopilotMediaError | null;
  race<T>(operation: Promise<T>): Promise<T>;
  clear(): void;
}

/**
 * 武装本次采集唯一的那个 pre-start 定时器。
 *
 * 判死时的次序是刻意的：先让 expiry 稳定 settle，再做 fail-closed 清理。反过来
 * 做有两个坑——failClosed 会 abort 在途请求，那条 AbortError 可能抢先落地赢下
 * race；failClosed 自己抛错更会让这个 promise 永远不 settle，请求又不返回时
 * 整条采集就悬死了。
 *
 * 截止时刻赢的时候：立刻物理关麦、中止在途请求、拒绝这次采集（不产生
 * record_started/record_stopped，也不进持久化阶段）。
 */
export function armCaptureDeadline(ports: CaptureAdmissionDeadlinePorts): CaptureDeadline {
  const expiry: { error: AutopilotMediaError | null } = { error: null };
  let handle: number | null = null;
  let rejectExpiry: ((reason: unknown) => void) | null = null;
  const expired = new Promise<never>((_resolve, reject) => { rejectExpiry = reject; });
  // 没有任何一段 race 它的时候，这条拒绝也不能掀翻进程。
  expired.catch(() => {});

  const fire = (): AutopilotMediaError => {
    if (expiry.error !== null) return expiry.error;
    const error = captureDeadlineError();
    expiry.error = error;
    rejectExpiry?.(error);
    failClosedBestEffort(ports, error);
    return error;
  };

  const check = (): AutopilotMediaError | null => {
    if (expiry.error !== null) return expiry.error;
    return ports.now() >= ports.deadlineAt ? fire() : null;
  };

  const remainingMs = ports.deadlineAt - ports.now();
  if (remainingMs <= 0) fire();
  else handle = ports.setTimer(() => { fire(); }, remainingMs);

  return {
    deadlineAt: ports.deadlineAt,
    check,
    async race<T>(operation: Promise<T>): Promise<T> {
      // 被中止/被判死之后这条请求的结局已经没人消费；先挂一个吞掉的续延，免得
      // 迟到的拒绝变成 unhandled rejection。
      operation.catch(() => {});
      const before = check();
      if (before) throw before;
      try {
        const value = await Promise.race([operation, expired]);
        const after = check();
        if (after) throw after;
        return value;
      } catch (error) {
        // 被 abort 的请求抛出的 AbortError 可能先赢 race。只要截止时刻已经判死，
        // 对外一律是 device_command_timeout，不让传输层错误盖掉真正的原因。
        throw expiry.error ?? error;
      }
    },
    clear() {
      if (handle !== null) {
        ports.clearTimer(handle);
        handle = null;
      }
    },
  };
}

/**
 * 权限返回后的那次授权复核，与同一个 pre-start 截止时刻做**可取消的赛跑**。
 *
 * 光靠一个"到点 signalStop"的定时器不够：协程此刻还卡在授权请求的 await 上。
 * 请求要是卡过截止时刻（甚至永不返回），设备准备状态会一直挂着，等授权终于
 * 回来才收拾——所以由截止时刻直接判死并物理关麦。
 */
export function admitCaptureBeforeDeadline<T>(
  authorization: Promise<T>,
  deadline: CaptureDeadline,
): Promise<T> {
  return deadline.race(authorization);
}

/**
 * 停麦读数之后、进入持久化之前的最后一道闸。
 *
 * 真实时长顶穿服务器上限时，这段录音在 ACK 阶段一定会被拒。既不许 cap、也不许
 * 改写上报值，那就干脆不 stage、不上传：让它照实变成一次 record_failed。
 *
 * 两个数都必须是可上报的实数：NaN/Infinity/负时长/非正上限说明读数本身坏了，
 * 同样不许进入持久化。等号放行——后端的判据也是严格 <=。
 */
export function assertCaptureDurationWithinCommandLimit(
  durationSeconds: number,
  maxDurationSeconds: number,
): void {
  const usable = Number.isFinite(durationSeconds) && durationSeconds >= 0
    && Number.isFinite(maxDurationSeconds) && maxDurationSeconds > 0;
  if (!usable || !(durationSeconds <= maxDurationSeconds)) {
    throw new AutopilotMediaError(
      "recording_runtime_failed", "本机录音时长不满足服务器为这条命令签发的上限");
  }
}
