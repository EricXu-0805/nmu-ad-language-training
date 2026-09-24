import assert from "node:assert/strict";
import test from "node:test";

import {
  VAD_ANALYSER_FFT_SIZE,
  VAD_SAMPLE_INTERVAL_MS,
  observeTrailingSilence,
  type VoiceActivitySamplerPorts,
} from "./autopilotBrowserVoiceActivity.ts";
import { VAD_TRAILING_SILENCE_MS } from "./autopilotVoiceActivity.ts";

class FakeSource {
  disconnects = 0;
  connectedTo: unknown = null;
  readonly stream: unknown;
  constructor(stream: unknown) { this.stream = stream; }
  connect(node: unknown): void { this.connectedTo = node; }
  disconnect(): void { this.disconnects += 1; }
}

class FakeAnalyser {
  fftSize = 0;
  reads = 0;
  /** 下一帧填进缓冲的常量幅度(RMS 就等于它);null 表示读数抛错。 */
  level: number | null = 0;
  getFloatTimeDomainData(buffer: Float32Array): void {
    this.reads += 1;
    if (this.level === null) throw new Error("analyser 读数失败");
    buffer.fill(this.level);
  }
}

class FakeAudioContext {
  state: "running" | "suspended" | "closed";
  closes = 0;
  resumes = 0;
  sources: FakeSource[] = [];
  readonly analyser = new FakeAnalyser();
  constructor(state: "running" | "suspended" = "running") { this.state = state; }
  createMediaStreamSource(stream: unknown): FakeSource {
    const source = new FakeSource(stream);
    this.sources.push(source);
    return source;
  }
  createAnalyser(): FakeAnalyser { return this.analyser; }
  close(): Promise<void> { this.closes += 1; this.state = "closed"; return Promise.resolve(); }
  resume(): Promise<void> { this.resumes += 1; return Promise.resolve(); }
}

/** 可控定时器 + 可控时钟;每 tick 推进一个采样间隔并跑一次采样回调。 */
function samplerHarness(options: { context?: FakeAudioContext | (() => never) } = {}) {
  const context = typeof options.context === "function"
    ? null : (options.context ?? new FakeAudioContext());
  let nowMs = 10_000;
  let interval: { callback: () => void; intervalMs: number } | null = null;
  let intervalHandle = 0;
  const cleared: number[] = [];
  const ports: VoiceActivitySamplerPorts = {
    createContext: () => {
      if (typeof options.context === "function") options.context();
      return context as unknown as AudioContext;
    },
    setInterval: (callback, intervalMs) => {
      interval = { callback, intervalMs };
      intervalHandle += 1;
      return intervalHandle;
    },
    clearInterval: (handle) => { cleared.push(handle); interval = null; },
    now: () => nowMs,
  };
  return {
    ports,
    context,
    cleared,
    get pending() { return interval !== null; },
    /** 喂 durationMs 的某个幅度:一帧一帧跑采样回调。 */
    feed(durationMs: number, level: number | null) {
      if (context) context.analyser.level = level;
      for (let elapsed = 0; elapsed < durationMs; elapsed += VAD_SAMPLE_INTERVAL_MS) {
        nowMs += VAD_SAMPLE_INTERVAL_MS;
        const current = interval as { callback: () => void } | null;
        if (current === null) return;
        current.callback();
      }
    },
  };
}

const STREAM = { getTracks: () => [] } as unknown as MediaStream;

test("说完了:回调恰好一次,而且回调前 AudioContext 已经 close、源节点断开、定时器撤掉", async () => {
  const harness = samplerHarness();
  const context = harness.context as FakeAudioContext;
  let callbacks = 0;
  let closedWhenCalled = -1;
  const release = observeTrailingSilence(STREAM, () => {
    callbacks += 1;
    closedWhenCalled = context.closes;
  }, harness.ports);

  // 只旁听 Recorder 交出的那条流,fftSize 按常量设,源节点接到 analyser 上。
  assert.equal(context.sources.length, 1);
  assert.equal(context.sources[0].stream, STREAM);
  assert.equal(context.analyser.fftSize, VAD_ANALYSER_FFT_SIZE);
  assert.equal(context.sources[0].connectedTo, context.analyser);
  assert.equal(harness.pending, true);

  harness.feed(1_000, 0.002);
  harness.feed(2_000, 0.2);
  assert.equal(callbacks, 0);
  harness.feed(VAD_TRAILING_SILENCE_MS + VAD_SAMPLE_INTERVAL_MS * 2, 0.002);
  assert.equal(callbacks, 1);
  assert.equal(closedWhenCalled, 1);
  assert.equal(context.closes, 1);
  assert.equal(context.sources[0].disconnects, 1);
  assert.equal(harness.pending, false);
  assert.deepEqual(harness.cleared, [1]);

  // 之后再拆是幂等的:不二次 close。
  release();
  release();
  assert.equal(context.closes, 1);
  assert.equal(callbacks, 1);
});

