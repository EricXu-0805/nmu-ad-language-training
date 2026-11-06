import { useCallback, useEffect, useState } from "react";

import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { StatusPill } from "../components/StatusPill";
import type { SupersededAudioGovernanceRow, WithdrawnAudioGovernanceRow } from "../types";

export function WithdrawnAudioGovernancePanel() {
  const [rows, setRows] = useState<WithdrawnAudioGovernanceRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [superseded, setSuperseded] = useState<SupersededAudioGovernanceRow[] | null>(null);
  const [supersededError, setSupersededError] = useState<string | null>(null);
  const [target, setTarget] = useState<WithdrawnAudioGovernanceRow | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    setError(null);
    setSupersededError(null);
    try { setRows(await api.withdrawnAudioGovernance()); }
    catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught));
    }
    try { setSuperseded(await api.supersededAudioGovernance()); }
    catch (caught) {
      setSupersededError(caught instanceof ApiError ? caught.detail : String(caught));
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
                {row.local_copy_disposal_device_count > 0 ? (
                  <span className="muted">
                    本地副本删除回执 {row.local_copy_disposal_device_count} 台设备
                    {row.local_copy_disposal_last_at
                      ? ` · 最近 ${new Date(row.local_copy_disposal_last_at).toLocaleString()}`
                      : ""}
                  </span>
                ) : (
                  <span className="muted">尚无设备确认删除本地副本</span>
                )}
              </div>
              <Button variant="danger" disabled={row.status === "deleted" || busy}
                onClick={() => setTarget(row)}>逐条物理删除</Button>
            </div>
          ))}
        </div>
      )}
      <SupersededAudioSlots rows={superseded} error={supersededError} />
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

// 同一台平板换到同一位受试者的新场次后被作废的旧场次录音槽(收据 255/257)。
// 只读:服务端没有字节可删,这里只回答「哪些槽被作废了、设备删了本地副本没有」。
export function SupersededAudioSlots({ rows, error }: {
  rows: SupersededAudioGovernanceRow[] | null;
  error: string | null;
}) {
  return (
    <details style={{ marginTop: "var(--sp-3)" }}>
      <summary style={{ cursor: "pointer", padding: "var(--sp-3) 0" }}>
        <strong>技术作废的录音槽（服务器从未收到字节，不是撤回）{rows ? ` · ${rows.length} 条` : ""}</strong>
      </summary>
      <div className="col" style={{ gap: 10, marginTop: "var(--sp-3)" }}>
        <p className="muted">平板换到同一位受试者的新场次后，旧场次没传上来或没登记上的录音槽会被作废。服务器对这些槽没有任何字节，这里只读，不提供删除。</p>
        {error && <Alert tone="danger" title="作废录音槽列表未读取">{error}</Alert>}
        {rows === null && !error && <StatusPill tone="muted">正在读取作废录音槽…</StatusPill>}
        {rows?.length === 0 && <Alert tone="info" title="暂无技术作废的录音槽">没有发现被作废的录音槽。</Alert>}
        {rows?.map((row) => (
          <div className="review-card" key={row.raw_audio_id}>
            <div className="review-card-main">
              <strong className="mono">{row.raw_audio_id}</strong>
              <span className="muted">研究编号 {row.patient_id} · 后台场次 {row.session_id}</span>
              <span><StatusPill tone={row.marked_by_rule ? "muted" : "warn"} size="sm">
                {row.marked_by_rule ? "作废规则已记账" : "无作废规则审计记录 · 来源需人工核对"}
              </StatusPill></span>
              {row.last_marked_at && (
                <span className="muted">作废时间 {new Date(row.last_marked_at).toLocaleString()}</span>
              )}
              {row.local_copy_disposal_device_count > 0 ? (
                <span className="muted">
                  本地副本删除回执 {row.local_copy_disposal_device_count} 台设备
                  {row.local_copy_disposal_last_at
                    ? ` · 最近 ${new Date(row.local_copy_disposal_last_at).toLocaleString()}`
                    : ""}
                </span>
              ) : (
                <span className="muted">尚无设备确认删除本地副本</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </details>
  );
}
