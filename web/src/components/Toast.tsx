import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { CONSOLE_NOTE_EVENT, PATIENT_VIEW_EVENT } from "../sync/messages";
import { ToastContext, type ToastTone } from "./ToastContext";

interface Toast { id: number; text: string; tone: ToastTone }

let seq = 1;
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  // 受试者画面叠层打开时,提示不上屏(抬 z 序会把报错闪给老人,违反"老人端永不报错"红线),
  // 也不能照常 12 秒过期——自动驾驶失败的接管信号会静默蒸发。暂存,叠层收起后补投;
  // 暂存量经窗内事件驱动宿主退出钮上的中性提示点。
  const overlayOpen = useRef(false);
  const held = useRef<Toast[]>([]);
  const expire = useCallback((id: number, tone: ToastTone) => {
    // danger 驻留 12s(丢录音级警告 4 秒就消失必然错过);其余 4s。点击可立即关。
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), tone === "danger" ? 12000 : 4000);
  }, []);
  const push = useCallback((text: string, tone: ToastTone = "info") => {
    const id = seq++;
    if (overlayOpen.current) {
      held.current = [...held.current, { id, text, tone }];
      window.dispatchEvent(new CustomEvent(CONSOLE_NOTE_EVENT, { detail: { count: held.current.length } }));
      return;
    }
    setToasts((t) => [...t, { id, text, tone }]);
    expire(id, tone);
  }, [expire]);
  useEffect(() => {
    const onToggle = (e: Event) => {
      const open = (e as CustomEvent<{ open: boolean }>).detail?.open === true;
      overlayOpen.current = open;
      if (!open && held.current.length) {
        const flush = held.current;
        held.current = [];
        window.dispatchEvent(new CustomEvent(CONSOLE_NOTE_EVENT, { detail: { count: 0 } }));
        setToasts((t) => [...t, ...flush]);
        flush.forEach((x) => expire(x.id, x.tone));
      }
    };
    window.addEventListener(PATIENT_VIEW_EVENT, onToggle);
    return () => window.removeEventListener(PATIENT_VIEW_EVENT, onToggle);
  }, [expire]);
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
