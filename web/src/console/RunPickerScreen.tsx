import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { StatusPill } from "../components/StatusPill";
import type { PatientSummary, Session } from "../types";

// 训练台选人:从登记表选一个受试者,一键进入本次训练安排;场次编号后台自动生成、全程不呈现。
// 有未完成场次的受试者可直接"继续",不用记场次编号。
export function RunPickerScreen({ onPickSubject, onResume, onGoPrep }: {
  onPickSubject: (patientId: string) => void;
  onResume: (session: Session) => void;
  onGoPrep: () => void;
}) {
  const [rows, setRows] = useState<PatientSummary[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    setErr(null);
    api.listPatients().then(setRows).catch((e) => setErr(e instanceof ApiError ? e.detail : String(e)));
  }, []);

  return (
    <div className="page-shell page-shell--wide">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">训练台 · 选择受试者</p>
          <h2 className="page-title">选人开始训练</h2>
          <p className="page-description">从准备区登记好的受试者里选一位开始。若上次训练中途暂停,可直接继续。</p>
        </div>
        <Button onClick={onGoPrep}>去准备区登记</Button>
      </header>

      {err && <Alert tone="danger" title="受试者列表加载失败">{err}</Alert>}
      {rows && rows.length === 0 && !err && (
        <Alert tone="info" title="登记表里还没有受试者"
          actions={<Button variant="primary" onClick={onGoPrep}>去准备区登记</Button>}>
          请先在准备区登记受试者,再回来选人开训。
        </Alert>
      )}

      {rows && rows.map((r) => (
        <section className="run-picker-card" key={r.patient_id}>
          <div className="run-picker-head">
            <div className="col" style={{ gap: 2 }}>
              <strong className="mono" style={{ fontSize: "1.1em" }}>{r.patient_id}</strong>
              <span className="muted" style={{ fontSize: "0.85em" }}>
                {r.dementia_severity ? `认知障碍 ${r.dementia_severity} · ` : ""}
                录音{r.recording_allowed === true ? "允许" : r.recording_allowed === false ? "禁止" : "未评"} ·
                已训练 {r.session_count} 场
              </span>
            </div>
            <div className="toolbar">
              {r.session_count > 0 && (
                <Button onClick={() => setExpanded(expanded === r.patient_id ? null : r.patient_id)}>
                  {expanded === r.patient_id ? "收起" : "继续未完成"}
                </Button>
              )}
              <Button variant="primary" disabled={r.withdrawal_status === "withdrawn"}
                onClick={() => onPickSubject(r.patient_id)}>开始训练</Button>
            </div>
          </div>
          {r.withdrawal_status === "withdrawn" && <StatusPill tone="danger" size="sm">已撤回,不可开训</StatusPill>}
          {expanded === r.patient_id && <ResumeList patientId={r.patient_id} onResume={onResume} />}
        </section>
      ))}
      {!rows && !err && <StatusPill tone="muted">正在加载受试者…</StatusPill>}
    </div>
  );
}

function ResumeList({ patientId, onResume }: { patientId: string; onResume: (s: Session) => void }) {
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.patientSessions(patientId).then(setSessions).catch((e) => setErr(e instanceof ApiError ? e.detail : String(e)));
  }, [patientId]);
  if (err) return <p className="muted">场次读取失败:{err}</p>;
  if (!sessions) return <p className="muted">正在读取既有场次…</p>;
  if (sessions.length === 0) return <p className="muted">该受试者暂无既有场次。</p>;
  return (
    <div className="resume-list">
      {sessions.map((s) => (
        <div className="resume-row" key={s.session_id}>
          <span>第 {s.week_no} 周 · {s.phase_type}{s.training_date ? ` · ${s.training_date}` : ""}
            {s.session_sitting_no && s.session_sitting_no > 1 ? ` · 第 ${s.session_sitting_no} 次续做` : ""}</span>
          <Button onClick={() => onResume(s)}>继续这场</Button>
        </div>
      ))}
    </div>
  );
}
