import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/ToastContext";
import type { PatientSummary, Session, VisitPlanReceipt, VisitPlanToday } from "../types";
import { sessionStatusMeta } from "../sessionLifecycle";
import { DataBoundaryBadge, DataBoundaryFilter } from "./DataBoundaryFilter";
import {
  isSameApprovedVisitPlan,
  reconcileStartedVisitPlan,
} from "../visitPlans";
import {
  partitionByDataClassification,
  patientDataClassification,
  sessionDataClassification,
  sessionMatchesPatientBoundary,
  type DataClassification,
} from "./dataClassification";
import { runPickerPresentation, sessionsVisibleForRunPicker } from "./runPickerAccess";
import { createAsyncOperationFence, deliverIfCurrent } from "./runPickerStartFence";
import { AssessmentQueuePanel } from "./AssessmentQueuePanel";

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.detail : String(error);
}

function startKey(plan: VisitPlanReceipt): string {
  return `vp-start-${plan.revision}-${crypto.randomUUID()}`;
}

const PLAN_STATUS_LABELS: Record<string, string> = {
  draft: "草稿",
  approved: "已审核",
  started: "已开始",
  cancelled: "已取消",
};

function planStatusLabel(status: string): string {
  return PLAN_STATUS_LABELS[status] ?? status;
}

function plannedTime(plan: VisitPlanReceipt): string {
  return plan.scheduled_time ? plan.scheduled_time.slice(0, 5) : "时间待定";
}

function startOutcomeIsUncertain(error: unknown): boolean {
  return !(error instanceof ApiError)
    || error.status === 0
    || error.status === 408
    || error.status >= 500;
}

interface PendingStart {
  plan: VisitPlanReceipt;
  idempotencyKey: string;
  confirmedReceipt: VisitPlanReceipt | null;
  detail: string;
}

