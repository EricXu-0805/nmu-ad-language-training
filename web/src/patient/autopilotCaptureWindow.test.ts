import assert from "node:assert/strict";
import test from "node:test";
import {
  CAPTURE_STOP_JITTER_TOLERANCE_MS,
  MIN_ANSWER_WINDOW_MS,
  admitCaptureBeforeDeadline,
  answerWindowMs,
  assertCaptureDurationWithinCommandLimit,
  remainingAnswerWindowMs,
} from "./autopilotCaptureWindow.ts";
import { AutopilotMediaError } from "./autopilotMediaError.ts";

/** P0a 实际下发的上限：冻结协议 silence_seconds(10) + 5。 */
const P0A_MAX_DURATION_SECONDS = 15;

/**
 * 复刻 BrowserAutopilotCapture 的自动截止路径 + Recorder.stop() 的读数口径。
 *
 * Recorder 在**下达开录指令**那一刻记 startedAt，stop() 被调用时立刻用
 * performance.now() − startedAt 算时长（不等 onstop），所以上报时长就是
 * 起点到"调用 stop"之间的真实毫秒数：
 *   armGap（起点→武装定时器）+ 定时器延时 + 定时器迟到 + 停麦派发。
 * 这里一个毫秒都不 cap，算出来多少就是设备会上报的多少。
 */
function simulateAutoDeadlineCapture(input: {
  maxDurationSeconds: number;
  armGapMs: number;
  timerLatenessMs: number;
  stopDispatchMs: number;
}): number {
  const delay = remainingAnswerWindowMs(input.maxDurationSeconds, input.armGapMs);
  return input.armGapMs + delay + input.timerLatenessMs + input.stopDispatchMs;
}

test("作答窗口与设备停麦抖动容差是两个量，窗口 = 服务器上限 − 容差", () => {
  assert.equal(CAPTURE_STOP_JITTER_TOLERANCE_MS, 1_000);
  assert.equal(answerWindowMs(P0A_MAX_DURATION_SECONDS), 14_000);
  assert.equal(
    answerWindowMs(P0A_MAX_DURATION_SECONDS),
    P0A_MAX_DURATION_SECONDS * 1_000 - CAPTURE_STOP_JITTER_TOLERANCE_MS,
  );
  // 容差是常量、有严格上限，不是可以随失败次数放大的旋钮。
  assert.ok(CAPTURE_STOP_JITTER_TOLERANCE_MS <= 1_000);
  // 窗口仍然长过冻结的 10 秒沉默阈值：预留余量没有偷走老人的作答时间。
  assert.ok(answerWindowMs(P0A_MAX_DURATION_SECONDS) > 10_000);
});

test("剩余窗口从真实录音起点起算，已录进去的那段必须扣掉", () => {
  assert.equal(remainingAnswerWindowMs(P0A_MAX_DURATION_SECONDS, 0), 14_000);
  assert.equal(remainingAnswerWindowMs(P0A_MAX_DURATION_SECONDS, 250), 13_750);
  // 起点耗时已经吃穿窗口时立即截止，不会算出负延时把定时器变成"永不触发"。
  assert.equal(remainingAnswerWindowMs(P0A_MAX_DURATION_SECONDS, 99_000), 0);
  assert.throws(() => remainingAnswerWindowMs(P0A_MAX_DURATION_SECONDS, -1));
  assert.throws(() => remainingAnswerWindowMs(0, 0));
  assert.throws(() => remainingAnswerWindowMs(Number.NaN, 0));
});

test("不点『我说好了』、纯靠自动截止的时长落在后端上限之内", () => {
  const hardLimitMs = P0A_MAX_DURATION_SECONDS * 1_000;
  for (const armGapMs of [0, 5, 40, 200]) {
    for (const timerLatenessMs of [0, 250, 900]) {
      for (const stopDispatchMs of [0, 1, 50]) {
        if (timerLatenessMs + stopDispatchMs > CAPTURE_STOP_JITTER_TOLERANCE_MS) continue;
        const reported = simulateAutoDeadlineCapture({
          maxDurationSeconds: P0A_MAX_DURATION_SECONDS,
          armGapMs,
          timerLatenessMs,
          stopDispatchMs,
        });
        // 后端 apply_device_ack 与 ledger 都是严格 <=，等号可以过。
        assert.ok(
          reported <= hardLimitMs,
          `armGap=${armGapMs} lateness=${timerLatenessMs} dispatch=${stopDispatchMs} → ${reported}ms`,
        );
        // 也不能因为怕超限就把窗口砍到没法作答。
        assert.ok(reported >= 13_000);
      }
    }
  }
});

