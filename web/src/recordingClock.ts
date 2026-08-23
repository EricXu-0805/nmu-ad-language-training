// 录音进行中的可见计时(P1-9):双端各自本地计,研究者端从示意(arm)起计,
// 到收到 audioSaved 回执或看门狗超时为止。纯函数,便于钉测试。
export function formatRecordingClock(elapsedMs: number): string {
  const totalSeconds = Math.max(0, Math.floor(elapsedMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

// 8 秒看门狗在屏上的剩余秒数;到点后停在 0,不出现负数。
export function watchdogSecondsLeft(deadlineMs: number, nowMs: number): number {
  return Math.max(0, Math.ceil((deadlineMs - nowMs) / 1000));
}
