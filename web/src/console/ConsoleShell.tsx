import { useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";
import { PATIENT_VIEW_EVENT, PATIENT_VIEW_EXIT_EVENT } from "../sync/messages";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PinPrompt } from "../components/PinPrompt";
import { StatusPill } from "../components/StatusPill";
import { ToastProvider } from "../components/Toast";
import { AbnormalDrawer } from "./abnormal/AbnormalDrawer";
import { AnalysisScreen } from "./AnalysisScreen";
import { consoleReducer, initialConsole, loadConsoleState, persistConsoleState, type ConsoleArea } from "./consoleReducer";
import { LoginScreen } from "./LoginScreen";
import { RelationshipConsoleScreen } from "./relationship/RelationshipConsoleScreen";
import { RunPickerScreen } from "./RunPickerScreen";
import { ScaleDrawer } from "./ScaleDrawer";
import { SessionCreateScreen } from "./SessionCreateScreen";
import { SessionWrapupScreen } from "./SessionWrapupScreen";
import { SubjectRegistryScreen } from "./SubjectRegistryScreen";
import { TrainingConsoleScreen } from "./scoring/TrainingConsoleScreen";
import { useConsoleAuth } from "./useConsoleAuth";
import { usePatientPresence, type PatientPresenceView } from "../sync/usePatientPresence";
import { usePatientRec } from "../sync/useCursorWriter";
import { PATIENT_VIEW_REC_EVENT } from "../sync/messages";

// 操作端顶层外壳。设 data-scale=console;三区标签(准备/训练/分析)+ run 区 reducer 驱动
// 选人→建/续场次→训练/关系建立→收尾。场次编号是后台概念,前端流程不呈现。
const AREA_TABS: { key: ConsoleArea; label: string; hint: string }[] = [
  { key: "prep", label: "准备区", hint: "登记受试者、录量表" },
  { key: "run", label: "训练台", hint: "选人一键开训" },
  { key: "analyze", label: "分析后台", hint: "回看 AI 判定与录音" },
];

