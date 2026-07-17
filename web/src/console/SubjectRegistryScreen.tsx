import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { StatusPill } from "../components/StatusPill";
import type { PatientSummary } from "../types";
import { PatientIntakeScreen } from "./PatientIntakeScreen";
import { ScaleDrawer } from "./ScaleDrawer";

// 准备区:测试前由审核员把受试者编号/认知障碍程度/同意/录音授权登记好,并可提前录量表。
// 训练台只从这张登记表选人,不再现场建档——把"老人在旁边等着填表"这一步挪到测试前。
export function SubjectRegistryScreen() {
  const [rows, setRows] = useState<PatientSummary[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const [mode, setMode] = useState<"list" | "new">("list");
  const [scaleFor, setScaleFor] = useState<string | null>(null);

  const reload = useCallback(() => {
    setErr(null);
    api.listPatients().then(setRows).catch((e) => setErr(e instanceof ApiError ? e.detail : String(e)));
  }, []);
  useEffect(reload, [reload, nonce]);

  if (mode === "new") {
    return (
      <div className="page-shell page-shell--medium">
        <PatientIntakeScreen context="registry" onReady={() => {
          setMode("list"); setNonce((n) => n + 1);
        }} />
        <div className="form-actions"><Button onClick={() => setMode("list")}>返回登记表</Button></div>
      </div>
    );
  }

  return (
    <div className="page-shell page-shell--wide">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">准备区 · 受试者登记表</p>
          <h2 className="page-title">测试前登记与量表</h2>
          <p className="page-description">测试开始前把受试者信息、同意与录音授权登记好；量表可提前录入。登记表只用研究编号,不含姓名。</p>
        </div>
        <Button variant="primary" onClick={() => setMode("new")}>登记新受试者</Button>
      </header>

      {err && <Alert tone="danger" title="登记表加载失败" actions={<Button onClick={reload}>重试</Button>}>{err}</Alert>}
      {rows && rows.length === 0 && !err && (
        <Alert tone="info" title="还没有登记任何受试者">点右上角"登记新受试者"开始;登记后即可在训练台选人开训。</Alert>
      )}

      {rows && rows.length > 0 && (
        <div className="registry-table" role="table" aria-label="受试者登记表">
          <div className="registry-row registry-head" role="row">
            <span role="columnheader">研究编号</span>
            <span role="columnheader">认知障碍程度</span>
            <span role="columnheader">普通话</span>
            <span role="columnheader">录音授权</span>
            <span role="columnheader">场次数</span>
            <span role="columnheader">操作</span>
          </div>
          {rows.map((r) => (
            <div className="registry-row" role="row" key={r.patient_id}>
              <strong className="mono" role="cell">{r.patient_id}</strong>
              <span role="cell">{r.dementia_severity || <span className="muted">未填</span>}</span>
              <span role="cell">{triLabel(r.mandarin_eligible)}</span>
              <span role="cell">{recordingLabel(r.recording_allowed)}</span>
              <span role="cell">{r.session_count}{r.last_training_date ? ` · 最近 ${r.last_training_date}` : ""}</span>
              <span role="cell"><Button onClick={() => setScaleFor(r.patient_id)}>录量表</Button></span>
            </div>
          ))}
        </div>
      )}
      {!rows && !err && <StatusPill tone="muted">正在加载登记表…</StatusPill>}

      {scaleFor && <ScaleDrawer patientId={scaleFor} open onClose={() => setScaleFor(null)} />}
    </div>
  );
}

function triLabel(v: boolean | null | undefined) {
  if (v === true) return <StatusPill tone="ok" size="sm">符合</StatusPill>;
  if (v === false) return <StatusPill tone="danger" size="sm">不符</StatusPill>;
  return <span className="muted">未评</span>;
}

function recordingLabel(v: boolean | null | undefined) {
  if (v === true) return <StatusPill tone="ok" size="sm">允许</StatusPill>;
  if (v === false) return <StatusPill tone="danger" size="sm">禁止</StatusPill>;
  return <StatusPill tone="warn" size="sm">未评</StatusPill>;
}
