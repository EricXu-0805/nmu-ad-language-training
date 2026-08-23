import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { CONSOLE_NOTE_EVENT, PATIENT_VIEW_EVENT } from "../sync/messages";
import { ToastContext, type ToastTone } from "./ToastContext";
import { humanizeToastText } from "./toastText";

interface Toast { id: number; text: string; tone: ToastTone; count: number }

// 主屏切换时清掉非危险提示:上一屏的"已取消/已保存"跟到下一屏只会造成误会。
// danger 级(丢录音/安全类)必须自己读完或点掉,不随路由蒸发。
export const TOAST_SCREEN_CHANGED_EVENT = "nmu:toast-screen-changed";

let seq = 1;
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  // setState 的 updater 不同步执行;合并/过期判断都要同步事实,故真值放 ref,状态只作镜像。
  const listRef = useRef<Toast[]>([]);
  const apply = useCallback((mutate: (list: Toast[]) => Toast[]) => {
    listRef.current = mutate(listRef.current);
    setToasts(listRef.current);
  }, []);
  // 受试者画面叠层打开时,提示不上屏(抬 z 序会把报错闪给老人,违反"老人端永不报错"红线),
  // 也不能照常 12 秒过期——自动驾驶失败的接管信号会静默蒸发。暂存,叠层收起后补投;
  // 暂存量经窗内事件驱动宿主退出钮上的中性提示点。
  const overlayOpen = useRef(false);
  const held = useRef<Toast[]>([]);
  const timers = useRef<Map<number, number>>(new Map());
  const expire = useCallback((id: number, tone: ToastTone) => {
    // danger 驻留 12s(丢录音级警告 4 秒就消失必然错过);其余 4s。点击可立即关。
    // 合并计数时重新起算,避免正在重复发生的问题提前消失。
    const previous = timers.current.get(id);
    if (previous !== undefined) window.clearTimeout(previous);
    timers.current.set(id, window.setTimeout(() => {
      timers.current.delete(id);
      apply((list) => list.filter((x) => x.id !== id));
    }, tone === "danger" ? 12000 : tone === "warn" ? 8000 : 4000));
  }, [apply]);
  const push = useCallback((rawText: string, tone: ToastTone = "info") => {
    // 统一出口剥壳:英文异常前缀与工程词都在这里翻成人话(P1-10)。
    const text = humanizeToastText(rawText);
    if (overlayOpen.current) {
      held.current = [...held.current, { id: seq++, text, tone, count: 1 }];
      window.dispatchEvent(new CustomEvent(CONSOLE_NOTE_EVENT, { detail: { count: held.current.length } }));
      return;
    }
    const existing = listRef.current.find((x) => x.text === text && x.tone === tone);
    if (existing) {
      // 同文同级合并计数,不叠两条一样的(P2-1)。
      apply((list) => list.map((x) => x.id === existing.id ? { ...x, count: x.count + 1 } : x));
      expire(existing.id, tone);
      return;
    }
    const id = seq++;
    apply((list) => [...list, { id, text, tone, count: 1 }]);
    expire(id, tone);
  }, [apply, expire]);
  useEffect(() => {
    const onToggle = (e: Event) => {
      const open = (e as CustomEvent<{ open: boolean }>).detail?.open === true;
      overlayOpen.current = open;
      if (!open && held.current.length) {
        const flush = held.current;
        held.current = [];
        window.dispatchEvent(new CustomEvent(CONSOLE_NOTE_EVENT, { detail: { count: 0 } }));
        apply((list) => [...list, ...flush]);
        flush.forEach((x) => expire(x.id, x.tone));
      }
    };
    const onScreenChanged = () => {
      apply((list) => list.filter((x) => x.tone === "danger"));
    };
    window.addEventListener(PATIENT_VIEW_EVENT, onToggle);
    window.addEventListener(TOAST_SCREEN_CHANGED_EVENT, onScreenChanged);
    return () => {
      window.removeEventListener(PATIENT_VIEW_EVENT, onToggle);
      window.removeEventListener(TOAST_SCREEN_CHANGED_EVENT, onScreenChanged);
    };
  }, [apply, expire]);
  return (
    <ToastContext.Provider value={push}>
      {children}
      {/* 底部居中:右侧是抽屉(zIndex 900)的地盘,不互相遮挡;容器不吃点击,子项可点关 */}
      <div className="toast-stack" aria-live="polite" aria-relevant="additions">
        {toasts.map((t) => (
          <div key={t.id} role={t.tone === "danger" ? "alert" : "status"} className={`toast toast-${t.tone} fade-in`}
            onClick={() => apply((list) => list.filter((x) => x.id !== t.id))}
            title="点击关闭">
            {t.text}{t.count > 1 ? `（×${t.count}）` : ""}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
