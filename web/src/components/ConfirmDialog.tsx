import { useId, type ReactNode } from "react";
import { Button } from "./Button";
import { useDialogFocusTrap } from "./useDialogFocusTrap";

// 危险动作确认(如"新受试者"清空现场):点背景=取消,永不误触即执行。
export function ConfirmDialog({ open, title, body, confirmLabel = "确认", confirmVariant = "danger", busy = false, confirmDisabled = false, onConfirm, onCancel }: {
  open: boolean; title: string; body?: ReactNode; confirmLabel?: string;
  /** 保护性动作(安全暂停类)传 "primary",避免红色破坏键吓住操作者;默认仍 danger。 */
  confirmVariant?: "danger" | "primary";
  busy?: boolean; confirmDisabled?: boolean;
  onConfirm: () => void; onCancel: () => void;
}) {
  const titleId = useId();
  const cancelSafely = () => { if (!busy) onCancel(); };
  const panelRef = useDialogFocusTrap<HTMLDivElement>({
    open,
    onCancel: cancelSafely,
    initialFocus: "first-button",
  });
  if (!open) return null;
  return (
    <div className="dialog-backdrop" onClick={cancelSafely}>
      <div ref={panelRef} className="dialog-panel fade-in" role="dialog" aria-modal="true"
        aria-busy={busy || undefined}
        aria-labelledby={titleId} tabIndex={-1} onClick={(e) => e.stopPropagation()}>
        <div className="dialog-header"><h3 id={titleId}>{title}</h3></div>
        {body && <div className="muted">{body}</div>}
        <div className="dialog-actions">
          <Button disabled={busy} onClick={cancelSafely}>取消</Button>
          <Button disabled={busy || confirmDisabled} variant={confirmVariant} onClick={onConfirm}>{confirmLabel}</Button>
        </div>
      </div>
    </div>
  );
}
