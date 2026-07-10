// 题图呈现 + 可选半区聚光(左命名/左作用→左半,右命名/右作用→右半,关系识别→双亮)。
// image_id=null(内容组未回填)→ 明确占位框,不静默;真实数据采集须先回填题图。
export type Spotlight = "left" | "right" | "both" | "none";

export function ImagePane({ imageId, spotlight = "none", alt }: { imageId: string | null; spotlight?: Spotlight; alt?: string }) {
  if (!imageId) {
    return (
      <div className="image-pane card" style={{
        display: "flex", alignItems: "center", justifyContent: "center", textAlign: "center",
        minHeight: 240, background: "var(--c-warn-bg)", color: "var(--c-warn)", border: "2px dashed var(--c-warn)",
      }}>
        <div>
          <div style={{ fontSize: "1.4em", fontWeight: 700 }}>（此题暂无图片）</div>
          <div className="muted">内容组待回填题图 · 不可用于真实数据采集</div>
        </div>
      </div>
    );
  }
  const half = (side: "left" | "right"): React.CSSProperties => ({
    position: "absolute", top: 0, bottom: 0, width: "50%", [side]: 0,
    background: spotlight === side ? "transparent" : "rgba(0,0,0,0.55)",
    transition: "background 300ms", pointerEvents: "none",
  } as React.CSSProperties);
  return (
    <div className="image-pane" style={{ position: "relative", display: "inline-block", borderRadius: "var(--radius)", overflow: "hidden" }}>
      <img src={`/img/${imageId}.webp`} alt={alt ?? ""} style={{ display: "block", maxWidth: "100%", height: "auto" }} />
      {spotlight !== "none" && spotlight !== "both" && (
        <>
          <div style={half("left")} aria-hidden />
          <div style={half("right")} aria-hidden />
        </>
      )}
    </div>
  );
}
