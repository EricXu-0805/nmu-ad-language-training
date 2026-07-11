import type { RecState } from "../sync/messages";

// 老人端麦克风区,三态:
// ① 空闲 + selfStart(操作端按录音资格下发)→ 大按钮"点这里,开始回答"(鼠标/触屏都行);
// ② 录音中(研究者 arm 或老人自助开录)→ 慢脉冲光环 +"请说…"+ 大按钮"我说好了"结束;
// ③ 空闲 + 无 selfStart → 半透明指示器(仅研究者可 arm,合规闸门不被老人端绕过)。
export function MicButton({ state, localActive, selfStart, micError, starting, onStart, onStop }: {
  state: RecState;
  localActive?: boolean;
  selfStart?: boolean;
  micError?: boolean;
  starting?: boolean;
  onStart?: () => void;
  onStop?: () => void;
}) {
  const recording = state === "recording" || state === "armed" || localActive === true;
  const big: React.CSSProperties = {
    minHeight: 96, minWidth: 260, fontSize: "var(--fs-md)", fontWeight: 700,
    borderRadius: "var(--radius)", cursor: "pointer",
  };
  return (
    <div className="col" style={{ alignItems: "center", gap: "var(--sp-3)" }}>
      <div
        className={`mic${recording ? " mic--pulse" : ""}`}
        role="img"
        aria-label={recording ? "正在聆听" : "麦克风"}
        style={{ display: "flex", alignItems: "center", justifyContent: "center", opacity: recording ? 1 : 0.5 }}
      >
        🎤
      </div>
      <div style={{ fontSize: "var(--fs-md)", minHeight: "1.2em", lineHeight: 1.2 }}>
        {recording ? "请说…" : " "}
      </div>
      {recording && onStop && (
        <button type="button" onClick={onStop}
          style={{ ...big, border: "2px solid var(--c-primary)", background: "var(--c-surface)", color: "var(--c-primary)" }}>
          我说好了
        </button>
      )}
      {!recording && selfStart && onStart && (
        <button type="button" onClick={onStart} disabled={starting}
          style={{ ...big, border: "none", background: "var(--c-primary)", color: "#fff", opacity: starting ? 0.7 : 1 }}>
          {starting ? "正在打开麦克风…" : "点这里，开始回答"}
        </button>
      )}
      {!recording && !starting && micError && (
        <p style={{ fontSize: "var(--fs-md)", margin: 0, opacity: 0.75 }}>麦克风没有打开，请找研究者看一看</p>
      )}
    </div>
  );
}
