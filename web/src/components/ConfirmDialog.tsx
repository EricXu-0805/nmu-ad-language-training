import { useEffect } from "react";
import { Button } from "./Button";

// 危险动作确认(如"新受试者"清空现场):点背景=取消,永不误触即执行。
export function ConfirmDialog({ open, title, body, confirmLabel = "确认", onConfirm, onCancel }: {
  open: boolean; title: string; body?: string; confirmLabel?: string;
  onConfirm: () => void; onCancel: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onCancel(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);
  if (!open) return null;
  return (
    <div className="dialog-backdrop" onClick={onCancel}>
      <div className="dialog-panel fade-in" role="dialog" aria-modal="true" aria-labelledby="confirm-dialog-title" onClick={(e) => e.stopPropagation()}>
        <div className="dialog-header"><h3 id="confirm-dialog-title">{title}</h3></div>
        {body && <p className="muted">{body}</p>}
        <div className="dialog-actions">
          <Button autoFocus onClick={onCancel}>取消</Button>
          <Button variant="danger" onClick={onConfirm}>{confirmLabel}</Button>
        </div>
      </div>
    </div>
  );
}