export function ConsoleShell() {
  const [state, dispatch] = useReducer(consoleReducer, initialConsole, loadConsoleState);
  const [abnormalOpen, setAbnormalOpen] = useState(false);
  const [scaleOpen, setScaleOpen] = useState(false);
  const [confirmLeave, setConfirmLeave] = useState<ConsoleArea | null>(null);
  const [confirmTrainPid, setConfirmTrainPid] = useState<string | null>(null);
  const [currentItemEventId, setCurrentItemEventId] = useState<number | null>(null);
  const auth = useConsoleAuth();
  const runPatientId = state.session?.patient_id ?? state.patientId ?? null;
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
  useEffect(() => { persistConsoleState(state); }, [state]);
  useEffect(() => { window.scrollTo({ top: 0, behavior: "auto" }); }, [state.screen, state.area]);

  // 切区:训练中(training/relationship)切走要确认——防误触离开正在进行的现场。
  const requestArea = (area: ConsoleArea) => {
    if (area === state.area) return;
    if (inLiveSession && area !== "run") setConfirmLeave(area);
    else dispatch({ t: "setArea", area });
  };

  // 准备区一键开训(建档直通/表内既有人)。注意不能用 inLiveSession 判残留场次——
  // 它要求 area==="run",而人在准备区时恰恰不满足;真正的判据是 state.session 还被持有。
  // 同一受试者=回到进行中的场次(放下重建会造出同人同周平行双场次,割裂数据);
  // 不同受试者才走「放下当前场次」确认。
  const startTrainingFor = (pid: string) => {
    if (state.session?.patient_id === pid) dispatch({ t: "setArea", area: "run" });
    else if (state.session) setConfirmTrainPid(pid);
    else dispatch({ t: "runPickSubject", patientId: pid });
  };

  // 账号登录门(公网部署):检查中先占位,未登录挡在登录页——工作台整棵树不挂载,
  // 任何研究数据请求都发不出去(比逐接口 401 更早、更彻底)。
  if (auth.mode === "loading") {
    return <main className="login-shell"><div className="login-card"><p className="muted">正在检查登录…</p></div></main>;
  }
  if (auth.mode === "login") {
    return <LoginScreen onLoggedIn={auth.refresh} />;
  }

  return (
    <ToastProvider>
      <header className="app-header" inert={patientView || undefined}>
        <div className="app-brand">
          <div className="app-brand-mark" aria-hidden>语</div>
          <div className="col" style={{ gap: 0 }}>
            <strong>语言沟通训练系统</strong>
            <span className="muted" style={{ fontSize: "0.82em" }}>研究者工作台 · 本地运行</span>
          </div>
        </div>
        <nav className="area-tabs" aria-label="工作台切换">
          {AREA_TABS.map((t) => (
            <button key={t.key} className={`area-tab${state.area === t.key ? " is-active" : ""}`}
              aria-current={state.area === t.key ? "page" : undefined} title={t.hint}
              onClick={() => requestArea(t.key)}>{t.label}</button>
          ))}
        </nav>
        <div className="toolbar">
          {state.area === "run" && runPatientId && <Button onClick={() => setScaleOpen(true)}>录入量表</Button>}
          {state.area === "run" && state.session && <Button onClick={() => setAbnormalOpen(true)}>记录现场情况</Button>}
          {auth.identity && (
            <div className="toolbar-account">
              <span className="muted" title={`账号 ${auth.identity.username}`}>👤 {auth.identity.display_id}</span>
              <Button onClick={() => { void auth.logout(); }}>退出</Button>
            </div>
          )}
        </div>
      </header>

      <div className="app-main" inert={patientView || undefined}>
        {inLiveSession && state.session && (
          <SessionContextBar patientId={state.session.patient_id}
            weekNo={state.session.week_no} phase={state.session.phase_type} eventLine={state.session.event_line}
            version={state.session.item_bank_version_id} presence={patientPresence}
            onEnd={() => setConfirmLeave("run")} onPatientView={enterPatientView} />
        )}

        <main>
          {state.area === "prep" && <SubjectRegistryScreen onStartTraining={startTrainingFor} />}
          {state.area === "analyze" && <AnalysisScreen />}
          {state.area === "run" && state.screen === "picker" && (
            <RunPickerScreen
              onPickSubject={(pid) => dispatch({ t: "runPickSubject", patientId: pid })}
              onResume={(s) => dispatch({ t: "sessionStarted", session: s })}
              onGoPrep={() => dispatch({ t: "setArea", area: "prep" })} />
          )}
          {state.area === "run" && state.screen === "sessionNew" && (
            <SessionCreateScreen patientId={state.patientId} hideSessionId
              onBack={() => dispatch({ t: "goRunPicker" })}
              onStarted={(s) => dispatch({ t: "sessionStarted", session: s })} />
          )}
          {state.area === "run" && state.screen === "training" && state.session && (
            <TrainingConsoleScreen session={state.session}
              onWrapup={() => dispatch({ t: "goWrapup" })} onExit={() => dispatch({ t: "goRunPicker" })}
              onItemEventChange={setCurrentItemEventId} />
          )}
          {state.area === "run" && state.screen === "relationship" && state.session && (
            <RelationshipConsoleScreen key={state.session.session_id} session={state.session} onWrapup={() => dispatch({ t: "goWrapup" })} />
          )}
          {state.area === "run" && state.screen === "wrapup" && state.session && (
            <SessionWrapupScreen session={state.session} onBack={() => dispatch({ t: "backToSession" })}
              onDone={() => dispatch({ t: "resetRun" })} />
          )}
        </main>
      </div>

      <ConfirmDialog open={confirmPatientView}
        title="另一个受试者端似乎在线"
        body="检测到已有受试者端连着本场次(另一窗口或另一台设备)。同时再开本机受试者画面会双开麦克风、同一环节产生两份录音。确认要在本机打开吗?"
        confirmLabel="仍在本机打开"
        onConfirm={openPatientView}
        onCancel={() => setConfirmPatientView(false)} />

      <ConfirmDialog open={confirmTrainPid !== null}
        title="放下当前场次，为新受试者开训？"
        body="手头还有一个未收尾的场次。开训会先放下它(已保存到服务器的数据不丢，训练台「继续未完成」可随时恢复)，再进入新受试者的场次设置。"
        confirmLabel="放下并开训"
        onConfirm={() => {
          const pid = confirmTrainPid!;
          setConfirmTrainPid(null);
          dispatch({ t: "runPickSubject", patientId: pid });
        }}
        onCancel={() => setConfirmTrainPid(null)} />

      <ConfirmDialog open={confirmLeave !== null}
        title={confirmLeave === "run" ? "结束当前训练？" : "离开正在进行的训练？"}
        body="已保存到服务器的数据和本机作业日志不会删除。训练台会保留该场次,可随时切回继续。"
        confirmLabel={confirmLeave === "run" ? "结束并回到选人" : "离开"}
        onConfirm={() => {
          const target = confirmLeave!;
          setConfirmLeave(null);
          if (target === "run") dispatch({ t: "resetRun" });
          else dispatch({ t: "setArea", area: target });
        }}
        onCancel={() => setConfirmLeave(null)} />

      {state.area === "run" && state.session && (
        <AbnormalDrawer sessionId={state.session.session_id} phaseType={state.session.phase_type}
          currentItemEventId={state.screen === "training" ? currentItemEventId : null}
          open={abnormalOpen} onClose={() => setAbnormalOpen(false)} />
      )}
      {state.area === "run" && runPatientId && <ScaleDrawer patientId={runPatientId} open={scaleOpen} onClose={() => setScaleOpen(false)} />}
      {/* 账号模式由登录门兜底,不再弹 PIN;仅 PIN/开放模式(无账号)保留 PinPrompt。 */}
      {!auth.accountsEnabled && <PinPrompt />}
    </ToastProvider>
  );
}

