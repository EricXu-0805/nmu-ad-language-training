import { lazy, Suspense, useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";
import { PATIENT_VIEW_EVENT, PATIENT_VIEW_EXIT_EVENT } from "../sync/messages";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { StatusPill } from "../components/StatusPill";
import { ToastProvider } from "../components/Toast";
import { AbnormalDrawer } from "./abnormal/AbnormalDrawer";
import { AnalysisScreen } from "./AnalysisScreen";
import {
  clearConsoleState,
  consoleReducer,
  loadConsoleState,
  persistConsoleState,
  type ConsoleArea,
} from "./consoleReducer";
import { LoginScreen } from "./LoginScreen";
import { RelationshipConsoleScreen } from "./relationship/RelationshipConsoleScreen";
import { RunPickerScreen } from "./RunPickerScreen";
import { SessionWrapupScreen } from "./SessionWrapupScreen";
import { SessionAbortControl } from "./SessionAbortControl";
import { SubjectRegistryScreen } from "./SubjectRegistryScreen";
import { TrainingConsoleScreen } from "./scoring/TrainingConsoleScreen";
import { useConsoleAuth } from "./useConsoleAuth";
import { usePatientPresence, type PatientPresenceView } from "../sync/usePatientPresence";
import { usePatientRec } from "../sync/useCursorWriter";
import { PATIENT_VIEW_REC_EVENT } from "../sync/messages";
import type { AuthIdentity } from "../types";
import {
  identityCanExportResearchData,
  identityCanOperateTraining,
  identityCanPhysicallyDeleteAudio,
  identityIsCaregiverOperator,
  shouldMountConsoleWorkspace,
} from "./authPolicy";
import { makeSessionSafeToExit } from "./sessionExitSafety";
import { bedsideProtocolRoute } from "./protocolAdmission";
import type { Session } from "../types";
import { useSessionRuntime } from "../hooks/useSessionRuntime";

// 操作端顶层外壳。设 data-scale=console;三区标签(准备/训练/分析)+ run 区 reducer 驱动
// 今日已审核安排→服务器原子开场/异常恢复→训练/关系建立→收尾。场次编号是后台概念,前端流程不呈现。
const AREA_TABS: { key: ConsoleArea; label: string; hint: string }[] = [
  { key: "prep", label: "准备区", hint: "建档、协议核对与训练安排" },
  { key: "run", label: "训练台", hint: "今天可以开始的训练" },
  { key: "analyze", label: "分析后台", hint: "回看 AI 判定与录音" },
];

const CaregiverWorkspaceEntry = lazy(async () => {
  const module = await import("../caregiver/CaregiverWorkspaceEntry");
  return { default: module.CaregiverWorkspaceEntry };
});

export function ConsoleShell() {
  const auth = useConsoleAuth();
  if (auth.mode === "loading") {
    return <main className="login-shell"><div className="login-card"><p className="muted">正在检查登录…</p></div></main>;
  }
  if (auth.mode === "error") {
    return (
      <main className="login-shell">
        <section className="login-card" aria-labelledby="auth-check-failure-title">
          <div className="login-brand" aria-hidden>语</div>
          <div className="page-kicker">无法连接</div>
          <h1 className="login-title" id="auth-check-failure-title">认证检查失败</h1>
          <Alert tone="danger" title="研究者工作台未打开">
            {auth.failure ?? "暂时无法确认登录状态。"}
          </Alert>
          <p className="muted">请检查网络后重试。</p>
          <Button type="button" variant="primary" onClick={() => { void auth.refresh(); }}>重新检查</Button>
        </section>
      </main>
    );
  }
  if (auth.mode === "login") {
    return <LoginScreen onLoggedIn={auth.refresh} />;
  }
  if (!shouldMountConsoleWorkspace(auth.mode)) return null;
  if (identityIsCaregiverOperator(auth.identity)) {
    return (
      <Suspense fallback={(
        <main className="login-shell">
          <div className="login-card"><p className="muted">正在打开照护员操作台…</p></div>
        </main>
      )}>
        <CaregiverWorkspaceEntry
          key={auth.identity.username}
          identity={auth.identity}
          onLogout={auth.logout}
        />
      </Suspense>
    );
  }
  return (
    <ConsoleWorkspace
      key={auth.identity?.username ?? "LOCAL-M0"}
      identity={auth.identity}
      onLogout={auth.logout}
    />
  );
}

function ConsoleWorkspace({ identity, onLogout }: {
  identity: AuthIdentity | null;
  onLogout: () => Promise<void>;
}) {
  const [state, dispatch] = useReducer(consoleReducer, loadConsoleState(identity));
  const [abnormalOpen, setAbnormalOpen] = useState(false);
  const [confirmLeave, setConfirmLeave] = useState<ConsoleArea | null>(null);
  const [currentItemEventId, setCurrentItemEventId] = useState<number | null>(null);
  const [wrapupCompletionBusy, setWrapupCompletionBusy] = useState(false);
  const [runPickerBusy, setRunPickerBusy] = useState(false);
  const [closeoutGateBlocked, setCloseoutGateBlocked] = useState(false);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [logoutSafetyError, setLogoutSafetyError] = useState<string | null>(null);
  const [leaveBusy, setLeaveBusy] = useState(false);
  const [leaveError, setLeaveError] = useState<string | null>(null);
  const leaveBusyRef = useRef(false);
  // 本地显式关闭账号认证的 M0 环境没有 identity；生产具名账号则严格按角色收口。
  const canOperateTraining = identityCanOperateTraining(identity);
  const patientPresence = usePatientPresence(state.session?.session_id);
  const inLiveSession = state.area === "run" && !!state.session
    && (state.screen === "training" || state.screen === "relationship");
  // 单机一条流:训练中把受试者画面全屏叠在操作端之上;操作端保持挂载,
  // 游标写者/自动驾驶在底下继续驱动(它是唯一驾驶员,同窗跳路由会把它卸载)。
  // 叠层本体挂在 App 路由层(红线守卫禁止 console/** import 老人端源码),
  // 这里只持有开关状态,经 window 事件与宿主同步。
  const [patientView, setPatientView] = useState(false);
  const [confirmPatientView, setConfirmPatientView] = useState(false);
  const patientRec = usePatientRec(state.session?.session_id ?? "");
  // 本机叠层自己的心跳也会把在场打成 online;刚关掉自己的叠层又重进不该被当成"另一台设备"。
  const overlayClosedAt = useRef(0);

  useEffect(() => {
    const root = document.documentElement;
    root.dataset.scale = "console";
    return () => { if (root.dataset.scale === "console") delete root.dataset.scale; };
  }, []);
  // 开关状态广播给 App 层宿主。不做卸载时兜底关闭:同路由内操作端只在崩溃(Boundary
  // 接管)时卸载,那一刻恰恰要让叠层留着——受试者继续看平和画面,而不是红色报错屏。
  useEffect(() => {
    window.dispatchEvent(new CustomEvent(PATIENT_VIEW_EVENT, { detail: { open: patientView } }));
  }, [patientView]);
  // 场次结束/离开训练即收起叠层;叠层收起后把 PatientShell 清理掉的字号尺度接回来。
  useEffect(() => { if (!inLiveSession && patientView) closePatientView(); }, [inLiveSession]); // eslint-disable-line react-hooks/exhaustive-deps
  useLayoutEffect(() => {
    if (!patientView) document.documentElement.dataset.scale = "console";
  }, [patientView]);
  // 退出通道:仅宿主的「按住→确认」。不接 Escape——单键瞬时退出会绕过防误触门槛。
  useEffect(() => {
    if (!patientView) return;
    const exit = () => closePatientView();
    window.addEventListener(PATIENT_VIEW_EXIT_EVENT, exit);
    return () => window.removeEventListener(PATIENT_VIEW_EXIT_EVENT, exit);
  }, [patientView]); // eslint-disable-line react-hooks/exhaustive-deps
  // 历史栈守卫:平板边缘回滑(popstate)不得一步退出训练直达落地页——压回哨兵原地不动。
  useEffect(() => {
    if (!patientView) return;
    history.pushState({ nmuPatientView: true }, "");
    const onPop = () => { history.pushState({ nmuPatientView: true }, ""); };
    window.addEventListener("popstate", onPop);
    return () => {
      window.removeEventListener("popstate", onPop);
      if ((history.state as { nmuPatientView?: boolean } | null)?.nmuPatientView) history.back();
    };
  }, [patientView]);
  // 录音真值转发给宿主退出钮:按住返回时若受试者正在作答,给研究者一个"录音中"提示。
  useEffect(() => {
    if (!patientView) return;
    window.dispatchEvent(new CustomEvent(PATIENT_VIEW_REC_EVENT, { detail: { active: patientRec?.active === true } }));
  }, [patientView, patientRec?.active]);

  const openPatientView = () => {
    setConfirmPatientView(false);
    setPatientView(true);
    // 平板上尽力全屏(去浏览器栏);拒绝(如 iOS Safari)不影响叠层本身。
    document.documentElement.requestFullscreen?.().catch(() => { /* 平台不支持即忽略 */ });
  };
  const enterPatientView = () => {
    // 已有受试者端在线(遗留窗口/另一设备)时再叠一个 = 双开麦、同一环节两份录音——先确认。
    // 15 秒宽限抵消本机叠层自己的心跳残留(退出→马上重进是常规操作,不该弹)。
    if (patientPresence.state === "online" && Date.now() - overlayClosedAt.current > 15000) {
      setConfirmPatientView(true);
    } else openPatientView();
  };
  const closePatientView = () => {
    overlayClosedAt.current = Date.now();
    setPatientView(false);
    if (document.fullscreenElement) document.exitFullscreen().catch(() => { /* 已不在全屏 */ });
  };
  useEffect(() => { persistConsoleState(state, identity); }, [identity, state]);
  useEffect(() => { window.scrollTo({ top: 0, behavior: "auto" }); }, [state.screen, state.area]);

  // 切区:训练中(training/relationship)切走要确认——防误触离开正在进行的现场。
  // 拒绝切换时把原因写进 logoutSafetyError 提示条:tab 用 aria-disabled 而非 disabled,
  // 点击仍会走到这里被拒——行为不变,只是触屏上能看到原因。
  const requestArea = (area: ConsoleArea) => {
    if (area === state.area) return;
    if (wrapupCompletionBusy || runPickerBusy) {
      setLogoutSafetyError(runPickerBusy ? "正在开始训练，请稍候再切换。" : "正在保存本场结果，请稍候再切换。");
      return;
    }
    if (closeoutGateBlocked && area !== "run") {
      setLogoutSafetyError("请先保存本场收尾，再切换。");
      return;
    }
    if (inLiveSession && area !== "run") {
      setLeaveError(null);
      setConfirmLeave(area);
    }
    else dispatch({ t: "setArea", area });
  };

  const requestLogout = async () => {
    if (logoutBusy) return;
    if (wrapupCompletionBusy || runPickerBusy) {
      setLogoutSafetyError(runPickerBusy ? "正在开始训练，完成前不能退出。" : "正在保存本场结果，完成前不能退出。");
      return;
    }
    if (closeoutGateBlocked) {
      setLogoutSafetyError("请先完成本场收尾，再退出。");
      return;
    }
    setLogoutBusy(true);
    setLogoutSafetyError(null);
    try {
      if (state.session) {
        const runtime = await makeSessionSafeToExit(api, state.session.session_id);
        dispatch({ t: "sessionRuntimeUpdated", status: runtime.status });
      }
      clearConsoleState(identity);
      await onLogout();
    } catch (error) {
      const detail = error instanceof ApiError ? error.detail
        : error instanceof Error && error.message.trim() ? error.message
          : "服务器未确认场次已安全暂停";
      setLogoutSafetyError(`${detail}。当前工作已保留，请稍后再试。`);
    } finally {
      setLogoutBusy(false);
    }
  };

  const requestSessionExit = () => {
    setLeaveError(null);
    setConfirmLeave("run");
  };

  const leaveSessionAfterServerSafe = async () => {
    if (leaveBusyRef.current || !confirmLeave) return;
    const target = confirmLeave;
    if (!state.session) {
      setConfirmLeave(null);
      if (target === "run") dispatch({ t: "resetRun" });
      else dispatch({ t: "setArea", area: target });
      return;
    }
    leaveBusyRef.current = true;
    setLeaveBusy(true);
    setLeaveError(null);
    try {
      const runtime = await makeSessionSafeToExit(api, state.session.session_id);
      dispatch({ t: "sessionRuntimeUpdated", status: runtime.status });
      setConfirmLeave(null);
      if (target === "run") dispatch({ t: "resetRun" });
      else dispatch({ t: "setArea", area: target });
    } catch (error) {
      const detail = error instanceof ApiError ? error.detail
        : error instanceof Error && error.message.trim() ? error.message
          : "服务器未确认场次已安全暂停";
      setLeaveError(detail);
    } finally {
      leaveBusyRef.current = false;
      setLeaveBusy(false);
    }
  };

  return (
    <ToastProvider>
      <header className="app-header" inert={patientView || undefined}>
        <div className="app-brand">
          <div className="app-brand-mark" aria-hidden>语</div>
          <div className="col" style={{ gap: 0 }}>
            <strong>语言沟通训练系统</strong>
            <span className="muted" style={{ fontSize: "0.82em" }}>研究者工作台</span>
          </div>
        </div>
        <nav className="area-tabs" aria-label="工作台切换">
          {/* aria-disabled 而非 disabled:点击仍进 requestArea 被拒并给出可见原因(触屏没有 hover/title)。 */}
          {AREA_TABS.map((t) => (
            <button key={t.key} className={`area-tab${state.area === t.key ? " is-active" : ""}`}
              aria-current={state.area === t.key ? "page" : undefined} title={t.hint}
              aria-disabled={wrapupCompletionBusy || runPickerBusy
                || (closeoutGateBlocked && t.key !== "run") || undefined}
              onClick={() => requestArea(t.key)}>{t.label}</button>
          ))}
        </nav>
        <div className="toolbar">
          {canOperateTraining && state.area === "run" && state.session
            && (state.screen === "training" || state.screen === "relationship")
            && <Button onClick={() => setAbnormalOpen(true)}>记录现场异常或介入</Button>}
          {identity && (
            <div className="toolbar-account">
              <span className="muted" title={`账号 ${identity.username}`}>账号 {identity.display_id}</span>
              {/* aria-disabled 同上:被拒时 requestLogout 会给出可见原因;title 仅作桌面补充。 */}
              <Button aria-disabled={runPickerBusy || wrapupCompletionBusy || logoutBusy || undefined}
                title={runPickerBusy ? "正在开始训练，完成前不能退出"
                  : wrapupCompletionBusy ? "正在保存本场结果，完成前不能退出"
                    : closeoutGateBlocked ? "请先完成本场收尾" : undefined}
                onClick={() => { void requestLogout(); }}>{logoutBusy ? "正在安全退出…" : "退出"}</Button>
            </div>
          )}
        </div>
      </header>

      <div className="app-main" inert={patientView || undefined}>
        {inLiveSession && state.session && (
          <SessionContextBar patientId={state.session.patient_id}
            weekNo={state.session.week_no} phase={state.session.phase_type} eventLine={state.session.event_line}
            presence={patientPresence}
            onEnd={requestSessionExit} onPatientView={enterPatientView} />
        )}

        <main>
          {logoutSafetyError && (
            <Alert tone="danger" title="已阻止不安全的离开"
              actions={<Button onClick={() => setLogoutSafetyError(null)}>知道了</Button>}>
              {logoutSafetyError}
            </Alert>
          )}
          {state.area === "prep" && <SubjectRegistryScreen
            canManagePlans={canOperateTraining} actorRole={identity?.role ?? null}
            onSessionStarted={(s) => dispatch({ t: "sessionStarted", session: s })} />}
          {state.area === "analyze" && <AnalysisScreen />}
          {state.area === "run" && state.screen === "picker" && (
            <RunPickerScreen
              canStart={canOperateTraining}
              canReview={canOperateTraining}
              canViewCompleted={true}
              onStartingChange={setRunPickerBusy}
              onResume={(s) => dispatch({ t: "sessionStarted", session: s })}
              onGoPrep={() => dispatch({ t: "setArea", area: "prep" })} />
          )}
          {state.area === "run" && state.screen === "training" && state.session && (
            <TrainingConsoleScreen key={state.session.session_id} session={state.session}
              hasNamedAccount={identity !== null}
              onWrapup={() => dispatch({ t: "goWrapup" })} onExit={requestSessionExit}
              onItemEventChange={setCurrentItemEventId} />
          )}
          {state.area === "run" && state.screen === "relationship" && state.session && (
            <RelationshipConsoleScreen key={state.session.session_id} session={state.session}
              onWrapup={() => dispatch({ t: "goWrapup" })} onExit={requestSessionExit} />
          )}
          {state.area === "run" && state.screen === "unsupported" && state.session && (
            <UnsupportedProtocolScreen session={state.session} onExit={requestSessionExit}
              onWrapup={() => dispatch({ t: "goWrapup" })} />
          )}
          {state.area === "run" && state.screen === "wrapup" && state.session && (
            <SessionWrapupScreen session={state.session}
              reviewerId={identity?.display_id ?? state.session.trainer_id ?? "PIN/本地"}
              canExport={identityCanExportResearchData(identity)}
              canDeleteAudio={identityCanPhysicallyDeleteAudio(identity)}
              onBack={() => dispatch({ t: "backToSession" })}
              onRuntimeStatusChange={(status) => dispatch({ t: "sessionRuntimeUpdated", status })}
              onDone={() => dispatch({ t: "resetRun" })}
              onCompletionBusyChange={setWrapupCompletionBusy}
              onCloseoutGateChange={setCloseoutGateBlocked} />
          )}
        </main>
      </div>

      <ConfirmDialog open={confirmPatientView}
        title="另一个受试者端似乎在线"
        body="另一台设备（或窗口）已打开本场次，再在本机打开会同时录下两份声音。确定在本机打开吗？"
        confirmLabel="仍在本机打开"
        confirmVariant="primary"
        onConfirm={openPatientView}
        onCancel={() => setConfirmPatientView(false)} />

      <ConfirmDialog open={confirmLeave !== null}
        title={confirmLeave === "run" ? "暂停当前训练并返回待办？" : "暂停当前训练并离开？"}
        body={`${leaveError ? `上次未能安全离开：${leaveError}。` : ""}离开前会先安全暂停录音和自动推进。已保存的数据不会丢，之后可以继续本场。`}
        confirmLabel={leaveBusy ? "正在确认安全状态…" : confirmLeave === "run" ? "安全暂停并返回" : "安全暂停并离开"}
        confirmVariant="primary"
        busy={leaveBusy}
        onConfirm={() => { void leaveSessionAfterServerSafe(); }}
        onCancel={() => { if (!leaveBusy) { setLeaveError(null); setConfirmLeave(null); } }} />

      {state.area === "run" && state.session && (
        <AbnormalDrawer sessionId={state.session.session_id} phaseType={state.session.phase_type}
          currentItemEventId={state.screen === "training" ? currentItemEventId : null}
          open={abnormalOpen} onClose={() => setAbnormalOpen(false)} />
      )}
    </ToastProvider>
  );
}

function UnsupportedProtocolScreen({ session, onExit, onWrapup }: {
  session: Session;
  onExit: () => void;
  onWrapup: () => void;
}) {
  const runtime = useSessionRuntime(session.session_id);
  const route = bedsideProtocolRoute(session);
  const reason = route.screen === "unsupported"
    ? route.reason
    : "这一场的安排还没有开放，暂时不能开始。";
  return (
    <div className="page-shell page-shell--medium">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">安全阻断</p>
          <h2 className="page-title">本场不能进入床旁操作</h2>
          <p className="page-description">这一场的安排还没有开放，系统已停止。</p>
        </div>
      </header>
      <Alert tone="danger" title="安排尚未开放">
        {reason}{" "}不会开始训练，也不会录音。
      </Alert>
      <div className="form-section">
        <h3>本场安排</h3>
        <p className="muted">第 {session.week_no} 周 · {session.phase_type} · {session.event_line}</p>
        <div className="form-actions">
          <Button variant="primary" onClick={onExit}>安全暂停并返回待办</Button>
          <SessionAbortControl sessionId={session.session_id}
            expectedRevision={runtime.runtime?.revision ?? null}
            disabled={runtime.loading || runtime.busy || runtime.terminal}
            onAborted={() => onWrapup()} />
        </div>
      </div>
    </div>
  );
}

// SessionContextBar:★绝不显示姓名/称呼;场次编号是后台概念,前端不呈现——只出研究编号与本次安排。
function SessionContextBar({ patientId, weekNo, phase, eventLine, presence, onEnd, onPatientView }: {
  patientId: string; weekNo: number; phase: string; eventLine: string; presence: PatientPresenceView;
  onEnd: () => void; onPatientView: () => void;
}) {
  const eventLabel = eventLine === phase ? null : eventLine;
  const presenceTone = presence.state === "online" ? "ok"
    : presence.state === "offline" ? "danger"
      : presence.state === "unavailable" || presence.state === "unsupported" ? "warn" : "muted";
  const presenceLabel = presence.state === "online" ? "受试者端在线"
    : presence.state === "offline" ? "受试者端已断开"
      : presence.state === "unavailable" ? "状态暂不可用"
        : presence.state === "unsupported" ? "该版本不支持"
        : presence.state === "checking" ? "正在确认" : "等待受试者端";
  return (
    <section className="session-context-bar" aria-label="当前场次摘要">
      <div className="session-context-item">
        <span className="muted">受试者</span>
        <strong className="mono">{patientId}</strong>
      </div>
      <div className="session-context-item grow">
        <span className="muted">本次安排</span>
        <strong>第 {weekNo} 周 · {phase}{eventLabel ? ` · ${eventLabel}` : ""}</strong>
      </div>
      <div className="session-presence" role="status" aria-live="polite">
        <StatusPill tone={presenceTone}>{presenceLabel}</StatusPill>
        <div className="session-presence__copy">
          <strong>{presence.screenLabel}</strong>
          {presence.lastSeenLabel && <span>{presence.lastSeenLabel}</span>}
        </div>
      </div>
      <div className="col" style={{ gap: "var(--sp-1)", alignItems: "flex-start" }}>
        <Button variant="primary" onClick={onPatientView}>受试者画面</Button>
        <span className="muted" style={{ fontSize: 12 }}>进入后按住左上角圆钮 2.5 秒返回</span>
      </div>
      <Button onClick={onEnd}>安全暂停并返回待办</Button>
    </section>
  );
}
