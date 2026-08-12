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
interface FakeRecorder {
  state: string;
  mimeType: string;
  starts: number;
  stops: number;
  onstart: (() => void) | null;
  onstop: (() => void) | null;
  onerror: ((event: unknown) => void) | null;
  ondataavailable: ((event: unknown) => void) | null;
  fireStart(): void;
  fireStop(): void;
}

function installBrowserDoubles(options: {
  tracks: TrackDouble[];
  nowMs: number;
  getTracksThrows?: boolean;
  /** 手动控制 onstart/onstop，用来钉住"同步下达指令"与"迟到事件"两类时序。 */
  manualEvents?: boolean;
  gateUserMedia?: boolean;
}) {
  const stream = streamDouble(options.tracks, {
    getTracksThrows: options.getTracksThrows,
  });
  const recorders: FakeRecorder[] = [];
  let userMediaCalls = 0;
  let releaseUserMedia: (() => void) | null = null;
  const userMediaGate = new Promise<void>((resolve) => { releaseUserMedia = resolve; });

  class FakeMediaRecorder {
    state = "inactive";
    mimeType = "audio/webm";
    starts = 0;
    stops = 0;
    onstart: (() => void) | null = null;
    onstop: (() => void) | null = null;
    onerror: ((event: unknown) => void) | null = null;
    ondataavailable: ((event: unknown) => void) | null = null;

    constructor() { recorders.push(this as unknown as FakeRecorder); }

    static isTypeSupported(): boolean { return true; }

    fireStart(): void { this.onstart?.(); }
    fireStop(): void { this.onstop?.(); }

    start(): void {
      this.starts += 1;
      this.state = "recording";
      if (!options.manualEvents) queueMicrotask(() => this.onstart?.());
    }

    stop(): void {
      this.stops += 1;
      this.state = "inactive";
      if (!options.manualEvents) queueMicrotask(() => this.onstop?.());
    }
  }

  const previousNow = performance.now;
  performance.now = () => options.nowMs;
  Object.defineProperty(globalThis, "navigator", {
    value: {
      mediaDevices: {
        getUserMedia: async () => {
          userMediaCalls += 1;
          if (options.gateUserMedia) await userMediaGate;
          return stream;
        },
      },
    },
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
    get userMediaCalls() { return userMediaCalls; },
    openUserMedia() { releaseUserMedia?.(); },
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

  // 白盒：直接把内部状态摆成"在录、有 MediaRecorder、无起点"，钉住这道防御闸。
  const internals = recorder as unknown as {
    mr: unknown; startedAt: number | null; stateValue: string;
  };
  internals.mr = { state: "recording", mimeType: "audio/webm" };
  internals.startedAt = null;
  internals.stateValue = "recording";
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

// ---------------- 两阶段状态机 ----------------

test("prepare() 拿到权限也绝不开录：start 调用数 0、无起点、无时长", async (context) => {
  const tracks = [track()];
  const doubles = installBrowserDoubles({ tracks, nowMs: 1_000, manualEvents: true });
  context.after(() => doubles.restore());

  const recorder = new Recorder();
  assert.equal(recorder.state, "idle");
  assert.equal(await recorder.prepare(), true);

  assert.equal(recorder.state, "prepared");
  assert.equal(recorder.active, false);
  assert.equal(recorder.startedAtMs, null);
  assert.equal(doubles.recorders.length, 1);
  assert.equal(doubles.recorders[0]?.starts, 0);      // 权限已到手，设备仍未开录
  await assert.rejects(recorder.stop(), /未在录音/);   // 也拿不出任何录音资产
});

test("startPrepared() 返回 Promise 之前已同步下达开录指令，起点绑真实 onstart", async (context) => {
  const doubles = installBrowserDoubles({
    tracks: [track()], nowMs: 500, manualEvents: true,
  });
  context.after(() => doubles.restore());

  const recorder = new Recorder();
  await recorder.prepare();
  const device = doubles.recorders[0] as FakeRecorder;

  const started = recorder.startPrepared();
  // 同一同步栈里已经调过 start()，中间没有任何 await。
  assert.equal(device.starts, 1);
  assert.equal(recorder.state, "starting");
  assert.equal(recorder.startedAtMs, null);          // onstart 还没来，就还没有起点

  doubles.setNow(900);
  device.fireStart();
  assert.equal(await started, true);
  assert.equal(recorder.state, "recording");
  assert.equal(recorder.startedAtMs, 900);           // 起点 = 真实事件那一刻
});

test("并行 prepare 单飞，只取一条 MediaStream", async (context) => {
  const doubles = installBrowserDoubles({
    tracks: [track()], nowMs: 1_000, manualEvents: true, gateUserMedia: true,
  });
  context.after(() => doubles.restore());

  const recorder = new Recorder();
  const first = recorder.prepare();
  const second = recorder.prepare();
  assert.equal(first, second);
  doubles.openUserMedia();
  assert.deepEqual([await first, await second], [true, true]);
  assert.equal(doubles.userMediaCalls, 1);
  assert.equal(doubles.recorders.length, 1);
});

test("prepared/starting 被丢弃：关掉全部 track，迟到的 onstart 不复活这条捕获", async (context) => {
  const tracks = [track(), track()];
  const doubles = installBrowserDoubles({ tracks, nowMs: 1_000, manualEvents: true });
  context.after(() => doubles.restore());

  const recorder = new Recorder();
  await recorder.prepare();
  const device = doubles.recorders[0] as FakeRecorder;
  const started = recorder.startPrepared();
  started.catch(() => {});

  recorder.discardActive();
  assert.equal(recorder.active, false);
  for (const row of tracks) assert.equal(row.stopped, 1);

  device.fireStart();                                // 设备晚了一步才回调
  assert.equal(recorder.active, false);
  assert.equal(recorder.startedAtMs, null);
});

test("成功停止已同步调用之后，dispose 只 join：不清 chunks、不 reject、不二次 stop", async (context) => {
  const doubles = installBrowserDoubles({
    tracks: [track()], nowMs: 1_000, manualEvents: true,
  });
  context.after(() => doubles.restore());

  const recorder = new Recorder();
  await recorder.prepare();
  const device = doubles.recorders[0] as FakeRecorder;
  const started = recorder.startPrepared();
  device.fireStart();
  await started;

  doubles.setNow(4_000);
  const stopping = recorder.stop();
  assert.equal(recorder.stopping, true);
  assert.equal(device.stops, 1);

  // pagehide / unmount / dispose 都落在这个窗口里：只能等，不能改写。
  assert.doesNotThrow(() => recorder.discardActive());
  assert.doesNotThrow(() => recorder.dispose());
  assert.equal(device.stops, 1);                     // 没有第二次 stop
  assert.equal(device.onstop !== null, true);        // handler 还在

  device.fireStop();
  const recording = await stopping;                  // 原来那条 Promise 正常收口
  assert.equal(recording.durationSeconds, 3);
  assert.equal(recorder.state, "disposed");          // dispose 诉求在收口后才完成
});

test("老人主动暂停可强制丢弃 stopping 窄窗：同步关 track，零 Recording 结果", async (context) => {
  const tracks = [track(), track()];
  const doubles = installBrowserDoubles({
    tracks, nowMs: 1_000, manualEvents: true,
  });
  context.after(() => doubles.restore());

  const recorder = new Recorder();
  await recorder.prepare();
  const device = doubles.recorders[0] as FakeRecorder;
  const started = recorder.startPrepared();
  device.fireStart();
  await started;

  doubles.setNow(2_000);
  const stopping = recorder.stop();
  recorder.discardActive({ force: true });
  // 与 discardActive 之间没有 await：物理 track 已全部关闭。
  assert.deepEqual(tracks.map((row) => row.stopped), [1, 1]);
  assert.equal(recorder.state, "idle");
  await assert.rejects(stopping, /强制取消/);
  device.fireStop();
  assert.equal(recorder.startedAtMs, null);
  assert.equal(recorder.mimeType, null);
});

test("legacy start() 仍是一次调用完成开麦，外部语义不变", async (context) => {
  const doubles = installBrowserDoubles({ tracks: [track()], nowMs: 0 });
  context.after(() => doubles.restore());

  const recorder = new Recorder();
  assert.equal(await recorder.start(), true);
  assert.equal(recorder.active, true);
  assert.equal(recorder.state, "recording");
  assert.equal(recorder.startedAtMs, 0);
  assert.equal(doubles.recorders[0]?.starts, 1);
  assert.equal(await recorder.start(), true);        // 已在录：幂等返回，不再开一次
  assert.equal(doubles.recorders.length, 1);
});

test("legacy start() 期间 dispose 抢在流之前：迟到的流被物理关掉，零次开录、返回 false", async (context) => {
  const tracks = [track(), track()];
  const doubles = installBrowserDoubles({
    tracks, nowMs: 1_000, manualEvents: true, gateUserMedia: true,
  });
  context.after(() => doubles.restore());

  const recorder = new Recorder();
  const starting = recorder.start();          // getUserMedia 还悬在权限弹窗上
  recorder.dispose();                         // 流还没到，卸载先赢
  assert.equal(recorder.state, "disposed");

  doubles.openUserMedia();                    // 权限这才回来
  assert.equal(await starting, false);

  for (const row of tracks) assert.equal(row.stopped, 1);   // 没留下一条热麦
  assert.equal(doubles.recorders.length, 0);                // 连 MediaRecorder 都没造
  assert.equal(
    doubles.recorders.reduce((total, row) => total + row.starts, 0), 0);
  assert.equal(recorder.state, "disposed");
  assert.equal(recorder.active, false);
  assert.equal(recorder.startedAtMs, null);
  assert.equal(recorder.mimeType, null);
});
