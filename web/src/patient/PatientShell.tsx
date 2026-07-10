import { useEffect, useState } from "react";
import { api } from "../api";
import { PinPrompt } from "../components/PinPrompt";
import { useLiveCursor } from "../sync/useLiveCursor";
import type { SessionPlan } from "../types";
import { Centered } from "./Centered";
import { PatientStage } from "./PatientStage";
import { RapportStage } from "./RapportStage";

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
    body = <RapportStage rapportStep={rapportStep} />;
  } else {
    body = <PatientStage plan={plan} cursor={cursor} sessionId={session.sessionId} />;
  }
  return <>{body}<PinPrompt /></>;
}
