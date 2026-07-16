import { useEffect, useLayoutEffect, useState } from "react";
import { api } from "../api";
import { PinPrompt } from "../components/PinPrompt";
import { useLiveCursor } from "../sync/useLiveCursor";
import { usePatientHeartbeat } from "../sync/usePatientHeartbeat";
import type { PatientPresenceScreen, SessionPlan } from "../types";
import { Centered } from "./Centered";
import { PatientStage } from "./PatientStage";
import { RapportStage } from "./RapportStage";
import { currentVoiceName, setTtsEnabled, speakSample, stopSpeaking, ttsEnabled } from "./tts";

// 老人端外壳:只读订阅游标的哑显示器。从不自主推进/判分/写游标。
// 无 session 超时、无自动黑屏、对沉默与错误宽容(永不报错、永不闪红)。
export function PatientShell() {
  const { session, cursor, rapportStep, connection } = useLiveCursor();
  // plan 必须与 sessionId 成对保存。新场次握手到达的首帧就拒用旧 plan，
  // 不能等 effect 清理后才撤下上一位受试者的题目。
  const [loadedPlan, setLoadedPlan] = useState<{ sessionId: string; plan: SessionPlan } | null>(null);
  const plan = session && loadedPlan?.sessionId === session.sessionId ? loadedPlan.plan : null;

  // useLayoutEffect:paint 前设好 34px 尺度,避免老人端首帧 16px→34px 全屏字号跳变
  useLayoutEffect(() => {
    document.documentElement.dataset.scale = "patient";
    return () => {
      // 仅清理自己仍持有的值，避免路由切换时覆盖另一端刚设置的尺度。
      if (document.documentElement.dataset.scale === "patient") {
        delete document.documentElement.dataset.scale;
      }
    };
  }, []);

  // 收到 session 握手 → 自取 plan;拉取失败每 3 秒悄悄重试直到成功——
  // 老人端是哑显示器,没有可点的恢复入口,后端抖一下不能让它永远停在"请稍候"。
  useEffect(() => {
    if (!session) { setLoadedPlan(null); return; }
    const sessionId = session.sessionId;
    setLoadedPlan(null);
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      api.sessionPlan(sessionId, session.weekNo, session.eventLine)
        .then((p) => { if (!cancelled) setLoadedPlan({ sessionId, plan: p }); })
        .catch(() => { if (!cancelled) timer = window.setTimeout(load, 3000); });
    };
    load();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.sessionId, session?.weekNo, session?.eventLine]);

  const currentScreen: PatientPresenceScreen = !session
    ? "waiting"
    : session.paused === true
      ? "paused"
      : session.mode === "rapport"
      ? rapportStep?.paused === true ? "paused" : "rapport"
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
  const commandSeq = session?.mode === "rapport" ? rapportStep?.wseq : cursor?.wseq;
  usePatientHeartbeat(session?.sessionId, currentScreen, commandSeq ?? session?.wseq);

  const connectionReady = connection === "connected";
  useEffect(() => {
    if (!connectionReady || currentScreen === "paused") stopSpeaking();
  }, [connectionReady, currentScreen]);

  let body: React.ReactNode;
  if (!session) {
    body = (
      <Centered>
        <div className="target">您好</div>
        <p className="question">准备好了我们就开始</p>
      </Centered>
    );
  } else if (session.mode === "rapport") {
    // 暂停不卸载舞台:卸载会丢掉保存失败的 pendingSave 与重试入口,恢复后该段回答
    // 从流程里无声消失。舞台保持挂载,休息屏由舞台自己渲染,录音经 suspended 立即停麦。
    body = <RapportStage key={session.sessionId} rapportStep={rapportStep} sessionId={session.sessionId}
      connectionReady={connectionReady} sessionPaused={session.paused === true} />;
  } else {
    body = <PatientStage key={session.sessionId} plan={plan} cursor={cursor} sessionId={session.sessionId}
      connectionReady={connectionReady} sessionPaused={session.paused === true} />;
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
    <TapToStart /><TtsToggle /><PinPrompt />
  </>;
}

// 点击式启动:浏览器要求页面有过一次点击才放行语音(user activation)。
// 与其让研究者记住"先摸一下屏幕"这条隐性规则,不如给一个明确的可点落点;
// 点击本身即授予激活,tts 的 pointerdown 补读会把当前该读的话读出来。
function TapToStart() {
  const [tapped, setTapped] = useState(false);
  if (tapped) return null;
  return (
    <button type="button" className="tap-overlay" aria-label="点一下，开始并准备声音播放" onClick={() => setTapped(true)}>
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
function TtsToggle() {
  const [on, setOn] = useState(ttsEnabled());
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
        setOn(next);
        if (next) speakSample();
      }}
    >
      <span className="tts-toggle-label" aria-hidden="true">朗读</span>
      <span className="tts-toggle-state" aria-hidden="true">{on ? "开" : "关"}</span>
    </button>
  );
}
