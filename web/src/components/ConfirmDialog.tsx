import { Button } from "./Button";

// 危险动作确认(如"新受试者"清空现场):点背景=取消,永不误触即执行。
export function ConfirmDialog({ open, title, body, confirmLabel = "确认", onConfirm, onCancel }: {
  open: boolean; title: string; body?: string; confirmLabel?: string;
  onConfirm: () => void; onCancel: () => void;
}) {
  if (!open) return null;
  return (
    <div
      onClick={onCancel}
      style={{ position: "fixed", inset: 0, background: "rgba(28,25,23,.4)", zIndex: 950,
        display: "flex", alignItems: "center", justifyContent: "center" }}
    >
      <div className="card col fade-in" onClick={(e) => e.stopPropagation()} style={{ width: 420, maxWidth: "90vw" }}>
        <h3 style={{ margin: 0 }}>{title}</h3>
        {body && <p className="muted">{body}</p>}
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <Button onClick={onCancel}>取消</Button>
          <Button variant="danger" onClick={onConfirm}>{confirmLabel}</Button>
        </div>
      </div>
    </div>
  );
}
