import { useEffect, useId, useState } from "react";
import { formatRecordingClock } from "../recordingClock";
import type { RecState } from "../sync/messages";

// 老人端麦克风区的可见状态以【本机录音器真值】为准:
// armed/starting 只表示“正在准备”,只有 localActive=true 才能告诉老人“请说”。
// 这样即使权限弹窗未处理或设备启动失败,也不会诱导受试者对着未开启的麦克风作答。
export function MicButton({ state, localActive, selfStart, micError, starting, onStart, onStop }: {
  state: RecState;
  localActive?: boolean;
  selfStart?: boolean;
  micError?: boolean;
  starting?: boolean;
  onStart?: () => void;
  onStop?: () => void;
}) {
  const statusId = useId();
  const recording = localActive === true;
  // P1-9:录音进行中的本机计时(mm:ss)。只跟本机录音器真值走,开麦即起,收麦即清。
  const [recStartedAt, setRecStartedAt] = useState<number | null>(null);
  const [, setClockTick] = useState(0);
  useEffect(() => {
    if (!recording) {
      setRecStartedAt(null);
      return;
    }
    setRecStartedAt((prev) => prev ?? Date.now());
    const timer = window.setInterval(() => setClockTick((n) => n + 1), 500);
    return () => window.clearInterval(timer);
  }, [recording]);
  const preparing = !recording && (starting === true || state === "armed" || state === "recording");
  const status = recording
    ? "麦克风已打开，请说"
    : preparing
      ? "正在准备麦克风，请稍候"
      : micError
        ? "麦克风没有打开，请找研究者看一看"
        : selfStart
          ? "准备好后，请点下面的按钮"
          : "请听研究者提示";

  return (
    <div className="col patient-mic-control" style={{ alignItems: "center", gap: "var(--sp-3)" }}>
      <div
        className={`mic${recording ? " mic--pulse" : ""}${preparing ? " mic--preparing" : ""}`}
        aria-hidden="true"
        style={{ display: "flex", alignItems: "center", justifyContent: "center", opacity: recording ? 1 : preparing ? 0.72 : 0.5 }}
      >
        <span className="mic-brand">语</span>
      </div>
      <p id={statusId} className="patient-status" role="status" aria-live="polite" aria-atomic="true">
        {status}
      </p>
      {recording && recStartedAt !== null && (
        <p className="patient-status" aria-hidden="true" style={{ opacity: 0.75 }}>
          已说 {formatRecordingClock(Date.now() - recStartedAt)}
        </p>
      )}
      {recording && onStop && (
        <button type="button" className="patient-primary-action patient-primary-action--secondary"
          aria-describedby={statusId} onClick={onStop}>
          我说好了
        </button>
      )}
      {!recording && !preparing && !micError && selfStart && onStart && (
        <button type="button" className="patient-primary-action" aria-describedby={statusId} onClick={onStart}>
          点这里，开始回答
        </button>
      )}
    </div>
  );
}
