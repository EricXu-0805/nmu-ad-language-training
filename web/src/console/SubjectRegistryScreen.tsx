import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/ToastContext";
import type { PatientSummary, PatientWithdrawalReceipt, Session } from "../types";
import { QUICK_DRILL_CONTEXT_LABEL, runQuickDrill } from "./quickDrill";
import { DataBoundaryBadge, DataBoundaryFilter } from "./DataBoundaryFilter";
import { PatientIntakeScreen } from "./PatientIntakeScreen";
import {
  DATA_CLASSIFICATION_META,
  partitionByDataClassification,
  patientDataClassification,
  type DataClassification,
} from "./dataClassification";
import { researchEligibilityIssueText } from "./researchEligibility";
import { ScaleDrawer } from "./ScaleDrawer";
import { SubjectWithdrawalPanel } from "./SubjectWithdrawalPanel";
import {
  applyWithdrawalReceiptToRegistry,
  createSubjectRegistryRefreshGate,
} from "./subjectRegistryRefresh";
import { VisitPlanCreateScreen } from "./VisitPlanCreateScreen";
import { WithdrawnAudioGovernancePanel } from "./WithdrawnAudioGovernancePanel";
import { WITHDRAWAL_REASON_LABELS } from "./subjectWithdrawal";

// 准备区流程模式:研究规范(默认多步)vs 快速演练(仅模拟档案一键 create→审核→开场)。
// 只存本机偏好,不影响任何服务端门禁;换账号/设备各自独立。
type PrepFlowMode = "research" | "quickDrill";
const FLOW_MODE_KEY = "nmu:prep:flowMode";
function loadPrepFlowMode(): PrepFlowMode {
  try { return localStorage.getItem(FLOW_MODE_KEY) === "quickDrill" ? "quickDrill" : "research"; }
  catch { return "research"; }
}

