import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { StatusPill } from "../components/StatusPill";
import type { AuditEntry, AuditVerify, AudioAsset, AttemptEvent, InteractionEvent, ItemEvent, PatientSummary, ScaleResult, Session, TurnEvent } from "../types";
import { AuthenticatedAudio } from "./AuthenticatedAudio";
import {
  buildEvidenceTimeline,
  processingStatusLabel,
  type EvidenceIssue,
  type ParsedInteraction,
  type TurnEvidence,
} from "./analysisEvidence";
import { DataBoundaryBadge, DataBoundaryFilter } from "./DataBoundaryFilter";
import {
  parseConfirmationRevisionsByTurn,
  parseRapportUtteranceList,
  parseTtsServeEvidenceList,
} from "./sessionAiEvidenceContract";
import {
  buildAiUsageSection,
  buildConfirmationRevisionSection,
  buildRapportReplaySection,
  buildTtsServeEvidenceSection,
  sessionAiEvidenceEmptyNotice,
  type AiUsageViewModel,
  type ConfirmationRevisionSectionViewModel,
  type ConfirmationRevisionTurnViewModel,
  type RapportReplaySectionViewModel,
  type TtsServeEvidenceSectionViewModel,
} from "./sessionAiEvidenceViewModel";
import { AIQualityDashboard } from "./quality/AIQualityDashboard";
import { qualityDashboardRequestClassification } from "./quality/qualityDashboardRequestPolicy";
import {
  qualityRetryDeadlineMs,
  qualityRetryRemainingSeconds,
} from "./quality/qualityDashboardRetryPolicy";
import {
  buildAIQualityDashboardViewModel,
  type AIQualityDashboardViewModel,
} from "./quality/qualityDashboardViewModel";
import { AI_QUALITY_RELEASE_SCHEMA_VERSION } from "./quality/qualityReleaseContract";
import { buildAIQualityReleaseViewModel } from "./quality/qualityReleaseViewModel";
import {
  DATA_CLASSIFICATION_META,
  partitionByDataClassification,
  patientDataClassification,
  sessionDataClassification,
  type DataClassification,
} from "./dataClassification";
import { auditActionLabel, auditDisplaySummary } from "./auditPresentation";
import { ResearchDataScreen } from "./research/ResearchDataScreen";