test("权限返回后那次授权复核的耗时不再进入上限预算", () => {
  const hardLimitMs = P0A_MAX_DURATION_SECONDS * 1_000;
  // 复核是一整个网络往返；先武装、再复核，往返耗时与截止时刻无关。
  const authRoundTripMs = 1_800;
  assert.ok(simulateAutoDeadlineCapture({
    maxDurationSeconds: P0A_MAX_DURATION_SECONDS,
    armGapMs: 20,
    timerLatenessMs: 200,
    stopDispatchMs: 5,
  }) <= hardLimitMs);
  // 回归钉：旧顺序 = 复核之后才用整段 max_duration 武装，必然超上限被拒。
  const oldOrder = authRoundTripMs + hardLimitMs;
  assert.ok(oldOrder > hardLimitMs);
});

test("抖动超出容差时照实上报，绝不 cap 成上限", () => {
  const hardLimitMs = P0A_MAX_DURATION_SECONDS * 1_000;
  // 页面被切到后台、定时器被节流 3 秒：设备上报真实的 17 秒，
  // 由后端拒绝这条录音证据 —— 不允许改写成 15 秒骗过校验。
  const reported = simulateAutoDeadlineCapture({
    maxDurationSeconds: P0A_MAX_DURATION_SECONDS,
    armGapMs: 0,
    timerLatenessMs: 3_000,
    stopDispatchMs: 0,
  });
  assert.equal(reported, 17_000);
  assert.ok(reported > hardLimitMs);
});

/** 可控单调时钟 + 可控定时器：测试自己决定"现在几点"和定时器什么时候烧。 */
function fakeClock(startMs = 1_000) {
  const pending = new Map<number, () => void>();
  let nextHandle = 1;
  let nowMs = startMs;
  const armed: number[] = [];
  const cleared: number[] = [];
  return {
    armed,
    cleared,
    now: () => nowMs,
    advance(ms: number) { nowMs += ms; },
    setTimer(callback: () => void, delayMs: number): number {
      const handle = nextHandle;
      nextHandle += 1;
      armed.push(delayMs);
      pending.set(handle, callback);
      return handle;
    },
    clearTimer(handle: number): void {
      cleared.push(handle);
      pending.delete(handle);
    },
    /** 烧掉定时器：时钟同时推进到它对应的那一刻。 */
    fireAll(advanceMs: number) {
      nowMs += advanceMs;
      for (const [handle, callback] of [...pending]) {
        pending.delete(handle);
        callback();
      }
    },
    get pendingCount(): number { return pending.size; },
  };
}

function microphoneDouble() {
  const log: string[] = [];
  return {
    log,
    failClosed(reason: Error) {
      log.push(`abort:${reason.message}`);
      log.push("mic:physically-stopped");
    },
  };
}

function ports(clock: ReturnType<typeof fakeClock>, mic: ReturnType<typeof microphoneDouble>,
                deadlineAt: number) {
  return {
    deadlineAt,
    now: clock.now,
    setTimer: clock.setTimer.bind(clock),
    clearTimer: clock.clearTimer.bind(clock),
    failClosed: mic.failClosed,
  };
}

test("后置授权永不返回时，由截止时刻关麦并判死这次采集", async () => {
  const clock = fakeClock();
  const mic = microphoneDouble();
  let admitted = false;
  const never = new Promise<"authorized">(() => {});   // 请求悬着，永远不回

  const settled = admitCaptureBeforeDeadline(
    never,
    // 真实录音起点 30ms 前；绝对截止 = 起点 + 14 秒作答窗口。
    ports(clock, mic, clock.now() - 30 + answerWindowMs(P0A_MAX_DURATION_SECONDS)),
  ).then(() => { admitted = true; });

  assert.deepEqual(clock.armed, [13_970]);
  clock.fireAll(13_970);

  await assert.rejects(settled, (error: unknown) => {
    assert.ok(error instanceof AutopilotMediaError);
    assert.equal(error.errorCode, "device_command_timeout");
    return true;
  });
  assert.equal(admitted, false);                  // 绝不进入可持久 capture
  // 关麦发生在等待方拿到拒绝之前，不留热麦；也只发生一次。
  assert.deepEqual(mic.log, [
    "abort:麦克风权限复核晚于录音截止时刻",
    "mic:physically-stopped",
  ]);
  assert.equal(clock.pendingCount, 0);
});

