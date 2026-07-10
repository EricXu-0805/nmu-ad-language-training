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
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4000);
  }, []);
  return (
    <Ctx.Provider value={push}>
      {children}
      <div style={{ position: "fixed", right: 16, bottom: 16, display: "flex", flexDirection: "column", gap: 8, zIndex: 1000 }}>
        {toasts.map((t) => (
          <div key={t.id} style={{ background: bg[t.tone], color: "#fff", padding: "10px 16px", borderRadius: "var(--radius)", maxWidth: 420, boxShadow: "0 4px 12px rgba(0,0,0,.2)" }}>
            {t.text}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}
