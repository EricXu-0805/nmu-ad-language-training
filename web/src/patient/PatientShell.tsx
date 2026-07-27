import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api } from "../api";
import { PinPrompt } from "../components/PinPrompt";
import { bus } from "../sync/bus";
import { PATIENT_ACTIVATION_EVENT } from "../sync/messages";
import { useLiveCursor } from "../sync/useLiveCursor";
import { usePatientHeartbeat } from "../sync/usePatientHeartbeat";
import type { PatientPresenceScreen } from "../types";
import { isSessionTerminalStatus } from "../sessionLifecycle";
import { Centered } from "./Centered";
import {
  bedsideSafetyIsLatched,
  latchBedsideSafetyStop,
  reconcileBedsideSafetyLatch,
  type BedsideSafetyLatch,
} from "./bedsideSafetyLatch";
import { canSignalBedsideActivation } from "./autopilotAdmission";
import { PatientAutopilotStage } from "./PatientAutopilotStage";
import { PatientStage } from "./PatientStage";
import { parsePatientSessionPlan, type PatientSessionPlan } from "./patientPlan";
import { RapportStage } from "./RapportStage";
import {
  clearTtsContext,
  currentVoiceName,
  setTtsContext,
  setTtsEnabled,
  speakSample,
  stopSpeaking,
  ttsEnabled,
} from "./tts";
import type { TtsPlaybackContextKey } from "./ttsContext";
import { usePatientAutopilot } from "./usePatientAutopilot";