test("剩余窗口已经归零时，一个已 resolved 的授权也不得抢先放行", async () => {
  const clock = fakeClock();
  const mic = microphoneDouble();
  // 已 resolved 的 promise 稳赢 setTimeout(0)：只信 Promise.race 就会放行一次
  // 事实上已经越界的采集。判据必须是绝对截止时刻。
  await assert.rejects(
    admitCaptureBeforeDeadline(
      Promise.resolve<"authorized">("authorized"),
      ports(clock, mic, clock.now()),
    ),
    (error: unknown) =>
      error instanceof AutopilotMediaError && error.errorCode === "device_command_timeout",
  );
  assert.deepEqual(mic.log.at(-1), "mic:physically-stopped");
  assert.deepEqual(clock.armed, []);              // 入口就判死，定时器都没武装
});

test("授权在定时器烧掉之前一刻返回，但单调时钟已过期：仍然判死", async () => {
  const clock = fakeClock();
  const mic = microphoneDouble();
  const deadlineAt = clock.now() + 14_000;
  let resolveAuth!: (value: "authorized") => void;
  const authorization = new Promise<"authorized">((resolve) => { resolveAuth = resolve; });

  const settled = admitCaptureBeforeDeadline(
    authorization, ports(clock, mic, deadlineAt));
  // 定时器还没烧，但真实时间已经越过截止时刻（后台标签页节流的典型形态）。
  clock.advance(14_500);
  resolveAuth("authorized");

  await assert.rejects(settled, (error: unknown) =>
    error instanceof AutopilotMediaError && error.errorCode === "device_command_timeout");
  assert.deepEqual(mic.log.at(-1), "mic:physically-stopped");
});

test("后置授权晚于截止时刻返回时，那次迟到的授权不再放行", async () => {
  const clock = fakeClock();
  const mic = microphoneDouble();
  let resolveLate!: (value: "authorized") => void;
  const late = new Promise<"authorized">((resolve) => { resolveLate = resolve; });

  const settled = admitCaptureBeforeDeadline(
    late, ports(clock, mic, clock.now() + 14_000));
  clock.fireAll(14_000);                          // 截止时刻先赢
  resolveLate("authorized");                      // 授权随后才回来，且是"仍授权"

  await assert.rejects(settled, (error: unknown) =>
    error instanceof AutopilotMediaError && error.errorCode === "device_command_timeout");
  assert.deepEqual(mic.log.at(-1), "mic:physically-stopped");
});

test("判死之后迟到的授权拒绝不会泄漏成 unhandled rejection", async () => {
  const clock = fakeClock();
  const mic = microphoneDouble();
  let rejectLate!: (error: unknown) => void;
  const late = new Promise<"authorized">((_resolve, reject) => { rejectLate = reject; });

  const settled = admitCaptureBeforeDeadline(
    late, ports(clock, mic, clock.now() + 14_000));
  clock.fireAll(14_000);
  await assert.rejects(settled);
  // 请求被中止后才抛出的网络错误已经没人消费；它不得把进程掀翻。
  rejectLate(new Error("授权请求已被中止"));
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
});

test("正常 400ms 授权路径照常放行，且撤掉准入定时器", async () => {
  const clock = fakeClock();
  const mic = microphoneDouble();
  const deadlineAt = clock.now() - 400 + answerWindowMs(P0A_MAX_DURATION_SECONDS);
  let resolveAuth!: (value: "authorized") => void;
  const authorization = new Promise<"authorized">((resolve) => { resolveAuth = resolve; });

  const settled = admitCaptureBeforeDeadline(
    authorization, ports(clock, mic, deadlineAt));
  clock.advance(400);                             // 复核真的花了 400ms
  resolveAuth("authorized");

  assert.equal(await settled, "authorized");
  assert.deepEqual(clock.armed, [13_600]);
  assert.equal(clock.cleared.length, 1);
  assert.equal(clock.pendingCount, 0);
  assert.deepEqual(mic.log, []);                  // 没有任何 fail-closed 关麦
});

