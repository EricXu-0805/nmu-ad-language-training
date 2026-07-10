import { useEffect, useReducer, useState } from "react";
import { Button } from "../components/Button";
import { PinPrompt } from "../components/PinPrompt";
import { StatusPill } from "../components/StatusPill";
import { ToastProvider } from "../components/Toast";
import { AbnormalDrawer } from "./abnormal/AbnormalDrawer";
import { consoleReducer, initialConsole } from "./consoleReducer";
import { PatientIntakeScreen } from "./PatientIntakeScreen";
import { RelationshipConsoleScreen } from "./relationship/RelationshipConsoleScreen";
import { ScaleDrawer } from "./ScaleDrawer";
import { SessionCreateScreen } from "./SessionCreateScreen";
import { SessionWrapupScreen } from "./SessionWrapupScreen";
import { TrainingConsoleScreen } from "./scoring/TrainingConsoleScreen";

// 操作端顶层外壳。设 data-scale=console;单 reducer 驱动 intake→sessionNew→training/relationship→wrapup。
export function ConsoleShell() {
  const [state, dispatch] = useReducer(consoleReducer, initialConsole);
  const [abnormalOpen, setAbnormalOpen] = useState(false);
  const [scaleOpen, setScaleOpen] = useState(false);
  const patientId = state.patientId ?? state.session?.patient_id ?? null;

  useEffect(() => { document.documentElement.dataset.scale = "console"; }, []);

  return (
    <ToastProvider>
      <div className="col" style={{ padding: "var(--sp-5)", gap: "var(--sp-4)" }}>
        <header className="row" style={{ justifyContent: "space-between" }}>
          <strong>语言沟通训练 · 操作端</strong>
          <div className="row">
            {patientId && <Button onClick={() => setScaleOpen(true)}>量表录入</Button>}
            {state.session && <Button onClick={() => setAbnormalOpen(true)}>记录异常/介入</Button>}
            <Button onClick={() => dispatch({ t: "reset" })}>新受试者</Button>
          </div>
        </header>

        {state.session && <SessionContextBar sessionId={state.session.session_id} patientId={state.session.patient_id}
          weekNo={state.session.week_no} phase={state.session.phase_type} eventLine={state.session.event_line} version={state.session.item_bank_version_id} />}

        <main>
          {state.screen === "intake" && <PatientIntakeScreen onReady={(pid) => dispatch({ t: "patientReady", patientId: pid })} />}
          {state.screen === "sessionNew" && <SessionCreateScreen patientId={state.patientId} onStarted={(s) => dispatch({ t: "sessionStarted", session: s })} />}
          {state.screen === "training" && state.session && <TrainingConsoleScreen session={state.session} onWrapup={() => dispatch({ t: "goWrapup" })} />}
          {state.screen === "relationship" && state.session && <RelationshipConsoleScreen session={state.session} onWrapup={() => dispatch({ t: "goWrapup" })} />}
          {state.screen === "wrapup" && state.session && <SessionWrapupScreen session={state.session} />}
        </main>
      </div>

      {state.session && (
        <AbnormalDrawer sessionId={state.session.session_id} phaseType={state.session.phase_type}
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
