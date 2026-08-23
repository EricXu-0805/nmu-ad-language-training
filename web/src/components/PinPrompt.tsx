import { useEffect, useRef, useState } from "react";
import { api, ApiError, DEVICE_PAIR_REQUIRED_EVENT, getPatientBinding } from "../api";
import { Button } from "./Button";
import { Field } from "./Field";
import { useDialogFocusTrap } from "./useDialogFocusTrap";

// 工作人员在问候页点「连接这台平板」时的手动弹框事件:与 DEVICE_PAIR_REQUIRED_EVENT
// 不同,它是人明确要求配对,即使设备存有受试者绑定也照样打开(绑定可能已静默失效)。
// 调用方直接 window.dispatchEvent(new Event(PIN_PROMPT_MANUAL_OPEN_EVENT))。
export const PIN_PROMPT_MANUAL_OPEN_EVENT = "nmu:pin-prompt-manual-open";

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
    const openNow = () => {
      // Concurrent device routes can repeat the pairing-required hint. Do not
      // erase a PIN that the researcher is in the middle of typing.
      if (openRef.current) return;
      openRef.current = true;
      setVal("");
      setError(null);
      setOpen(true);
    };
    const show = () => {
      // 存有受试者绑定的设备由自动跟场循环静默续上能力;绑定真死时循环会
      // 清除绑定并重新广播本事件,那时才弹人工配对框。
      if (getPatientBinding()) return;
      openNow();
    };
    window.addEventListener(DEVICE_PAIR_REQUIRED_EVENT, show);
    window.addEventListener(PIN_PROMPT_MANUAL_OPEN_EVENT, openNow);
    return () => {
      window.removeEventListener(DEVICE_PAIR_REQUIRED_EVENT, show);
      window.removeEventListener(PIN_PROMPT_MANUAL_OPEN_EVENT, openNow);
    };
  }, []);

  if (!open) return null;
  const pair = async () => {
    const pin = val.trim();
    if (!pin || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.pairDeviceWithCode(pin);
      setVal("");
      close();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) setError("配对码不对，请重新输入");
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
        <div className="dialog-header"><h3 id="pin-dialog-title">连接这台平板</h3></div>
        <Field label="请工作人员输入配对码"
          hint="可输入这位老人的专属配对码（一次输入，以后开场自动连接），或本次训练的配对码">
          <input inputMode="numeric" type="password" autoComplete="one-time-code"
            value={val} onChange={(e) => { setVal(e.target.value); setError(null); }}
            className="pin-input" aria-label="配对码" disabled={busy} />
        </Field>
        {error && <p role="alert" className="danger-text">{error}</p>}
        <div className="dialog-actions">
          <Button type="button" onClick={cancelSafely} disabled={busy}>暂不配对</Button>
          <Button type="submit" variant="primary" disabled={!val.trim() || busy}>
            {busy ? "正在配对…" : "完成配对"}
          </Button>
        </div>
      </form>
    </div>
  );
}
