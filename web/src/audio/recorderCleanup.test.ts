import assert from "node:assert/strict";
import test from "node:test";
import { Recorder, releaseMediaStreamTracks } from "./recorder.ts";

type TrackDouble = { stopped: number; stop(): void };

function track(options: { throws?: boolean } = {}): TrackDouble {
  const row: TrackDouble = {
    stopped: 0,
    stop() {
      row.stopped += 1;
      if (options.throws) throw new Error("设备层已回收这条 track");
    },
  };
  return row;
}

function streamDouble(tracks: TrackDouble[], options: { getTracksThrows?: boolean } = {}) {
  return {
    getTracks() {
      if (options.getTracksThrows) throw new Error("MediaStream 已失效");
      return tracks;
    },
  } as unknown as MediaStream;
}

/** 装一套可控的 getUserMedia / MediaRecorder / performance 全局。 */
function installBrowserDoubles(options: {
  tracks: TrackDouble[];
  nowMs: number;
  getTracksThrows?: boolean;
}) {
  const stream = streamDouble(options.tracks, {
    getTracksThrows: options.getTracksThrows,
  });
  const recorders: Array<Record<string, unknown>> = [];

  class FakeMediaRecorder {
    state = "inactive";
    mimeType = "audio/webm";
    onstart: (() => void) | null = null;
    onstop: (() => void) | null = null;
    onerror: ((event: unknown) => void) | null = null;
    ondataavailable: ((event: unknown) => void) | null = null;

    constructor() { recorders.push(this as unknown as Record<string, unknown>); }

    static isTypeSupported(): boolean { return true; }

    start(): void {
      this.state = "recording";
      queueMicrotask(() => this.onstart?.());
    }

    stop(): void {
      this.state = "inactive";
      queueMicrotask(() => this.onstop?.());
    }
  }

  const previousNow = performance.now;
  performance.now = () => options.nowMs;
  Object.defineProperty(globalThis, "navigator", {
    value: { mediaDevices: { getUserMedia: async () => stream } },
    configurable: true,
    writable: true,
  });
  (globalThis as Record<string, unknown>).MediaRecorder = FakeMediaRecorder;
  (globalThis as Record<string, unknown>).Blob = class {
    size = 4;
    type = "audio/webm";
    constructor(_parts?: unknown, _options?: { type?: string }) {}
  };
  return {
    recorders,
    restore() { performance.now = previousNow; },
    setNow(value: number) { performance.now = () => value; },
  };
}

test("performance.now() 恰为 0 也是合法录音起点，不能被当成『未开始』", async (context) => {
  const tracks = [track()];
  const doubles = installBrowserDoubles({ tracks, nowMs: 0 });
  context.after(() => doubles.restore());

  const recorder = new Recorder();
  assert.equal(recorder.startedAtMs, null);          // 真的没开始时才是 null
  assert.equal(await recorder.start(), true);
  assert.equal(recorder.startedAtMs, 0);             // 0 是起点，不是哨兵

  doubles.setNow(3_500);
  const recording = await recorder.stop();
  assert.equal(recording.durationSeconds, 3.5);      // 从 0 起算，不是"没开始"
  assert.equal(recorder.startedAtMs, null);          // 收尾后重新变回未开始
});

test("stop() 拒绝没有真实起点的录音，绝不拿 0 造一个时长", async (context) => {
  const doubles = installBrowserDoubles({ tracks: [track()], nowMs: 1_000 });
  context.after(() => doubles.restore());

  const recorder = new Recorder();
  await assert.rejects(recorder.stop(), /未在录音/);

  // 白盒：直接把内部状态摆成"有 MediaRecorder、无起点"，钉住这道防御闸。
  const internals = recorder as unknown as { mr: unknown; startedAt: number | null };
  internals.mr = { state: "recording", mimeType: "audio/webm" };
  internals.startedAt = null;
  await assert.rejects(recorder.stop(), /录音缺少真实起点时刻/);
});

test("逐条 best-effort 关 track：一条抛错不拖累其余，getTracks 抛错也不外抛", () => {
  const good = track();
  const bad = track({ throws: true });
  const alsoGood = track();
  assert.doesNotThrow(() =>
    releaseMediaStreamTracks(streamDouble([bad, good, alsoGood])));
  assert.equal(bad.stopped, 1);
  assert.equal(good.stopped, 1);
  assert.equal(alsoGood.stopped, 1);

  assert.doesNotThrow(() =>
    releaseMediaStreamTracks(streamDouble([good], { getTracksThrows: true })));
  assert.doesNotThrow(() => releaseMediaStreamTracks(null));
});

test("track.stop 抛错时 discardActive 仍然清空全部字段", async (context) => {
  const tracks = [track({ throws: true }), track({ throws: true })];
  const doubles = installBrowserDoubles({ tracks, nowMs: 1_000 });
  context.after(() => doubles.restore());

  const recorder = new Recorder();
  await recorder.start();
  assert.equal(recorder.active, true);

  assert.doesNotThrow(() => recorder.discardActive());
  assert.equal(recorder.active, false);
  assert.equal(recorder.startedAtMs, null);
  assert.equal(recorder.mimeType, null);
  for (const row of tracks) assert.equal(row.stopped, 1);   // 每条都试过
});

test("getTracks 抛错时 dispose 不外抛，引用照样断干净", async (context) => {
  const doubles = installBrowserDoubles({
    tracks: [track()], nowMs: 1_000, getTracksThrows: true,
  });
  context.after(() => doubles.restore());

  const recorder = new Recorder();
  await recorder.start();
  recorder.discardActive();                       // 先离开 active，dispose 才会关流
  assert.doesNotThrow(() => recorder.dispose());
  assert.equal(recorder.mimeType, null);
  assert.equal(recorder.startedAtMs, null);
});
