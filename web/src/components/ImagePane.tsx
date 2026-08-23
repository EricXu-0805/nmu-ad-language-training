import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  loadCurrentPatientAsset,
  type LoadedPatientAsset,
  type PatientAssetReadinessEvent,
} from "../patient/currentPatientAsset.ts";
import { browserCurrentPatientAssetDependencies } from "../patient/browserCurrentPatientAsset.ts";

// 题图呈现 + 可选半区聚光(左命名/左作用→左半,右命名/右作用→右半,关系识别→双亮)。
// 只取服务器当前游标授权的字节；不接收/拼接 image_id，DOM 仅出现临时 blob URL。
export type Spotlight = "left" | "right" | "both" | "none";

// 题图源是 docx 抽出的线稿,原始像素小(最小 132px)——不能按原尺寸摆,大屏上只有一角。
// 按视口给足呈现面积:高最多 IMG_VH·视口高、宽最多 IMG_VW·视口宽,允许放大(线稿放大可接受),
// 用 naturalWidth/Height 算显式宽高,聚光半区永远与图片边缘对齐。
const IMG_VW = 0.9;

function fit(nw: number, nh: number, vw: number, vh: number, compact: boolean): { width: number; height: number } {
  // 竖屏(iPad 竖持)图让高给文字与麦克风;线索挂出来时(compact)图再让一档,内容永不叠
  // 矮屏优先给线索和“我说好了”留空间；主操作不能被题图挤出视口。
  const short = vh <= 760;
  const imgVh = short
    ? (compact ? 0.23 : 0.31)
    : vh > vw
      ? (compact ? 0.26 : 0.34)
      : (compact ? 0.24 : 0.40);
  const scale = Math.min((vw * IMG_VW) / nw, (vh * imgVh) / nh);
  return { width: Math.round(nw * scale), height: Math.round(nh * scale) };
}

export function ImagePane({
  sessionId,
  requestKey,
  required = true,
  spotlight = "none",
  compact = false,
  alt,
  onReadinessChange,
}: {
  sessionId: string;
  /** Local race key only. It is never sent to the server or written into DOM. */
  requestKey: string;
  required?: boolean;
  spotlight?: Spotlight;
  compact?: boolean;
  alt?: string;
  onReadinessChange?(event: PatientAssetReadinessEvent): void;
}) {
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [loaded, setLoaded] = useState<{
    requestKey: string;
    asset: LoadedPatientAsset;
  } | null>(null);
  const [failedKey, setFailedKey] = useState<string | null>(null);
  // Prop changes render before effects clean up. Bind both success and failure
  // to the exact key so the previous item's pixels cannot flash for one frame.
  const asset = loaded?.requestKey === requestKey ? loaded.asset : null;
  const failed = failedKey === requestKey;
  const readinessCallback = useRef(onReadinessChange);
  readinessCallback.current = onReadinessChange;

  // 解码通过后在 paint 前定好尺寸,不闪未适配帧。
  useLayoutEffect(() => {
    if (!asset) return;
    const apply = () => setSize(fit(
      asset.naturalWidth,
      asset.naturalHeight,
      window.innerWidth,
      window.innerHeight,
      compact,
    ));
    apply();
    window.addEventListener("resize", apply);
    return () => window.removeEventListener("resize", apply);
  }, [asset, compact]);

  useEffect(() => {
    setSize(null);
    setLoaded(null);
    setFailedKey(null);
    if (!required) {
      readinessCallback.current?.({ requestKey, readiness: "ready" });
      return;
    }
    const controller = new AbortController();
    let ownedAsset: LoadedPatientAsset | null = null;
    readinessCallback.current?.({ requestKey, readiness: "loading" });
    void loadCurrentPatientAsset(
      sessionId,
      controller.signal,
      browserCurrentPatientAssetDependencies,
    ).then((loaded) => {
      if (controller.signal.aborted) {
        loaded.dispose();
        return;
      }
      ownedAsset = loaded;
      setLoaded({ requestKey, asset: loaded });
      readinessCallback.current?.({ requestKey, readiness: "ready" });
    }).catch((error: unknown) => {
      if (controller.signal.aborted
          || (error instanceof DOMException && error.name === "AbortError")) return;
      setFailedKey(requestKey);
      readinessCallback.current?.({ requestKey, readiness: "failed" });
    });
    return () => {
      controller.abort(new DOMException("题目已切换", "AbortError"));
      ownedAsset?.dispose();
    };
  }, [required, requestKey, sessionId]);

  if (!required) return null;
  const half = (side: "left" | "right"): React.CSSProperties => ({
    position: "absolute", top: 0, bottom: 0, width: "50%", [side]: 0,
    background: spotlight === side ? "transparent" : "rgba(0,0,0,0.55)",
    transition: "background 300ms", pointerEvents: "none",
  } as React.CSSProperties);
  if (failed) {
    // 安全取图或解码失败即关闭媒体门禁；不给老人暴露技术细节。
    return (
      <div className="image-pane patient-status" role="alert" aria-live="assertive" style={{
        width: "min(60vw, 640px)", height: "28vh", borderRadius: "var(--radius)",
        background: "var(--c-surface)", boxShadow: "var(--shadow-md)",
        display: "flex", alignItems: "center", justifyContent: "center", textAlign: "center",
      }}>
        <span>我们先等一下<br /><span className="muted">请研究者查看题目图片</span></span>
      </div>
    );
  }
  if (!asset) {
    return (
      <div className="image-pane card patient-status" role="status" aria-live="polite" aria-busy="true" style={{
        display: "flex", alignItems: "center", justifyContent: "center", textAlign: "center",
        width: "min(84vw, 640px)", minHeight: "min(28vh, 240px)",
        background: "var(--c-surface)", color: "var(--c-ink)",
      }}>
        <div>
          <div style={{ fontWeight: 700 }}>图片正在准备</div>
          <div className="muted">请稍候</div>
        </div>
      </div>
    );
  }
  return (
    <div className="image-pane" style={{ position: "relative", display: "inline-block", borderRadius: "var(--radius)", overflow: "hidden", background: "#fff" }}>
      <img src={asset.objectUrl} alt={alt ?? ""}
        onError={() => {
          asset.dispose();
          setLoaded(null);
          setFailedKey(requestKey);
          readinessCallback.current?.({ requestKey, readiness: "failed" });
        }}
        style={size
          ? { display: "block", width: size.width, height: size.height }
          : { display: "block", width: 1, height: 1, visibility: "hidden" }} />
      {spotlight !== "none" && spotlight !== "both" && (
        <>
          <div style={half("left")} aria-hidden />
          <div style={half("right")} aria-hidden />
        </>
      )}
    </div>
  );
}