// 老人端外壳:旧流程仍是只读游标显示器；只有服务器证明 P0a 活跃后，
// 才互斥切换到独立的设备命令/ACK 运行器。
// 无 session 超时、无自动黑屏、对沉默与错误宽容(永不报错、永不闪红)。
export function PatientShell() {
  const { session, cursor, rapportStep, connection } = useLiveCursor();
  const terminal = isSessionTerminalStatus(session?.runtimeStatus);
  const connectionReady = connection === "connected";
  const [patientActivated, setPatientActivated] = useState(false);
  const [ttsOn, setTtsOn] = useState(ttsEnabled());
  const [safetyLatch, setSafetyLatch] = useState<BedsideSafetyLatch | null>(null);
  const safetyPaused = bedsideSafetyIsLatched(safetyLatch, session?.sessionId ?? null);
  const effectivePaused = session?.paused === true || safetyPaused;
  // plan 必须与 sessionId 成对保存。新场次握手到达的首帧就拒用旧 plan，
  // 不能等 effect 清理后才撤下上一位受试者的题目。
  const [loadedPlan, setLoadedPlan] = useState<{ sessionId: string; plan: PatientSessionPlan } | null>(null);
  const plan = session && loadedPlan?.sessionId === session.sessionId ? loadedPlan.plan : null;
  const autopilot = usePatientAutopilot({
    sessionId: session && session.mode !== "rapport" && !terminal ? session.sessionId : null,
    activated: patientActivated,
    ttsOn,
    connectionReady,
    sessionPaused: effectivePaused,
    sessionTerminal: terminal,
  });
  const stopAutopilotMediaRef = useRef(autopilot.stopMediaNow);
  stopAutopilotMediaRef.current = autopilot.stopMediaNow;

  useEffect(() => bus.subscribe((message) => {
    if (message.type !== "safetyStop" || message.sessionId !== session?.sessionId) return;
    // BroadcastChannel 的安全停是减权事件：先同步撤销两套语音/媒体执行器，
    // 再闩住 React 舞台，旧 cursor 即使晚到也无法重新开麦。
    clearTtsContext();
    stopAutopilotMediaRef.current();
    setSafetyLatch(() => reconcileBedsideSafetyLatch(
      latchBedsideSafetyStop(session.sessionId, message.sessionId),
      session.sessionId,
      session.paused === true,
    ));
  }), [session?.sessionId, session?.paused]);

  useLayoutEffect(() => {
    setSafetyLatch((current) => reconcileBedsideSafetyLatch(
      current,
      session?.sessionId ?? null,
      session?.paused === true,
    ));
  }, [session?.sessionId, session?.paused]);

  // 切场即清空激活态与已发信号记录:旧场次的激活绝不能替新场次说话。
  // 点击激活在发生的那一刻就绑定当时的 sessionId(ref 同步写,不等 setState),
  // 切场后的那一次 commit 里即使 patientActivated 还是旧值也发不出信号。
  const activatedForSession = useRef<string | null>(null);
  const activationSignaledFor = useRef<string | null>(null);
  useEffect(() => {
    setPatientActivated(false);
    activatedForSession.current = null;
    activationSignaledFor.current = null;
  }, [session?.sessionId]);

  // 老人一次明确点击 + 朗读开 + 连接就绪 → 对本窗操作端发一次 exact-session 激活信号。
  // 同一场次同一激活生命周期只发一次;它只是本机触发条件,操作端仍走全套门禁,
  // 服务端照常 fail-closed 重验——这里不产生任何授权或使用证据。
  useEffect(() => {
    const sessionId = session?.sessionId ?? null;
    if (!canSignalBedsideActivation({
      sessionId,
      sessionMode: session ? session.mode : null,
      sessionTerminal: terminal,
      activatedForSessionId: patientActivated ? activatedForSession.current : null,
      ttsOn,
      connectionReady,
      alreadySignaledSessionId: activationSignaledFor.current,
    })) return;
    activationSignaledFor.current = sessionId;
    window.dispatchEvent(new CustomEvent(PATIENT_ACTIVATION_EVENT, { detail: { sessionId } }));
  }, [session, terminal, patientActivated, ttsOn, connectionReady]);

  // useLayoutEffect:paint 前设好 34px 尺度,避免老人端首帧 16px→34px 全屏字号跳变
  useLayoutEffect(() => {
    document.documentElement.dataset.scale = "patient";
    return () => {
      clearTtsContext();
      // 仅清理自己仍持有的值，避免路由切换时覆盖另一端刚设置的尺度。
      if (document.documentElement.dataset.scale === "patient") {
        delete document.documentElement.dataset.scale;
      }
    };
  }, []);

  // 收到 session 握手 → 自取 plan;拉取失败每 3 秒悄悄重试直到成功——
  // 老人端是哑显示器,没有可点的恢复入口,后端抖一下不能让它永远停在"请稍候"。
  useEffect(() => {
    // Server-owned/probing mode consumes only current-command projections. Do
    // not prefetch the legacy multi-item plan into that control plane.
    if (!session || terminal || session.mode === "rapport" || autopilot.mode !== "legacy") {
      setLoadedPlan(null);
      return;
    }
    const sessionId = session.sessionId;
    setLoadedPlan(null);
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      api.patientSessionPlan(sessionId, session.weekNo, session.eventLine)
        .then((raw) => parsePatientSessionPlan(raw))
        .then((p) => { if (!cancelled) setLoadedPlan({ sessionId, plan: p }); })
        .catch(() => { if (!cancelled) timer = window.setTimeout(load, 3000); });
    };
    load();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.sessionId, session?.weekNo, session?.eventLine, session?.mode, terminal, autopilot.mode]);

  const autopilotScreen: PatientPresenceScreen = autopilot.runtime?.phase === "recording"
    || autopilot.runtime?.phase === "record_ready" ? "record"
    : autopilot.runtime?.phase === "tts_playing" || autopilot.runtime?.phase === "tts_ready"
      ? "present"
      : autopilot.runtime?.phase === "paused" || autopilot.mode === "blocked"
        ? "paused" : "waiting";
  const currentScreen: PatientPresenceScreen = !session
    ? "waiting"
    : terminal
      ? "complete"
      : effectivePaused
      ? "paused"
      : session.mode === "rapport"
      ? rapportStep?.paused === true ? "paused" : "rapport"
      : autopilot.mode !== "legacy"
        ? autopilotScreen
      : cursor?.screen === "thanks"
        ? "thanks"
        : cursor?.screen === "paused"
          ? "paused"
        : cursor?.screen === "done"
          ? "complete"
          : !plan || !cursor
            ? "loading"
            : cursor.screen === "record"
              ? "record"
              : cursor.screen === "present"
                ? "present"
                : cursor.screen === "idle"
                  ? "waiting"
                  : "waiting";
  const commandSeq = session?.mode === "rapport"
    ? rapportStep?.wseq
    : autopilot.mode !== "legacy" ? autopilot.current?.command_seq : cursor?.wseq;
  usePatientHeartbeat(session?.sessionId, currentScreen, commandSeq ?? session?.wseq);

  // Generic/legacy TTS gets an exact replay namespace.  It is deliberately
  // absent while a new item is loading, during pause/disconnect/terminal views,
  // and whenever the server-owned command runner controls media.
  const ttsContextKey: TtsPlaybackContextKey | null = !session || terminal
      || effectivePaused || !connectionReady
    ? null
    : session.mode === "rapport" && currentScreen === "rapport"
      && rapportStep?.sectionKey !== undefined
      ? `session:${session.sessionId}|plane:rapport|section:${rapportStep.sectionKey}|question:${rapportStep.questionIdx}`
      : session.mode !== "rapport" && autopilot.mode === "legacy"
        && plan && cursor && (currentScreen === "present" || currentScreen === "record")
        ? `session:${session.sessionId}|plane:legacy|item:${cursor.itemIdx}|turn:${cursor.turnIdx}`
        : null;

  useLayoutEffect(() => {
    setTtsContext(ttsContextKey);
  }, [ttsContextKey]);

  useLayoutEffect(() => {
    if (!connectionReady || ["paused", "complete", "loading", "waiting", "thanks"].includes(currentScreen)) {
      stopSpeaking();
    }
  }, [connectionReady, currentScreen]);

  let body: React.ReactNode;
  if (!session) {
    body = (
      <Centered>
        <div className="target">您好</div>
        <p className="question">准备好了我们就开始</p>
      </Centered>
    );
  } else if (terminal) {
    body = <Centered><div className="target">今天辛苦了</div><p className="question">今天的练习已经结束</p></Centered>;
  } else if (session.mode === "rapport") {
    // 暂停不卸载舞台:卸载会丢掉保存失败的 pendingSave 与重试入口,恢复后该段回答
    // 从流程里无声消失。舞台保持挂载,休息屏由舞台自己渲染,录音经 suspended 立即停麦。
    body = <RapportStage key={session.sessionId} rapportStep={rapportStep} sessionId={session.sessionId}
      ttsContextKey={ttsContextKey}
      connectionReady={connectionReady} sessionPaused={effectivePaused} sessionTerminal={terminal} />;
  } else {
    body = autopilot.mode === "legacy"
      ? <PatientStage key={session.sessionId} plan={plan} cursor={cursor} sessionId={session.sessionId}
          ttsContextKey={ttsContextKey}
          connectionReady={connectionReady} sessionPaused={effectivePaused} sessionTerminal={terminal} />
      : <PatientAutopilotStage
          key={session.sessionId}
          autopilot={autopilot}
          sessionId={session.sessionId}
          activated={patientActivated}
          ttsOn={ttsOn}
          externallyPaused={effectivePaused}
        />;
  }
  return <>
    {body}
    {session && !connectionReady && (
      <div className="patient-connection-notice" role="status" aria-live="polite" aria-atomic="true">
        <div className="patient-connection-notice__mark" aria-hidden="true">语</div>
        <div className="patient-connection-notice__copy">
          <strong>{connection === "connecting" ? "正在连接" : "正在重新连接"}</strong>
          <span>请稍候，我们会接着刚才的内容</span>
        </div>
      </div>
    )}
    <TapToStart tapped={patientActivated} onTap={() => {
      activatedForSession.current = session?.sessionId ?? null;
      setPatientActivated(true);
      // 按钮明示"准备声音播放":这一次点击同时把朗读打开,主流程不再需要第二次触碰。
      // 角落的朗读钮仍可随时关掉。
      if (!ttsOn) {
        setTtsEnabled(true);
        setTtsOn(true);
      }
    }} />
    <TtsToggle on={ttsOn} onChange={setTtsOn} sampleContextKey={ttsContextKey} />
    <PinPrompt />
  </>;
}

