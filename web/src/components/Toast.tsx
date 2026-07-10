import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

type Tone = "info" | "ok" | "warn" | "danger";
interface Toast { id: number; text: string; tone: Tone }

const Ctx = createContext<(text: string, tone?: Tone) => void>(() => {});
export const useToast = () => useContext(Ctx);

let seq = 1;
const bg: Record<Tone, string> = { info: "var(--c-primary)", ok: "var(--c-ok)", warn: "var(--c-warn)", danger: "var(--c-danger)" };

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, tone: Tone = "info") => {
    const id = seq++;
    setToasts((t) => [...t, { id, text, tone }]);
    // danger 驻留 12s(丢录音级警告 4 秒就消失必然错过);其余 4s。点击可立即关。
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), tone === "danger" ? 12000 : 4000);
  }, []);
  return (
    <Ctx.Provider value={push}>
      {children}
      {/* 左下角:右侧是抽屉(zIndex 900)的地盘,不互相遮挡;容器不吃点击,子项可点关 */}
      <div style={{ position: "fixed", left: 16, bottom: 16, display: "flex", flexDirection: "column", gap: 8, zIndex: 1000, pointerEvents: "none" }}>
        {toasts.map((t) => (
          <div key={t.id} role="status" className="fade-in"
            onClick={() => setToasts((ts) => ts.filter((x) => x.id !== t.id))}
            title="点击关闭"
            style={{ background: bg[t.tone], color: "#fff", padding: "10px 16px", borderRadius: "var(--radius)",
              maxWidth: 420, boxShadow: "0 4px 12px rgba(0,0,0,.2)", cursor: "pointer", pointerEvents: "auto" }}>
            {t.text}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}
