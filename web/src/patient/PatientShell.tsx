import { useEffect, useLayoutEffect, useState } from "react";
import { api } from "../api";
import { PinPrompt } from "../components/PinPrompt";
import { useLiveCursor } from "../sync/useLiveCursor";
import type { SessionPlan } from "../types";
import { Centered } from "./Centered";
import { PatientStage } from "./PatientStage";
import { RapportStage } from "./RapportStage";
import { currentVoiceName, setTtsEnabled, speakSample, ttsEnabled } from "./tts";

// 老人端外壳:只读订阅游标的哑显示器。从不自主推进/判分/写游标。
// 无 session 超时、无自动黑屏、对沉默与错误宽容(永不报错、永不闪红)。
export function PatientShell() {
  const { session, cursor, rapportStep } = useLiveCursor();
  const [plan, setPlan] = useState<SessionPlan | null>(null);

  // useLayoutEffect:paint 前设好 34px 尺度,避免老人端首帧 16px→34px 全屏字号跳变
  useLayoutEffect(() => { document.documentElement.dataset.scale = "patient"; }, []);

  // 收到 session 握手 → 自取 plan;拉取失败每 3 秒悄悄重试直到成功——
  // 老人端是哑显示器,没有可点的恢复入口,后端抖一下不能让它永远停在"请稍候"。
  useEffect(() => {
    if (!session) { setPlan(null); return; }
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      api.sessionPlan(session.sessionId, session.weekNo, session.eventLine)
        .then((p) => { if (!cancelled) setPlan(p); })
        .catch(() => { if (!cancelled) timer = window.setTimeout(load, 3000); });
    };
    load();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.sessionId, session?.weekNo, session?.eventLine]);

  let body: React.ReactNode;
  if (!session) {
    body = (
      <Centered>
        <div className="target">您好</div>
        <p className="question">准备好了我们就开始</p>
      </Centered>
    );
  } else if (session.mode === "rapport") {
    body = <RapportStage rapportStep={rapportStep} sessionId={session.sessionId} />;
  } else {
    body = <PatientStage plan={plan} cursor={cursor} sessionId={session.sessionId} />;
  }
  return <>{body}<TapToStart /><TtsToggle /><PinPrompt /></>;
}

// 点击式启动:浏览器要求页面有过一次点击才放行语音(user activation)。
// 与其让研究者记住"先摸一下屏幕"这条隐性规则,不如给一个明确的可点落点;
// 点击本身即授予激活,tts 的 pointerdown 补读会把当前该读的话读出来。
function TapToStart() {
  const [tapped, setTapped] = useState(false);
  if (tapped) return null;
  return (
    <button type="button" className="tap-overlay" onClick={() => setTapped(true)}>
      <div className="tap-overlay-inner">
        <div style={{ fontSize: 88 }} aria-hidden>👋</div>
        <div>点一下，开始</div>
        <div className="tap-overlay-sub">（打开声音）</div>
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
      aria-label={on ? "关闭语音" : "开启语音"}
      title={`语音${on ? "开" : "关"}${currentVoiceName() ? ` · 音色:${currentVoiceName()}` : " · 本机无中文语音"}`}
      onClick={() => {
        const next = !on;
        setTtsEnabled(next);
        setOn(next);
        if (next) speakSample();
      }}
    >
      {on ? "🔊" : "🔇"}
    </button>
  );
}
