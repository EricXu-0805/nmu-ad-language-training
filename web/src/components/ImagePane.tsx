import React, { useCallback, useEffect, useLayoutEffect, useState } from "react";

// 题图呈现 + 可选半区聚光(左命名/左作用→左半,右命名/右作用→右半,关系识别→双亮)。
// image_id=null(内容组未回填)→ 明确占位框,不静默;真实数据采集须先回填题图。
export type Spotlight = "left" | "right" | "both" | "none";

// 题图源是 docx 抽出的线稿,原始像素小(最小 132px)——不能按原尺寸摆,大屏上只有一角。
// 按视口给足呈现面积:高最多 IMG_VH·视口高、宽最多 IMG_VW·视口宽,允许放大(线稿放大可接受),
// 用 naturalWidth/Height 算显式宽高,聚光半区永远与图片边缘对齐。
const IMG_VW = 0.9;

function fit(nw: number, nh: number, vw: number, vh: number, compact: boolean): { width: number; height: number } {
  // 竖屏(iPad 竖持)图让高给文字与麦克风;线索挂出来时(compact)图再让一档,内容永不叠
  const imgVh = vh > vw ? (compact ? 0.26 : 0.34) : (compact ? 0.30 : 0.40);
  const scale = Math.min((vw * IMG_VW) / nw, (vh * imgVh) / nh);
  return { width: Math.round(nw * scale), height: Math.round(nh * scale) };
}

export function ImagePane({ imageId, spotlight = "none", compact = false, alt }: { imageId: string | null; spotlight?: Spotlight; compact?: boolean; alt?: string }) {
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [failed, setFailed] = useState(false);

  const onLoad = useCallback((e: React.SyntheticEvent<HTMLImageElement>) => {
    const img = e.currentTarget;
    if (img.naturalWidth > 0) setNatural({ w: img.naturalWidth, h: img.naturalHeight });
  }, []);

  // useLayoutEffect:naturalWH 一到就在 paint 前定好尺寸,不闪未适配帧
  useLayoutEffect(() => {
    if (!natural) return;
    const apply = () => setSize(fit(natural.w, natural.h, window.innerWidth, window.innerHeight, compact));
    apply();
    window.addEventListener("resize", apply);
    return () => window.removeEventListener("resize", apply);
  }, [natural, compact]);

  // 换题时回到未测量状态,避免用上一张图的尺寸闪一帧
  useEffect(() => { setSize(null); setNatural(null); setFailed(false); }, [imageId]);

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
  if (failed) {
    // 图片字节加载失败(文件缺/损坏):给一个平和的空白画框,不永久隐形也不报错闪红;
    // 研究者从操作端能看到当前题号,QA 时据此排查。
    return (
      <div className="image-pane" style={{
        width: "min(60vw, 640px)", height: "28vh", borderRadius: "var(--radius)",
        background: "var(--c-surface)", boxShadow: "var(--shadow-md)",
        display: "flex", alignItems: "center", justifyContent: "center", fontSize: "3em", opacity: 0.35,
      }} aria-label="图片未能显示">🖼</div>
    );
  }
  return (
    <div className="image-pane" style={{ position: "relative", display: "inline-block", borderRadius: "var(--radius)", overflow: "hidden", background: "#fff" }}>
      <img src={`/img/${imageId}.webp`} alt={alt ?? ""} onLoad={onLoad} onError={() => setFailed(true)}
        style={size
          ? { display: "block", width: size.width, height: size.height }
          : { display: "block", maxWidth: "90vw", maxHeight: "40vh", visibility: "hidden" }} />
      {spotlight !== "none" && spotlight !== "both" && (
        <>
          <div style={half("left")} aria-hidden />
          <div style={half("right")} aria-hidden />
        </>
      )}
    </div>
  );
}