type QualityRequestState = {
  classification: "research" | "simulation";
  status: "loading" | "forbidden";
} | {
  classification: "research" | "simulation";
  status: "error";
  retryAtMs: number | null;
} | {
  classification: "research" | "simulation";
  status: "ready";
  model: AIQualityDashboardViewModel;
};

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
  return <StatusPill tone="warn" size="sm" title={String(v.problem)}>⚠ 审计记录异常,请联系数据管理员</StatusPill>;
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
        <p className="muted">只记录操作人、时间和动作,不含患者作答内容,记录不可修改。</p>
      </div></div>
      {forbidden && (
        <Alert tone="info" title="审计记录由数据管理员查看">
          审计记录需数据管理员账号查看。
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
  const [researchDataOpen, setResearchDataOpen] = useState(false);
  const [qualityState, setQualityState] = useState<QualityRequestState | null>(null);
  const [qualityReloadToken, setQualityReloadToken] = useState(0);
  const [qualityNowMs, setQualityNowMs] = useState(() => Date.now());

  useEffect(() => {
    api.listPatients().then(setRows).catch((e) => setErr(e instanceof ApiError ? e.detail : String(e)));
  }, []);

  useEffect(() => {
    const classification = qualityDashboardRequestClassification(classificationFilter);
    if (classification === null) {
      setQualityState(null);
      return;
    }
    let current = true;
    const controller = new AbortController();
    setQualityState({ classification, status: "loading" });
    api.getAIQualityMetrics(classification, controller.signal)
      .then((contract) => {
        if (!current) return;
        setQualityState({
          classification,
          status: "ready",
          model: contract.schema_version === AI_QUALITY_RELEASE_SCHEMA_VERSION
            ? buildAIQualityReleaseViewModel(contract)
            : buildAIQualityDashboardViewModel(contract),
        });
      })
      .catch((error: unknown) => {
        if (!current || (error instanceof DOMException && error.name === "AbortError")) return;
        const nowMs = Date.now();
        setQualityNowMs(nowMs);
        if (error instanceof ApiError && error.status === 403) {
          setQualityState({ classification, status: "forbidden" });
        } else {
          setQualityState({
            classification,
            status: "error",
            retryAtMs: qualityRetryDeadlineMs(error, nowMs),
          });
        }
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [classificationFilter, qualityReloadToken]);

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
  const qualityClassification = qualityDashboardRequestClassification(classificationFilter);
  const currentQualityState = qualityClassification !== null
    && qualityState?.classification === qualityClassification
    ? qualityState
    : null;
  const qualityRetryAtMs = currentQualityState?.status === "error"
    ? currentQualityState.retryAtMs
    : null;
  const qualityRetrySeconds = qualityRetryRemainingSeconds(qualityRetryAtMs, qualityNowMs);

  useEffect(() => {
    if (qualityRetryAtMs === null) return;
    const updateClock = () => {
      const nowMs = Date.now();
      setQualityNowMs(nowMs);
      if (nowMs >= qualityRetryAtMs) window.clearInterval(timer);
    };
    const timer = window.setInterval(updateClock, 250);
    updateClock();
    return () => window.clearInterval(timer);
  }, [qualityRetryAtMs]);

  if (researchDataOpen) {
    return <ResearchDataScreen onBack={() => setResearchDataOpen(false)} />;
  }
  if (sessionId && patientId) {
    // key 强制按 exact patient+session 重新挂载:换场次绝不能在 effect 清空前的
    // 那一次 render 里,把 A 场次的 journal/usage state 短暂配上 B 的标题渲染出来。
    return (
      <SessionAnalysis
        key={`${patientId}:${sessionId}`}
        patientId={patientId}
        sessionId={sessionId}
        onBack={() => setSessionId(null)}
      />
    );
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
          <p className="page-description">点受试者编号,回看每场的 AI 判定、提示、锁分和录音。</p>
        </div>
        <IntegrityBadge />
      </header>
      <section className="form-section">
        <div className="form-section-header">
          <div>
            <h3>去标识研究数据</h3>
            <p className="muted">受试者、场次、逐环节三张表,可翻页查看、可导出 CSV,只向数据管理员与管理员开放。</p>
          </div>
          <Button onClick={() => setResearchDataOpen(true)}>打开研究数据总览</Button>
        </div>
      </section>
      {err && <Alert tone="danger" title="受试者列表加载失败">{err}</Alert>}
      {rows && rows.length === 0 && !err && <Alert tone="info" title="暂无数据">还没有登记受试者或采集数据。</Alert>}
      {rows && rows.length > 0 && (
        <section className="form-section">
          <div className="form-section-header">
            <div>
              <h3>分析数据分区</h3>
              <p className="muted">默认显示真实研究数据;模拟与历史数据需切换查看。</p>
            </div>
            <DataBoundaryBadge classification={classificationFilter} entity="patient" />
          </div>
          <DataBoundaryFilter value={classificationFilter} counts={classificationCounts} onChange={setClassificationFilter} label="分析后台数据分区" />
          {classificationFilter === "simulation" && (
            <Alert tone="warn" title="专用模拟数据分析区">模拟数据,不计入研究结果。</Alert>
          )}
          {classificationFilter === "legacy_unknown" && (
            <Alert tone="danger" title="历史/未知数据隔离区">分类缺失的数据只供迁移核查，不能纳入真实研究汇总或结论。</Alert>
          )}
        </section>
      )}
      {rows && rows.length > 0 && visibleRows.length === 0 && (
        <Alert tone="info" title={`${DATA_CLASSIFICATION_META[classificationFilter].label}分区暂无数据`}>
          可在上方切换分区查看。
        </Alert>
      )}
      {qualityClassification === null && (
        <Alert tone="info" title="历史/未知分区不请求 AI 质量汇总">
          该分区数据分类缺失,不提供 AI 质量汇总。
        </Alert>
      )}
      {qualityClassification !== null
        && (currentQualityState === null || currentQualityState.status === "loading") && (
        <section className="form-section" aria-label="AI 质量汇总加载状态">
          <StatusPill role="status" tone="muted">正在加载当前分区 AI 质量总览…</StatusPill>
        </section>
      )}
      {currentQualityState?.status === "forbidden" && (
        <Alert tone="warn" title="当前账号无权查看 AI 质量汇总">
          AI 质量汇总需授权研究账号查看。
        </Alert>
      )}
      {currentQualityState?.status === "error" && (
        <Alert
          tone="danger"
          title="AI 质量汇总读取失败"
          actions={(
            <Button
              disabled={qualityRetrySeconds > 0}
              onClick={() => setQualityReloadToken((token) => token + 1)}
            >
              {qualityRetrySeconds > 0 ? `${qualityRetrySeconds} 秒后可重试` : "重试质量汇总"}
            </Button>
          )}
        >
          {qualityRetrySeconds > 0
            ? "服务器繁忙,请等倒计时结束后重试。"
            : "读取失败,已停止显示。请稍后重试。"}
          <details className="muted">
            <summary>技术详情</summary>
            {qualityRetrySeconds > 0
              ? "服务器已要求暂缓质量查询；倒计时结束前不会重复请求，也不会沿用上一分区数据。"
              : "服务器不可用，或返回的数据未通过隐私校验；已拒绝显示，不会沿用上一分区数据。"}
          </details>
        </Alert>
      )}
      {currentQualityState?.status === "ready" && (
        <AIQualityDashboard
          headingId={`ai-quality-dashboard-${currentQualityState.classification}`}
          model={currentQualityState.model}
        />
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
          <p className="muted">旧系统迁移来的量表记录,仅供核查,不计入研究结果。</p>
        </div></div>
        {scales && scales.length > 0 && (
          <Alert tone="warn" title="历史记录未验证 · 不计正式结局">
            旧记录缺少完整施测证据,不能与研究数据合并统计。
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
    ttsServesRaw: unknown;
    rapportUtterancesRaw: unknown;
    confirmationRevisionsRaw: unknown;
  } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [contractError, setContractError] = useState<string | null>(null);
  const [audioAccess, setAudioAccess] = useState<"checking" | "allowed" | "denied">("checking");
  useEffect(() => {
    let current = true;
    const controller = new AbortController();
    setData(null);
    setErr(null);
    setContractError(null);
    api.sessionJournal(sessionId, controller.signal)
      .then((j) => {
        if (!current) return;
        // exact-session fence:晚到的响应必须严格核对本次请求的 sid;撤回墓碑
        // 因隐私不携带 patient_id,只能核对 sid,不能与当前受试者做进一步核对。
        if (j.session.session_id !== sessionId) {
          setErr("数据与当前场次不符,未显示。请返回重进。");
          return;
        }
        if (j.session.content_state !== "withdrawn_tombstone" && j.session.patient_id !== patientId) {
          setErr("数据与当前受试者不符,未显示。请返回重进。");
          return;
        }
        const missing: string[] = [];
        if (!Array.isArray(j.attempts)) missing.push("attempts");
        if (!Array.isArray(j.interactions)) missing.push("interactions");
        setContractError(missing.length > 0
          ? "本场部分 AI 过程记录缺失;人工锁定的研究数据仍可查看。"
          : null);
        setData({
          items: j.items,
          turns: j.turns,
          audios: j.audios,
          attempts: Array.isArray(j.attempts) ? j.attempts : [],
          interactions: Array.isArray(j.interactions) ? j.interactions : [],
          session: j.session,
          ttsServesRaw: j.tts_serves,
          rapportUtterancesRaw: j.rapport_utterances,
          confirmationRevisionsRaw: j.confirmation_revisions,
        });
      })
      .catch((e: unknown) => {
        if (!current || (e instanceof DOMException && e.name === "AbortError")) return;
        setErr(e instanceof ApiError ? e.detail : String(e));
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [sessionId, patientId]);
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

  const [aiUsageState, setAiUsageState] = useState<AiUsageViewModel>({ status: "loading" });
  useEffect(() => {
    let current = true;
    const controller = new AbortController();
    setAiUsageState({ status: "loading" });
    api.getSessionAiUsage(sessionId, controller.signal)
      .then((contract) => {
        if (!current) return;
        setAiUsageState({ status: "ready", model: buildAiUsageSection(contract) });
      })
      .catch((error: unknown) => {
        if (!current || (error instanceof DOMException && error.name === "AbortError")) return;
        if (error instanceof ApiError) {
          const detailData = error.detailData;
          const code = detailData !== null && typeof detailData === "object" && !Array.isArray(detailData)
            ? (detailData as { code?: unknown }).code
            : undefined;
          if (error.status === 409 && code === "subject_withdrawn_content_unavailable") {
            setAiUsageState({ status: "withdrawn" });
          } else if (error.status === 403) {
            setAiUsageState({ status: "forbidden" });
          } else {
            setAiUsageState({ status: "network-error", message: error.detail });
          }
          return;
        }
        setAiUsageState({ status: "contract-error", message: error instanceof Error ? error.message : String(error) });
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [sessionId]);

  const classification = data ? sessionDataClassification(data.session) : null;
  const withdrawn = data?.session.content_state === "withdrawn_tombstone";
  // 撤回墓碑没有真实 item/turn/attempt 内容,绝不能喂进逐次证据时间线——
  // 那会渲染出 0 次统计或"第 undefined 周"这类假信息。
  const timeline = data && !withdrawn ? buildEvidenceTimeline(data) : null;

  const ttsSection: TtsServeEvidenceSectionViewModel | null = useMemo(() => {
    if (!data) return null;
    if (withdrawn) return { status: "withdrawn" };
    try {
      const records = parseTtsServeEvidenceList(data.ttsServesRaw, data.session.session_id, data.session.is_simulation);
      return buildTtsServeEvidenceSection(records);
    } catch (error) {
      return { status: "contract-error", message: error instanceof Error ? error.message : String(error) };
    }
  }, [data, withdrawn]);

  const rapportSection: RapportReplaySectionViewModel | null = useMemo(() => {
    if (!data) return null;
    if (withdrawn) return { status: "withdrawn" };
    // 旧后端(部署窗口/回滚期)的 journal 没有 rapport_utterances 键:按"无记录"
    // 处理并整块隐藏,不能把它当契约损坏在所有场次顶出红警。
    if (data.rapportUtterancesRaw === undefined) return { status: "empty" };
    try {
      const utterances = parseRapportUtteranceList(
        data.rapportUtterancesRaw, data.session.session_id, data.session.is_simulation);
      const serves = parseTtsServeEvidenceList(
        data.ttsServesRaw, data.session.session_id, data.session.is_simulation);
      return buildRapportReplaySection(utterances, serves);
    } catch (error) {
      return { status: "contract-error", message: error instanceof Error ? error.message : String(error) };
    }
  }, [data, withdrawn]);

  const confirmationSection: ConfirmationRevisionSectionViewModel | null = useMemo(() => {
    if (!data) return null;
    if (withdrawn) return { status: "withdrawn" };
    try {
      const byTurn = parseConfirmationRevisionsByTurn(
        data.confirmationRevisionsRaw,
        data.turns.map((turn) => ({ id: turn.id, confirmation_revision: turn.confirmation_revision })),
      );
      const hasConfirmedTextByTurnId = new Map(
        data.turns.map((turn) => [turn.id, Boolean(turn.confirmed_response_text?.trim())] as const),
      );
      return buildConfirmationRevisionSection(byTurn, hasConfirmedTextByTurnId);
    } catch (error) {
      return { status: "contract-error", message: error instanceof Error ? error.message : String(error) };
    }
  }, [data, withdrawn]);

  const confirmationByTurnId: Map<number, ConfirmationRevisionTurnViewModel> | null =
    confirmationSection?.status === "ready"
      ? new Map(confirmationSection.turns.map((turn) => [turn.turnId, turn]))
      : null;

  return (
    <div className="page-shell page-shell--wide">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">分析后台 · <span className="mono">{patientId}</span></p>
          <h2 className="page-title">
            {withdrawn ? "场次内容已撤回" : data ? `第 ${data.session.week_no} 周 · ${data.session.phase_type}` : "场次逐环节"}
          </h2>
          <p className="page-description">按题目和环节回看录音、转写、AI 判定与提示,并与人工锁分对照。</p>
        </div>
        <div className="toolbar">
          {classification && !withdrawn && <DataBoundaryBadge classification={classification} />}
          <Button onClick={onBack}>返回场次列表</Button>
        </div>
      </header>
      {err && <Alert tone="danger" title="场次日志读取失败">{err}</Alert>}
      {withdrawn && (
        <Alert tone="warn" title="受试者内容已撤回">
          受试者已撤回,本场内容依隐私要求关闭查看。
        </Alert>
      )}
      {!withdrawn && (
        <>
          {classification === "simulation" && (
            <Alert tone="warn" title="专用模拟场次">本页内容只用于演练和模型核查，不得进入真实研究汇总。</Alert>
          )}
          {classification === "legacy_unknown" && (
            <Alert tone="danger" title="历史场次分类未知">系统未推测该场次类型；完成迁移和人工核对前，不得纳入研究结果。</Alert>
          )}
          <Alert tone="info" title="怎么读本页数据">
            AI 判定仅供参考;研究结果以人工确认并锁定的分值为准。
          </Alert>
          {audioAccess === "checking" && <StatusPill tone="muted">正在核对原声回放权限…</StatusPill>}
          {audioAccess === "denied" && (
            <Alert tone="warn" title="原声回放未授权">
              当前登录方式不能播放录音。请用研究者或数据管理员的个人账号重新登录。
            </Alert>
          )}
          {audioAccess === "allowed" && <p className="muted">播放录音需点击,每次播放都会记录审计。</p>}
          {contractError && <Alert tone="danger" title="AI 过程记录不完整">{contractError}</Alert>}
          {timeline && timeline.issues.length > 0 && <EvidenceIssues issues={timeline.issues} title="场次级证据异常" />}

          {timeline && (
            <section className="form-section evidence-summary">
              <div className="form-section-header"><div>
                <h3>本场证据概览</h3>
              </div></div>
              <div className="evidence-stat-grid">
                <EvidenceStat label="AI 处理次数" value={timeline.summary.totalAttempts} />
                <EvidenceStat label="已完成 AI" value={timeline.summary.completedAttempts} tone="ok" />
                <EvidenceStat label="技术失败" value={timeline.summary.technicalFailures} tone={timeline.summary.technicalFailures ? "danger" : "muted"} />
                <EvidenceStat label="未完成" value={timeline.summary.incompleteAttempts} tone={timeline.summary.incompleteAttempts ? "warn" : "muted"} />
                <EvidenceStat label="已关联来源" value={timeline.summary.sourceBoundTurns} />
                <EvidenceStat label="人工锁分" value={timeline.summary.lockedTruths} tone="ok" />
                <EvidenceStat label="严重证据问题" value={timeline.summary.dangerIssues} tone={timeline.summary.dangerIssues ? "danger" : "ok"} />
                <EvidenceStat label="待核查" value={timeline.summary.warningIssues} tone={timeline.summary.warningIssues ? "warn" : "muted"} />
              </div>
            </section>
          )}

          {rapportSection && (rapportSection.status !== "empty" || data?.session.week_no === 1)
            && <RapportReplaySectionView section={rapportSection} />}
          {ttsSection && <TtsServeEvidenceSectionView section={ttsSection} />}
          {confirmationSection?.status === "contract-error" && (
            <Alert tone="danger" title="修订历史无法核实">
              修订历史未通过完整性检查,已停止显示;人工确认文本与锁分仍可查看。
              <details className="muted"><summary>技术详情</summary>{confirmationSection.message}</details>
            </Alert>
          )}
          {data && <AiUsageSectionView state={aiUsageState} />}

          {timeline && timeline.turns.length === 0 && (timeline.unboundAudios.length === 0
            ? <Alert tone="info" title="该场次暂无逐次证据">本场尚未产生逐次作答记录。</Alert>
            : <Alert tone="warn" title="只找到未对应的录音">录音未能对应到题目环节,下方仅列出原始录音。</Alert>)}

          {timeline?.turns.map((row) => (
            <EvidenceTurnCard
              key={row.key}
              row={row}
              canPlayAudio={audioAccess === "allowed"}
              confirmation={row.turn ? confirmationByTurnId?.get(row.turn.id) : undefined}
            />
          ))}

          {timeline && timeline.sessionInteractions.length > 0 && (
            <section className="form-section">
              <div className="form-section-header"><div>
                <h3>场次级交互事件</h3>
                <p className="muted">以下事件未对应到具体题目。</p>
              </div></div>
              <InteractionTimeline rows={timeline.sessionInteractions} />
            </section>
          )}

          {timeline && timeline.unboundAudios.length > 0 && (
            <section className="form-section">
              <div className="form-section-header"><div>
                <h3>未配对录音</h3>
                <p className="muted">已录音但未对应到题目(可能来自关系建立环节)。</p>
              </div></div>
              {timeline.unboundAudios.map((audio) => <AudioEvidence key={audio.raw_audio_id} audio={audio} canPlay={audioAccess === "allowed"} />)}
            </section>
          )}
        </>
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

// 场次级 TTS 服务端实际返回/降级证据。served 只证明服务器把音频字节交回了请求方；
// 终端侧音频消费情况属于 T5 的 started/ended playback ACK，本区不涉及。
export function TtsServeEvidenceSectionView({ section }: { section: TtsServeEvidenceSectionViewModel }) {
  return (
    <section className="form-section" aria-label="本场语音返回记录">
      <div className="form-section-header"><div>
        <h3>语音（TTS）返回记录（本场）</h3>
        <p className="muted">这里只记录服务器是否返回了语音,不代表老人设备上已实际播放。</p>
      </div></div>
      {section.status === "contract-error" && (
        <Alert tone="danger" title="语音返回记录异常">
          数据未通过完整性检查,已停止显示。
          <details className="muted"><summary>技术详情</summary>{section.message}</details>
        </Alert>
      )}
      {section.status === "empty" && <Alert tone="info" title="暂无 TTS 服务证据">{section.notice}</Alert>}
      {section.status === "ready" && (
        <>
          <div className="evidence-stat-grid">
            <EvidenceStat label="已实际返回" value={section.summary.served} tone="ok" />
            <EvidenceStat label="缓存命中" value={section.summary.cacheHits} />
            <EvidenceStat label="已降级" value={section.summary.degraded} tone={section.summary.degraded ? "warn" : "muted"} />
          </div>
          <div className="evidence-attempt-section">
            {section.rows.map((row) => (
              <article className="evidence-attempt" key={row.key}>
                <div className="analysis-turn-head">
                  <strong className="mono">{row.time}</strong>
                  <StatusPill tone={row.resultTone} size="sm">{row.resultLabel}</StatusPill>
                </div>
                <div className="evidence-detail-grid evidence-detail-grid--attempt">
                  <EvidenceValue label="来源" value={row.sourceLabel} />
                  <EvidenceValue label="指令" value={row.commandLabel} mono />
                  <EvidenceValue label="引擎" value={row.engineVersion} mono />
                  <EvidenceValue label="缓存" value={row.cacheLabel} />
                  <EvidenceValue label="字节" value={row.byteLabel} mono />
                </div>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

// 第1周对话回放:发声账本(定稿待说) × 语音返回记录(是否说出)合看。
export function RapportReplaySectionView({ section }: { section: RapportReplaySectionViewModel }) {
  if (section.status === "withdrawn") return null;
  return (
    <section className="form-section" aria-label="第1周对话回放">
      <div className="form-section-header"><div>
        <h3>第 1 周对话回放（机器人说过的每句话）</h3>
        <p className="muted">按发生顺序列出机器人在回应拍说的话：谁触发的、句子从哪来、服务器是否真返回了语音。</p>
      </div></div>
      {section.status === "contract-error" && (
        <Alert tone="danger" title="对话回放记录异常">
          数据未通过完整性检查,已停止显示。
          <details className="muted"><summary>技术详情</summary>{section.message}</details>
        </Alert>
      )}
      {section.status === "empty" && (
        <Alert tone="info" title="本场没有机器人回应记录">
          本场次没有记录到机器人回应（2026-08-31 上线发声记录之前的场次没有这项数据）。
        </Alert>
      )}
      {section.status === "ready" && (
        <>
          <div className="evidence-stat-grid">
            <EvidenceStat label="回应总数" value={section.summary.total} />
            <EvidenceStat label="自动回应" value={section.summary.auto} />
            <EvidenceStat label="AI 现编" value={section.summary.llm} tone={section.summary.llm ? "warn" : "muted"} />
            <EvidenceStat label="退回固定句" value={section.summary.degraded} tone={section.summary.degraded ? "warn" : "muted"} />
            <EvidenceStat label="无语音返回记录" value={section.summary.unspoken} tone={section.summary.unspoken ? "warn" : "muted"} />
          </div>
          <div className="evidence-attempt-section">
            {section.rows.map((row) => (
              <article className="evidence-attempt" key={row.key}>
                <div className="analysis-turn-head">
                  <strong className="mono">{row.time}</strong>
                  <StatusPill tone={row.sourceTone} size="sm">{row.sourceLabel}</StatusPill>
                  <StatusPill tone={row.spokenTone} size="sm">{row.spokenLabel}</StatusPill>
                </div>
                <p>「{row.text}」</p>
                <div className="evidence-detail-grid evidence-detail-grid--attempt">
                  <EvidenceValue label="位置" value={row.positionLabel} />
                  <EvidenceValue label="触发" value={row.originLabel} />
                  {row.degradedLabel && <EvidenceValue label="降级" value={row.degradedLabel} />}
                  {row.asrText && <EvidenceValue label="老人当时说的（转写）" value={row.asrText} />}
                </div>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

// 内容无关的确认修订履历:只列 revision/actor/时间,绝不复述确认文本或校验哈希链。
export function ConfirmationRevisionHistory({ confirmation }: { confirmation: ConfirmationRevisionTurnViewModel }) {
  if (confirmation.status === "no_ledger") {
    return (
      <div className="evidence-confirmation-history">
        <div className="evidence-section-title"><strong>确认修订历史</strong></div>
        <p className="muted">{confirmation.notice}</p>
      </div>
    );
  }
  return (
    <div className="evidence-confirmation-history">
      <div className="evidence-section-title">
        <strong>确认修订历史</strong>
        <span className="muted">共 {confirmation.finalRevision} 次 · 内容无关记录</span>
      </div>
      <ol className="evidence-timeline">
        {confirmation.entries.map((entry) => (
          <li key={entry.key}>
            <span className="evidence-timeline-dot" aria-hidden />
            <div>
              <span className="mono muted">第 {entry.revision} 次修订 · {entry.time}</span>
              <strong>操作者 {entry.actorDisplayId}</strong>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

// 场次级 AI 实际使用汇总:后端聚合账本,不是 provider readiness、模型准确率或 autopilot 控制状态。
export function AiUsageSectionView({ state }: { state: AiUsageViewModel }) {
  return (
    <section className="form-section" aria-label="本场 AI 使用汇总">
      <div className="form-section-header"><div>
        <h3>本场 AI 使用汇总</h3>
        <p className="muted">本场实际调用 AI 服务的次数汇总;进行中的场次,此处计数可能与上方略有延迟差异,属正常。</p>
      </div></div>
      {state.status === "loading" && <StatusPill role="status" tone="muted">正在加载 AI 使用汇总…</StatusPill>}
      {state.status === "forbidden" && (
        <Alert tone="warn" title="当前账号无权查看 AI 使用汇总">该证据仅向获授权账号开放。</Alert>
      )}
      {state.status === "withdrawn" && (
        <Alert tone="warn" title="受试者内容已撤回">受试者已撤回,本项依隐私要求关闭查看。</Alert>
      )}
      {state.status === "network-error" && <Alert tone="danger" title="AI 使用汇总读取失败">{state.message}</Alert>}
      {state.status === "contract-error" && (
        <Alert tone="danger" title="AI 使用汇总数据异常">
          数据未通过完整性检查,已停止显示。
          <details className="muted"><summary>技术详情</summary>{state.message}</details>
        </Alert>
      )}
      {state.status === "ready" && (
        <>
          {!state.model.hasAnyRecords && <Alert tone="info" title="暂无实际使用证据">{sessionAiEvidenceEmptyNotice}</Alert>}
          <div className="evidence-attempt-section">
            <div className="evidence-section-title"><strong>TTS</strong></div>
            {state.model.ttsRows.length === 0 && <p className="muted">无记录。</p>}
            {state.model.ttsRows.map((row) => (
              <div className="evidence-value" key={row.key}>
                <span className="mono">{row.label}</span>
                <strong>{row.detail}</strong>
              </div>
            ))}
          </div>
          <div className="evidence-attempt-section">
            <div className="evidence-section-title"><strong>ASR</strong></div>
            {state.model.asrRows.length === 0 && <p className="muted">无记录。</p>}
            {state.model.asrRows.map((row) => (
              <div className="evidence-value" key={row.key}>
                <span className="mono">{row.label}</span>
                <strong>{row.detail}</strong>
              </div>
            ))}
          </div>
          <div className="evidence-attempt-section">
            <div className="evidence-section-title"><strong>判类（judge）</strong></div>
            {state.model.judgeRows.length === 0 && <p className="muted">无记录。</p>}
            {state.model.judgeRows.map((row) => (
              <div className="evidence-value" key={row.key}>
                <span className="mono">{row.label}</span>
                <strong>{row.detail}</strong>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
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

function EvidenceTurnCard({ row, canPlayAudio, confirmation }: {
  row: TurnEvidence;
  canPlayAudio: boolean;
  confirmation?: ConfirmationRevisionTurnViewModel;
}) {
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
            ? <StatusPill tone="warn" size="sm">未形成环节记录</StatusPill>
            : turn.source_attempt_id != null
              ? <StatusPill tone="primary" size="sm">来源记录 #{turn.source_attempt_id}</StatusPill>
              : <StatusPill tone="danger" size="sm">环节缺少来源记录</StatusPill>}
          {turn?.score_locked
            ? <StatusPill tone="ok" size="sm">研究评分 {researchValue ?? "缺失"}</StatusPill>
            : <StatusPill tone="warn" size="sm">研究评分未锁定</StatusPill>}
        </div>
      </div>
      <EvidenceIssues issues={row.issues} />

      <div className="evidence-truth-panel">
        <div className="evidence-section-title">
          <strong>人工研究评分</strong>
          <StatusPill tone={turn?.score_locked ? "ok" : "warn"} size="sm">{turn?.score_locked ? "已锁定" : "未锁定"}</StatusPill>
        </div>
        {turn ? (
          <div className="evidence-detail-grid">
            <EvidenceValue label="环节 / 来源" value={`#${turn.id} / ${turn.source_attempt_id != null ? `#${turn.source_attempt_id}` : "缺失"}`} mono />
            <EvidenceValue label="人工锁分" value={turn.score_locked ? String(researchValue ?? "分值缺失") : "尚未锁定"} />
            <EvidenceValue label="锁分人" value={turn.reviewer_id ?? "未记录"} />
            <EvidenceValue label="最高提示" value={turn.prompt_level != null ? `${turn.prompt_level} 级${turn.cue_type ? ` · ${turn.cue_type}` : ""}` : "未记录"} />
            <EvidenceValue label="权威 ASR 原文" value={turn.asr_text ?? "缺失"} />
            <EvidenceValue label="人工确认文本" value={turn.confirmed_response_text ?? "未确认"} marker={corrected ? "已校正 ASR" : undefined} />
          </div>
        ) : <p className="muted">尚未形成环节记录，因此没有可用的人工研究评分。</p>}
        <p className={`evidence-comparison is-${row.comparison.state}`}>{row.comparison.message}</p>
        {confirmation && <ConfirmationRevisionHistory confirmation={confirmation} />}
      </div>

      <div className="evidence-attempt-section">
        <div className="evidence-section-title">
          <strong>AI 逐次处理证据</strong>
          <span className="muted">{row.attempts.length} 次 · 不作为研究评分</span>
        </div>
        {row.attempts.length === 0 && <Alert tone="danger" title="缺少 AI 处理证据">该环节缺少录音与 AI 处理记录。</Alert>}
        {row.attempts.map((entry) => {
          const attempt = entry.attempt;
          const statusTone = attempt.processing_status === "completed" ? "ok"
            : attempt.processing_status === "technical_failure" ? "danger" : "warn";
          return (
            <article className={`evidence-attempt is-${attempt.processing_status}`} key={attempt.id}>
              <div className="analysis-turn-head">
                <strong>第 {attempt.attempt_seq} 次处理 <span className="mono muted">ID {attempt.id}</span></strong>
                <div className="row wrap" style={{ gap: 6 }}>
                  {entry.isTurnSource && <StatusPill tone="primary" size="sm">最终记录来源</StatusPill>}
                  <StatusPill tone={statusTone} size="sm">{processingStatusLabel(attempt.processing_status)}</StatusPill>
                  {attempt.operational_needs_review && <StatusPill tone="warn" size="sm">AI 建议复核</StatusPill>}
                </div>
              </div>
              <div className="muted evidence-attempt-meta">
                <span>开始 {formatEvidenceTime(attempt.created_at)}</span>
                <span>完成 {formatEvidenceTime(attempt.processed_at)}</span>
                <span>提示 {attempt.prompt_level} 级{attempt.cue_type ? ` · ${attempt.cue_type}` : ""}</span>
              </div>
              <EvidenceIssues issues={entry.issues} title={attempt.processing_status === "technical_failure" ? "技术失败（非作答错误）" : "这次处理的证据问题"} />
              <div className="evidence-detail-grid evidence-detail-grid--attempt">
                <EvidenceValue label="录音引用" value={attempt.raw_audio_id} mono />
                <EvidenceValue label="时长" value={attempt.duration_seconds != null ? `${attempt.duration_seconds.toFixed(1)}s` : "未记录"} />
                <EvidenceValue label="ASR 引擎" value={attempt.asr_engine_version ?? "缺失"} mono />
                <EvidenceValue label="ASR 置信度" value={attempt.asr_confidence != null ? String(attempt.asr_confidence) : "未记录"} />
                <EvidenceValue label="ASR 原文" value={attempt.asr_text ?? "无权威转写"} />
                <EvidenceValue label="AI 判定类型" value={attempt.operational_answer_type ?? "未形成"} marker="仅运营决策" />
                <EvidenceValue label="AI 判定分" value={attempt.operational_score != null ? String(attempt.operational_score) : "不适用/缺失"} />
                <EvidenceValue label="判类引擎" value={[attempt.judge_mode, attempt.judge_engine_version].filter(Boolean).join(" · ") || "缺失"} mono />
                <EvidenceValue label="理由" value={attempt.judge_reason ?? "未提供（规则引擎可为空）"} />
                <EvidenceValue label="匹配依据" value={attempt.matched_on ?? "未记录"} mono />
                <EvidenceValue label="包含完整目标词" value={attempt.contains_target == null ? "未记录" : attempt.contains_target ? "是" : "否"} />
                <EvidenceValue label="错误码" value={attempt.error_code ?? "无"} mono />
              </div>
              {entry.audio ? <AudioEvidence audio={entry.audio} compact canPlay={canPlayAudio} /> : <StatusPill tone="danger" size="sm">录音资产缺失</StatusPill>}
              <InteractionTimeline rows={entry.interactions} empty="未找到与这次处理关联的过程事件。" />
            </article>
          );
        })}
      </div>

      {row.interactions.length > 0 && (
        <div className="evidence-loose-events">
          <div className="evidence-section-title"><strong>环节交互（未对应到 AI 处理）</strong></div>
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
              <strong>{row.payloadError ? `${row.summary}（记录损坏）` : row.summary}</strong>
              <small className="mono">{row.event.event_type}</small>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

const AUDIO_STATUS_LABELS: Record<string, string> = {
  recorded: "已录音",
  exported: "已导出",
  checksum_verified: "校验通过",
  reliability_review_done: "信度复核完成",
  deletable: "可删除",
  deleted: "已删除",
};

function AudioEvidence({ audio, compact = false, canPlay }: { audio: AudioAsset; compact?: boolean; canPlay: boolean }) {
  return (
    <div className={`evidence-audio${compact ? " is-compact" : ""}`}>
      <div className="row wrap" style={{ gap: 6 }}>
        <DataBoundaryBadge classification={sessionDataClassification(audio)} />
        <StatusPill tone="muted" size="sm">{AUDIO_STATUS_LABELS[audio.status] ?? audio.status}</StatusPill>
        {audio.contains_direct_identifier && <StatusPill tone="warn" size="sm">含直接标识</StatusPill>}
        <span className="mono muted">{audio.raw_audio_id}</span>
      </div>
      {!compact && <span className="muted">{audio.turn_key ?? "未标注环节"} · {audio.audio_format}{audio.checksum ? " · 完整性校验已登记" : " · 完整性校验缺失"}</span>}
      {canPlay
        ? <AuthenticatedAudio rawAudioId={audio.raw_audio_id} lazy />
        : <StatusPill tone="warn" size="sm">需授权账号登录后才能播放</StatusPill>}
    </div>
  );
}

function formatEvidenceTime(value: string | null | undefined): string {
  if (!value) return "未记录";
  return value.replace("T", " ").slice(0, 19);
}
