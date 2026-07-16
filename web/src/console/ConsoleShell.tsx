import { useEffect, useReducer, useState } from "react";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PinPrompt } from "../components/PinPrompt";
import { StatusPill } from "../components/StatusPill";
import { ToastProvider } from "../components/Toast";
import { AbnormalDrawer } from "./abnormal/AbnormalDrawer";
import { consoleReducer, initialConsole, loadConsoleState, persistConsoleState } from "./consoleReducer";
import { PatientIntakeScreen } from "./PatientIntakeScreen";
import { RelationshipConsoleScreen } from "./relationship/RelationshipConsoleScreen";
import { ScaleDrawer } from "./ScaleDrawer";
import { SessionCreateScreen } from "./SessionCreateScreen";
import { SessionWrapupScreen } from "./SessionWrapupScreen";
import { TrainingConsoleScreen } from "./scoring/TrainingConsoleScreen";
import { usePatientPresence, type PatientPresenceView } from "../sync/usePatientPresence";

// 操作端顶层外壳。设 data-scale=console;单 reducer 驱动 intake→sessionNew→training/relationship→wrapup。
export function ConsoleShell() {
  const [state, dispatch] = useReducer(consoleReducer, initialConsole, loadConsoleState);
  const [abnormalOpen, setAbnormalOpen] = useState(false);
  const [scaleOpen, setScaleOpen] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [currentItemEventId, setCurrentItemEventId] = useState<number | null>(null);
  const patientId = state.patientId ?? state.session?.patient_id ?? null;
  const patientPresence = usePatientPresence(state.session?.session_id);

  useEffect(() => {
    const root = document.documentElement;
    root.dataset.scale = "console";
    return () => { if (root.dataset.scale === "console") delete root.dataset.scale; };
  }, []);
  useEffect(() => { persistConsoleState(state); }, [state]);
  // 长页面（训练/收尾）切到下一步时回到页首，避免 sticky 顶栏遮住新页面标题。
  useEffect(() => { window.scrollTo({ top: 0, behavior: "auto" }); }, [state.screen]);

  return (
    <ToastProvider>
      <header className="app-header">
        <div className="app-brand">
          <div className="app-brand-mark" aria-hidden>语</div>
          <div className="col" style={{ gap: 0 }}>
            <strong>语言沟通训练系统</strong>
            <span className="muted" style={{ fontSize: "0.82em" }}>研究者工作台 · 本地运行</span>
          </div>
        </div>
        <div className="toolbar">
          {patientId && <Button onClick={() => setScaleOpen(true)}>录入量表</Button>}
          {state.session && <Button onClick={() => setAbnormalOpen(true)}>记录现场情况</Button>}
          {/* 有现场时误触=丢整个屏幕状态,必须确认;还没进场次时直接重置无妨 */}
          <Button onClick={() => (state.session || state.patientId ? setConfirmReset(true) : dispatch({ t: "reset" }))}>切换受试者</Button>
        </div>
      </header>

      <div className="app-main">
        <WorkflowStepper screen={state.screen} />
        {state.session && <SessionContextBar sessionId={state.session.session_id} patientId={state.session.patient_id}
          weekNo={state.session.week_no} phase={state.session.phase_type} eventLine={state.session.event_line}
          version={state.session.item_bank_version_id} presence={patientPresence} />}

        <main>
          {state.screen === "intake" && <PatientIntakeScreen onReady={(pid) => dispatch({ t: "patientReady", patientId: pid })} />}
          {state.screen === "sessionNew" && <SessionCreateScreen patientId={state.patientId} onStarted={(s) => dispatch({ t: "sessionStarted", session: s })} />}
          {state.screen === "training" && state.session && <TrainingConsoleScreen session={state.session}
            onWrapup={() => dispatch({ t: "goWrapup" })} onExit={() => dispatch({ t: "goSessionNew" })}
            onItemEventChange={setCurrentItemEventId} />}
          {state.screen === "relationship" && state.session && <RelationshipConsoleScreen key={state.session.session_id} session={state.session} onWrapup={() => dispatch({ t: "goWrapup" })} />}
          {state.screen === "wrapup" && state.session && <SessionWrapupScreen session={state.session} onBack={() => dispatch({ t: "backToSession" })} />}
        </main>
      </div>

      <ConfirmDialog open={confirmReset} title="切换受试者？"
        body={state.session ? `将离开当前场次 ${state.session.session_id}。已保存的服务器数据和本机作业日志不会删除，但研究者工作台将不再停留在该场次。` : "当前未保存的建档内容将清空。"}
        confirmLabel="确认切换"
        onConfirm={() => { setConfirmReset(false); dispatch({ t: "reset" }); }}
        onCancel={() => setConfirmReset(false)} />

      {state.session && (
        <AbnormalDrawer sessionId={state.session.session_id} phaseType={state.session.phase_type}
          currentItemEventId={state.screen === "training" ? currentItemEventId : null}
          open={abnormalOpen} onClose={() => setAbnormalOpen(false)} />
      )}
      {patientId && <ScaleDrawer patientId={patientId} open={scaleOpen} onClose={() => setScaleOpen(false)} />}
      <PinPrompt />
    </ToastProvider>
  );
}

function WorkflowStepper({ screen }: { screen: ReturnType<typeof loadConsoleState>["screen"] }) {
  const current = screen === "intake" ? 0 : screen === "sessionNew" ? 1 : screen === "wrapup" ? 3 : 2;
  const steps = ["受试者建档", "建立场次", "开展训练", "场次收尾"];
  return (
    <nav className="workflow-stepper" aria-label="场次工作流程">
      {steps.map((label, index) => (
        <div
          key={label}
          className={`workflow-step${index === current ? " is-active" : ""}${index < current ? " is-complete" : ""}`}
          aria-current={index === current ? "step" : undefined}
        >
          <span aria-hidden>{index + 1}</span>
          <span>{label}</span>
        </div>
      ))}
    </nav>
  );
}

// SessionContextBar:★绝不显示姓名/称呼,只出 patient_id 等研究标识。
function SessionContextBar({ sessionId, patientId, weekNo, phase, eventLine, version, presence }: {
  sessionId: string; patientId: string; weekNo: number; phase: string; eventLine: string; version: string; presence: PatientPresenceView;
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
        <strong>{patientId}</strong>
      </div>
      <div className="session-context-item">
        <span className="muted">本次安排</span>
        <strong>第 {weekNo} 周 · {phase}{eventLabel ? ` · ${eventLabel}` : ""}</strong>
      </div>
      <div className="session-context-item grow">
        <span className="muted">场次编号</span>
        <span className="mono">{sessionId}</span>
      </div>
      <StatusPill tone="muted">题库 {version}</StatusPill>
      <div className="session-presence" role="status" aria-live="polite">
        <StatusPill tone={presenceTone}>{presenceLabel}</StatusPill>
        <div className="session-presence__copy">
          <strong>{presence.screenLabel}</strong>
          {presence.lastSeenLabel && <span>{presence.lastSeenLabel}</span>}
        </div>
      </div>
    </section>
  );
}