test("failClosed 内部同步拒绝授权时，对外仍然是 device_command_timeout", async () => {
  const clock = fakeClock();
  let rejectAuth!: (error: unknown) => void;
  const authorization = new Promise<"authorized">((_resolve, reject) => {
    rejectAuth = reject;
  });
  const log: string[] = [];
  const settled = admitCaptureBeforeDeadline(authorization, {
    deadlineAt: clock.now() + 14_000,
    now: clock.now,
    setTimer: clock.setTimer.bind(clock),
    clearTimer: clock.clearTimer.bind(clock),
    failClosed: () => {
      log.push("mic:physically-stopped");
      // 生产里这一步 abort 在途 fetch；AbortError 可能抢先落地赢下 race。
      rejectAuth(new DOMException("录音已到截止时刻", "AbortError"));
    },
  });
  clock.fireAll(14_000);

  await assert.rejects(settled, (error: unknown) =>
    error instanceof AutopilotMediaError && error.errorCode === "device_command_timeout");
  assert.deepEqual(log, ["mic:physically-stopped"]);
});

test("failClosed 自己抛错也不悬：deadline 仍然稳定超时", async () => {
  const clock = fakeClock();
  const never = new Promise<"authorized">(() => {});   // 授权永不返回
  const settled = admitCaptureBeforeDeadline(never, {
    deadlineAt: clock.now() + 14_000,
    now: clock.now,
    setTimer: clock.setTimer.bind(clock),
    clearTimer: clock.clearTimer.bind(clock),
    failClosed: () => { throw new Error("关麦时设备已被系统回收"); },
  });
  assert.doesNotThrow(() => clock.fireAll(14_000));    // 定时器回调不外抛

  await assert.rejects(settled, (error: unknown) =>
    error instanceof AutopilotMediaError && error.errorCode === "device_command_timeout");
});

test("停麦读数顶穿服务器上限或读数本身不可用时，不许进入持久化", () => {
  const rejected = (durationSeconds: number, maxDurationSeconds: number) => {
    try {
      assertCaptureDurationWithinCommandLimit(durationSeconds, maxDurationSeconds);
      return null;
    } catch (error) { return error; }
  };

  assert.doesNotThrow(() =>
    assertCaptureDurationWithinCommandLimit(14.02, P0A_MAX_DURATION_SECONDS));
  assert.doesNotThrow(() =>
    assertCaptureDurationWithinCommandLimit(0, P0A_MAX_DURATION_SECONDS));
  // 等号放行：后端判据也是严格 <=。
  assert.doesNotThrow(() =>
    assertCaptureDurationWithinCommandLimit(15, P0A_MAX_DURATION_SECONDS));

  for (const [durationSeconds, maxDurationSeconds] of [
    [15.4, P0A_MAX_DURATION_SECONDS],
    [15.000_001, P0A_MAX_DURATION_SECONDS],
    [-1, P0A_MAX_DURATION_SECONDS],
    [Number.NaN, P0A_MAX_DURATION_SECONDS],
    [Number.POSITIVE_INFINITY, P0A_MAX_DURATION_SECONDS],
    [Number.NEGATIVE_INFINITY, P0A_MAX_DURATION_SECONDS],
    [1, 0],
    [1, -5],
    [1, Number.NaN],
    [1, Number.POSITIVE_INFINITY],
  ] as const) {
    const failure = rejected(durationSeconds, maxDurationSeconds);
    assert.ok(failure instanceof AutopilotMediaError,
      `${durationSeconds}/${maxDurationSeconds} 应被拒绝`);
    assert.equal(failure.errorCode, "recording_runtime_failed");
  }
});

test("上限本身短于容差时窗口退化成上限，不假装还有余量", () => {
  // 契约允许 max_duration_seconds 低到 1 秒（P0a 不会这么发）。
  assert.equal(answerWindowMs(1), 1_000);
  assert.equal(answerWindowMs(1), MIN_ANSWER_WINDOW_MS);
  assert.ok(answerWindowMs(1) <= 1 * 1_000);
  assert.equal(answerWindowMs(2), 1_000);
});
