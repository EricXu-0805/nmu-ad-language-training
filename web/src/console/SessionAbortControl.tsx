import { useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import {
  SESSION_ABORT_REASONS,
  createSessionAbortIntent,
  type SessionAbortIntent,
  type SessionAbortReasonCode,
} from "../sessionAbort";
import type { SessionRuntimeState } from "../types";

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.detail : String(error);
}

export function SessionAbortControl({ sessionId, expectedRevision, disabled = false, onAborted }: {
  sessionId: string;
  expectedRevision: number | null;
  disabled?: boolean;
  onAborted: (runtime: SessionRuntimeState) => void;
}) {
  const [open, setOpen] = useState(false);
  const [reasonCode, setReasonCode] = useState<SessionAbortReasonCode | "">("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [intent, setIntent] = useState<SessionAbortIntent | null>(null);

  async function confirmAbort() {
    if ((!reasonCode && !intent) || expectedRevision == null || busy) return;
    const operation = intent ?? createSessionAbortIntent(
      reasonCode as SessionAbortReasonCode,
      expectedRevision,
      `abort-${crypto.randomUUID()}`,
    );
    if (intent == null) setIntent(operation);
    setBusy(true);
    setError(null);
    try {
      const runtime = await api.abortSession(sessionId, operation);
      if (runtime.status !== "aborted") {
        throw new Error("服务器未返回可核对的中止终态");
      }
      setOpen(false);
      setIntent(null);
      onAborted(runtime);
    } catch (cause) {
      // Keep the selected closed reason and dialog open: an ambiguous response
      // can be retried with the same exact request without losing bedside state.
      setError(errorText(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button variant="danger" disabled={disabled || busy || expectedRevision == null} onClick={() => {
        setError(null);
        setOpen(true);
      }}>
        中止本场
      </Button>
      <ConfirmDialog
        open={open}
        title="确认中止本场？"
        confirmLabel={busy ? "正在中止…" : "确认中止，不再继续"}
        busy={busy}
        confirmDisabled={!reasonCode && !intent}
        body={(
          <div>
            <p className="muted">中止后题目推进、录音和 AI 干预会立即关闭，已保存证据保留为只读。</p>
            <label className="field">
              <span className="field-label">中止原因（必选）</span>
              <select value={intent?.reason_code ?? reasonCode} disabled={busy || intent !== null} onChange={(event) => {
                setReasonCode(event.target.value as SessionAbortReasonCode | "");
                setError(null);
              }}>
                <option value="">请选择受控原因</option>
                {SESSION_ABORT_REASONS.map(([code, label]) => (
                  <option key={code} value={code}>{label}</option>
                ))}
              </select>
            </label>
            <Alert tone="warn" title="本次访问位保留">
              场次中止后不会自动释放或重排训练访问；是否补做由协议负责人决定。
            </Alert>
            {error && <Alert tone="danger" title="服务器尚未确认中止">{error}</Alert>}
            {intent && error && (
              <p className="muted">再次确认会复用同一幂等键与原运行修订，不会改写中止原因。</p>
            )}
          </div>
        )}
        onConfirm={() => { void confirmAbort(); }}
        onCancel={() => { if (!busy) setOpen(false); }}
      />
    </>
  );
}
