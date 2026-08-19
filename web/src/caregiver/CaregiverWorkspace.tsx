import { useCallback, useEffect, useId, useRef, useState } from "react";
import { isProviderReadinessPrewriteConflict } from "../autopilot/providerReadiness";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { StatusPill } from "../components/StatusPill";
import {
  CAREGIVER_END_REASONS,
  CAREGIVER_DEMO_BOUNDARY_MESSAGE,
  CAREGIVER_HELP_REASONS,
  caregiverActionAvailability,
  caregiverHelpPrompt,
  caregiverPatientPresenceLabel,
  caregiverSessionIsTerminal,
  caregiverStatusPresentation,
  makeCaregiverEndRequest,
  type CaregiverApi,
  type CaregiverEndSelection,
  type CaregiverHelpHumanState,
  type CaregiverHelpReasonCode,
  type CaregiverHelpStatus,
  type CaregiverIdentity,
  type CaregiverPlan,
  type CaregiverSessionStatus,
  type CaregiverSessionSummary,
  type CaregiverToday,
} from "./caregiverPolicy";

type BusyAction =
  | "start"
  | "start-practice"
  | "pause"
  | "help"
  | "take-over"
  | "end"
  | "logout"
  | null;

const PROVIDER_NOT_READY_MESSAGE = "语音服务未准备，请联系管理员。";

interface EndedView {
  session: CaregiverSessionSummary;
  title: string;
  detail: string;
}

export interface CaregiverWorkspaceProps {
  api: CaregiverApi;
  identity: CaregiverIdentity;
  onLogout: () => Promise<void>;
}

function scheduleText(plan: CaregiverPlan): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(plan.scheduledDate);
  const date = match ? `${Number(match[2])}月${Number(match[3])}日` : plan.scheduledDate;
  const time = plan.scheduledTime?.slice(0, 5) ?? "时间待定";
  return `${date} ${time}`;
}

function newRequestKey(): string {
  return `cg-${crypto.randomUUID()}`;
}

function CaregiverHeader({ identity, disabled, exiting, onLogout }: {
  identity: CaregiverIdentity;
  disabled: boolean;
  exiting: boolean;
  onLogout: () => void;
}) {
  return (
    <header className="app-header">
      <div className="app-brand">
        <div className="app-brand-mark" aria-hidden>护</div>
        <div className="col" style={{ gap: 0 }}>
          <strong>语言训练 · 演练模式</strong>
          <span className="muted" style={{ fontSize: "0.82em" }}>床旁操作台</span>
        </div>
      </div>
      <div className="toolbar-account">
        <span className="muted" title={identity.username ? `账号 ${identity.username}` : undefined}>
          当前人员 {identity.displayId}
        </span>
        <Button type="button" disabled={disabled} onClick={onLogout}>
          {exiting ? "正在安全退出…" : "退出"}
        </Button>
      </div>
    </header>
  );
}

