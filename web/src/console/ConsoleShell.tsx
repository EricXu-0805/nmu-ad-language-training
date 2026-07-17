import { useEffect, useReducer, useState } from "react";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PinPrompt } from "../components/PinPrompt";
import { StatusPill } from "../components/StatusPill";
import { ToastProvider } from "../components/Toast";
import { AbnormalDrawer } from "./abnormal/AbnormalDrawer";
import { AnalysisScreen } from "./AnalysisScreen";
import { consoleReducer, initialConsole, loadConsoleState, persistConsoleState, type ConsoleArea } from "./consoleReducer";
import { RelationshipConsoleScreen } from "./relationship/RelationshipConsoleScreen";
import { RunPickerScreen } from "./RunPickerScreen";
import { ScaleDrawer } from "./ScaleDrawer";
import { SessionCreateScreen } from "./SessionCreateScreen";
import { SessionWrapupScreen } from "./SessionWrapupScreen";
import { SubjectRegistryScreen } from "./SubjectRegistryScreen";
import { TrainingConsoleScreen } from "./scoring/TrainingConsoleScreen";
import { usePatientPresence, type PatientPresenceView } from "../sync/usePatientPresence";

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
  const [currentItemEventId, setCurrentItemEventId] = useState<number | null>(null);
  const runPatientId = state.session?.patient_id ?? state.patientId ?? null;
  const patientPresence = usePatientPresence(state.session?.session_id);
  const inLiveSession = state.area === "run" && !!state.session
    && (state.screen === "training" || state.screen === "relationship");

  useEffect(() => {
    const root = document.documentElement;
    root.dataset.scale = "console";
    return () => { if (root.dataset.scale === "console") delete root.dataset.scale; };
  }, []);
  useEffect(() => { persistConsoleState(state); }, [state]);
  useEffect(() => { window.scrollTo({ top: 0, behavior: "auto" }); }, [state.screen, state.area]);

  // 切区:训练中(training/relationship)切走要确认——防误触离开正在进行的现场。
  const requestArea = (area: ConsoleArea) => {
    if (area === state.area) return;
    if (inLiveSession && area !== "run") setConfirmLeave(area);
    else dispatch({ t: "setArea", area });
  };

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
        </div>
      </header>

      <div className="app-main">
        {inLiveSession && state.session && (
          <SessionContextBar patientId={state.session.patient_id}
            weekNo={state.session.week_no} phase={state.session.phase_type} eventLine={state.session.event_line}
            version={state.session.item_bank_version_id} presence={patientPresence}
            onEnd={() => setConfirmLeave("run")} />
        )}

        <main>
          {state.area === "prep" && <SubjectRegistryScreen />}
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
      <PinPrompt />
    </ToastProvider>
  );
}

// SessionContextBar:★绝不显示姓名/称呼;场次编号是后台概念,前端不呈现——只出研究编号与本次安排。
function SessionContextBar({ patientId, weekNo, phase, eventLine, version, presence, onEnd }: {
  patientId: string; weekNo: number; phase: string; eventLine: string; version: string; presence: PatientPresenceView; onEnd: () => void;
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
      <Button onClick={onEnd}>结束/切换受试者</Button>
    </section>
  );
}
