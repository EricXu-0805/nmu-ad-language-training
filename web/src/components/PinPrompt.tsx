import { useEffect, useState } from "react";
import { PIN_REQUIRED_EVENT, getPin, setPin } from "../api";
import { Button } from "./Button";

// 内网模式的 PIN 输入(后端 401 时弹出;单机模式后端不设 PIN,永不出现)。
// 两端各输一次存本机;输错下次写操作会再次弹出。
export function PinPrompt() {
  const [open, setOpen] = useState(false);
  const [val, setVal] = useState("");

  useEffect(() => {
    const show = () => { setVal(getPin() ?? ""); setOpen(true); };
    window.addEventListener(PIN_REQUIRED_EVENT, show);
    return () => window.removeEventListener(PIN_REQUIRED_EVENT, show);
  }, []);

  if (!open) return null;
  const save = () => { const v = val.trim(); if (!v) return; setPin(v); setOpen(false); };
  return (
    <div onClick={() => setOpen(false)} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.5)", zIndex: 1100,
                  display: "flex", alignItems: "center", justifyContent: "center" }}>
      <form className="card col fade-in" onClick={(e) => e.stopPropagation()}
        onSubmit={(e) => { e.preventDefault(); save(); }} style={{ maxWidth: 380 }}>
        <h3>需要操作端 PIN</h3>
        <p className="muted">内网模式下写操作须验证。PIN 显示在服务器启动终端;输入一次即存本机。</p>
        <input autoFocus inputMode="numeric" value={val} onChange={(e) => setVal(e.target.value)}
          style={{ padding: "var(--sp-3)", fontSize: "1.4em", textAlign: "center", letterSpacing: 4,
                   border: "1px solid var(--c-line)", borderRadius: "var(--radius)" }} />
        <div className="row">
          <Button type="button" onClick={() => setOpen(false)}>稍后</Button>
          <Button type="submit" variant="primary" disabled={!val.trim()} style={{ flex: 1 }}>
            保存(之后重试刚才的操作)
          </Button>
        </div>
      </form>
    </div>
  );
}