// 准备区把建档、量表与训练安排合成一条测试前工作流。床旁训练台只消费已审核安排，
// 不再让研究人员临场填写周次、阶段、任务线或场次编号。
// onSessionStarted(可选):快速演练一键开场后,把服务端权威场次交回外壳直挂床旁。
export function SubjectRegistryScreen({ canManagePlans = true, actorRole = null, onSessionStarted }: {
  canManagePlans?: boolean;
  actorRole?: string | null;
  onSessionStarted?: (session: Session) => void;
}) {
  const toast = useToast();
  const [rows, setRows] = useState<PatientSummary[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const [mode, setMode] = useState<"list" | "new" | "plan">("list");
  const [planPatientId, setPlanPatientId] = useState<string | null>(null);
  const [scaleFor, setScaleFor] = useState<string | null>(null);
  const [classificationFilter, setClassificationFilter] = useState<DataClassification>("research");
  const [withdrawalFor, setWithdrawalFor] = useState<PatientSummary | null>(null);
  const [withdrawalReceipt, setWithdrawalReceipt] = useState<PatientWithdrawalReceipt | null>(null);
  const [refreshGate] = useState(() => createSubjectRegistryRefreshGate());
  const [flowMode, setFlowMode] = useState<PrepFlowMode>(loadPrepFlowMode);
  const [quickDrillBusy, setQuickDrillBusy] = useState<string | null>(null);
  const canRegisterWithdrawal = actorRole === "admin";
  const quickDrillEnabled = Boolean(onSessionStarted) && canManagePlans;

  const selectFlowMode = (next: PrepFlowMode) => {
    setFlowMode(next);
    try { localStorage.setItem(FLOW_MODE_KEY, next); } catch { /* 存储不可用不阻塞流程 */ }
  };

  // 一键演练:仅对模拟档案,原子走服务端 create→approve→start,成功即把场次交回外壳。
  // 失败(如同槽位已有安排/内容未冻结)平静提示,绝不静默;不改任何服务端门禁。
  const startQuickDrill = useCallback(async (patientId: string) => {
    if (!onSessionStarted || quickDrillBusy) return;
    setQuickDrillBusy(patientId);
    try {
      const session = await runQuickDrill(api, patientId);
      onSessionStarted(session); // 成功即切走本屏,不再回写本组件状态
    } catch (e) {
      const detail = e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e);
      toast(`一键演练开训未成功：${detail}`, "danger");
      setQuickDrillBusy(null);
    }
  }, [onSessionStarted, quickDrillBusy, toast]);

  // `mode` is an in-page route. Without resetting the viewport, a long intake form
  // leaves the following plan screen half way down the page and hides its title.
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "auto" });
  }, [mode, planPatientId]);

  const reload = useCallback(async () => {
    const ticket = refreshGate.begin();
    setErr(null);
    try {
      const nextRows = await api.listPatients();
      if (!refreshGate.accepts(ticket)) return;
      setRows(nextRows);
    } catch (e) {
      if (!refreshGate.accepts(ticket)) return;
      setErr(e instanceof ApiError ? e.detail : String(e));
    }
  }, [refreshGate]);
  useEffect(() => {
    void reload();
    return () => refreshGate.invalidate();
  }, [reload, nonce, refreshGate]);

  const sections = partitionByDataClassification(rows ?? [], patientDataClassification);
  const classificationCounts = {
    research: sections.research.length,
    simulation: sections.simulation.length,
    legacy_unknown: sections.legacy_unknown.length,
  };
  const visibleRows = sections[classificationFilter];

  if (mode === "new") {
    return (
      <div className="page-shell page-shell--medium">
        <PatientIntakeScreen context="registry" onReady={() => {
          setMode("list"); setNonce((n) => n + 1);
        }} onCreatePlan={(patientId) => {
          setPlanPatientId(patientId);
          setMode("plan");
          setNonce((n) => n + 1);
        }} onQuickDrill={flowMode === "quickDrill" && quickDrillEnabled
          ? (patientId) => { void startQuickDrill(patientId); }
          : undefined} />
        {/* 返回也刷新列表：建档请求可能已完成，不能因后续网络失败把新档案隐藏。 */}
        <div className="form-actions"><Button onClick={() => { setMode("list"); setNonce((n) => n + 1); }}>返回登记表</Button></div>
      </div>
    );
  }

  if (mode === "plan" && planPatientId) {
    return (
      <VisitPlanCreateScreen
        patientId={planPatientId}
        canManage={canManagePlans}
        onBack={() => { setPlanPatientId(null); setMode("list"); setNonce((n) => n + 1); }}
      />
    );
  }

  return (
    <div className="page-shell page-shell--wide">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">准备区 · 受试者登记表</p>
          <h2 className="page-title">受试者档案与训练安排</h2>
          <p className="page-description">测试开始前一次完成受试者信息、同意与录音授权、量表协议就绪核对和训练安排。登记表只用研究编号，不含姓名。</p>
        </div>
        <Button variant="primary" disabled={!canManagePlans}
          title={!canManagePlans ? "当前账号为只读角色" : undefined}
          onClick={() => setMode("new")}>登记新受试者</Button>
      </header>

      {quickDrillEnabled && (
        <section className="form-section" role="group" aria-label="准备区流程模式">
          <div className="form-section-header">
            <div>
              <h3>流程模式</h3>
              <p className="muted">
                {flowMode === "quickDrill"
                  ? `快速演练:对模拟档案一键 建档→创建安排→审核→开场（${QUICK_DRILL_CONTEXT_LABEL}）。全程仍逐步走服务端计划账本、不绕过任何门禁；真实受试者永远走规范多步流程。`
                  : "研究规范（默认）：建档 → 创建训练安排 → 审核 → 训练台开场的多步流程。"}
              </p>
            </div>
            <div className="toolbar" role="radiogroup" aria-label="选择准备区流程模式">
              <Button variant={flowMode === "research" ? "primary" : undefined}
                aria-pressed={flowMode === "research"}
                onClick={() => selectFlowMode("research")}>研究规范流程</Button>
              <Button variant={flowMode === "quickDrill" ? "primary" : undefined}
                aria-pressed={flowMode === "quickDrill"}
                onClick={() => selectFlowMode("quickDrill")}>快速演练（仅模拟）</Button>
            </div>
          </div>
        </section>
      )}

      {err && <Alert tone="danger" title="登记表加载失败" actions={<Button onClick={reload}>重试</Button>}>{err}</Alert>}
      {withdrawalReceipt && (
        <Alert tone="info" title={`已登记研究撤回：${withdrawalReceipt.patient_id}`}>
          权威治理版本已更新为 {withdrawalReceipt.governance_revision}；系统已隔离 {withdrawalReceipt.affected_audio_count} 条录音并围栏 {withdrawalReceipt.affected_session_count} 个相关场次。
        </Alert>
      )}
      {rows && rows.length === 0 && !err && (
        <Alert tone="info" title="还没有登记任何受试者">点右上角“登记新受试者”开始；保存档案后继续创建并审核训练安排。</Alert>
      )}

      {rows && rows.length > 0 && (
        <section className="form-section">
          <div className="form-section-header">
            <div>
              <h3>档案数据分区</h3>
              <p className="muted">默认只显示真实研究档案；专用模拟和历史未知档案必须显式切换查看，不参与研究名单。</p>
            </div>
            <DataBoundaryBadge classification={classificationFilter} entity="patient" />
          </div>
          <DataBoundaryFilter value={classificationFilter} counts={classificationCounts} onChange={setClassificationFilter} label="登记表数据分区" />
          {classificationFilter === "simulation" && (
            <Alert tone="danger" title="当前是专用模拟档案分区">
              这里只允许合成数据或工作人员演练；不得登记真实患者、老人或受试者，也不得并入真实研究统计。
            </Alert>
          )}
          {classificationFilter === "legacy_unknown" && (
            <Alert tone="danger" title="当前是历史/未知隔离分区">
              这些档案缺少可信分类，必须完成迁移和人工核对；系统不会把它们猜成真实研究数据。
            </Alert>
          )}
        </section>
      )}

      {rows && rows.length > 0 && visibleRows.length === 0 && (
        <Alert tone="info" title={`${DATA_CLASSIFICATION_META[classificationFilter].label}分区暂无档案`}>
          其他分区的数据不会在这里混合显示，请使用上方分区按钮单独查看。
        </Alert>
      )}

      {rows && visibleRows.length > 0 && (
        <div className="registry-table" role="table" aria-label="受试者登记表">
          <div className="registry-row registry-head" role="row">
            <span role="columnheader">研究编号</span>
            <span role="columnheader">认知障碍程度</span>
            <span role="columnheader">普通话</span>
            <span role="columnheader">授权与准入</span>
            <span role="columnheader">场次数</span>
            <span role="columnheader">操作</span>
          </div>
          {visibleRows.map((r) => {
            const classification = patientDataClassification(r);
            const cannotPlan = Boolean(r.withdrawal_status) || classification === "legacy_unknown" || !canManagePlans;
            return (
            <div className="registry-row" role="row" key={r.patient_id}>
              <span className="col" style={{ gap: 4 }} role="cell">
                <span className="registry-cell-label">研究编号</span>
                <strong className="mono">{r.patient_id}</strong>
                <DataBoundaryBadge classification={classification} entity="patient" />
              </span>
              <span role="cell">
                <span className="registry-cell-label">认知障碍程度</span>
                {r.dementia_severity || <span className="muted">未填</span>}
              </span>
              <span role="cell">
                <span className="registry-cell-label">普通话</span>
                {triLabel(r.mandarin_eligible)}
              </span>
              <span role="cell" className="col" style={{ gap: 4 }}>
                <span className="registry-cell-label">授权与准入</span>
                {recordingLabel(r.recording_allowed)}
                {eligibilityLabel(r, classification)}
                {r.withdrawal_status && <StatusPill tone="danger" size="sm">
                  撤回终态 · 治理版本 {r.governance_revision}
                </StatusPill>}
                {r.withdrawal_status && r.withdrawal_reason_code && (
                  <span className="muted">
                    权威原因：{WITHDRAWAL_REASON_LABELS[r.withdrawal_reason_code]}
                    {r.withdrawal_occurred_at ? ` · ${r.withdrawal_occurred_at.slice(0, 10)}` : ""}
                  </span>
                )}
                {r.withdrawal_status && !r.withdrawal_event_id && (
                  <span className="muted">历史撤回状态缺少新账本回执，继续保持隔离</span>
                )}
              </span>
              <span role="cell">
                <span className="registry-cell-label">场次数</span>
                {r.session_count}{r.last_training_date ? ` · 最近 ${r.last_training_date}` : ""}
              </span>
              <span role="cell" className="registry-actions-cell">
                <span className="registry-cell-label">操作</span>
                <Button
                  title={!canManagePlans ? "当前账号可只读核对协议状态与历史未验证记录" : undefined}
                  onClick={() => setScaleFor(r.patient_id)}>量表就绪状态</Button>
                  {!r.is_simulation_subject && !r.withdrawal_status && canRegisterWithdrawal && (
                    <Button variant="danger" onClick={() => {
                      setWithdrawalReceipt(null);
                      setWithdrawalFor(r);
                    }}>登记研究撤回</Button>
                  )}
                  <Button variant="primary" disabled={cannotPlan}
                    title={r.withdrawal_status
                      ? `撤回/退出状态为「${r.withdrawal_status}」，不可安排训练`
                      : classification === "legacy_unknown" ? "档案分类未知，完成迁移核对前不可安排训练"
                        : !canManagePlans ? "当前账号为只读角色" : undefined}
                    onClick={() => { setPlanPatientId(r.patient_id); setMode("plan"); }}>
                    {classification === "simulation" ? "安排模拟演练" : "安排训练"}
                  </Button>
                  {flowMode === "quickDrill" && quickDrillEnabled
                    && classification === "simulation" && !r.withdrawal_status && (
                    <Button variant="primary" disabled={quickDrillBusy !== null}
                      title={`一键 建档已完成 → 审核 → 开场（${QUICK_DRILL_CONTEXT_LABEL}）；不绕过服务端计划账本`}
                      onClick={() => { void startQuickDrill(r.patient_id); }}>
                      {quickDrillBusy === r.patient_id ? "正在开场…" : "一键演练开训"}
                    </Button>
                  )}
              </span>
            </div>
            );
          })}
        </div>
      )}
      {!rows && !err && <StatusPill tone="muted">正在加载登记表…</StatusPill>}

      {scaleFor && <ScaleDrawer patientId={scaleFor} open onClose={() => setScaleFor(null)} />}
      {actorRole === "admin" && <WithdrawnAudioGovernancePanel />}
      {withdrawalFor && (
        <SubjectWithdrawalPanel patient={withdrawalFor}
          onCancel={() => {
            setWithdrawalFor(null);
            setNonce((value) => value + 1);
          }}
          onDone={(receipt) => {
            // 回执已经确认不可逆治理终态：先废弃所有旧列表请求并立即锁住该行，
            // 再发起权威列表刷新，避免慢旧响应短暂恢复“安排训练”。
            refreshGate.invalidate();
            setRows((current) => applyWithdrawalReceiptToRegistry(current, receipt));
            setWithdrawalReceipt(receipt);
            setWithdrawalFor(null);
            setNonce((value) => value + 1);
          }} />
      )}
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

function eligibilityLabel(row: PatientSummary, classification: DataClassification) {
  if (classification === "simulation") return <StatusPill tone="warn" size="sm">不进入真实研究准入</StatusPill>;
  if (classification === "legacy_unknown") return <StatusPill tone="danger" size="sm">分类未知 · 禁止开场</StatusPill>;
  if (row.research_eligible === true) {
    return <span title="仅表示受试者级资料门禁已核对，不代表内容、量表、伦理或平台已获正式研究放行">
      <StatusPill tone="ok" size="sm">受试者级准入已核对</StatusPill>
    </span>;
  }
  if (row.research_eligible === false) {
    const issues = row.research_eligibility_issues ?? [];
    const detail = issues.length > 0 ? researchEligibilityIssueText(issues) : "准入未通过，服务器未返回具体缺项";
    return (
      <span className="muted" title={detail}>
        <StatusPill tone="warn" size="sm">准入待补 {issues.length || ""} 项</StatusPill>
        {issues.length > 0 && <span style={{ display: "block", marginTop: 2 }}>缺项：{researchEligibilityIssueText(issues)}</span>}
      </span>
    );
  }
  return <StatusPill tone="muted" size="sm">准入待核对</StatusPill>;
}
