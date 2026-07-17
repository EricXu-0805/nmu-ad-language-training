import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { StatusPill } from "../components/StatusPill";
import type { AuditEntry, AuditVerify, AudioAsset, ItemEvent, PatientSummary, ScaleResult, Session, TurnEvent } from "../types";
import { AuthenticatedAudio } from "./AuthenticatedAudio";

// 审计动作码 → 中文标签
const AUDIT_ACTION_LABELS: Record<string, string> = {
  score_lock: "锁分", scale_record: "录量表", abnormal: "异常/介入",
  audio_delete: "删录音", data_export: "导出", login: "登录",
};
const auditActionLabel = (a: string) => AUDIT_ACTION_LABELS[a] ?? a;

// 全局审计链完整性徽标:重算哈希链 + 比对高水位锚点,报告改动/删除。
function IntegrityBadge() {
  const [v, setV] = useState<AuditVerify | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => { api.auditVerify().then(setV).catch(() => setErr(true)); }, []);
  if (err) return <StatusPill tone="muted" size="sm">审计链状态未知</StatusPill>;
  if (!v) return <StatusPill tone="muted" size="sm">校验审计链…</StatusPill>;
  if (v.ok) return <StatusPill tone="ok" size="sm">审计链完整 · {v.count} 条</StatusPill>;
  if (v.problem === "chain_broken") return <StatusPill tone="danger" size="sm">⚠ 审计链在 #{v.broken_at} 处被改</StatusPill>;
  if (v.problem === "truncated") return <StatusPill tone="danger" size="sm">⚠ 审计疑似被删({v.count}/{v.expected_count} 条)</StatusPill>;
  return <StatusPill tone="warn" size="sm">⚠ 审计链异常({v.problem})</StatusPill>;
}

// 某场次的操作审计(只读元数据:谁/何时/做了什么,无患者作答文本)。
function AuditPanel({ sessionId }: { sessionId: string }) {
  const [rows, setRows] = useState<AuditEntry[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.listAudit({ sessionId }).then(setRows).catch((e) => setErr(e instanceof ApiError ? e.detail : String(e)));
  }, [sessionId]);
  return (
    <section className="form-section">
      <div className="form-section-header"><div>
        <h3>操作审计(本场)</h3>
        <p className="muted">只追加、哈希链防篡改;仅记元数据,不含患者作答文本。</p>
      </div></div>
      {err && <Alert tone="danger" title="审计读取失败">{err}</Alert>}
      {rows && rows.length === 0 && <p className="muted">本场暂无审计记录。</p>}
      {rows && rows.length > 0 && (
        <div className="audit-list">
          {rows.map((r) => (
            <div className="audit-row" key={r.id}>
              <span className="audit-ts mono">{r.ts.replace("T", " ").slice(0, 19)}</span>
              <StatusPill tone="primary" size="sm">{auditActionLabel(r.action)}</StatusPill>
              <span className="audit-actor">👤 {r.actor}</span>
              <span className="audit-summary">{r.summary}</span>
            </div>
          ))}
        </div>
      )}
      {!rows && !err && <StatusPill tone="muted" size="sm">正在加载审计…</StatusPill>}
    </section>
  );
}

// 分析后台:按受试者 → 场次 → 逐环节,回看 AI 判定/提示级/人工锁分/录音,供后期核查
// "AI 判得对不对、指引对不对"。只读;绝不显示画像(judge_portrait_used 恒 false)。
export function AnalysisScreen() {
  const [rows, setRows] = useState<PatientSummary[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [patientId, setPatientId] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);

  useEffect(() => {
    api.listPatients().then(setRows).catch((e) => setErr(e instanceof ApiError ? e.detail : String(e)));
  }, []);

  if (sessionId && patientId) {
    return <SessionAnalysis patientId={patientId} sessionId={sessionId} onBack={() => setSessionId(null)} />;
  }
  if (patientId) {
    return <PatientAnalysis patientId={patientId} onOpenSession={setSessionId} onBack={() => setPatientId(null)} />;
  }
  return (
    <div className="page-shell page-shell--wide">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">分析后台 · 受试者</p>
          <h2 className="page-title">数据回看与核查</h2>
          <p className="page-description">按受试者编号进入,回看每场次逐环节的 AI 判定、提示层级、人工锁分和录音,用于核查 AI 判得准不准、指引对不对。</p>
        </div>
        <IntegrityBadge />
      </header>
      {err && <Alert tone="danger" title="受试者列表加载失败">{err}</Alert>}
      {rows && rows.length === 0 && !err && <Alert tone="info" title="暂无数据">还没有登记受试者或采集数据。</Alert>}
      {rows && rows.map((r) => (
        <button className="analysis-subject-row" key={r.patient_id} onClick={() => setPatientId(r.patient_id)}>
          <strong className="mono">{r.patient_id}</strong>
          <span className="muted">{r.session_count} 场{r.last_training_date ? ` · 最近 ${r.last_training_date}` : ""}</span>
          <span aria-hidden>›</span>
        </button>
      ))}
      {!rows && !err && <StatusPill tone="muted">正在加载…</StatusPill>}
    </div>
  );
}