// 床旁训练台的主入口只消费“已审核且已到日期”的服务器队列。研究者不再临场选人、
// 填周次或创建场次；一键开训会在服务器同一事务内生成场次和运行时。
export function RunPickerScreen({
  onResume,
  onGoPrep,
  canStart = true,
  canReview = canStart,
  canViewCompleted = canReview,
  onStartingChange,
}: {
  onResume: (session: Session) => void;
  onGoPrep: () => void;
  canStart?: boolean;
  canReview?: boolean;
  canViewCompleted?: boolean;
  onStartingChange?: (busy: boolean) => void;
}) {
  const toast = useToast();
  const presentation = runPickerPresentation({ canStart, canViewCompleted });
  const [today, setToday] = useState<VisitPlanToday | null>(null);
  const [queueLoading, setQueueLoading] = useState(false);
  const [queueError, setQueueError] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [pendingStart, setPendingStart] = useState<PendingStart | null>(null);
  const [startingPlanId, setStartingPlanId] = useState<string | null>(null);
  const [classificationFilter, setClassificationFilter] = useState<DataClassification>("research");
  const [recoveryOpen, setRecoveryOpen] = useState(presentation.completedOnly);
  const startKeys = useRef(new Map<string, string>());
  const startInFlight = useRef<string | null>(null);
  const startFence = useRef(createAsyncOperationFence());
  const queueRequest = useRef(0);

  const loadQueue = useCallback(async () => {
    const request = ++queueRequest.current;
    // 一旦发起刷新，旧队列就不再是可操作真值。清空它，并只允许
    // 最后一个请求恢复操作，避免慢响应覆盖新响应。
    setToday(null);
    setQueueError(null);
    setQueueLoading(true);
    try {
      const next = await api.listTodayVisitPlans();
      if (queueRequest.current === request) setToday(next);
    } catch (error) {
      if (queueRequest.current === request) {
        setToday(null);
        setQueueError(errorText(error));
      }
    } finally {
      if (queueRequest.current === request) setQueueLoading(false);
    }
  }, []);

  useEffect(() => {
    const fence = startFence.current;
    if (!presentation.completedOnly) void loadQueue();
    return () => {
      queueRequest.current += 1;
      fence.invalidate();
    };
  }, [loadQueue, presentation.completedOnly]);

  useEffect(() => {
    onStartingChange?.(Boolean(startingPlanId));
    return () => onStartingChange?.(false);
  }, [onStartingChange, startingPlanId]);

  useEffect(() => {
    if (presentation.completedOnly) setRecoveryOpen(true);
  }, [presentation.completedOnly]);

  async function enterStartedSession(
    plan: VisitPlanReceipt,
    receipt: VisitPlanReceipt,
    generation: number,
  ): Promise<boolean> {
    return deliverIfCurrent(
      startFence.current,
      generation,
      () => api.getStartedVisitSession(receipt),
      (session) => {
        startKeys.current.delete(`${plan.plan_id}:${plan.revision}`);
        setPendingStart(null);
        toast(receipt.is_simulation ? "专用模拟演练已开始" : "本次训练已开始", "ok");
        onResume(session);
      },
    );
  }

  function rememberPending(
    plan: VisitPlanReceipt,
    idempotencyKey: string,
    detail: string,
    confirmedReceipt: VisitPlanReceipt | null = null,
    generation?: number,
  ) {
    if (generation !== undefined && !startFence.current.isCurrent(generation)) return;
    setPendingStart({ plan, idempotencyKey, confirmedReceipt, detail });
    setStartError(null);
  }

  async function reconcileAfterStartRequest(
    plan: VisitPlanReceipt,
    idempotencyKey: string,
    requestError: unknown,
    uncertain: boolean,
    generation: number,
  ): Promise<boolean> {
    try {
      const plans = await api.listPatientVisitPlans(plan.patient_id);
      if (!startFence.current.isCurrent(generation)) return false;
      const current = plans.find((candidate) => candidate.plan_id === plan.plan_id);
      if (current?.status === "started") {
        const receipt = reconcileStartedVisitPlan(plan, current);
        try {
          return await enterStartedSession(plan, receipt, generation);
        } catch (sessionError) {
          rememberPending(
            plan,
            idempotencyKey,
            `已启动，但进入训练页前的检查未通过：${errorText(sessionError)}。请点「继续确认」。`,
            receipt,
            generation,
          );
        }
        return false;
      }
      if (current && !isSameApprovedVisitPlan(plan, current)) {
        startKeys.current.delete(`${plan.plan_id}:${plan.revision}`);
        setPendingStart(null);
        setStartError(`该安排状态已变化（${planStatusLabel(current.status)}），请刷新队列后再操作。`);
        return false;
      }
      if (!uncertain && current && isSameApprovedVisitPlan(plan, current)) {
        startKeys.current.delete(`${plan.plan_id}:${plan.revision}`);
        setPendingStart(null);
        setStartError(`服务器拒绝了本次启动：${errorText(requestError)}。安排未受影响，可重试。`);
        return false;
      }
      rememberPending(
        plan,
        idempotencyKey,
        `还没确认启动是否成功：${errorText(requestError)}。放心重试，不会重复建立场次。`,
        null,
        generation,
      );
      return false;
    } catch (reconcileError) {
      if (!startFence.current.isCurrent(generation)) return false;
      rememberPending(
        plan,
        idempotencyKey,
        `仍未确认启动结果：${errorText(reconcileError)}。可再次点击确认。`,
        null,
        generation,
      );
      return false;
    }
  }

  async function submitStart(
    plan: VisitPlanReceipt,
    idempotencyKey: string,
    retryUncertainOnce: boolean,
    uncertaintyAlreadyExists = false,
    generation: number,
  ): Promise<boolean> {
    let lastError: unknown = null;
    let outcomeUncertain = uncertaintyAlreadyExists;
    const attempts = retryUncertainOnce ? 2 : 1;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        const receipt = await api.startVisitPlan(plan, {
          idempotency_key: idempotencyKey,
          expected_revision: plan.revision,
        });
        if (!startFence.current.isCurrent(generation)) return false;
        try {
          return await enterStartedSession(plan, receipt, generation);
        } catch (sessionError) {
          rememberPending(
            plan,
            idempotencyKey,
            `已启动，但进入训练页前的检查未通过：${errorText(sessionError)}。请点「继续确认」。`,
            receipt,
            generation,
          );
        }
        return false;
      } catch (error) {
        if (!startFence.current.isCurrent(generation)) return false;
        lastError = error;
        outcomeUncertain ||= startOutcomeIsUncertain(error);
        if (attempt === 0 && retryUncertainOnce && startOutcomeIsUncertain(error)) continue;
        break;
      }
    }
    return reconcileAfterStartRequest(
      plan,
      idempotencyKey,
      lastError,
      outcomeUncertain,
      generation,
    );
  }

  async function start(plan: VisitPlanReceipt) {
    if (!canStart || startInFlight.current) return;
    const generation = startFence.current.begin();
    startInFlight.current = plan.plan_id;
    setStartError(null);
    setStartingPlanId(plan.plan_id);
    const keyId = `${plan.plan_id}:${plan.revision}`;
    let idempotencyKey = startKeys.current.get(keyId);
    if (!idempotencyKey) {
      idempotencyKey = startKey(plan);
      startKeys.current.set(keyId, idempotencyKey);
    }
    try {
      setPendingStart(null);
      const entered = await submitStart(plan, idempotencyKey, true, false, generation);
      if (!entered && startFence.current.isCurrent(generation)) await loadQueue();
    } finally {
      if (startInFlight.current === plan.plan_id) startInFlight.current = null;
      if (startFence.current.isCurrent(generation)) setStartingPlanId(null);
    }
  }

  async function confirmPendingStart() {
    if (!pendingStart || startInFlight.current) return;
    const pending = pendingStart;
    const generation = startFence.current.begin();
    startInFlight.current = pending.plan.plan_id;
    setStartingPlanId(pending.plan.plan_id);
    setStartError(null);
    try {
      let entered = false;
      if (pending.confirmedReceipt) {
        try {
          entered = await enterStartedSession(
            pending.plan, pending.confirmedReceipt, generation,
          );
        } catch (error) {
          rememberPending(
            pending.plan,
            pending.idempotencyKey,
            `进入训练页前的检查仍未通过：${errorText(error)}。可再试一次。`,
            pending.confirmedReceipt,
            generation,
          );
        }
      } else {
        // 继续对账始终复用首次的键和请求体，不生成第二个启动命令。
        entered = await submitStart(
          pending.plan, pending.idempotencyKey, false, true, generation,
        );
      }
      if (!entered && startFence.current.isCurrent(generation)) await loadQueue();
    } finally {
      if (startInFlight.current === pending.plan.plan_id) startInFlight.current = null;
      if (startFence.current.isCurrent(generation)) setStartingPlanId(null);
    }
  }

  const plans = today?.plans ?? [];
  const sections = partitionByDataClassification(
    plans,
    (plan) => plan.data_classification,
  );
  const classificationCounts = {
    research: sections.research.length,
    simulation: sections.simulation.length,
    legacy_unknown: 0,
  };
  const visiblePlans = sections[classificationFilter];

  return (
    <div className="page-shell page-shell--wide">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">{presentation.kicker}</p>
          <h2 className="page-title">{presentation.title}</h2>
          <p className="page-description">{presentation.description}</p>
        </div>
        <div className="row wrap">
          {!presentation.completedOnly && (
            <Button disabled={queueLoading || Boolean(startingPlanId)} onClick={() => { void loadQueue(); }}>
              {queueLoading ? "正在刷新…" : "刷新今日队列"}
            </Button>
          )}
          <Button disabled={Boolean(startingPlanId)} onClick={onGoPrep}>{presentation.prepActionLabel}</Button>
        </div>
      </header>

      {!canStart && (
        <Alert tone={presentation.completedOnly ? "info" : "warn"} title={presentation.readOnlyTitle}>
          {presentation.readOnlyDescription}
        </Alert>
      )}
      {!presentation.completedOnly && queueError && (
        <Alert tone="danger" title="今日训练安排加载失败" actions={<Button onClick={() => { void loadQueue(); }}>重试</Button>}>
          {queueError}
        </Alert>
      )}
      {!presentation.completedOnly && startError && (
        <Alert tone="danger" title="本次启动未完成">
          {startError}
        </Alert>
      )}
      {!presentation.completedOnly && pendingStart && (
        <Alert
          tone="warn"
          title="启动结果待确认"
          actions={(
            <Button
              disabled={Boolean(startingPlanId)}
              onClick={() => { void confirmPendingStart(); }}
            >
              {startingPlanId ? "正在确认…" : "继续确认启动结果"}
            </Button>
          )}
        >
          {pendingStart.detail}
        </Alert>
      )}
      {!presentation.completedOnly && !today && !queueError && queueLoading && (
        <StatusPill tone="muted">正在读取今日安排…</StatusPill>
      )}

      {!presentation.completedOnly && <AssessmentQueuePanel />}

      {!presentation.completedOnly && today && plans.length === 0 && (
        <Alert tone="info" title="今天没有待开始的安排" actions={<Button variant="primary" disabled={Boolean(startingPlanId)} onClick={onGoPrep}>创建训练安排</Button>}>
          只显示已审核、今天到期的安排。刚才启动时页面中断的，请用下方恢复入口。
        </Alert>
      )}

      {!presentation.completedOnly && today && plans.length > 0 && (
        <section className="form-section">
          <div className="form-section-header">
            <div>
              <h3>今日训练队列</h3>
              <p className="muted">{today.as_of_date} · 含逾期未开始的安排</p>
            </div>
            <DataBoundaryBadge classification={classificationFilter} />
          </div>
          <DataBoundaryFilter
            value={classificationFilter}
            counts={classificationCounts}
            onChange={setClassificationFilter}
            label="今日训练数据分区"
          />
          {classificationFilter === "simulation" && (
            <Alert tone="danger" title="当前是模拟演练队列">
              演练队列仅供工作人员练习，禁止真实老人参加。
            </Alert>
          )}
          {today.withheld_count > 0 && (
            <Alert tone="warn" title="有安排暂不能开始">
              有 {today.withheld_count} 条今日安排暂不能开始（可能因同意撤回、题库或方案更新），已自动隐藏。请到该受试者的训练安排页查看原因。
            </Alert>
          )}
          {visiblePlans.length === 0 && (
            <p className="muted">当前数据分区没有待开始安排。</p>
          )}
          <div className="visit-plan-queue" role="list" aria-label="今日已审核训练安排">
            {visiblePlans.map((plan, index) => {
              const starting = startingPlanId === plan.plan_id;
              const reconciling = pendingStart?.plan.plan_id === plan.plan_id;
              const overdue = today.as_of_date > plan.scheduled_date;
              const blockedByOther = canStart && !starting && !reconciling
                && (Boolean(startingPlanId) || Boolean(pendingStart));
              return (
                <article className="run-picker-card" role="listitem" key={plan.plan_id}>
                  <div className="run-picker-head">
                    <div className="col" style={{ gap: 5 }}>
                      <div className="row wrap">
                        <StatusPill tone="primary">队列第 {index + 1} 位</StatusPill>
                        <DataBoundaryBadge classification={plan.data_classification} />
                        {overdue && <StatusPill tone="warn">逾期待开始</StatusPill>}
                      </div>
                      <strong className="mono" style={{ fontSize: "1.15em" }}>{plan.patient_id}</strong>
                      <span>{plan.scheduled_date} · {plannedTime(plan)} · 第 {plan.week_no} 周</span>
                      <span className="muted">{plan.phase_type} · {plan.event_line}{plan.session_sitting_no > 1 ? ` · 同周第 ${plan.session_sitting_no} 次` : ""}</span>
                    </div>
                    <div className="col" style={{ alignItems: "flex-end" }}>
                      <StatusPill tone="ok">已审核通过</StatusPill>
                      <Button variant="primary" size="lg"
                        disabled={!canStart
                          || Boolean(startingPlanId)
                          || Boolean(pendingStart && !reconciling)}
                        title={!canStart ? "当前账号为只读角色" : undefined}
                        onClick={() => {
                          if (reconciling) void confirmPendingStart();
                          else void start(plan);
                        }}>
                        {starting
                          ? "正在确认…"
                          : reconciling
                            ? "继续确认启动结果"
                            : plan.is_simulation ? "开始模拟演练" : "开始训练"}
                      </Button>
                      {blockedByOther && (
                        <span className="muted" style={{ fontSize: "0.85em" }}>另一条安排正在启动，请先完成确认</span>
                      )}
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      )}

      <section className="form-section recovery-entry">
        <div className="form-section-header">
          <div>
            <h3>{presentation.historyTitle}</h3>
            <p className="muted">{presentation.historyDescription}</p>
          </div>
          <Button
            variant="quiet"
            aria-expanded={recoveryOpen}
            aria-controls="run-picker-recovery-panel"
            onClick={() => setRecoveryOpen((value) => !value)}
          >
            {recoveryOpen ? presentation.historyCloseLabel : presentation.historyOpenLabel}
          </Button>
        </div>
        {recoveryOpen && (
          <div id="run-picker-recovery-panel">
            <RecoveryPicker canResume={canReview} canViewCompleted={canViewCompleted}
              completedOnly={presentation.completedOnly}
              onResume={onResume} />
          </div>
        )}
      </section>
    </div>
  );
}

function RecoveryPicker({ canResume, canViewCompleted, completedOnly, onResume }: {
  canResume: boolean;
  canViewCompleted: boolean;
  completedOnly: boolean;
  onResume: (session: Session) => void;
}) {
  const [rows, setRows] = useState<PatientSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [classificationFilter, setClassificationFilter] = useState<DataClassification>("research");
  const requestGeneration = useRef(0);

  const loadRows = useCallback(async () => {
    const request = ++requestGeneration.current;
    // 恢复列表失效后不得继续使用旧行进入场次。
    setRows(null);
    setExpanded(null);
    setError(null);
    try {
      const next = await api.listPatients();
      if (requestGeneration.current === request) setRows(next);
    } catch (reason) {
      if (requestGeneration.current === request) {
        setRows(null);
        setError(errorText(reason));
      }
    }
  }, []);

  useEffect(() => {
    void loadRows();
    return () => { requestGeneration.current += 1; };
  }, [loadRows]);

  if (error) return (
    <Alert
      tone="danger"
      title={completedOnly ? "已完成场次列表读取失败" : "异常恢复列表读取失败"}
      actions={<Button onClick={() => { void loadRows(); }}>重试读取</Button>}
    >
      {error}
    </Alert>
  );
  if (!rows) return (
    <StatusPill tone="muted">
      {completedOnly ? "正在读取已完成场次索引…" : "正在读取可恢复场次…"}
    </StatusPill>
  );

  // 摘要的 unfinished 只统计 active/paused，无法代表 intervention_completed 的未完研究复核；
  // 因此保留所有有历史场次的受试者，再由权威 runtime 状态决定可续做/复核/只读。
  const candidates = rows.filter((row) => row.session_count > 0);
  const sections = partitionByDataClassification(candidates, patientDataClassification);
  const counts = {
    research: sections.research.length,
    simulation: sections.simulation.length,
    legacy_unknown: sections.legacy_unknown.length,
  };
  const visibleRows = sections[classificationFilter];

  return (
    <div className="col">
      <div className="row wrap" style={{ justifyContent: "flex-end" }}>
        <Button onClick={() => { void loadRows(); }}>
          {completedOnly ? "刷新已完成场次" : "刷新恢复列表"}
        </Button>
      </div>
      <DataBoundaryFilter value={classificationFilter} counts={counts}
        onChange={(value) => { setClassificationFilter(value); setExpanded(null); }}
        label={completedOnly ? "已完成场次数据分区" : "异常恢复数据分区"} />
      {classificationFilter === "legacy_unknown" && (
        <Alert tone="danger" title="历史场次待核对">
          这些历史场次待人工核对，暂时只读。
        </Alert>
      )}
      {visibleRows.length === 0 && <p className="muted">当前分区没有可核查的既有场次。</p>}
      {visibleRows.map((row, index) => {
        const classification = patientDataClassification(row);
        const panelId = `recovery-session-list-${index}`;
        const isExpanded = expanded === row.patient_id;
        return (
          <div className="run-picker-card" key={row.patient_id}>
            <div className="run-picker-head">
              <div className="col" style={{ gap: 4 }}>
                <strong className="mono">{row.patient_id}</strong>
                <DataBoundaryBadge classification={classification} entity="patient" />
                <span className="muted">
                  {completedOnly
                    ? `既有 ${row.session_count} 场 · 展开后仅显示最终完成场次`
                    : `进行中/暂停 ${row.unfinished_session_count ?? "待核对"} 场 · 既有 ${row.session_count} 场`}
                </span>
              </div>
              <Button
                aria-expanded={isExpanded}
                aria-controls={panelId}
                onClick={() => setExpanded(isExpanded ? null : row.patient_id)}
              >
                {isExpanded ? "收起" : completedOnly ? "查看已完成场次" : "核查既有场次"}
              </Button>
            </div>
            {isExpanded && (
              <div id={panelId}>
                <ResumeList
                  patientId={row.patient_id}
                  patientClassification={classification}
                  canResume={canResume}
                  canViewCompleted={canViewCompleted}
                  completedOnly={completedOnly}
                  onResume={onResume}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function ResumeList({ patientId, patientClassification, canResume, canViewCompleted, completedOnly, onResume }: {
  patientId: string;
  patientClassification: DataClassification;
  canResume: boolean;
  canViewCompleted: boolean;
  completedOnly: boolean;
  onResume: (session: Session) => void;
}) {
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [classificationFilter, setClassificationFilter] = useState<DataClassification>(patientClassification);
  const requestGeneration = useRef(0);

  const loadSessions = useCallback(async () => {
    const request = ++requestGeneration.current;
    setSessions(null);
    setError(null);
    try {
      const next = await api.patientSessions(patientId);
      if (requestGeneration.current === request) setSessions(next);
    } catch (reason) {
      if (requestGeneration.current === request) {
        setSessions(null);
        setError(errorText(reason));
      }
    }
  }, [patientId]);

  useEffect(() => {
    void loadSessions();
    return () => { requestGeneration.current += 1; };
  }, [loadSessions]);

  if (error) return (
    <Alert
      tone="danger"
      title="场次读取失败"
      actions={<Button onClick={() => { void loadSessions(); }}>重试读取</Button>}
    >
      {error}
    </Alert>
  );
  if (!sessions) return <p className="muted">正在读取既有场次…</p>;
  if (sessions.length === 0) return <p className="muted">该受试者暂无既有场次。</p>;

  const entrySessions = sessionsVisibleForRunPicker(sessions, completedOnly);
  if (entrySessions.length === 0) {
    return <p className="muted">{completedOnly ? "该受试者暂无最终完成场次。" : "该受试者暂无可进入场次。"}</p>;
  }
  const sections = partitionByDataClassification(entrySessions, sessionDataClassification);
  const counts = {
    research: sections.research.length,
    simulation: sections.simulation.length,
    legacy_unknown: sections.legacy_unknown.length,
  };
  const visibleSessions = sections[classificationFilter];

  return (
    <div className="resume-list">
      <div className="row wrap" style={{ justifyContent: "flex-end" }}>
        <Button size="sm" onClick={() => { void loadSessions(); }}>刷新既有场次</Button>
      </div>
      <DataBoundaryFilter value={classificationFilter} counts={counts}
        onChange={setClassificationFilter}
        label={completedOnly ? "最终完成场次数据分区" : "既有场次数据分区"} />
      {classificationFilter === "legacy_unknown" && (
        <Alert tone="danger" title="历史场次待核对">
          历史场次待人工核对，暂时只读。
        </Alert>
      )}
      {visibleSessions.length === 0 && <p className="muted">当前分区暂无场次。</p>}
      {visibleSessions.map((session) => {
        const classification = sessionDataClassification(session);
        const status = sessionStatusMeta(session.runtime_status);
        const boundaryMatches = sessionMatchesPatientBoundary(patientClassification, classification);
        const canEnter = status.action === "view" ? canViewCompleted : canResume;
        return (
          <div className="resume-row" key={session.session_id}>
            <span>
              第 {session.week_no} 周 · {session.phase_type}{session.training_date ? ` · ${session.training_date}` : ""}
              {session.session_sitting_no && session.session_sitting_no > 1 ? ` · 同周第 ${session.session_sitting_no} 次` : ""}
              <span style={{ display: "block", marginTop: 4 }}><DataBoundaryBadge classification={classification} /></span>
            </span>
            <div className="row wrap">
              <StatusPill tone={status.tone} size="sm">{status.label}</StatusPill>
              {status.action && boundaryMatches && canEnter && (
                <Button onClick={() => onResume(session)}>{status.actionLabel}</Button>
              )}
              {status.action && boundaryMatches && !canEnter && (
                <StatusPill tone="warn" size="sm">
                  {status.action === "view" ? "当前账号不可查看" : "只读账号 · 不可续做或复核"}
                </StatusPill>
              )}
              {status.action && !boundaryMatches && (
                <StatusPill tone="danger" size="sm">边界不一致 · 禁止进入</StatusPill>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
