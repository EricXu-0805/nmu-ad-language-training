import assert from "node:assert/strict";
import test from "node:test";
import { AutopilotMediaError } from "./autopilotMediaError.ts";
import { PatientAutopilotController } from "./autopilotController.ts";
import { AutopilotPageLifecycle } from "./autopilotPageLifecycle.ts";
import type { NextCommandProjection } from "./autopilotProtocol.ts";
import {
  BrowserAutopilotSpeechExecutor,
  type AutopilotSpeechBrowserPorts,
} from "./autopilotSpeechExecutor.ts";

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

function harness(options: { playResult?: Promise<void>; fetch?: Promise<Blob | null> } = {}) {
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
  let waiting: { fire(): void } | null = null;
  const revoked: string[] = [];
  const ports: AutopilotSpeechBrowserPorts = {
    enabled: () => true,
    stopSpeaking: () => {},
    fetchTts: () => Promise.resolve(new Blob(["tts"], { type: "audio/wav" })),
    createAudio: () => audio as unknown as HTMLAudioElement,
    createObjectUrl: () => "blob:test-1",
    revokeObjectUrl: (url) => { revoked.push(url); },
    now: () => 100,
    playOnNextGesture: (element, signal) => new Promise<void>((resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      waiting = { fire: () => resolve(element.play()) };
    }),
    announceGestureNeeded: (needed) => { announced.push(needed); },
  };
  const executor = new BrowserAutopilotSpeechExecutor("S-SPEECH", ports);
  return {
    audio, announced, revoked,
    start: () => executor.start(question()),
    tap: () => { waiting?.fire(); waiting = null; },
    waiting: () => waiting !== null,
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