function TodayScreen({ today, loading, onStart, startingPlanId, busy }: {
  today: CaregiverToday | null;
  loading: boolean;
  onStart: (plan: CaregiverPlan) => void;
  startingPlanId: string | null;
  busy: boolean;
}) {
  return (
    <section className="page-shell page-shell--medium" aria-labelledby="caregiver-today-title">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">今天任务</p>
          <h1 className="page-title" id="caregiver-today-title">今天要做的练习</h1>
        </div>
        {today && <StatusPill tone="primary">{today.asOfDateLabel}</StatusPill>}
      </header>

      {loading && <Alert title="正在查看今天的安排">请稍等，页面会自动显示。</Alert>}

      {!loading && today?.plans.length === 0 && (
        <Alert tone="ok" title="现在没有待开始任务">
          有新安排会自动显示。
        </Alert>
      )}

      {today && today.withheldCount > 0 && (
        <Alert tone="warn" compact title="有安排暂不能开始">
          另有 {today.withheldCount} 条安排暂时不能开始，请联系负责人。
        </Alert>
      )}

      {today && today.plans.length > 0 && (
        <div className="visit-plan-queue" role="list" aria-label="今天的练习安排">
          {today.plans.map((plan) => {
            const starting = startingPlanId === plan.planId;
            return (
              <article className="run-picker-card" role="listitem" key={plan.planId}>
                <div className="run-picker-head">
                  <div className="col" style={{ gap: 6 }}>
                    <StatusPill tone="warn">演练</StatusPill>
                    <strong style={{ fontSize: "1.15em" }}>{plan.participantLabel}</strong>
                    <span>{scheduleText(plan)}</span>
                    <span className="muted">{plan.weekLabel} · {plan.phaseLabel}</span>
                  </div>
                  <Button
                    type="button"
                    variant="primary"
                    size="lg"
                    disabled={busy}
                    onClick={() => onStart(plan)}
                  >
                    {starting ? "正在开始…" : "开始本次"}
                  </Button>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

function SessionScreen({
  session,
  status,
  busy,
  busyAction,
  onStartPractice,
  onPause,
  onHelp,
  onTakeOver,
  onEnd,
}: {
  session: CaregiverSessionSummary;
  status: CaregiverSessionStatus | null;
  busy: boolean;
  busyAction: BusyAction;
  onStartPractice: () => void;
  onPause: () => void;
  onHelp: () => void;
  onTakeOver: () => void;
  onEnd: () => void;
}) {
  const presentation = status ? caregiverStatusPresentation(status) : null;
  const actions = status ? caregiverActionAvailability(status) : null;
  const operationalDemoReady = session.operationalDemoReady
    && status?.operationalDemoReady === true;
  const safeCloseoutOnly = !session.operationalDemoReady
    || status?.operationalDemoReady === false;
  const waitingForPatient = status?.practiceState === "not_started" && status.patientPresence !== "online";
  const showStart = operationalDemoReady
    && status?.runtimeState === "active"
    && status.practiceState === "not_started";
  // 状态重新确认期间仍保留纯安全动作。暂停接口无请求体且服务端幂等，
  // 不应因为一次状态读取失败让现场人员失去暂停按钮。
  const showPause = !status || status.runtimeState === "active";
  const showTakeOver = status?.runtimeState === "paused"
    && (status.practiceState === "pausing" || status.practiceState === "paused");

  return (
    <section className="page-shell page-shell--medium" aria-labelledby="caregiver-session-title">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">本次进行中</p>
          <h1 className="page-title" id="caregiver-session-title">{session.participantLabel}</h1>
          <p className="page-description">{session.weekLabel} · {session.phaseLabel}</p>
        </div>
        {status && (
          <StatusPill tone={status.patientPresence === "online" ? "ok" : "muted"}>
            {caregiverPatientPresenceLabel(status.patientPresence)}
          </StatusPill>
        )}
      </header>

      <div className="stack">
        {presentation ? (
          <Alert tone={presentation.tone} title={presentation.title}>{presentation.detail}</Alert>
        ) : (
          <Alert title="正在确认本次状态">请保持页面打开，不要重复点击。</Alert>
        )}

        {safeCloseoutOnly && (
          <Alert tone="warn" title="本次只能暂停或结束">
            本次不能开始练习，只能暂停、求助、接管或结束。如有疑问请联系负责人。
          </Alert>
        )}

        <section className="form-section" aria-labelledby="two-window-title">
          <div className="form-section-header">
            <div>
              <h2 id="two-window-title">两个窗口都要保持打开</h2>
              <p className="muted">老人画面在另一个窗口，两个窗口都不要关。</p>
            </div>
          </div>
          {waitingForPatient && (
            <p>请先打开老人画面，连上后才能开始。</p>
          )}
          {status?.patientPresence === "online" && (
            <p>老人画面已打开。请留在老人旁边。</p>
          )}
        </section>

        <section className="form-section" aria-labelledby="caregiver-actions-title">
          <div className="form-section-header">
            <div>
              <h2 id="caregiver-actions-title">现场操作</h2>
              <p className="muted">如果老人不适或情况不对，立即点“暂停练习”。</p>
            </div>
          </div>

          {/* 固定两列、暂停恒在第一格：按钮位置不随状态漂移，肌肉记忆才有效。 */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
              gap: "var(--sp-3)",
            }}
          >
            {showPause && (
              <Button
                type="button"
                variant="danger"
                size="lg"
                fullWidth
                disabled={busy || (actions !== null && !actions.pause)}
                onClick={onPause}
              >
                {busyAction === "pause" ? "正在暂停…" : "暂停练习"}
              </Button>
            )}
            {showStart && (
              <div className="col" style={{ gap: "var(--sp-1)" }}>
                <Button
                  type="button"
                  variant="primary"
                  size="lg"
                  fullWidth
                  disabled={busy || !actions?.startPractice}
                  title={waitingForPatient ? "老人画面打开后才能开始" : undefined}
                  onClick={onStartPractice}
                >
                  {busyAction === "start-practice" ? "正在开始…" : "开始练习"}
                </Button>
                {waitingForPatient && (
                  <span className="muted" style={{ fontSize: "var(--text-sm)" }}>
                    老人画面打开后才能开始
                  </span>
                )}
              </div>
            )}
            <Button
              type="button"
              variant="secondary"
              size="lg"
              fullWidth
              disabled={busy || (actions !== null && !actions.help)}
              onClick={onHelp}
            >
              请求协助
            </Button>
            {showTakeOver && (
              <div className="col" style={{ gap: "var(--sp-1)" }}>
                <Button
                  type="button"
                  variant="secondary"
                  size="lg"
                  fullWidth
                  disabled={busy || !actions?.takeOver}
                  title={!actions?.takeOver ? "正在停止声音，请稍候" : undefined}
                  onClick={onTakeOver}
                >
                  {busyAction === "take-over" ? "正在接管…" : "接管练习"}
                </Button>
                {!actions?.takeOver && (
                  <span className="muted" style={{ fontSize: "var(--text-sm)" }}>
                    正在停止声音，请稍候
                  </span>
                )}
              </div>
            )}
            <Button
              type="button"
              variant="secondary"
              size="lg"
              fullWidth
              disabled={busy || !actions?.end}
              onClick={onEnd}
            >
              结束本次
            </Button>
          </div>

          {status?.runtimeState === "paused" && (
            <Alert tone="warn" compact title="暂停后不会自动继续">
              可接管、结束，或联系负责人。
            </Alert>
          )}
        </section>
      </div>
    </section>
  );
}

function ReasonChoices<T extends string>({
  legend,
  options,
  value,
  name,
  disabled,
  onChange,
}: {
  legend: string;
  options: readonly { code: T; label: string; hint: string }[];
  value: T | null;
  name: string;
  disabled: boolean;
  onChange: (value: T) => void;
}) {
  return (
    <fieldset style={{ border: 0, margin: 0, padding: 0 }} disabled={disabled}>
      <legend style={{ marginBottom: "var(--sp-3)", color: "var(--c-fg)", fontWeight: 650 }}>
        {legend}
      </legend>
      <div className="stack" style={{ gap: "var(--sp-3)" }}>
        {options.map((option) => (
          <label className="check-row" key={option.code}>
            <input
              type="radio"
              name={name}
              value={option.code}
              checked={value === option.code}
              onChange={() => onChange(option.code)}
            />
            <span className="col" style={{ gap: 2 }}>
              <strong style={{ color: "var(--c-fg)" }}>{option.label}</strong>
              <span className="muted" style={{ fontSize: "var(--text-sm)" }}>{option.hint}</span>
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

export function CaregiverWorkspace({ api, identity, onLogout }: CaregiverWorkspaceProps) {
  const helpChoiceName = useId();
  const endChoiceName = useId();
  const [today, setToday] = useState<CaregiverToday | null>(null);
  const [current, setCurrent] = useState<CaregiverSessionSummary | null>(null);
  const [status, setStatus] = useState<CaregiverSessionStatus | null>(null);
  const [ended, setEnded] = useState<EndedView | null>(null);
  const [loadingToday, setLoadingToday] = useState(true);
  const [todayProblem, setTodayProblem] = useState(false);
  const [syncProblem, setSyncProblem] = useState(false);
  const [operationProblem, setOperationProblem] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [helpStatus, setHelpStatus] = useState<CaregiverHelpStatus | null>(null);
  const [busyAction, setBusyAction] = useState<BusyAction>(null);
  const [startingPlanId, setStartingPlanId] = useState<string | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const [helpReason, setHelpReason] = useState<CaregiverHelpReasonCode | null>(null);
  const [endOpen, setEndOpen] = useState(false);
  const [endSelection, setEndSelection] = useState<CaregiverEndSelection | null>(null);
  const requestKeys = useRef(new Map<string, string>());
  const todayRequest = useRef(0);
  const currentSessionId = current?.sessionId ?? null;

  const keyFor = useCallback((scope: string) => {
    const existing = requestKeys.current.get(scope);
    if (existing) return existing;
    const created = newRequestKey();
    requestKeys.current.set(scope, created);
    return created;
  }, []);

  const clearKey = useCallback((scope: string) => {
    requestKeys.current.delete(scope);
  }, []);

  const adoptStatus = useCallback((next: CaregiverSessionStatus) => {
    setStatus((previous) => {
      if (!previous || previous.sessionId !== next.sessionId) return next;
      // 操作回执可能与较早发出的轮询响应交错。两类修订只能向前，
      // 任何一类倒退都拒绝整个候选状态，避免暂停/结束后又显示旧按钮。
      if (next.runtimeRevision < previous.runtimeRevision
        || next.practiceRevision < previous.practiceRevision) return previous;
      return next;
    });
  }, []);

  const loadToday = useCallback(async (adoptCurrent = true, showLoading = true) => {
    const request = ++todayRequest.current;
    if (showLoading) setLoadingToday(true);
    try {
      const next = await api.listToday();
      if (todayRequest.current !== request) return;
      setToday(next);
      setTodayProblem(false);
      if (adoptCurrent && next.currentSession) setCurrent(next.currentSession);
    } catch {
      if (todayRequest.current === request) setTodayProblem(true);
    } finally {
      // 后发静默刷新会取代较早的显式请求；它也要收起旧 loading，
      // 否则严格模式下的交错响应可能让“正在查看”永久挂住。
      if (todayRequest.current === request) setLoadingToday(false);
    }
  }, [api]);

  useEffect(() => {
    const root = document.documentElement;
    root.dataset.scale = "console";
    return () => {
      if (root.dataset.scale === "console") delete root.dataset.scale;
    };
  }, []);

  useEffect(() => {
    void loadToday();
    return () => { todayRequest.current += 1; };
  }, [loadToday]);

  useEffect(() => {
    if (!todayProblem || current || ended) return;
    const timer = window.setTimeout(() => { void loadToday(true, false); }, 5000);
    return () => window.clearTimeout(timer);
  }, [current, ended, loadToday, todayProblem]);

  useEffect(() => {
    if (current || ended) return;
    const timer = window.setInterval(() => { void loadToday(true, false); }, 10000);
    return () => window.clearInterval(timer);
  }, [current, ended, loadToday]);

  useEffect(() => {
    if (!currentSessionId) return;
    const sessionId = currentSessionId;
    let closed = false;
    let checking = false;

    const check = async (activate: boolean) => {
      if (checking) return;
      checking = true;
      try {
        let next: CaregiverSessionStatus;
        if (activate) {
          try {
            next = await api.activateSession(sessionId);
          } catch {
            next = await api.getSessionStatus(sessionId);
          }
        } else {
          next = await api.getSessionStatus(sessionId);
        }
        if (!closed) {
          adoptStatus(next);
          setSyncProblem(false);
        }
      } catch {
        if (!closed) setSyncProblem(true);
      } finally {
        checking = false;
      }
    };

    setStatus(null);
    setSyncProblem(false);
    void check(true);
    const timer = window.setInterval(() => { void check(false); }, 2500);
    return () => {
      closed = true;
      window.clearInterval(timer);
    };
  }, [adoptStatus, api, currentSessionId]);

  useEffect(() => {
    if (!current || !status || !caregiverSessionIsTerminal(status)) return;
    const presentation = caregiverStatusPresentation(status);
    setEnded({ session: current, title: presentation.title, detail: presentation.detail });
    setCurrent(null);
    setStatus(null);
    setNotice(null);
    void loadToday(false, false);
  }, [current, loadToday, status]);

  const reportUnconfirmed = (label: string) => {
    setOperationProblem(`“${label}”还没确认成功，页面会自动重新检查，请稍候。`);
  };

  const startPlan = async (plan: CaregiverPlan) => {
    if (busyAction) return;
    const scope = `start:${plan.planId}:${plan.revision}`;
    setBusyAction("start");
    setStartingPlanId(plan.planId);
    setOperationProblem(null);
    setNotice(null);
    try {
      const session = await api.startVisitPlan(plan.planId, {
        idempotencyKey: keyFor(scope),
        expectedRevision: plan.revision,
      });
      clearKey(scope);
      setCurrent(session);
      setToday((previous) => previous
        ? { ...previous, plans: previous.plans.filter((candidate) => candidate.planId !== plan.planId) }
        : previous);
      setNotice("已开始。请确认老人画面已打开。");
    } catch (error) {
      if (isProviderReadinessPrewriteConflict(error)) {
        setOperationProblem(PROVIDER_NOT_READY_MESSAGE);
      } else {
        reportUnconfirmed("开始本次");
      }
    } finally {
      setBusyAction(null);
      setStartingPlanId(null);
    }
  };

  const startPractice = async () => {
    if (!current || !status || busyAction) return;
    const actions = caregiverActionAvailability(status);
    if (!current.operationalDemoReady || !actions.startPractice) return;
    const scope = `start-practice:${current.sessionId}`;
    setBusyAction("start-practice");
    setOperationProblem(null);
    setNotice(null);
    try {
      const next = await api.startPractice(current.sessionId, {
        idempotencyKey: keyFor(scope),
        expectedRevision: status.practiceRevision,
      });
      clearKey(scope);
      adoptStatus(next);
      setNotice("练习已开始。请留在老人旁边观察。");
    } catch (error) {
      if (isProviderReadinessPrewriteConflict(error)) {
        setOperationProblem(PROVIDER_NOT_READY_MESSAGE);
      } else {
        reportUnconfirmed("开始练习");
      }
    } finally {
      setBusyAction(null);
    }
  };

  const pausePractice = async () => {
    if (!current || busyAction) return;
    if (status && (caregiverSessionIsTerminal(status) || status.runtimeState === "paused")) return;
    setBusyAction("pause");
    setOperationProblem(null);
    setNotice(null);
    try {
      const next = await api.pauseSession(current.sessionId);
      adoptStatus(next);
      setNotice("已暂停。请先照顾老人，再选择下一步。");
    } catch {
      reportUnconfirmed("暂停练习");
    } finally {
      setBusyAction(null);
    }
  };

  const submitHelp = async () => {
    if (!current || !helpReason || busyAction) return;
    if (status && !caregiverActionAvailability(status).help) return;
    const scope = `help:${current.sessionId}:${helpReason}`;
    setBusyAction("help");
    setOperationProblem(null);
    setNotice(null);
    try {
      const receipt = await api.requestHelp(current.sessionId, {
        reasonCode: helpReason,
        idempotencyKey: keyFor(scope),
      });
      clearKey(scope);
      adoptStatus(receipt.status);
      setHelpOpen(false);
      setHelpReason(null);
      setNotice(null);
      try {
        setHelpStatus(await api.getHelpStatus(current.sessionId, receipt.requestId));
      } catch {
        // 读不到四态投影不影响"已经登记"这个事实，但不能因此说得更乐观。
        setHelpStatus({
          requestId: receipt.requestId, state: "recorded",
          notifyChannelConfigured: false, deliveryReachable: false, reached: [],
        });
      }
    } catch {
      setHelpOpen(false);
      setHelpReason(null);
      reportUnconfirmed("请求协助");
    } finally {
      setBusyAction(null);
    }
  };

  const recordHelpDisposition = async (state: CaregiverHelpHumanState) => {
    if (!current || !helpStatus || busyAction) return;
    setBusyAction("help");
    setOperationProblem(null);
    try {
      setHelpStatus(await api.recordHelpDisposition(
        current.sessionId, helpStatus.requestId,
        { state, note: state === "acknowledged" ? "工作人员已到场" : "现场已处理" }));
    } catch {
      reportUnconfirmed(state === "acknowledged" ? "记录到场" : "记录处理完成");
    } finally {
      setBusyAction(null);
    }
  };

  const takeOverPractice = async () => {
    if (!current || !status || busyAction) return;
    const actions = caregiverActionAvailability(status);
    if (!actions.takeOver) return;
    const scope = `take-over:${current.sessionId}`;
    setBusyAction("take-over");
    setOperationProblem(null);
    setNotice(null);
    try {
      const next = await api.takeOverSession(current.sessionId, {
        idempotencyKey: keyFor(scope),
        expectedRevision: status.practiceRevision,
      });
      clearKey(scope);
      adoptStatus(next);
      setNotice("已接管。系统已停止自动练习。");
    } catch {
      reportUnconfirmed("接管练习");
    } finally {
      setBusyAction(null);
    }
  };

  const submitEnd = async () => {
    if (!current || !status || !endSelection || busyAction) return;
    const actions = caregiverActionAvailability(status);
    if (!actions.end) return;
    const scope = `end:${current.sessionId}:${endSelection}`;
    setBusyAction("end");
    setOperationProblem(null);
    setNotice(null);
    try {
      const next = await api.endSession(
        current.sessionId,
        makeCaregiverEndRequest(endSelection, status, keyFor(scope)),
      );
      clearKey(scope);
      setEndOpen(false);
      setEndSelection(null);
      adoptStatus(next);
      if (!caregiverSessionIsTerminal(next)) {
        setOperationProblem("正在确认结束，请留在本页稍候。");
      }
    } catch {
      setEndOpen(false);
      setEndSelection(null);
      reportUnconfirmed("结束本次");
    } finally {
      setBusyAction(null);
    }
  };

  const logoutSafely = async () => {
    if (busyAction) return;
    setBusyAction("logout");
    setOperationProblem(null);
    setNotice(null);
    try {
      if (current) {
        if (!status) {
          setOperationProblem("正在确认安全暂停，请稍候再退出。");
          return;
        }
        if (status.runtimeState === "active") {
          const actions = caregiverActionAvailability(status);
          if (!actions.pause) {
            setOperationProblem("本次尚未确认安全暂停，已阻止退出。请先联系负责人。");
            return;
          }
          const paused = await api.pauseSession(current.sessionId);
          if (paused.runtimeState !== "paused") {
            adoptStatus(paused);
            setOperationProblem("本次还没有确认安全暂停，已阻止退出。");
            return;
          }
          adoptStatus(paused);
        }
      }
      await onLogout();
    } catch {
      reportUnconfirmed("安全退出");
    } finally {
      setBusyAction(null);
    }
  };

  const showToday = () => {
    setEnded(null);
    setOperationProblem(null);
    setNotice(null);
    void loadToday();
  };

  const anyBusy = busyAction !== null;

  return (
    <>
      <CaregiverHeader
        identity={identity}
        disabled={anyBusy}
        exiting={busyAction === "logout"}
        onLogout={() => { void logoutSafely(); }}
      />
      <div className="app-main">
        <main aria-busy={anyBusy || undefined}>
          <div className="stack">
            <Alert tone="warn" title="使用范围固定">
              {CAREGIVER_DEMO_BOUNDARY_MESSAGE}
            </Alert>
            {todayProblem && !current && !ended && (
              <Alert tone="danger" title="还没有读到今天的安排">
                正在自动重试，请保持页面打开。
              </Alert>
            )}
            {syncProblem && current && (
              <Alert tone="warn" title="正在重新确认状态">
                请勿重复操作。如老人不适，先关掉设备声音，并立即联系负责人。
              </Alert>
            )}
            {operationProblem && (
              <Alert
                tone="danger"
                title={operationProblem === PROVIDER_NOT_READY_MESSAGE
                  ? "暂时不能开始"
                  : "这一步还没有确认"}
              >
                {operationProblem}
              </Alert>
            )}
            {notice && <Alert tone="ok" title="已确认">{notice}</Alert>}
            {helpStatus && (() => {
              const prompt = caregiverHelpPrompt(helpStatus);
              return (
                <Alert tone={prompt.tone} title="求助已登记">
                  <p>{prompt.text}</p>
                  <div className="form-actions">
                    {prompt.canRecordArrival && (
                      <Button
                        type="button" variant="secondary"
                        disabled={busyAction !== null}
                        onClick={() => { void recordHelpDisposition("acknowledged"); }}
                      >已有人到场</Button>
                    )}
                    {prompt.canRecordResolved && (
                      <Button
                        type="button" variant="secondary"
                        disabled={busyAction !== null}
                        onClick={() => { void recordHelpDisposition("resolved"); }}
                      >已处理完成</Button>
                    )}
                  </div>
                </Alert>
              );
            })()}

            {ended ? (
              <section className="page-shell page-shell--medium" aria-labelledby="caregiver-ended-title">
                <div className="form-section">
                  <p className="page-kicker">已结束</p>
                  <h1 className="page-title" id="caregiver-ended-title">{ended.title}</h1>
                  <p>{ended.session.participantLabel} · {ended.session.weekLabel} · {ended.session.phaseLabel}</p>
                  <Alert tone="ok">{ended.detail}</Alert>
                  <div className="form-actions">
                    <Button type="button" variant="primary" size="lg" onClick={showToday}>查看今天任务</Button>
                  </div>
                </div>
              </section>
            ) : current ? (
              <SessionScreen
                session={current}
                status={status}
                busy={anyBusy}
                busyAction={busyAction}
                onStartPractice={() => { void startPractice(); }}
                onPause={() => { void pausePractice(); }}
                onHelp={() => { setHelpReason(null); setHelpOpen(true); }}
                onTakeOver={() => { void takeOverPractice(); }}
                onEnd={() => { setEndSelection(null); setEndOpen(true); }}
              />
            ) : (
              <TodayScreen
                today={today}
                loading={loadingToday}
                startingPlanId={startingPlanId}
                busy={anyBusy}
                onStart={(plan) => { void startPlan(plan); }}
              />
            )}
          </div>
        </main>
      </div>

      <ConfirmDialog
        open={helpOpen}
        title="请求协助"
        body={(
          <div className="stack">
            <p>提交后练习会暂停并留下记录。请再当面通知负责人。</p>
            <ReasonChoices
              legend="请选择最符合的情况"
              options={CAREGIVER_HELP_REASONS}
              value={helpReason}
              name={helpChoiceName}
              disabled={busyAction === "help"}
              onChange={setHelpReason}
            />
          </div>
        )}
        confirmLabel={busyAction === "help" ? "正在暂停并记录…" : "暂停并记录请求"}
        confirmDisabled={helpReason === null}
        busy={busyAction === "help"}
        onConfirm={() => { void submitHelp(); }}
        onCancel={() => { setHelpReason(null); setHelpOpen(false); }}
      />

      <ConfirmDialog
        open={endOpen}
        title="结束本次"
        body={(
          <div className="stack">
            <p>结束后，本页不能重新开始本次练习。</p>
            <ReasonChoices
              legend="本次为什么结束？"
              options={CAREGIVER_END_REASONS}
              value={endSelection}
              name={endChoiceName}
              disabled={busyAction === "end"}
              onChange={setEndSelection}
            />
          </div>
        )}
        confirmLabel={busyAction === "end" ? "正在确认结束…" : "确认结束本次"}
        confirmDisabled={endSelection === null}
        busy={busyAction === "end"}
        onConfirm={() => { void submitEnd(); }}
        onCancel={() => { setEndSelection(null); setEndOpen(false); }}
      />
    </>
  );
}
