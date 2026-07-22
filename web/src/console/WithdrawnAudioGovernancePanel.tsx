import { useCallback, useEffect, useState } from "react";

import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { StatusPill } from "../components/StatusPill";
import type { WithdrawnAudioGovernanceRow } from "../types";

export function WithdrawnAudioGovernancePanel() {
  const [rows, setRows] = useState<WithdrawnAudioGovernanceRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [target, setTarget] = useState<WithdrawnAudioGovernanceRow | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    setError(null);
    try { setRows(await api.withdrawnAudioGovernance()); }
    catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught));
    }
  }, []);
  useEffect(() => { void reload(); }, [reload]);

  const remove = async () => {
    if (!target || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.deleteWithdrawnAudio(target.raw_audio_id, target.session_id);
      setTarget(null);
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="form-section" aria-labelledby="withdrawn-audio-title">
      <div className="form-section-header">
        <div>
          <h3 id="withdrawn-audio-title">撤回录音治理 · 仅管理员</h3>
          <p className="muted">这里只列出已由受试者撤回账本隔离的录音。普通提前结束的录音不会混入；物理删除必须逐条核对研究编号与后台场次。</p>
        </div>
        <Button onClick={() => { void reload(); }}>刷新治理列表</Button>
      </div>
      {error && <Alert tone="danger" title="治理操作未完成">{error}</Alert>}
      {rows === null && !error && <StatusPill tone="muted">正在读取隔离录音…</StatusPill>}
      {rows?.length === 0 && <Alert tone="info" title="暂无待治理的撤回录音">没有发现已隔离录音。</Alert>}
      {rows && rows.length > 0 && (
        <div className="col" style={{ gap: 10 }}>
          {rows.map((row) => (
            <div className="review-card" key={row.raw_audio_id}>
              <div className="review-card-main">
                <strong className="mono">{row.raw_audio_id}</strong>
                <span className="muted">研究编号 {row.patient_id} · 后台场次 {row.session_id}</span>
                <span><StatusPill tone={row.status === "deleted" ? "muted" : "warn"} size="sm">
                  {row.status === "deleted" ? "已物理删除" : "已隔离 · 保留字节"}
                </StatusPill></span>
              </div>
              <Button variant="danger" disabled={row.status === "deleted" || busy}
                onClick={() => setTarget(row)}>逐条物理删除</Button>
            </div>
          ))}
        </div>
      )}
      <ConfirmDialog open={target !== null}
        title="物理删除这条已撤回录音？"
        body={target ? `将使用撤回专用通道删除 ${target.raw_audio_id}，并精确核对后台场次 ${target.session_id}。此操作不会用于普通提前结束证据。` : undefined}
        confirmLabel={busy ? "正在删除…" : "确认物理删除"}
        busy={busy}
        onConfirm={() => { void remove(); }}
        onCancel={() => { if (!busy) setTarget(null); }} />
    </section>
  );
}