// 点击式启动:浏览器要求页面有过一次点击才放行语音(user activation)。
// 与其让研究者记住"先摸一下屏幕"这条隐性规则,不如给一个明确的可点落点;
// 点击本身即授予激活,tts 的 pointerdown 补读会把当前该读的话读出来。
function TapToStart({ tapped, onTap }: { tapped: boolean; onTap(): void }) {
  if (tapped) return null;
  return (
    <button type="button" className="tap-overlay" aria-label="点一下，开始并准备声音播放" onClick={onTap}>
      <div className="tap-overlay-inner">
        <div className="tap-overlay-mark" aria-hidden="true">语</div>
        <div>点一下，开始</div>
        <div className="tap-overlay-sub">准备声音播放</div>
      </div>
    </button>
  );
}

// 语音开关(设备本地设置,研究者代点):角落小钮,不进老人的视觉主线。
// 点开即试读当前屏显文本——既给浏览器用户激活,也当场听到用的是哪个音色。
function TtsToggle({ on, onChange, sampleContextKey }: {
  on: boolean;
  onChange(on: boolean): void;
  sampleContextKey: TtsPlaybackContextKey | null;
}) {
  return (
    <button
      type="button"
      className="tts-toggle"
      aria-label={on ? "关闭语音朗读" : "开启语音朗读"}
      aria-pressed={on}
      title={`语音${on ? "开" : "关"}${currentVoiceName() ? ` · 音色:${currentVoiceName()}` : " · 本机无中文语音"}`}
      onClick={() => {
        const next = !on;
        setTtsEnabled(next);
        onChange(next);
        // P0a owns its own receipt-bearing player. A legacy sample here would
        // overlap the server command and create an unacknowledged second voice.
        if (next && sampleContextKey) speakSample(sampleContextKey);
      }}
    >
      <span className="tts-toggle-label" aria-hidden="true">朗读</span>
      <span className="tts-toggle-state" aria-hidden="true">{on ? "开" : "关"}</span>
    </button>
  );
}
