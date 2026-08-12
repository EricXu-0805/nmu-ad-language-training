import assert from "node:assert/strict";
import test from "node:test";
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
  await Promise.all([
    assert.rejects(playback.started, /autoplay rejected/),
    assert.rejects(playback.ended, /autoplay rejected/),
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
