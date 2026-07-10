import type { RecState } from "../sync/messages";

// 老人端麦克风指示。VOX 自动录音——默认无"开始"钮;录音中显慢脉冲光环(非闪烁)+"请说…"。
// 可选巨钮"我说好了"仅作软停(onStop),老人不必按也能被研究者端停止。
export function MicButton({ state, onStop }: { state: RecState; onStop?: () => void }) {
  const recording = state === "recording" || state === "armed";
  return (
    <div className="col" style={{ alignItems: "center", gap: "var(--sp-5)" }}>
      <div
        className={`mic${recording ? " mic--pulse" : ""}`}
        role="img"
        aria-label={recording ? "正在聆听" : "麦克风"}
        style={{ display: "flex", alignItems: "center", justifyContent: "center", opacity: recording ? 1 : 0.5 }}
      >
        🎤
      </div>
      <div style={{ fontSize: "var(--fs-md)", minHeight: "1.5em" }}>
        {recording ? "请说…" : " "}
      </div>
      {recording && onStop && (
        <button type="button" onClick={onStop}
          style={{ minHeight: 96, minWidth: 240, fontSize: "var(--fs-md)", fontWeight: 700,
            borderRadius: "var(--radius)", border: "2px solid var(--c-primary)", background: "var(--c-surface)", color: "var(--c-primary)" }}>
          我说好了
        </button>
      )}
    </div>
  );
}
