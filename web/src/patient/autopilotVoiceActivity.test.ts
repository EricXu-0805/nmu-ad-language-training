import assert from "node:assert/strict";
import test from "node:test";

import {
  VAD_ABSOLUTE_MIN_RMS,
  VAD_CALIBRATION_MS,
  VAD_MAX_FRAME_GAP_MS,
  VAD_SPEECH_ONSET_MS,
  VAD_TRAILING_SILENCE_MS,
  createVoiceActivityDetector,
  type VoiceActivityDetector,
  type VoiceActivityState,
} from "./autopilotVoiceActivity.ts";

const QUIET = 0.002;
const NOISY = 0.05;
const SPEECH = 0.2;

/** 一段声音:持续多久、RMS 多大。 */
type Segment = readonly [durationMs: number, rms: number];

interface Trace {
  /** 第一次判「说完」的帧时刻;没判过就是 null。 */
  stoppedAt: number | null;
  /** 每一帧之后的状态,按 atMs 索引。 */
  states: Map<number, VoiceActivityState>;
  lastAt: number;
}

/** 把几段声音按固定帧率喂进去;帧时刻从 fromMs + frameMs 开始、每帧 +frameMs。 */
function feed(
  detector: VoiceActivityDetector, segments: readonly Segment[], frameMs: number, fromMs = 0,
): Trace {
  const states = new Map<number, VoiceActivityState>();
  let stoppedAt: number | null = null;
  let atMs = fromMs;
  let boundary = fromMs;
  for (const [durationMs, rms] of segments) {
    boundary += durationMs;
    while (atMs + frameMs <= boundary) {
      atMs += frameMs;
      const state = detector.push(rms, atMs);
      states.set(atMs, state);
      if (state === "stopped" && stoppedAt === null) stoppedAt = atMs;
    }
  }
  return { stoppedAt, states, lastAt: atMs };
}

test("纯静音永远不判「说完」:没开过口就没有说完,按钮与作答窗口仍是仅有的两条收麦路", () => {
  const detector = createVoiceActivityDetector();
  const trace = feed(detector, [[30_000, QUIET]], 100);
  assert.equal(trace.stoppedAt, null);
  assert.equal(detector.state, "idle");
  // 标定过了:阈值落在绝对下限上,不是 0。
  assert.equal(detector.onsetThreshold, VAD_ABSOLUTE_MIN_RMS);
});

test("短于 250 ms 的响声不算开口:敲一下桌子之后再静 10 s 也不判说完", () => {
  for (const frameMs of [50, 100]) {
    const detector = createVoiceActivityDetector();
    const trace = feed(detector, [
      [1_000, QUIET], [VAD_SPEECH_ONSET_MS - 50, SPEECH], [10_000, QUIET],
    ], frameMs);
    assert.equal(trace.stoppedAt, null, `frame=${frameMs}`);
    assert.equal([...trace.states.values()].includes("speaking"), false, `frame=${frameMs}`);
  }
});

test("说了两秒、静了三秒:恰好在静默满 3000 ms 的那一帧判说完,前一帧还是 trailing_silence", () => {
  for (const frameMs of [50, 100]) {
    const detector = createVoiceActivityDetector();
    const trace = feed(detector, [[1_000, QUIET], [2_000, SPEECH], [5_000, QUIET]], frameMs);
    const speechEndsAt = 3_000;
    assert.equal(trace.stoppedAt, speechEndsAt + VAD_TRAILING_SILENCE_MS, `frame=${frameMs}`);
    assert.equal(trace.states.get(speechEndsAt + VAD_TRAILING_SILENCE_MS - frameMs),
      "trailing_silence", `frame=${frameMs}`);
    assert.equal(trace.states.get(speechEndsAt), "speaking", `frame=${frameMs}`);
  }
});

test("句子里 1–2 s 的停顿不判说完:只有最后一句之后静满 3 s 才停", () => {
  const detector = createVoiceActivityDetector();
  const trace = feed(detector, [
    [1_000, QUIET],
    [1_500, SPEECH], [1_000, QUIET],
    [1_000, SPEECH], [2_000, QUIET],
    [1_000, SPEECH], [4_000, QUIET],
  ], 100);
  const lastSpeechEndsAt = 1_000 + 1_500 + 1_000 + 1_000 + 2_000 + 1_000;
  assert.equal(trace.stoppedAt, lastSpeechEndsAt + VAD_TRAILING_SILENCE_MS);
  // 两次停顿里都回到过 trailing_silence,又都被下一句拉回 speaking。
  assert.equal(trace.states.get(3_000), "trailing_silence");
  assert.equal(trace.states.get(3_600), "speaking");
  assert.equal(trace.states.get(5_500), "trailing_silence");
  assert.equal(trace.states.get(6_600), "speaking");
});