function PatientAnalysis({ patientId, onOpenSession, onBack }: {
  patientId: string; onOpenSession: (sid: string) => void; onBack: () => void;
}) {
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [scales, setScales] = useState<ScaleResult[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    Promise.all([api.patientSessions(patientId), api.listScales(patientId)])
      .then(([ss, sc]) => { setSessions(ss); setScales(sc); })
      .catch((e) => setErr(e instanceof ApiError ? e.detail : String(e)));
  }, [patientId]);
  return (
    <div className="page-shell page-shell--wide">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">分析后台 · <span className="mono">{patientId}</span></p>
          <h2 className="page-title">场次与量表</h2>
        </div>
        <Button onClick={onBack}>返回受试者列表</Button>
      </header>
      {err && <Alert tone="danger" title="读取失败">{err}</Alert>}

      <section className="form-section">
        <div className="form-section-header"><div><h3>量表</h3></div></div>
        {scales && scales.length === 0 && <p className="muted">暂无量表记录。</p>}
        {scales && scales.length > 0 && (
          <div className="registry-table">
            <div className="registry-row registry-head">
              <span>阶段</span><span>量表</span><span>分项</span><span>分数</span><span>评估人</span>
            </div>
            {scales.map((sc) => (
              <div className="registry-row" key={sc.id ?? `${sc.scale_name}-${sc.phase_type}-${sc.subscale}`}>
                <span>{sc.phase_type}</span><span>{sc.scale_name}</span>
                <span>{sc.subscale || <span className="muted">—</span>}</span>
                <span>{sc.score ?? <span className="muted">—</span>}</span>
                <span>{sc.assessor_id || <span className="muted">—</span>}</span>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="form-section">
        <div className="form-section-header"><div><h3>训练场次</h3></div></div>
        {sessions && sessions.length === 0 && <p className="muted">暂无场次。</p>}
        {sessions && sessions.map((s) => (
          <button className="analysis-subject-row" key={s.session_id} onClick={() => onOpenSession(s.session_id)}>
            <strong>第 {s.week_no} 周 · {s.phase_type}</strong>
            <span className="muted">{s.training_date || "日期未记"}{s.session_sitting_no && s.session_sitting_no > 1 ? ` · 第 ${s.session_sitting_no} 次续做` : ""}</span>
            <span aria-hidden>›</span>
          </button>
        ))}
        {!sessions && !err && <StatusPill tone="muted">正在加载场次…</StatusPill>}
      </section>
    </div>
  );
}

function SessionAnalysis({ patientId, sessionId, onBack }: {
  patientId: string; sessionId: string; onBack: () => void;
}) {
  const [data, setData] = useState<{ items: ItemEvent[]; turns: TurnEvent[]; audios: AudioAsset[]; session: Session } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.sessionJournal(sessionId)
      .then((j) => setData({ items: j.items, turns: j.turns, audios: j.audios, session: j.session }))
      .catch((e) => setErr(e instanceof ApiError ? e.detail : String(e)));
  }, [sessionId]);

  const audioByKey = new Map<string, AudioAsset>();
  const audioById = new Map<string, AudioAsset>();
  for (const a of data?.audios ?? []) {
    if (a.turn_key) audioByKey.set(a.turn_key, a);
    audioById.set(a.raw_audio_id, a);
  }
  const itemById = new Map<number, ItemEvent>();
  for (const it of data?.items ?? []) if (it.id != null) itemById.set(it.id, it);
  // 已被逐环节卡消费的录音;剩余的(关系建立段等未绑 turn 的录音)单列出来,不静默丢弃。
  const usedAudioIds = new Set<string>();

  return (
    <div className="page-shell page-shell--wide">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">分析后台 · <span className="mono">{patientId}</span></p>
          <h2 className="page-title">
            {data ? `第 ${data.session.week_no} 周 · ${data.session.phase_type}` : "场次逐环节"}
          </h2>
          <p className="page-description">逐环节回看:AI 判定类型/初评分、提示到了第几级、人工是否锁分、锁定分,以及该环节录音回放。AI 判定仅供核查,锁定分才是研究真值。</p>
        </div>
        <Button onClick={onBack}>返回场次列表</Button>
      </header>
      {err && <Alert tone="danger" title="场次日志读取失败">{err}</Alert>}
      {data && data.turns.length === 0 && (data.audios.length === 0
        ? <Alert tone="info" title="该场次暂无逐环节记录">可能尚未开始训练或未产生 turn。</Alert>
        : <Alert tone="info" title="该场次无逐环节评分记录">下方"未配对录音"列出本场已采集但未绑定评分环节的录音(如关系建立段)。</Alert>)}

      {data && data.turns.map((t) => {
        const it = itemById.get(t.item_event_id);
        const audio = (t.raw_audio_id && audioById.get(t.raw_audio_id))
          || (it && audioByKey.get(`${it.item_id}#${t.turn_seq}`));
        if (audio) usedAudioIds.add(audio.raw_audio_id);
        return (
          <section className="analysis-turn-card" key={t.id}>
            <div className="analysis-turn-head">
              <strong>{it ? it.item_id.replace(/^(SE|DE)_/, "") : `#${t.item_event_id}`}
                <span className="muted"> · {t.response_role || "环节"} #{t.turn_seq}</span></strong>
              <div className="row wrap" style={{ gap: 6 }}>
                {t.prompt_level != null && <StatusPill tone="muted" size="sm">提示级 {t.prompt_level}</StatusPill>}
                {t.ai_answer_type && <StatusPill tone="primary" size="sm">AI:{t.ai_answer_type}{t.ai_score != null ? ` (${t.ai_score})` : ""}</StatusPill>}
                {t.ai_needs_review && <StatusPill tone="warn" size="sm">待复核</StatusPill>}
                {t.score_locked
                  ? <StatusPill tone="ok" size="sm">已锁 {t.element_value ?? t.reviewed_score ?? "?"}</StatusPill>
                  : <StatusPill tone="muted" size="sm">未锁分</StatusPill>}
              </div>
            </div>
            <div className="analysis-turn-body">
              <div className="col" style={{ gap: 4 }}>
                {t.asr_text != null && <span><span className="muted">转写:</span>{t.asr_text || <em className="muted">空</em>}</span>}
                {t.confirmed_response_text != null && <span><span className="muted">人工确认:</span>{t.confirmed_response_text}</span>}
                {t.asr_text == null && t.confirmed_response_text == null && <span className="muted">无转写文本</span>}
                {t.reviewer_id && <span className="muted">锁分人 {t.reviewer_id}</span>}
              </div>
              {audio ? <AuthenticatedAudio rawAudioId={audio.raw_audio_id} lazy /> : <StatusPill tone="muted" size="sm">无录音</StatusPill>}
            </div>
          </section>
        );
      })}

      {data && (() => {
        const unbound = data.audios.filter((a) => !usedAudioIds.has(a.raw_audio_id));
        if (unbound.length === 0) return null;
        return (
          <section className="form-section">
            <div className="form-section-header"><div>
              <h3>未配对录音</h3>
              <p className="muted">本场已采集但未绑定评分环节的录音(关系建立段、或采集异常)。含直接标识的须可回看以便撤回。</p>
            </div></div>
            {unbound.map((a) => (
              <section className="analysis-turn-card" key={a.raw_audio_id}>
                <div className="analysis-turn-head">
                  <strong>{a.turn_key || <span className="muted">无环节标识</span>}</strong>
                  <div className="row wrap" style={{ gap: 6 }}>
                    {a.contains_direct_identifier && <StatusPill tone="warn" size="sm">含直接标识</StatusPill>}
                    <StatusPill tone="muted" size="sm">{a.status}</StatusPill>
                  </div>
                </div>
                <div className="analysis-turn-body">
                  <AuthenticatedAudio rawAudioId={a.raw_audio_id} lazy />
                </div>
              </section>
            ))}
          </section>
        );
      })()}

      <AuditPanel sessionId={sessionId} />
      {!data && !err && <StatusPill tone="muted">正在加载场次逐环节…</StatusPill>}
    </div>
  );
}
