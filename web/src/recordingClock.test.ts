import assert from "node:assert/strict";
import test from "node:test";
import { formatRecordingClock, watchdogSecondsLeft } from "./recordingClock.ts";

test("录音计时 mm:ss:起点 00:00,分钟进位,负值收敛为 00:00", () => {
  assert.equal(formatRecordingClock(0), "00:00");
  assert.equal(formatRecordingClock(999), "00:00");
  assert.equal(formatRecordingClock(1_000), "00:01");
  assert.equal(formatRecordingClock(61_500), "01:01");
  assert.equal(formatRecordingClock(600_000), "10:00");
  assert.equal(formatRecordingClock(-5_000), "00:00");
});

test("看门狗倒计时:向上取整,到点停在 0 不出负数", () => {
  const deadline = 10_000;
  assert.equal(watchdogSecondsLeft(deadline, 2_000), 8);
  assert.equal(watchdogSecondsLeft(deadline, 9_100), 1);
  assert.equal(watchdogSecondsLeft(deadline, 10_000), 0);
  assert.equal(watchdogSecondsLeft(deadline, 12_000), 0);
});
