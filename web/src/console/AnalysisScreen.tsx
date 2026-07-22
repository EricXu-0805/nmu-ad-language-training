import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { StatusPill } from "../components/StatusPill";
import type { AuditEntry, AuditVerify, AudioAsset, AttemptEvent, InteractionEvent, ItemEvent, PatientSummary, ScaleResult, Session, TurnEvent } from "../types";
import { AuthenticatedAudio } from "./AuthenticatedAudio";
import {
  buildEvidenceTimeline,
  type EvidenceIssue,
  type ParsedInteraction,
  type TurnEvidence,
} from "./analysisEvidence";
import { DataBoundaryBadge, DataBoundaryFilter } from "./DataBoundaryFilter";
import {
  DATA_CLASSIFICATION_META,
  partitionByDataClassification,
  patientDataClassification,
  sessionDataClassification,
  type DataClassification,
} from "./dataClassification";
import { auditActionLabel, auditDisplaySummary } from "./auditPresentation";

// 全局审计链完整性徽标:重算哈希链 + 比对高水位锚点,报告改动/删除。
function IntegrityBadge() {
  const [v, setV] = useState<AuditVerify | null>(null);
  const [err, setErr] = useState<"forbidden" | "unavailable" | null>(null);
  useEffect(() => {
    api.auditVerify().then(setV).catch((error) => {
      setErr(error instanceof ApiError && error.status === 403 ? "forbidden" : "unavailable");
    });
  }, []);
  if (err === "forbidden") {
    return <StatusPill tone="muted" size="sm">需数据管理员校验审计链</StatusPill>;
  }
  if (err === "unavailable") {
    return <StatusPill tone="warn" size="sm">审计链暂时无法校验</StatusPill>;
  }
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
  const [forbidden, setForbidden] = useState(false);
  useEffect(() => {
    api.listAudit({ sessionId }).then(setRows).catch((e) => {
      if (e instanceof ApiError && e.status === 403) {
        setForbidden(true);
        return;
      }
      setErr(e instanceof ApiError ? e.detail : String(e));
    });
  }, [sessionId]);
  return (
    <section className="form-section">
      <div className="form-section-header"><div>
        <h3>操作审计(本场)</h3>
        <p className="muted">只追加、哈希链防篡改;仅记元数据,不含患者作答文本。</p>
      </div></div>
      {forbidden && (
        <Alert tone="info" title="审计元数据由数据管理员查看">
          当前账号可复核 AI 与研究真值，但不显示全局审计账本。
        </Alert>
      )}
      {err && <Alert tone="danger" title="审计读取失败">{err}</Alert>}
      {rows && rows.length === 0 && <p className="muted">本场暂无审计记录。</p>}
      {rows && rows.length > 0 && (
        <div className="audit-list">
          {rows.map((r) => (
            <div className="audit-row" key={r.id}>
              <span className="audit-ts mono">{r.ts.replace("T", " ").slice(0, 19)}</span>
              <StatusPill tone="primary" size="sm">{auditActionLabel(r.action)}</StatusPill>
              <span className="audit-actor">操作者 {r.actor}</span>
              <span className="audit-summary">{auditDisplaySummary(r)}</span>
            </div>
          ))}
        </div>
      )}
      {!rows && !err && !forbidden && <StatusPill tone="muted" size="sm">正在加载审计…</StatusPill>}
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
  const [classificationFilter, setClassificationFilter] = useState<DataClassification>("research");

  useEffect(() => {
    api.listPatients().then(setRows).catch((e) => setErr(e instanceof ApiError ? e.detail : String(e)));
  }, []);

  const sections = partitionByDataClassification(rows ?? [], patientDataClassification);
  const classificationCounts = {
    research: sections.research.length,
    simulation: sections.simulation.length,
    legacy_unknown: sections.legacy_unknown.length,
  };
  const visibleRows = sections[classificationFilter];
  const selectedPatient = rows?.find((row) => row.patient_id === patientId);
  const selectedPatientClassification = selectedPatient
    ? patientDataClassification(selectedPatient)
    : "legacy_unknown";

  if (sessionId && patientId) {
    return <SessionAnalysis patientId={patientId} sessionId={sessionId} onBack={() => setSessionId(null)} />;
  }
  if (patientId) {
    return (
      <PatientAnalysis
        patientId={patientId}
        patientClassification={selectedPatientClassification}
        onOpenSession={setSessionId}
        onBack={() => setPatientId(null)}
      />
    );
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
      {rows && rows.length > 0 && (
        <section className="form-section">
          <div className="form-section-header">
            <div>
              <h3>分析数据分区</h3>
              <p className="muted">默认只进入真实研究分区；模拟与历史未知数据必须单独查看，不会进入默认研究汇总。</p>
            </div>
            <DataBoundaryBadge classification={classificationFilter} entity="patient" />
          </div>
          <DataBoundaryFilter value={classificationFilter} counts={classificationCounts} onChange={setClassificationFilter} label="分析后台数据分区" />
          {classificationFilter === "simulation" && (
            <Alert tone="warn" title="专用模拟数据分析区">本区结果只用于流程和模型调试，不得并入真实研究结果。</Alert>
          )}
          {classificationFilter === "legacy_unknown" && (
            <Alert tone="danger" title="历史/未知数据隔离区">分类缺失的数据只供迁移核查，不能纳入真实研究汇总或结论。</Alert>
          )}
        </section>
      )}
      {rows && rows.length > 0 && visibleRows.length === 0 && (
        <Alert tone="info" title={`${DATA_CLASSIFICATION_META[classificationFilter].label}分区暂无数据`}>
          其他数据分区不会混入当前列表。
        </Alert>
      )}
      {rows && visibleRows.map((r) => {
        const classification = patientDataClassification(r);
        return (
        <button className="analysis-subject-row" key={r.patient_id} onClick={() => setPatientId(r.patient_id)}>
          <strong className="mono">{r.patient_id}</strong>
          <DataBoundaryBadge classification={classification} entity="patient" />
          <span className="muted">{r.session_count} 场{r.last_training_date ? ` · 最近 ${r.last_training_date}` : ""}</span>
          <span aria-hidden>›</span>
        </button>
        );
      })}
      {!rows && !err && <StatusPill tone="muted">正在加载…</StatusPill>}
    </div>
  );
}

function PatientAnalysis({ patientId, patientClassification, onOpenSession, onBack }: {
  patientId: string;
  patientClassification: DataClassification;
  onOpenSession: (sid: string) => void;
  onBack: () => void;
}) {
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [scales, setScales] = useState<ScaleResult[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [sessionFilter, setSessionFilter] = useState<DataClassification>(patientClassification);
  useEffect(() => {
    Promise.all([api.patientSessions(patientId), api.listScales(patientId)])
      .then(([ss, sc]) => { setSessions(ss); setScales(sc); })
      .catch((e) => setErr(e instanceof ApiError ? e.detail : String(e)));
  }, [patientId]);
  const sessionSections = partitionByDataClassification(sessions ?? [], sessionDataClassification);
  const sessionCounts = {
    research: sessionSections.research.length,
    simulation: sessionSections.simulation.length,
    legacy_unknown: sessionSections.legacy_unknown.length,
  };
  const visibleSessions = sessionSections[sessionFilter];
  return (
    <div className="page-shell page-shell--wide">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">分析后台 · <span className="mono">{patientId}</span></p>
          <h2 className="page-title">场次与量表</h2>
          <p className="page-description">当前档案和场次始终按数据分区单独回看，不生成跨分区默认汇总。</p>
        </div>
        <div className="toolbar">
          <DataBoundaryBadge classification={patientClassification} entity="patient" />
          <Button onClick={onBack}>返回受试者列表</Button>
        </div>
      </header>
      {err && <Alert tone="danger" title="读取失败">{err}</Alert>}
      {patientClassification === "simulation" && (
        <Alert tone="warn" title="专用模拟档案">该档案的量表与场次只用于演练核查，不得进入真实研究汇总。</Alert>
      )}
      {patientClassification === "legacy_unknown" && (
        <Alert tone="danger" title="历史档案分类未知">该档案数据只供迁移核查，完成分类前不得进入真实研究汇总。</Alert>
      )}

      <section className="form-section">
        <div className="form-section-header"><div>
          <h3>历史未验证量表迁移记录</h3>
          <p className="muted">这里只回看旧自由字段容器，供迁移核查；它不是正式量表施测证据，也不进入正式研究结局。</p>
        </div></div>
        {scales && scales.length > 0 && (
          <Alert tone="warn" title="历史记录未验证 · 不计正式结局">
            旧记录缺少冻结定义、逐题响应、计分证据和服务器签发的施测身份；只能在当前档案内做迁移核查，不能与研究场次合并统计。
          </Alert>
        )}
        {scales && scales.length === 0 && <p className="muted">暂无量表记录。</p>}
        {scales && scales.length > 0 && (
          <div className="registry-table">
            <div className="registry-row registry-head">
              <span>阶段</span><span>旧称谓</span><span>旧分项</span><span>旧报告分</span><span>未验证来源</span>
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
        <div className="form-section-header">
          <div>
            <h3>训练场次</h3>
            <p className="muted">场次只按服务端 data_classification 分区；缺字段一律进入历史/未知。</p>
          </div>
          <DataBoundaryBadge classification={sessionFilter} />
        </div>
        {sessions && sessions.length > 0 && (
          <DataBoundaryFilter value={sessionFilter} counts={sessionCounts} onChange={setSessionFilter} label="分析场次数据分区" />
        )}
        {sessionFilter === "legacy_unknown" && sessions && sessions.length > 0 && (
          <Alert tone="danger" title="历史/未知场次隔离">这些场次只供迁移核查，不能纳入研究汇总。</Alert>
        )}
        {sessions && sessions.length === 0 && <p className="muted">暂无场次。</p>}
        {sessions && sessions.length > 0 && visibleSessions.length === 0 && <p className="muted">当前分区暂无场次。</p>}
        {sessions && visibleSessions.map((s) => {
          const classification = sessionDataClassification(s);
          return (
          <button className="analysis-subject-row" key={s.session_id} onClick={() => onOpenSession(s.session_id)}>
            <strong>第 {s.week_no} 周 · {s.phase_type}</strong>
            <DataBoundaryBadge classification={classification} />
            <span className="muted">{s.training_date || "日期未记"}{s.session_sitting_no && s.session_sitting_no > 1 ? ` · 第 ${s.session_sitting_no} 次续做` : ""}</span>
            <span aria-hidden>›</span>
          </button>
          );
        })}
        {!sessions && !err && <StatusPill tone="muted">正在加载场次…</StatusPill>}
      </section>
    </div>
  );
}

function SessionAnalysis({ patientId, sessionId, onBack }: {
  patientId: string; sessionId: string; onBack: () => void;
}) {
  const [data, setData] = useState<{
    items: ItemEvent[];
    turns: TurnEvent[];
    audios: AudioAsset[];
    attempts: AttemptEvent[];
    interactions: InteractionEvent[];
    session: Session;
  } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [contractError, setContractError] = useState<string | null>(null);
  const [audioAccess, setAudioAccess] = useState<"checking" | "allowed" | "denied">("checking");
  useEffect(() => {
    api.sessionJournal(sessionId)
      .then((j) => {
        const missing: string[] = [];
        if (!Array.isArray(j.attempts)) missing.push("attempts");
        if (!Array.isArray(j.interactions)) missing.push("interactions");
        setContractError(missing.length > 0
          ? `场次 journal 缺少 ${missing.join("、")} 证据数组；已保留可读的研究真值，但不得宣称 AI 证据链完整。`
          : null);
        setData({
          items: j.items,
          turns: j.turns,
          audios: j.audios,
          attempts: Array.isArray(j.attempts) ? j.attempts : [],
          interactions: Array.isArray(j.interactions) ? j.interactions : [],
          session: j.session,
        });
      })
      .catch((e) => setErr(e instanceof ApiError ? e.detail : String(e)));
  }, [sessionId]);
  useEffect(() => {
    let cancelled = false;
    setAudioAccess("checking");
    api.authMe()
      .then((identity) => {
        if (cancelled) return;
        setAudioAccess(["researcher", "data_steward", "admin"].includes(identity.role) ? "allowed" : "denied");
      })
      .catch(() => { if (!cancelled) setAudioAccess("denied"); });
    return () => { cancelled = true; };
  }, [sessionId]);
  const classification = data ? sessionDataClassification(data.session) : null;
  const timeline = data ? buildEvidenceTimeline(data) : null;

  return (
    <div className="page-shell page-shell--wide">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">分析后台 · <span className="mono">{patientId}</span></p>
          <h2 className="page-title">
            {data ? `第 ${data.session.week_no} 周 · ${data.session.phase_type}` : "场次逐环节"}
          </h2>
          <p className="page-description">按题目、环节和 attempt 回看录音引用、ASR、AI 运营判类、提示/反馈与技术暂停，再与人工锁定的研究真值对照。</p>
        </div>
        <div className="toolbar">
          {classification && <DataBoundaryBadge classification={classification} />}
          <Button onClick={onBack}>返回场次列表</Button>
        </div>
      </header>
      {err && <Alert tone="danger" title="场次日志读取失败">{err}</Alert>}
      {classification === "simulation" && (
        <Alert tone="warn" title="专用模拟场次">本页内容只用于演练和模型核查，不得进入真实研究汇总。</Alert>
      )}
      {classification === "legacy_unknown" && (
        <Alert tone="danger" title="历史场次分类未知">系统未推测该场次类型；完成迁移和人工核对前，不得纳入研究结果。</Alert>
      )}
      <Alert tone="info" title="证据口径">
        <strong>AI operational 判类是自动提示与推进的运营证据，不是研究真值。</strong>
        只有研究者确认文本并锁定的分值才是研究真值；“数值一致”也不代表两者语义等同。
      </Alert>
      {audioAccess === "checking" && <StatusPill tone="muted">正在核对原声回放权限…</StatusPill>}
      {audioAccess === "denied" && (
        <Alert tone="warn" title="原声回放未授权">
          证据元数据仍可核查，但共享 PIN、开放开发模式或无权角色不会显示播放按钮。请使用 researcher、data_steward 或 admin 具名账号登录。
        </Alert>
      )}
      {contractError && <Alert tone="danger" title="AI 证据契约不完整">{contractError}</Alert>}
      {timeline && timeline.issues.length > 0 && <EvidenceIssues issues={timeline.issues} title="场次级证据异常" />}

      {timeline && (
        <section className="form-section evidence-summary">
          <div className="form-section-header"><div>
            <h3>本场证据概览</h3>
            <p className="muted">仅统计当前数据分区中的这一场；不与模拟或历史未知数据混合。</p>
          </div></div>
          <div className="evidence-stat-grid">
            <EvidenceStat label="AI attempts" value={timeline.summary.totalAttempts} />
            <EvidenceStat label="已完成 AI" value={timeline.summary.completedAttempts} tone="ok" />
            <EvidenceStat label="技术失败" value={timeline.summary.technicalFailures} tone={timeline.summary.technicalFailures ? "danger" : "muted"} />
            <EvidenceStat label="未完成" value={timeline.summary.incompleteAttempts} tone={timeline.summary.incompleteAttempts ? "warn" : "muted"} />
            <EvidenceStat label="已绑 source" value={timeline.summary.sourceBoundTurns} />
            <EvidenceStat label="人工真值" value={timeline.summary.lockedTruths} tone="ok" />
            <EvidenceStat label="严重证据问题" value={timeline.summary.dangerIssues} tone={timeline.summary.dangerIssues ? "danger" : "ok"} />
            <EvidenceStat label="待核查" value={timeline.summary.warningIssues} tone={timeline.summary.warningIssues ? "warn" : "muted"} />
          </div>
        </section>
      )}

      {timeline && timeline.turns.length === 0 && (timeline.unboundAudios.length === 0
        ? <Alert tone="info" title="该场次暂无逐次证据">尚未产生 attempt、Turn 或定位到环节的 interaction。</Alert>
        : <Alert tone="warn" title="只找到未绑定录音">未找到可与环节对齐的 attempt/Turn，下方仅列出原始音频资产。</Alert>)}

      {timeline?.turns.map((row) => <EvidenceTurnCard key={row.key} row={row} canPlayAudio={audioAccess === "allowed"} />)}

      {timeline && timeline.sessionInteractions.length > 0 && (
        <section className="form-section">
          <div className="form-section-header"><div>
            <h3>场次级交互事件</h3>
            <p className="muted">这些事件未绑定到具体题目/环节，不会被静默塞入某个 attempt。</p>
          </div></div>
          <InteractionTimeline rows={timeline.sessionInteractions} />
        </section>
      )}

      {timeline && timeline.unboundAudios.length > 0 && (
        <section className="form-section">
          <div className="form-section-header"><div>
            <h3>未配对录音</h3>
            <p className="muted">已采集但没有被 attempt 或 Turn 引用，可能来自关系建立段或采集异常。</p>
          </div></div>
          {timeline.unboundAudios.map((audio) => <AudioEvidence key={audio.raw_audio_id} audio={audio} canPlay={audioAccess === "allowed"} />)}
        </section>
      )}

      <AuditPanel sessionId={sessionId} />
      {!data && !err && <StatusPill tone="muted">正在加载场次逐环节…</StatusPill>}
    </div>
  );
}

function EvidenceStat({ label, value, tone = "muted" }: {
  label: string;
  value: number;
  tone?: "ok" | "warn" | "danger" | "muted";
}) {
  return <div className="evidence-stat"><span>{label}</span><StatusPill tone={tone}>{value}</StatusPill></div>;
}

function EvidenceIssues({ issues, title = "证据问题" }: { issues: EvidenceIssue[]; title?: string }) {
  if (issues.length === 0) return null;
  const danger = issues.some((row) => row.tone === "danger");
  return (
    <Alert tone={danger ? "danger" : "warn"} title={title} compact>
      <ul className="evidence-issue-list">
        {issues.map((row, index) => <li key={`${row.code}-${index}`}>{row.message}</li>)}
      </ul>
    </Alert>
  );
}

function EvidenceTurnCard({ row, canPlayAudio }: { row: TurnEvidence; canPlayAudio: boolean }) {
  const turn = row.turn;
  const researchValue = turn?.element_value ?? turn?.reviewed_score;
  const corrected = turn?.confirmed_response_text != null && turn.confirmed_response_text !== turn.asr_text;
  return (
    <section className="analysis-turn-card evidence-turn-card">
      <div className="analysis-turn-head">
        <div>
          <span className="page-kicker">{row.item?.task_type ?? "题目类型未知"}</span>
          <strong>{row.itemId.replace(/^(SE|DE)_/, "")}
            <span className="muted"> · {row.responseRole ?? "环节角色缺失"} #{row.turnSeq}</span>
          </strong>
        </div>
        <div className="row wrap" style={{ gap: 6 }}>
          {!turn
            ? <StatusPill tone="warn" size="sm">未生成 Turn</StatusPill>
            : turn.source_attempt_id != null
              ? <StatusPill tone="primary" size="sm">source attempt #{turn.source_attempt_id}</StatusPill>
              : <StatusPill tone="danger" size="sm">Turn 无 source attempt</StatusPill>}
          {turn?.score_locked
            ? <StatusPill tone="ok" size="sm">研究真值 {researchValue ?? "缺失"}</StatusPill>
            : <StatusPill tone="warn" size="sm">研究真值未锁定</StatusPill>}
        </div>
      </div>
      <EvidenceIssues issues={row.issues} />

      <div className="evidence-truth-panel">
        <div className="evidence-section-title">
          <strong>人工研究真值</strong>
          <StatusPill tone={turn?.score_locked ? "ok" : "warn"} size="sm">{turn?.score_locked ? "已锁定" : "未锁定"}</StatusPill>
        </div>
        {turn ? (
          <div className="evidence-detail-grid">
            <EvidenceValue label="Turn / source" value={`#${turn.id} / ${turn.source_attempt_id != null ? `#${turn.source_attempt_id}` : "缺失"}`} mono />
            <EvidenceValue label="人工锁分" value={turn.score_locked ? String(researchValue ?? "分值缺失") : "尚未锁定"} />
            <EvidenceValue label="锁分人" value={turn.reviewer_id ?? "未记录"} />
            <EvidenceValue label="最高提示" value={turn.prompt_level != null ? `${turn.prompt_level} 级${turn.cue_type ? ` · ${turn.cue_type}` : ""}` : "未记录"} />
            <EvidenceValue label="权威 ASR 原文" value={turn.asr_text ?? "缺失"} />
            <EvidenceValue label="人工确认文本" value={turn.confirmed_response_text ?? "未确认"} marker={corrected ? "已校正 ASR" : undefined} />
          </div>
        ) : <p className="muted">尚未生成 Turn，因此没有可用的人工研究真值。</p>}
        <p className={`evidence-comparison is-${row.comparison.state}`}>{row.comparison.message}</p>
      </div>

      <div className="evidence-attempt-section">
        <div className="evidence-section-title">
          <strong>AI operational 逐次证据</strong>
          <span className="muted">{row.attempts.length} 次 · 不是研究真值</span>
        </div>
        {row.attempts.length === 0 && <Alert tone="danger" title="缺少 attempt 证据">该环节没有可回溯的录音→ASR→AI operational 处理链。</Alert>}
        {row.attempts.map((entry) => {
          const attempt = entry.attempt;
          const statusTone = attempt.processing_status === "completed" ? "ok"
            : attempt.processing_status === "technical_failure" ? "danger" : "warn";
          return (
            <article className={`evidence-attempt is-${attempt.processing_status}`} key={attempt.id}>
              <div className="analysis-turn-head">
                <strong>Attempt #{attempt.attempt_seq} <span className="mono muted">ID {attempt.id}</span></strong>
                <div className="row wrap" style={{ gap: 6 }}>
                  {entry.isTurnSource && <StatusPill tone="primary" size="sm">Turn 终值来源</StatusPill>}
                  <StatusPill tone={statusTone} size="sm">{attempt.processing_status}</StatusPill>
                  {attempt.operational_needs_review && <StatusPill tone="warn" size="sm">AI 建议复核</StatusPill>}
                </div>
              </div>
              <div className="muted evidence-attempt-meta">
                <span>开始 {formatEvidenceTime(attempt.created_at)}</span>
                <span>完成 {formatEvidenceTime(attempt.processed_at)}</span>
                <span>提示 {attempt.prompt_level} 级{attempt.cue_type ? ` · ${attempt.cue_type}` : ""}</span>
              </div>
              <EvidenceIssues issues={entry.issues} title={attempt.processing_status === "technical_failure" ? "技术失败（非作答错误）" : "attempt 证据问题"} />
              <div className="evidence-detail-grid evidence-detail-grid--attempt">
                <EvidenceValue label="录音引用" value={attempt.raw_audio_id} mono />
                <EvidenceValue label="时长" value={attempt.duration_seconds != null ? `${attempt.duration_seconds.toFixed(1)}s` : "未记录"} />
                <EvidenceValue label="ASR 引擎" value={attempt.asr_engine_version ?? "缺失"} mono />
                <EvidenceValue label="ASR 置信度" value={attempt.asr_confidence != null ? String(attempt.asr_confidence) : "未记录"} />
                <EvidenceValue label="ASR 原文" value={attempt.asr_text ?? "无权威转写"} />
                <EvidenceValue label="AI operational 类型" value={attempt.operational_answer_type ?? "未形成"} marker="仅运营决策" />
                <EvidenceValue label="AI operational 分" value={attempt.operational_score != null ? String(attempt.operational_score) : "不适用/缺失"} />
                <EvidenceValue label="判类引擎" value={[attempt.judge_mode, attempt.judge_engine_version].filter(Boolean).join(" · ") || "缺失"} mono />
                <EvidenceValue label="理由" value={attempt.judge_reason ?? "未提供（规则引擎可为空）"} />
                <EvidenceValue label="匹配依据" value={attempt.matched_on ?? "未记录"} mono />
                <EvidenceValue label="包含完整目标词" value={attempt.contains_target == null ? "未记录" : attempt.contains_target ? "是" : "否"} />
                <EvidenceValue label="错误码" value={attempt.error_code ?? "无"} mono />
              </div>
              {entry.audio ? <AudioEvidence audio={entry.audio} compact canPlay={canPlayAudio} /> : <StatusPill tone="danger" size="sm">录音资产缺失</StatusPill>}
              <InteractionTimeline rows={entry.interactions} empty="未找到与本 attempt 关联的 interaction 时间线。" />
            </article>
          );
        })}
      </div>

      {row.interactions.length > 0 && (
        <div className="evidence-loose-events">
          <div className="evidence-section-title"><strong>环节交互（未绑定 attempt）</strong></div>
          <InteractionTimeline rows={row.interactions} />
        </div>
      )}
    </section>
  );
}

function EvidenceValue({ label, value, mono = false, marker }: {
  label: string;
  value: string;
  mono?: boolean;
  marker?: string;
}) {
  return (
    <div className="evidence-value">
      <span>{label}</span>
      <strong className={mono ? "mono" : undefined}>{value}</strong>
      {marker && <small>{marker}</small>}
    </div>
  );
}

function InteractionTimeline({ rows, empty }: { rows: ParsedInteraction[]; empty?: string }) {
  if (rows.length === 0) return empty ? <p className="muted">{empty}</p> : null;
  return (
    <ol className="evidence-timeline">
      {rows.map((row) => {
        const danger = row.event.event_type.endsWith("_failed") || row.event.event_type === "technical_pause" || row.payloadError;
        return (
          <li key={row.event.id} className={danger ? "is-danger" : undefined}>
            <span className="evidence-timeline-dot" aria-hidden />
            <div>
              <span className="mono muted">#{row.event.event_seq} · {formatEvidenceTime(row.event.created_at)}</span>
              <strong>{row.payloadError ? `${row.summary}（载荷损坏）` : row.summary}</strong>
              <small className="mono">{row.event.event_type}</small>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function AudioEvidence({ audio, compact = false, canPlay }: { audio: AudioAsset; compact?: boolean; canPlay: boolean }) {
  return (
    <div className={`evidence-audio${compact ? " is-compact" : ""}`}>
      <div className="row wrap" style={{ gap: 6 }}>
        <DataBoundaryBadge classification={sessionDataClassification(audio)} />
        <StatusPill tone="muted" size="sm">{audio.status}</StatusPill>
        {audio.contains_direct_identifier && <StatusPill tone="warn" size="sm">含直接标识</StatusPill>}
        <span className="mono muted">{audio.raw_audio_id}</span>
      </div>
      {!compact && <span className="muted">{audio.turn_key ?? "无 turn_key"} · {audio.audio_format}{audio.checksum ? " · checksum 已登记" : " · checksum 缺失"}</span>}
      <p className="muted evidence-audio-note">原声仅在具名有权账号显式点击后临时请求；服务端每次重查权限并记审计，响应禁止缓存，离开组件即释放临时地址。</p>
      {canPlay
        ? <AuthenticatedAudio rawAudioId={audio.raw_audio_id} lazy />
        : <StatusPill tone="warn" size="sm">需具名授权账号才能显式点播</StatusPill>}
    </div>
  );
}

function formatEvidenceTime(value: string | null | undefined): string {
  if (!value) return "未记录";
  return value.replace("T", " ").slice(0, 19);
}
