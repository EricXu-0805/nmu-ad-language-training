import { useEffect, useState } from "react";
import { PIN_REQUIRED_EVENT, getPin, setPin } from "../api";
import { Button } from "./Button";

// 内网模式的 PIN 输入(后端 401 时弹出;单机模式后端不设 PIN,永不出现)。
// 两端各输一次存本机;输错下次写操作会再次弹出。
export function PinPrompt() {
  const [open, setOpen] = useState(false);
  const [val, setVal] = useState("");
  const patientDevice = window.location.pathname.startsWith("/patient");

  useEffect(() => {
    const show = () => { setVal(getPin() ?? ""); setOpen(true); };
    window.addEventListener(PIN_REQUIRED_EVENT, show);
    return () => window.removeEventListener(PIN_REQUIRED_EVENT, show);
  }, []);

  if (!open) return null;
  const save = () => { const v = val.trim(); if (!v) return; setPin(v); setOpen(false); };
  return (
    <div className="dialog-backdrop dialog-backdrop-elevated" onClick={() => setOpen(false)}>
      <form className="dialog-panel fade-in" role="dialog" aria-modal="true" aria-labelledby="pin-dialog-title" onClick={(e) => e.stopPropagation()}
        onSubmit={(e) => { e.preventDefault(); save(); }}>
        <div className="dialog-header"><h3 id="pin-dialog-title">需要设备 PIN</h3></div>
        <p className="muted">
          {patientDevice ? "请由研究者输入服务器启动终端显示的 PIN。验证后本设备才能安全上传心跳与录音。"
            : "内网模式下，研究数据读写需要验证。PIN 显示在服务器启动终端，输入一次后保存在本设备。"}
        </p>
        <input autoFocus inputMode="numeric" value={val} onChange={(e) => setVal(e.target.value)}
          className="pin-input" aria-label="操作端 PIN" />
        <div className="dialog-actions">
          <Button type="button" onClick={() => setOpen(false)}>稍后</Button>
          <Button type="submit" variant="primary" disabled={!val.trim()}>
            保存并返回
          </Button>
        </div>
      </form>
    </div>
  );
}
