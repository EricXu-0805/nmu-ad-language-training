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

// 操作端顶层外壳。设 data-scale=console;单 reducer 驱动 intake→sessionNew→training/relationship→wrapup。
export function ConsoleShell() {
  const [state, dispatch] = useReducer(consoleReducer, initialConsole, loadConsoleState);
  const [abnormalOpen, setAbnormalOpen] = useState(false);
  const [scaleOpen, setScaleOpen] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [currentItemEventId, setCurrentItemEventId] = useState<number | null>(null);
  const patientId = state.patientId ?? state.session?.patient_id ?? null;

  useEffect(() => { document.documentElement.dataset.scale = "console"; }, []);
  useEffect(() => { persistConsoleState(state); }, [state]);

  return (
    <ToastProvider>
      <header className="app-header">
        <div className="app-brand">
          <div className="app-brand-mark" aria-hidden>语</div>
          <div className="col" style={{ gap: 0 }}>
            <strong>语言沟通训练系统</strong>
            <span className="muted" style={{ fontSize: "0.82em" }}>操作端 · 研究者专用</span>
          </div>
        </div>
        <div className="row">
          {patientId && <Button onClick={() => setScaleOpen(true)}>量表录入</Button>}
          {state.session && <Button onClick={() => setAbnormalOpen(true)}>记录异常/介入</Button>}
          {/* 有现场时误触=丢整个屏幕状态,必须确认;还没进场次时直接重置无妨 */}
          <Button onClick={() => (state.session || state.patientId ? setConfirmReset(true) : dispatch({ t: "reset" }))}>新受试者</Button>
        </div>
      </header>

      <div className="app-main">
        {state.session && <SessionContextBar sessionId={state.session.session_id} patientId={state.session.patient_id}
          weekNo={state.session.week_no} phase={state.session.phase_type} eventLine={state.session.event_line} version={state.session.item_bank_version_id} />}

        <main>
          {state.screen === "intake" && <PatientIntakeScreen onReady={(pid) => dispatch({ t: "patientReady", patientId: pid })} />}
          {state.screen === "sessionNew" && <SessionCreateScreen patientId={state.patientId} onStarted={(s) => dispatch({ t: "sessionStarted", session: s })} />}
          {state.screen === "training" && state.session && <TrainingConsoleScreen session={state.session}
            onWrapup={() => dispatch({ t: "goWrapup" })} onExit={() => dispatch({ t: "goSessionNew" })}
            onItemEventChange={setCurrentItemEventId} />}
          {state.screen === "relationship" && state.session && <RelationshipConsoleScreen session={state.session} onWrapup={() => dispatch({ t: "goWrapup" })} />}
          {state.screen === "wrapup" && state.session && <SessionWrapupScreen session={state.session} onBack={() => dispatch({ t: "backToSession" })} />}
        </main>
      </div>

      <ConfirmDialog open={confirmReset} title="换新受试者?"
        body={state.session ? `当前场次 ${state.session.session_id} 的屏幕现场将清空。已保存到服务器的数据与本机作业日志不受影响,但本屏将无法直接回到该场次。` : "当前建档进度将清空。"}
        confirmLabel="清空并新建"
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

// SessionContextBar:★绝不显示姓名/称呼,只出 patient_id 等研究标识。
function SessionContextBar({ sessionId, patientId, weekNo, phase, eventLine, version }: {
  sessionId: string; patientId: string; weekNo: number; phase: string; eventLine: string; version: string;
}) {
  return (
    <div className="card row wrap" style={{ gap: "var(--sp-3)" }}>
      <StatusPill tone="primary">场次 {sessionId}</StatusPill>
      <span>受试者 <strong>{patientId}</strong></span>
      <span>第 {weekNo} 周</span>
      <span>{phase}</span>
      <span className="muted">{eventLine}</span>
      <StatusPill tone="muted">🔒 {version}</StatusPill>
    </div>
  );
}