test("吵的房间抬高阈值:安静房间 0.1 就算开口,噪声底 0.05 的房间要 0.15 以上", () => {
  const quiet = createVoiceActivityDetector();
  feed(quiet, [[VAD_CALIBRATION_MS + 100, QUIET]], 100);
  const noisy = createVoiceActivityDetector();
  feed(noisy, [[VAD_CALIBRATION_MS + 100, NOISY]], 100);
  assert.equal(quiet.onsetThreshold, VAD_ABSOLUTE_MIN_RMS);
  assert.equal(noisy.onsetThreshold, NOISY * 3);
  assert.ok((noisy.onsetThreshold as number) > (quiet.onsetThreshold as number));
  assert.ok((noisy.releaseThreshold as number) < (noisy.onsetThreshold as number));

  // 同一段 0.1 的声音:安静房间判开口→说完;吵的房间连开口都不算。
  const soft = 0.1;
  const calibratedAt = VAD_CALIBRATION_MS + 100;
  const quietTrace = feed(quiet, [[2_000, soft], [5_000, QUIET]], 100, calibratedAt);
  const noisyTrace = feed(noisy, [[2_000, soft], [5_000, NOISY]], 100, calibratedAt);
  assert.equal(quietTrace.stoppedAt, calibratedAt + 2_000 + VAD_TRAILING_SILENCE_MS);
  assert.equal(noisyTrace.stoppedAt, null);
  assert.equal(noisy.state, "idle");

  // 吵的房间里真正大声说话照样判:0.3 开口,回到 0.05 的底就是静默。
  const loudTrace = feed(noisy, [[2_000, 0.3], [5_000, NOISY]], 100, noisyTrace.lastAt);
  assert.equal(loudTrace.stoppedAt, noisyTrace.lastAt + 2_000 + VAD_TRAILING_SILENCE_MS);
});

test("帧率变了秒数不变:50/64/100 ms 一帧,说完时刻离最后一帧语音都是 3000 ms(误差不超过一帧)", () => {
  for (const frameMs of [50, 64, 100]) {
    const detector = createVoiceActivityDetector();
    const trace = feed(detector, [[1_000, QUIET], [2_000, SPEECH], [5_000, QUIET]], frameMs);
    assert.notEqual(trace.stoppedAt, null, `frame=${frameMs}`);
    // 64 ms 一帧时最后一帧语音落在 2944,不是 3000:秒数从它算起。
    const lastSpeechFrameAt = Math.floor(3_000 / frameMs) * frameMs;
    assert.equal(trace.states.get(lastSpeechFrameAt), "speaking", `frame=${frameMs}`);
    const expected = lastSpeechFrameAt + VAD_TRAILING_SILENCE_MS;
    assert.ok((trace.stoppedAt as number) >= expected, `frame=${frameMs}: ${trace.stoppedAt}`);
    assert.ok((trace.stoppedAt as number) < expected + frameMs, `frame=${frameMs}: ${trace.stoppedAt}`);
  }
});

test("一开录老人就在说话:标定出的底是语音级,第一次停顿把底放下来,之后照常判说完", () => {
  const detector = createVoiceActivityDetector();
  const trace = feed(detector, [
    [2_000, SPEECH], [500, QUIET], [1_000, SPEECH], [5_000, QUIET],
  ], 100);
  // 标定期全是语音:开口阈值被抬到语音之上,头一句没判开口。
  assert.equal(trace.states.get(1_000), "idle");
  // 500 ms 停顿之后底已经跟下来,第二句判开口、静满 3 s 判说完。
  assert.equal(trace.states.get(3_500), "speaking");
  assert.equal(trace.stoppedAt, 3_500 + VAD_TRAILING_SILENCE_MS);
});

test("stopped 是终态:之后再喂大声也不回到 speaking", () => {
  const detector = createVoiceActivityDetector();
  feed(detector, [[1_000, QUIET], [2_000, SPEECH], [3_000, QUIET]], 100);
  assert.equal(detector.state, "stopped");
  assert.equal(detector.push(SPEECH, 7_000), "stopped");
  assert.equal(detector.push(SPEECH, 8_000), "stopped");
  assert.equal(detector.state, "stopped");
});

test("坏读数整帧丢掉:NaN/负数既不推进时间也不改状态", () => {
  const detector = createVoiceActivityDetector();
  feed(detector, [[1_000, QUIET], [2_000, SPEECH], [2_900, QUIET]], 100);
  assert.equal(detector.state, "trailing_silence");
  assert.equal(detector.push(Number.NaN, 20_000), "trailing_silence");
  assert.equal(detector.push(-1, 20_000), "trailing_silence");
  // 下一帧真实静默:只记它自己那 100 ms,不记 NaN 帧「跳过」的那十几秒。
  assert.equal(detector.push(QUIET, 6_000), "stopped");
});

test("定时器卡住之后补来的一帧最多记 500 ms:不能把没观察到的几秒整段记成静默", () => {
  const detector = createVoiceActivityDetector();
  feed(detector, [[1_000, QUIET], [2_000, SPEECH]], 100);
  assert.equal(detector.state, "speaking");
  // 一帧跨了 5 s:只记 VAD_MAX_FRAME_GAP_MS,离 3 s 还远。
  assert.equal(detector.push(QUIET, 8_000), "trailing_silence");
  assert.equal(detector.push(QUIET, 13_000), "trailing_silence");
  assert.ok(VAD_MAX_FRAME_GAP_MS * 2 < VAD_TRAILING_SILENCE_MS);
  // 再正常喂满剩下的静默才停。
  let atMs = 13_000;
  let state: VoiceActivityState = detector.state;
  let frames = 0;
  while (state !== "stopped" && frames < 100) {
    atMs += 100;
    frames += 1;
    state = detector.push(QUIET, atMs);
  }
  assert.equal(state, "stopped");
  assert.equal(frames, (VAD_TRAILING_SILENCE_MS - VAD_MAX_FRAME_GAP_MS * 2) / 100);
});

test("常量就是协议里说的那几个数", () => {
  assert.equal(VAD_SPEECH_ONSET_MS, 250);
  assert.equal(VAD_TRAILING_SILENCE_MS, 3_000);
  assert.equal(VAD_CALIBRATION_MS, 400);
  assert.equal(VAD_ABSOLUTE_MIN_RMS, 0.01);
});
