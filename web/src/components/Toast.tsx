import { useCallback, useState, type ReactNode } from "react";
import { ToastContext, type ToastTone } from "./ToastContext";

interface Toast { id: number; text: string; tone: ToastTone }

let seq = 1;
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, tone: ToastTone = "info") => {
    const id = seq++;
    setToasts((t) => [...t, { id, text, tone }]);
    // danger 驻留 12s(丢录音级警告 4 秒就消失必然错过);其余 4s。点击可立即关。
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), tone === "danger" ? 12000 : 4000);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      {/* 左下角:右侧是抽屉(zIndex 900)的地盘,不互相遮挡;容器不吃点击,子项可点关 */}
      <div className="toast-stack" aria-live="polite" aria-relevant="additions">
        {toasts.map((t) => (
          <div key={t.id} role={t.tone === "danger" ? "alert" : "status"} className={`toast toast-${t.tone} fade-in`}
            onClick={() => setToasts((ts) => ts.filter((x) => x.id !== t.id))}
            title="点击关闭">
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
