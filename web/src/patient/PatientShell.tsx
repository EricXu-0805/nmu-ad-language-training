import { useEffect, useState } from "react";
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

  useEffect(() => { document.documentElement.dataset.scale = "patient"; }, []);

  // 收到 session 握手 → 自取一次 plan(task 模式需要;rapport 模式 plan 为空亦无妨)
  useEffect(() => {
    if (!session) { setPlan(null); return; }
    api.sessionPlan(session.sessionId, session.weekNo, session.eventLine).then(setPlan).catch(() => setPlan(null));
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
  return <>{body}<TtsToggle /><PinPrompt /></>;
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