// SessionContextBar:★绝不显示姓名/称呼;场次编号是后台概念,前端不呈现——只出研究编号与本次安排。
function SessionContextBar({ patientId, weekNo, phase, eventLine, version, presence, onEnd, onPatientView }: {
  patientId: string; weekNo: number; phase: string; eventLine: string; version: string; presence: PatientPresenceView;
  onEnd: () => void; onPatientView: () => void;
}) {
  const eventLabel = eventLine === phase ? null : eventLine;
  const presenceTone = presence.state === "online" ? "ok"
    : presence.state === "offline" ? "danger"
      : presence.state === "unavailable" || presence.state === "unsupported" ? "warn" : "muted";
  const presenceLabel = presence.state === "online" ? "患者端在线"
    : presence.state === "offline" ? "患者端已断开"
      : presence.state === "unavailable" ? "状态暂不可用"
        : presence.state === "unsupported" ? "该版本不支持"
        : presence.state === "checking" ? "正在确认" : "等待患者端";
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
      <StatusPill tone="muted">题库 {version}</StatusPill>
      <div className="session-presence" role="status" aria-live="polite">
        <StatusPill tone={presenceTone}>{presenceLabel}</StatusPill>
        <div className="session-presence__copy">
          <strong>{presence.screenLabel}</strong>
          {presence.lastSeenLabel && <span>{presence.lastSeenLabel}</span>}
        </div>
      </div>
      <Button variant="primary" onClick={onPatientView} title="本机全屏切到受试者画面;研究者按住画面角落的按钮即可返回">
        受试者画面
      </Button>
      <Button onClick={onEnd}>结束/切换受试者</Button>
    </section>
  );
}
