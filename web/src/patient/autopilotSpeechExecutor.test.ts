import assert from "node:assert/strict";
import test from "node:test";
import type { DeviceCredentialSelection } from "../api.ts";
import { browserBargeInPorts, startBargeInMonitor, type BargeInExposure } from "./autopilotBrowserBargeIn.ts";
import { AutopilotMediaError } from "./autopilotMediaError.ts";
import { PatientAutopilotController } from "./autopilotController.ts";
import { fetchAutopilotVoiceSettings } from "./autopilotMediaTransport.ts";
import { AutopilotPageLifecycle } from "./autopilotPageLifecycle.ts";
import { playAutopilotOnNextGesture } from "./autopilotGesturePlayback.ts";
import type {
  AutopilotAck,
  AutopilotOptionalAckFact,
  NextCommandProjection,
} from "./autopilotProtocol.ts";
import {
  BrowserAutopilotSpeechExecutor,
  type AutopilotSpeechBrowserPorts,
} from "./autopilotSpeechExecutor.ts";
import { AutopilotVoiceSettingsCache } from "./autopilotVoiceSettings.ts";

type TtsCommand = Extract<NextCommandProjection, { kind: "tts" }>;

function question(): TtsCommand {
  return {
    schema_version: 1,
    command_key: "cmd-speech-runtime-0001",
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

class AudioDouble {
  src = "";
  duration = 1.25;
  currentTime = 0;
  paused = true;
  onplaying: (() => void) | null = null;
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  pauseCalls = 0;
  playCalls = 0;
  playResult: Promise<void> = Promise.resolve();

  play(): Promise<void> {
    this.playCalls += 1;
    this.paused = false;
    return this.playResult;
  }

  pause(): void {
    this.pauseCalls += 1;
    this.paused = true;
  }

  playing(): void { this.onplaying?.(); }
  ended(): void { this.onended?.(); }
  error(): void { this.onerror?.(); }
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

async function flush(): Promise<void> {
  for (let turn = 0; turn < 6; turn += 1) await Promise.resolve();
}

function observe<T>(promise: Promise<T>): { settled: boolean; value?: T; error?: unknown } {
  const result: { settled: boolean; value?: T; error?: unknown } = { settled: false };
  void promise.then(
    (value) => { result.settled = true; result.value = value; },
    (error) => { result.settled = true; result.error = error; },
  );
  return result;
}

function harness(options: {
  playResult?: Promise<void>;
  fetch?: Promise<Blob | null>;
  startBargeIn?: AutopilotSpeechBrowserPorts["startBargeIn"];
} = {}) {
  const audio = new AudioDouble();
  if (options.playResult) audio.playResult = options.playResult;
  let now = 100;
  const created: string[] = [];
  const revoked: string[] = [];
  let stopped = 0;
  let observedSignal: AbortSignal | null = null;
  let abortCount = 0;
  const ports: AutopilotSpeechBrowserPorts = {
    enabled: () => true,
    stopSpeaking: () => { stopped += 1; },
    fetchTts: (_sessionId, _command, signal) => {
      observedSignal = signal;
      signal.addEventListener("abort", () => { abortCount += 1; }, { once: true });
      return options.fetch ?? Promise.resolve(new Blob(["tts"], { type: "audio/wav" }));
    },
    createAudio: () => audio as unknown as HTMLAudioElement,
    createObjectUrl: () => {
      const url = `blob:test-${created.length + 1}`;
      created.push(url);
      return url;
    },
    revokeObjectUrl: (url) => { revoked.push(url); },
    now: () => now,
    startBargeIn: options.startBargeIn,
  };
  const executor = new BrowserAutopilotSpeechExecutor("S-SPEECH", ports);
  return {
    audio,
    created,
    revoked,
    stopped: () => stopped,
    abortCount: () => abortCount,
    abortReason: () => observedSignal?.reason,
    start: () => executor.start(question()),
    setNow: (value: number) => { now = value; },
  };
}

for (const stage of ["downloading", "playing", "start_ack_pending"] as const) {
  for (const trigger of ["visibilitychange", "pagehide", "freeze"] as const) {
    test(`页面 ${trigger} 在 TTS ${stage} 时同步停播，等待旧请求收敛且不伪造播完`, async () => {
      const download = deferred<Blob | null>();
      const startedAck = deferred<void>();
      const run = harness(stage === "downloading" ? { fetch: download.promise } : {});
      const acks: string[] = [];
      let command = question();
      let recordStarts = 0;
      const controller = new PatientAutopilotController({
        sessionId: "S-SPEECH-LIFECYCLE",
        speech: { start: () => run.start() },
        recording: { start: () => { recordStarts += 1; throw new Error("不应开麦"); } },
        transport: {
          next: async () => command,
          ack: async (_sessionId, _commandKey, ack) => {
            acks.push(ack.ack_type);
            if (ack.ack_type === "tts_started") {
              if (stage === "start_ack_pending") await startedAck.promise;
              command = { ...command, state: "started", command_revision: 1 };
            }
            return { accepted: true };
          },
        },
        idempotencyKey: (_command, type, seq) => `speech-lifecycle:${type}:${seq}:0001`,
      });
      let hidden = false;
      const handlers = new Map<string, () => void>();
      let shutdown: Promise<void> | undefined;
      const lifecycle = new AutopilotPageLifecycle({
        ownerGeneration: 1,
        host: {
          isHidden: () => hidden,
          supportsFreeze: () => true,
          addEventListener: (type, handler) => { handlers.set(type, handler); },
          removeEventListener: (type) => { handlers.delete(type); },
          scheduleMicrotask: queueMicrotask,
        },
        shutdown: () => { shutdown = controller.interruptRecordingForLifecycleAndWait(); },
      });
      lifecycle.install();
      const running = controller.pollOnce();
      await flush();
      if (stage !== "downloading") {
        run.audio.playing();
        for (let index = 0; index < 8; index += 1) await flush();
        assert.equal(run.audio.paused, false);
        assert.deepEqual(acks, ["tts_started"]);
      }
      hidden = true;
      handlers.get(trigger)?.();
      // No Promise turn is allowed before the physical pause and abort.
      assert.equal(run.audio.paused, true);
      assert.equal(run.audio.pauseCalls, 1);
      assert.equal(run.abortCount(), 1);
      assert.equal(controller.interruptRecordingForLifecycleAndWait(), shutdown);
      let drained = false;
      void shutdown?.then(() => { drained = true; });
      await flush();
      if (stage === "start_ack_pending") {
        assert.equal(drained, false, "旧 ACK 未返回前不能释放执行器所有权");
        startedAck.resolve();
      }
      await Promise.all([running, shutdown]);
      download.resolve(new Blob(["late-tts"], { type: "audio/wav" }));
      await flush();
      run.audio.ended();
      await controller.pollOnce();
      assert.equal(controller.state.phase, "paused");
      assert.equal(recordStarts, 0);
      assert.equal(run.audio.playCalls, stage === "downloading" ? 0 : 1);
      assert.deepEqual(acks, stage === "downloading" ? [] : ["tts_started"]);
      assert.equal(lifecycle.aborted, true);
      lifecycle.uninstall();
    });
  }
}

test("production playback settles only on playing then ended, and ended is exact-once", async () => {
  const run = harness();
  const playback = run.start();
  const started = observe(playback.started);
  const ended = observe(playback.ended);
  const closed = observe(playback.closed);
  await flush();

  assert.equal(run.audio.playCalls, 1);
  assert.equal(started.settled, false);
  assert.equal(ended.settled, false);
  assert.equal(closed.settled, false);

  run.audio.playing();
  await flush();
  assert.equal(started.settled, true);
  assert.deepEqual(started.value, { media_duration_ms: 1_250 });
  assert.equal(ended.settled, false);
  assert.equal(closed.settled, false);

  run.setNow(1_000);
  run.audio.ended();
  run.audio.ended();
  await flush();
  assert.deepEqual(ended.value, { media_duration_ms: 900 });
  assert.equal(closed.settled, true);
  assert.deepEqual(run.created, ["blob:test-1"]);
  assert.deepEqual(run.revoked, ["blob:test-1"]);
  assert.equal(run.stopped(), 1);
});

for (const failureAt of ["started_ack", "started_refresh"] as const) {
  test(`起播后 ${failureAt} 失败先停声音再等待收口，不等浏览器补发 ended`, async () => {
    const run = harness();
    const acks: string[] = [];
    let reads = 0;
    const controller = new PatientAutopilotController({
      sessionId: "S-SPEECH-TRANSPORT-FAILURE",
      speech: { start: () => run.start() },
      recording: { start: () => { throw new Error("失败后不得开麦"); } },
      transport: {
        next: async () => {
          reads += 1;
          if (reads > 1) throw new Error("started revision rejected");
          return question();
        },
        ack: async (_sessionId, _commandKey, ack) => {
          acks.push(ack.ack_type);
          if (failureAt === "started_ack") throw new Error("started ACK rejected");
          return { accepted: true };
        },
      },
      idempotencyKey: (_command, type, seq) => `speech-failure:${type}:${seq}:0001`,
    });
    const outcome = observe(controller.pollOnce());
    try {
      await flush();
      run.audio.playing();
      for (let turn = 0; turn < 5; turn += 1) await flush();
      assert.equal(run.audio.paused, true);
      assert.equal(run.audio.pauseCalls, 1);
      assert.equal(outcome.settled, true, "transport failure must not wait forever for native ended");
      assert.equal(outcome.value?.phase, "paused");
      assert.deepEqual(acks, ["tts_started"]);
      assert.deepEqual(run.revoked, ["blob:test-1"]);
    } finally {
      await controller.stopAndWait();
    }
  });
}

test("现在回答先物理停播,只给 interrupted 事实;自然结束先赢时不能再打断", async () => {
  const run = harness();
  const playback = run.start();
  await flush();
  assert.equal(playback.answerNow?.(), false, "还未真实开播不能打断");
  run.audio.playing();
  await playback.started;
  run.setNow(800);
  run.audio.currentTime = 0.7;
  assert.equal(playback.answerNow?.(), true);
  assert.equal(run.audio.paused, true);
  await playback.closed;
  assert.deepEqual(await playback.ended, { interrupted: true, media_duration_ms: 700 });
  run.audio.ended();
  assert.equal(playback.answerNow?.(), false);
  assert.equal(run.audio.pauseCalls, 1);

  const natural = harness();
  const completed = natural.start();
  await flush();
  natural.audio.playing();
  natural.audio.ended();
  assert.equal(completed.answerNow?.(), false);
  assert.equal((await completed.ended).interrupted, undefined);
});

test("iPad compatibility leaves natural playback pending and manual answer-now available", async () => {
  for (const manual of [false, true]) {
    let monitorClosed: Promise<void> | undefined;
    const run = harness({ startBargeIn: (input) => {
      const blocked = () => { throw new Error("incompatible device must not open capture"); };
      const monitor = startBargeInMonitor(input, {
        ...browserBargeInPorts,
        supportsConcurrentCapture: () => false,
        permissionGranted: blocked, authorize: blocked, acquireLease: blocked,
        getUserMedia: blocked, createContext: blocked, foreground: () => true,
      });
      monitorClosed = monitor.closed;
      return monitor;
    }});
    const playback = run.start();
    await flush();
    run.audio.playing();
    await playback.started;
    playback.armBargeIn?.({ ...question(), state: "started", command_revision: 1 });
    assert.ok(monitorClosed);
    await monitorClosed;
    const ended = observe(playback.ended);
    await flush();
    assert.equal(ended.settled, false, "disabling detection must not finish the question");
    assert.equal(run.audio.paused, false);
    assert.equal(run.audio.pauseCalls, 0);
    if (manual) assert.equal(playback.answerNow?.(), true);
    else run.audio.ended();
    await playback.closed;
    assert.equal((await playback.ended).interrupted, manual ? true : undefined);
    assert.equal(playback.answerNow?.(), false);
  }
});

test("ended before playing is ignored and cannot fabricate successful playback", async () => {
  const run = harness();
  const playback = run.start();
  const started = observe(playback.started);
  const ended = observe(playback.ended);
  const closed = observe(playback.closed);
  await flush();

  run.audio.ended();
  await flush();
  assert.equal(started.settled, false);
  assert.equal(ended.settled, false);
  assert.equal(closed.settled, false);
  assert.deepEqual(run.revoked, []);

  run.audio.playing();
  run.setNow(250);
  run.audio.ended();
  await Promise.all([playback.started, playback.ended, playback.closed]);
  assert.deepEqual(run.revoked, ["blob:test-1"]);
});

test("audio error rejects media facts, closes, pauses, and revokes the URL once", async () => {
  const run = harness();
  const playback = run.start();
  await flush();
  run.audio.error();

  await Promise.all([
    assert.rejects(playback.started, /TTS 音频解码或播放失败/),
    assert.rejects(playback.ended, /TTS 音频解码或播放失败/),
    playback.closed,
  ]);
  run.audio.error();
  assert.equal(run.audio.pauseCalls, 1);
  assert.deepEqual(run.revoked, ["blob:test-1"]);
});

test("play rejection closes, pauses, and revokes the created URL", async () => {
  const run = harness({ playResult: Promise.reject(new Error("autoplay rejected")) });
  const playback = run.start();
  const playRejected = (error: unknown) => error instanceof AutopilotMediaError
    && error.failureStage === "play_rejected"
    && String((error.cause as Error | undefined)?.message).includes("autoplay rejected");
  await Promise.all([
    assert.rejects(playback.started, playRejected),
    assert.rejects(playback.ended, playRejected),
    playback.closed,
  ]);

  assert.equal(run.audio.pauseCalls, 1);
  assert.deepEqual(run.revoked, ["blob:test-1"]);
});

test("cancel aborts an initialized playback and revokes the URL exactly once", async () => {
  const run = harness();
  const playback = run.start();
  await flush();
  playback.cancel();
  playback.cancel();

  await Promise.all([
    assert.rejects(playback.started, (error: unknown) =>
      error instanceof DOMException && error.name === "AbortError"),
    assert.rejects(playback.ended, (error: unknown) =>
      error instanceof DOMException && error.name === "AbortError"),
    playback.closed,
  ]);
  assert.equal(run.audio.pauseCalls, 1);
  assert.deepEqual(run.revoked, ["blob:test-1"]);
  assert.equal(run.abortCount(), 1);
  assert.ok(run.abortReason() instanceof DOMException);
  assert.equal((run.abortReason() as DOMException).name, "AbortError");
});

test("cancel while fetch is pending prevents URL creation when bytes arrive late", async () => {
  const bytes = deferred<Blob | null>();
  const run = harness({ fetch: bytes.promise });
  const playback = run.start();
  playback.started.catch(() => {});
  playback.ended.catch(() => {});
  playback.cancel();
  bytes.resolve(new Blob(["late"], { type: "audio/wav" }));
  await playback.closed;
  await flush();

  assert.deepEqual(run.created, []);
  assert.deepEqual(run.revoked, []);
  assert.equal(run.audio.playCalls, 0);
});

// ── 浏览器要手势才肯放声(2026-09-04 生产三台设备:第一句能放,答完之后的反馈句
//    play() 一律 NotAllowedError → 整场被当设备故障安全暂停)──────────────────
class GestureAudioDouble extends AudioDouble {
  playResults: Promise<void>[] = [];
  override play(): Promise<void> {
    this.playCalls += 1;
    this.paused = false;
    return this.playResults.shift() ?? Promise.resolve();
  }
}

function notAllowed(): DOMException {
  return new DOMException("play() failed because the user didn't interact", "NotAllowedError");
}

function gestureHarness(playResults: Promise<void>[]) {
  const audio = new GestureAudioDouble();
  audio.playResults = playResults;
  const announced: boolean[] = [];
  const gestureHost = new EventTarget();
  let waiting = false;
  const revoked: string[] = [];
  const ports: AutopilotSpeechBrowserPorts = {
    enabled: () => true,
    stopSpeaking: () => {},
    fetchTts: () => Promise.resolve(new Blob(["tts"], { type: "audio/wav" })),
    createAudio: () => audio as unknown as HTMLAudioElement,
    createObjectUrl: () => "blob:test-1",
    revokeObjectUrl: (url) => { revoked.push(url); },
    now: () => 100,
    playOnNextGesture: (element, signal) => {
      waiting = true;
      const retried = playAutopilotOnNextGesture(element, signal, gestureHost);
      void retried.then(() => { waiting = false; }, () => { waiting = false; });
      return retried;
    },
    announceGestureNeeded: (needed) => { announced.push(needed); },
  };
  const executor = new BrowserAutopilotSpeechExecutor("S-SPEECH", ports);
  return {
    audio, announced, revoked,
    start: () => executor.start(question()),
    tap: (type: "pointerdown" | "click" = "pointerdown") => { gestureHost.dispatchEvent(new Event(type)); },
    waiting: () => waiting,
  };
}

test("NotAllowedError 不算设备故障:亮出「点一下」,在那一下里重放,之后照常起播/播完", async () => {
  const run = gestureHarness([Promise.reject(notAllowed())]);
  const playback = run.start();
  const started = observe(playback.started);
  await flush();
  assert.equal(run.audio.playCalls, 1);
  assert.equal(started.settled, false, "被拒不能直接判失败");
  assert.deepEqual(run.announced, [true]);
  assert.equal(run.waiting(), true);

  run.tap();
  await flush();
  assert.equal(run.audio.playCalls, 2, "手势里同步重放一次");
  assert.deepEqual(run.announced, [true, false]);
  run.audio.playing();
  await flush();
  assert.equal(started.settled, true);
  assert.equal(started.error, undefined);
  run.audio.ended();
  await playback.ended;
  await playback.closed;
});

for (const firstGesture of ["click", "pointerdown"] as const) {
  test(`手势重试支持 ${firstGesture}，保留真实音源和回调且不被后续 click 重放两遍`, async () => {
    const run = gestureHarness([Promise.reject(notAllowed())]);
    const playback = run.start();
    observe(playback.started);
    observe(playback.ended);
    try {
      await flush();
      const src = run.audio.src;
      const handlers = [run.audio.onplaying, run.audio.onended, run.audio.onerror];
      run.tap(firstGesture);
      assert.equal(run.audio.playCalls, 2, "keyboard/assistive click must retry synchronously");
      run.tap("click");
      run.tap("pointerdown");
      assert.equal(run.audio.playCalls, 2, "pointerdown and its subsequent click represent one retry");
      assert.equal(run.audio.src, src);
      assert.deepEqual([run.audio.onplaying, run.audio.onended, run.audio.onerror], handlers);
      await flush();
      assert.deepEqual(run.announced, [true, false]);
      run.audio.playing();
      run.audio.ended();
      await playback.ended;
    } finally {
      playback.cancel();
      await playback.closed;
    }
  });
}

for (const lateResult of ["resolve", "reject"] as const) {
  test(`点击后原生 play 挂起仍可取消；迟到 ${lateResult} 不再变更手势提示`, async () => {
    const nativePlay = deferred<void>();
    const run = gestureHarness([Promise.reject(notAllowed()), nativePlay.promise]);
    const playback = run.start();
    const started = observe(playback.started);
    const ended = observe(playback.ended);
    await flush();
    run.tap();
    assert.equal(run.audio.playCalls, 2, "retry must begin synchronously inside the gesture");
    await flush();
    assert.deepEqual(run.announced, [true]);
    playback.cancel();
    await playback.closed;
    await flush();
    try {
      assert.deepEqual(run.announced, [true, false], "cancel must clear the gesture overlay without waiting for play");
      assert.equal(started.settled, true);
      assert.equal(ended.settled, true);
      assert.equal(run.audio.paused, true);
      run.tap();
      run.tap("click");
      assert.equal(run.audio.playCalls, 2, "cancelled retry must not react to another tap");
    } finally {
      if (lateResult === "resolve") nativePlay.resolve();
      else nativePlay.reject(new Error("late native rejection"));
      await flush();
    }
    assert.deepEqual(run.announced, [true, false]);
  });
}

test("等待点屏时解码失败必须撤销手势重放，后续触碰不能复活失败话术", async () => {
  const run = gestureHarness([Promise.reject(notAllowed())]);
  const playback = run.start();
  const started = observe(playback.started);
  const ended = observe(playback.ended);
  await flush();
  assert.deepEqual(run.announced, [true]);
  run.audio.error();
  await playback.closed;
  await flush();
  try {
    assert.deepEqual(run.announced, [true, false]);
    assert.equal(started.settled, true);
    assert.equal(ended.settled, true);
    assert.equal((started.error as AutopilotMediaError).failureStage, "media_decode_error");
    run.tap();
    run.tap("click");
    assert.equal(run.audio.playCalls, 1);
    assert.equal(run.audio.paused, true);
  } finally {
    run.tap();
    await flush();
  }
});

test("点屏后仍被拒才是 play_rejected", async () => {
  const run = gestureHarness([Promise.reject(notAllowed()), Promise.reject(notAllowed())]);
  const playback = run.start();
  await flush();
  run.tap();
  const playRejected = (error: unknown) => error instanceof AutopilotMediaError
    && error.failureStage === "play_rejected"
    && /点屏后仍被拒/.test(error.message);
  await Promise.all([
    assert.rejects(playback.started, playRejected),
    assert.rejects(playback.ended, playRejected),
    playback.closed,
  ]);
  assert.deepEqual(run.announced, [true, false]);
  assert.deepEqual(run.revoked, ["blob:test-1"]);
});

test("等手势期间被取消(控制器 15 秒起播期限/换命令):AbortError 收口,覆层收走,不伪造 play_rejected", async () => {
  const run = gestureHarness([Promise.reject(notAllowed())]);
  const playback = run.start();
  playback.started.catch(() => {});
  playback.ended.catch(() => {});
  await flush();
  assert.equal(run.waiting(), true);
  playback.cancel();
  await playback.closed;
  await flush();
  await assert.rejects(playback.started, (error: unknown) =>
    error instanceof DOMException && error.name === "AbortError");
  assert.deepEqual(run.announced, [true, false]);
  assert.equal(run.audio.playCalls, 1);
});

test("不是手势问题的 play() 拒绝(NotSupportedError 等)仍直接 play_rejected,不等手势", async () => {
  const run = gestureHarness([Promise.reject(new DOMException("no source", "NotSupportedError"))]);
  const playback = run.start();
  await Promise.all([
    assert.rejects(playback.started, (error: unknown) => error instanceof AutopilotMediaError
      && error.failureStage === "play_rejected"),
    playback.ended.catch(() => {}),
    playback.closed,
  ]);
  assert.deepEqual(run.announced, []);
  assert.equal(run.waiting(), false);
});

// ── 停播检测两个事实:播放收口那一刻定值,回执组装那一刻才问服务端认不认 ──────────
function bargeInHarness(options: {
  purpose?: "question" | "cue" | "feedback";
  exposure?: BargeInExposure;
  accepts?: (name: AutopilotOptionalAckFact) => boolean;
} = {}) {
  const audio = new AudioDouble();
  const monitors: Array<{ closes: number; reads: number; onConfirmed(): boolean }> = [];
  const base = question();
  const command: TtsCommand = options.purpose === "feedback"
    ? { ...base, payload: { speech_key: "wk2.01.feedback", speech_text: "说得很好。", purpose: "feedback" } }
    : options.purpose === "cue"
      ? { ...base, prompt_level: 2, attempt_seq: 3,
        payload: { speech_key: "wk2.01.cue2", speech_text: "它是橙色的。", purpose: "cue" } }
      : base;
  const ports: AutopilotSpeechBrowserPorts = {
    enabled: () => true,
    stopSpeaking: () => {},
    fetchTts: () => Promise.resolve(new Blob(["tts"], { type: "audio/wav" })),
    createAudio: () => audio as unknown as HTMLAudioElement,
    createObjectUrl: () => "blob:test-1",
    revokeObjectUrl: () => {},
    now: () => 100,
    startBargeIn: (input) => {
      const monitor = { closes: 0, reads: 0, onConfirmed: input.onConfirmed };
      monitors.push(monitor);
      return {
        close: () => { monitor.closes += 1; },
        closed: Promise.resolve(),
        exposure: () => {
          monitor.reads += 1;
          return options.exposure ?? { armed: true, listenedMs: 900 };
        },
      };
    },
    acceptsAckFact: options.accepts ?? (() => true),
  };
  const executor = new BrowserAutopilotSpeechExecutor("S-SPEECH", ports);
  return {
    audio,
    monitors,
    command,
    start: () => executor.start(command),
    started: (): TtsCommand => ({ ...command, state: "started", command_revision: 1 }),
  };
}

async function playedOut(run: ReturnType<typeof bargeInHarness>, arm = true) {
  const playback = run.start();
  await flush();
  run.audio.playing();
  await playback.started;
  if (arm) playback.armBargeIn?.(run.started());
  run.audio.ended();
  await playback.closed;
  return playback;
}

test("停播检测事实取自收口时那一份定值;服务端两键都认才两键都报", async () => {
  const run = bargeInHarness();
  const playback = await playedOut(run);
  assert.deepEqual(playback.bargeInFacts?.(), { barge_in_armed: true, barge_in_listened_ms: 900 });
  assert.deepEqual(playback.bargeInFacts?.(), { barge_in_armed: true, barge_in_listened_ms: 900 });
  assert.equal(run.monitors.length, 1);
  assert.deepEqual([run.monitors[0].closes, run.monitors[0].reads], [1, 1], "关检测后只读一次");
});

test("服务端没声明认这两键:空对象,回执一个键都不带", async () => {
  const run = bargeInHarness({ accepts: () => false });
  const playback = await playedOut(run);
  assert.deepEqual(playback.bargeInFacts?.(), {});
});

test("只声明认 barge_in_armed:只报 armed,不报监听毫秒", async () => {
  const run = bargeInHarness({ accepts: (name) => name === "barge_in_armed" });
  const playback = await playedOut(run);
  assert.deepEqual(playback.bargeInFacts?.(), { barge_in_armed: true });
});

test("检测没走出校准、或这句播完时根本没武装:armed=false,不报监听毫秒", async () => {
  const unarmed = bargeInHarness({ exposure: { armed: false, listenedMs: null } });
  assert.deepEqual((await playedOut(unarmed)).bargeInFacts?.(), { barge_in_armed: false });
  const neverStarted = bargeInHarness();
  assert.deepEqual((await playedOut(neverStarted, false)).bargeInFacts?.(), { barge_in_armed: false });
  assert.equal(neverStarted.monitors.length, 0);
});

test("反馈句不是提问/提示:不开检测,也不报这两个事实", async () => {
  const run = bargeInHarness({ purpose: "feedback" });
  const playback = await playedOut(run);
  assert.equal(run.monitors.length, 0);
  assert.deepEqual(playback.bargeInFacts?.(), {});
});

test("线索被老人开口停播、或点「现在回答」:同样带检测事实", async () => {
  const voice = bargeInHarness({ purpose: "cue", exposure: { armed: true, listenedMs: 420 } });
  const stopped = voice.start();
  await flush();
  voice.audio.playing();
  await stopped.started;
  stopped.armBargeIn?.(voice.started());
  voice.audio.currentTime = 1.2;
  assert.equal(voice.monitors[0].onConfirmed(), true);
  await stopped.closed;
  assert.deepEqual(await stopped.ended,
    { interrupted: true, media_duration_ms: 1_200, interrupt_reason: "voice_activity" });
  assert.deepEqual(stopped.bargeInFacts?.(), { barge_in_armed: true, barge_in_listened_ms: 420 });

  const button = bargeInHarness({ exposure: { armed: false, listenedMs: null } });
  const answered = button.start();
  await flush();
  button.audio.playing();
  await answered.started;
  answered.armBargeIn?.(button.started());
  assert.equal(answered.answerNow?.(), true);
  await answered.closed;
  assert.deepEqual(answered.bargeInFacts?.(), { barge_in_armed: false });
});

test("服务端认不认在回执组装那一刻才问:开播时还没读到设置,收口后读到了照样报", async () => {
  let accepted = false;
  const run = bargeInHarness({ accepts: () => accepted });
  const playback = await playedOut(run);
  assert.deepEqual(playback.bargeInFacts?.(), {});
  accepted = true;
  assert.deepEqual(playback.bargeInFacts?.(), { barge_in_armed: true, barge_in_listened_ms: 900 });
});

const VOICE_CREDENTIAL: DeviceCredentialSelection = {
  source: "active",
  record: { capability: "x".repeat(43), sessionId: "S-SPEECH", expiresAt: "2026-07-19T12:00:00Z" },
  headers: { "X-Device-Capability": "x".repeat(43) },
};

for (const row of [
  {
    name: "四键(不认 ?ack_facts 的服务端,如回滚后的 af59c28):检测武装了也一个键都不发",
    body: { vad_enabled: true, vad_trailing_silence_ms: 3_000, vad_short_trailing_silence_ms: 4_500,
      barge_in_enabled: true },
    expected: {},
  },
  {
    name: "五键声明两个事实:tts_ended 带上检测事实",
    body: { vad_enabled: true, vad_trailing_silence_ms: 3_000, vad_short_trailing_silence_ms: 4_500,
      barge_in_enabled: true, ack_facts: ["barge_in_armed", "barge_in_listened_ms"] },
    expected: { barge_in_armed: true, barge_in_listened_ms: 900 },
  },
]) {
  test(`语音设置读面 → 缓存 → 执行器 → 回执:${row.name}`, async () => {
    const requested: string[] = [];
    const settings = new AutopilotVoiceSettingsCache("S-SPEECH", {
      fetch: (sessionId, signal) => fetchAutopilotVoiceSettings(sessionId, signal, {
        fetchImpl: async (input) => { requested.push(String(input)); return Response.json(row.body); },
        selectCredential: () => VOICE_CREDENTIAL,
        handleAuthorizationFailure: () => false,
        csrf: () => ({}),
        nextCommand: async () => null,
      }),
      now: () => 0,
    });
    settings.refreshInBackground();
    for (let turn = 0; turn < 4; turn += 1) await flush();
    assert.deepEqual(requested, ["/sessions/S-SPEECH/autopilot/voice-settings?ack_facts=1"]);

    const audio = new AudioDouble();
    const executor = new BrowserAutopilotSpeechExecutor("S-SPEECH", {
      enabled: () => true,
      stopSpeaking: () => {},
      fetchTts: () => Promise.resolve(new Blob(["tts"], { type: "audio/wav" })),
      createAudio: () => audio as unknown as HTMLAudioElement,
      createObjectUrl: () => "blob:test-1",
      revokeObjectUrl: () => {},
      now: () => 100,
      startBargeIn: () => ({
        close: () => {}, closed: Promise.resolve(),
        exposure: () => ({ armed: true, listenedMs: 900 }),
      }),
      acceptsAckFact: (name) => settings.acceptsAckFact(name),
    });
    const acks: AutopilotAck[] = [];
    let command: NextCommandProjection = question();
    const controller = new PatientAutopilotController({
      sessionId: "S-SPEECH",
      speech: executor,
      recording: { start: () => { throw new Error("不应开麦"); } },
      transport: {
        next: async () => command,
        ack: async (_sessionId, _commandKey, ack) => {
          acks.push(ack);
          if (ack.ack_type === "tts_started") command = { ...command, state: "started", command_revision: 1 };
          return { accepted: true };
        },
      },
      idempotencyKey: (_command, type, seq) => `speech-barge-facts:${type}:${seq}:0001`,
    });
    const running = controller.pollOnce();
    await flush();
    audio.playing();
    for (let turn = 0; turn < 8; turn += 1) await flush();
    audio.ended();
    await running;
    const ended = acks.find((ack) => ack.ack_type === "tts_ended");
    assert.ok(ended, JSON.stringify(acks.map((ack) => ack.ack_type)));
    const { barge_in_armed: armed, barge_in_listened_ms: listened } = ended as Record<string, unknown>;
    assert.deepEqual(Object.fromEntries(Object.entries({
      barge_in_armed: armed, barge_in_listened_ms: listened,
    }).filter(([, value]) => value !== undefined)), row.expected);
    if (Object.keys(row.expected).length === 0) {
      assert.equal(Object.hasOwn(ended, "barge_in_armed"), false);
      assert.equal(Object.hasOwn(ended, "barge_in_listened_ms"), false);
    }
    settings.dispose();
  });
}