test("判定之前拆除:定时器撤掉、AudioContext close 一次,之后一次回调都没有", () => {
  const harness = samplerHarness();
  const context = harness.context as FakeAudioContext;
  let callbacks = 0;
  const release = observeTrailingSilence(STREAM, () => { callbacks += 1; }, harness.ports);
  harness.feed(1_000, 0.002);
  harness.feed(2_000, 0.2);
  harness.feed(2_000, 0.002);   // 静默还没满 3 s

  release();
  assert.equal(harness.pending, false);
  assert.equal(context.closes, 1);
  assert.equal(context.sources[0].disconnects, 1);
  // 采样回调已经没了:再"喂"什么都不会跑,更不会回调。
  const readsBefore = context.analyser.reads;
  harness.feed(10_000, 0.002);
  assert.equal(context.analyser.reads, readsBefore);
  assert.equal(callbacks, 0);
  release();
  assert.equal(context.closes, 1);
});

test("没有 AudioContext(构造抛错):静默无为,返回的拆除函数可调、零回调、不外抛", () => {
  const harness = samplerHarness({
    context: () => { throw new Error("AudioContext is not defined"); },
  });
  let callbacks = 0;
  let release: (() => void) | null = null;
  assert.doesNotThrow(() => {
    release = observeTrailingSilence(STREAM, () => { callbacks += 1; }, harness.ports);
  });
  assert.equal(harness.pending, false);
  assert.doesNotThrow(() => (release as unknown as () => void)());
  assert.equal(callbacks, 0);
});

test("上下文被自动播放策略挂在 suspended:试着 resume;读到的全是 0 就永远不回调,拆除照样 close", () => {
  const context = new FakeAudioContext("suspended");
  const harness = samplerHarness({ context });
  let callbacks = 0;
  const release = observeTrailingSilence(STREAM, () => { callbacks += 1; }, harness.ports);
  assert.equal(context.resumes, 1);
  harness.feed(30_000, 0);
  assert.equal(callbacks, 0);
  assert.equal(harness.pending, true);
  release();
  assert.equal(context.closes, 1);
  assert.equal(harness.pending, false);
});

test("说话后 AudioContext 暂停时的零值不是静音证据,恢复后也不能沿用暂停前的倒计时", () => {
  const harness = samplerHarness();
  const context = harness.context as FakeAudioContext;
  let callbacks = 0;
  const release = observeTrailingSilence(STREAM, () => { callbacks += 1; }, harness.ports);
  harness.feed(1_000, 0.002);
  harness.feed(2_000, 0.2);
  harness.feed(2_500, 0.002);
  const readsBeforeSuspension = context.analyser.reads;
  context.state = "suspended";
  harness.feed(4_000, 0);
  assert.equal(callbacks, 0);
  assert.equal(context.analyser.reads, readsBeforeSuspension);

  context.state = "running";
  harness.feed(5_000, 0.002);
  assert.equal(callbacks, 0);
  harness.feed(2_000, 0.2);
  harness.feed(VAD_TRAILING_SILENCE_MS + VAD_SAMPLE_INTERVAL_MS * 2, 0.002);
  assert.equal(callbacks, 1);
  release();
});

test("analyser 读数抛错:采样器自拆(close 一次、定时器撤掉),零回调,错误不从定时器里冒出来", () => {
  const harness = samplerHarness();
  const context = harness.context as FakeAudioContext;
  let callbacks = 0;
  observeTrailingSilence(STREAM, () => { callbacks += 1; }, harness.ports);
  harness.feed(1_000, 0.002);
  harness.feed(2_000, 0.2);
  assert.doesNotThrow(() => harness.feed(VAD_SAMPLE_INTERVAL_MS, null));
  assert.equal(harness.pending, false);
  assert.equal(context.closes, 1);
  harness.feed(10_000, 0.002);
  assert.equal(callbacks, 0);
});

test("close() 本身拒绝或抛错也不影响拆除的其余步骤", () => {
  const context = new FakeAudioContext();
  context.close = () => { context.closes += 1; throw new Error("close 失败"); };
  const harness = samplerHarness({ context });
  const release = observeTrailingSilence(STREAM, () => {}, harness.ports);
  assert.doesNotThrow(() => release());
  assert.equal(context.closes, 1);
  assert.equal(context.sources[0].disconnects, 1);
  assert.equal(harness.pending, false);
});
