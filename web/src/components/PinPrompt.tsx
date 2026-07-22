import { useEffect, useRef, useState } from "react";
import { api, ApiError, DEVICE_PAIR_REQUIRED_EVENT } from "../api";
import { Button } from "./Button";
import { useDialogFocusTrap } from "./useDialogFocusTrap";

// PIN 只用于这一次当面配对：不写 localStorage/sessionStorage，不成为后续 API bearer。
export function PinPrompt() {
  const [open, setOpen] = useState(false);
  const [val, setVal] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const openRef = useRef(false);

  const close = () => {
    openRef.current = false;
    setOpen(false);
  };
  const cancelSafely = () => { if (!busy) close(); };
  const panelRef = useDialogFocusTrap<HTMLFormElement>({
    open,
    onCancel: cancelSafely,
  });

  useEffect(() => {
    const show = () => {
      // Concurrent device routes can repeat the pairing-required hint. Do not
      // erase a PIN that the researcher is in the middle of typing.
      if (openRef.current) return;
      openRef.current = true;
      setVal("");
      setError(null);
      setOpen(true);
    };
    window.addEventListener(DEVICE_PAIR_REQUIRED_EVENT, show);
    return () => window.removeEventListener(DEVICE_PAIR_REQUIRED_EVENT, show);
  }, []);

  if (!open) return null;
  const pair = async () => {
    const pin = val.trim();
    if (!pin || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.pairDevice(pin);
      setVal("");
      close();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) setError("PIN 不正确，请重新输入");
      else setError(caught instanceof ApiError ? caught.detail : "配对失败，请检查连接后重试");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="dialog-backdrop dialog-backdrop-elevated" onClick={cancelSafely}>
      <form ref={panelRef} className="dialog-panel fade-in" role="dialog" aria-modal="true"
        aria-labelledby="pin-dialog-title" aria-busy={busy || undefined} tabIndex={-1} onClick={(e) => e.stopPropagation()}
        onSubmit={(e) => { e.preventDefault(); void pair(); }}>
        <div className="dialog-header"><h3 id="pin-dialog-title">配对受试者端</h3></div>
        <p className="muted">
          请由研究者在现场输入设备 PIN。配对只对当前训练场次有效，关闭此标签页后自动消失。
        </p>
        <input inputMode="numeric" type="password" autoComplete="one-time-code"
          value={val} onChange={(e) => { setVal(e.target.value); setError(null); }}
          className="pin-input" aria-label="设备 PIN" disabled={busy} />
        {error && <p role="alert" className="danger-text">{error}</p>}
        <div className="dialog-actions">
          <Button type="button" onClick={cancelSafely} disabled={busy}>稍后</Button>
          <Button type="submit" variant="primary" disabled={!val.trim() || busy}>
            {busy ? "正在配对…" : "完成配对"}
          </Button>
        </div>
      </form>
    </div>
  );
}
