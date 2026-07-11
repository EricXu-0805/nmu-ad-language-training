import { useEffect } from "react";
import { MicButton } from "../components/MicButton";
import { ImagePane, type Spotlight } from "../components/ImagePane";
import { lookupCue, useItemBankBundle } from "../content/bundle";
import { turnKey } from "../lib/ids";
import type { CursorMsg } from "../sync/messages";
import type { SessionPlan } from "../types";
import { Centered } from "./Centered";
import { speak } from "./tts";
import { useVoxRecorder } from "./useVoxRecorder";

// 一屏一图一环节:呈现→录音→沉默后逐级线索。超大/高对比/无倒计时/无对错。
// ★组件树无任何画像来源;线索文本只从版本锁定的 bundle 查表,null 就留空,绝不拼接兜底。
export function PatientStage({ plan, cursor, sessionId }: { plan: SessionPlan | null; cursor?: CursorMsg; sessionId: string }) {
  const { bundle } = useItemBankBundle();

  const item = plan && cursor ? plan.items[cursor.itemIdx] : undefined;
  const planTurn = item && cursor ? item.turns[cursor.turnIdx] : undefined;
  const role = planTurn?.response_role ?? "命名";
  const tk = item && planTurn ? turnKey(item.item_id, planTurn.turn_seq) : "";

  const { stopAndSave, startNow, saving, recActive, micError, saveError, starting } = useVoxRecorder({ sessionId, recording: cursor?.recording, recSeq: cursor?.recSeq, turnKey: tk });

  const spotlight: Spotlight = role.startsWith("左") ? "left" : role.startsWith("右") ? "right" : role === "关系识别" ? "both" : "none";
  const question = role.includes("作用") ? "它是做什么用的呢？" : role === "关系识别" ? "它们之间有什么关系呢？" : "请看这张图片，这是什么？";
  const cueText = item ? lookupCue(bundle, item.item_id, item.task_type, role, cursor?.cueLevel ?? 0) : null;

  // 小语开口:换环节读问句;线索到达/升级读线索(读的都是屏上原文)。
  // 线索用 enqueue:恢复/跳题时问句与线索同帧到达,排队读而不是让线索的 cancel 掐掉问句。
  useEffect(() => { if (tk) speak(question, { tag: tk }); }, [tk, question]);
  useEffect(() => { if (cueText) speak(cueText, { tag: tk, enqueue: true }); }, [cueText, tk]);

  // 收尾/结束的收回游标(screen:"thanks"):撤下作答界面,给平和的过渡屏
  if (cursor?.screen === "thanks") {
    return <Centered><div className="target">今天辛苦了</div><p className="question">请稍候…</p></Centered>;
  }
  if (!item || !planTurn) {
    return <Centered><p className="question">请稍候…</p></Centered>;
  }

  return (
    <div className="patient-stage">
      <div className="stage-image"><ImagePane imageId={item.image_id} spotlight={spotlight} compact={!!cueText} alt="题目图片" /></div>
      <p className="question">{question}</p>
      {/* 线索槽恒占位:线索出现/消失不再把问句和麦克风上下顶(布局零跳动) */}
      <div className="cue-slot">
        {cueText && <p className="cue" style={{ maxWidth: "84vw", margin: 0 }}>{cueText}</p>}
      </div>
      <div className="stage-mic">
        {saving
          ? <p className="cue" style={{ border: "none", boxShadow: "none", background: "transparent" }}>好的，收到了…</p>
          : (
            <>
              <MicButton state={cursor?.recording ?? "idle"} localActive={recActive}
                selfStart={cursor?.selfStart === true} micError={micError} starting={starting}
                onStart={() => void startNow()} onStop={stopAndSave} />
              {saveError && <p style={{ fontSize: "var(--fs-md)", margin: 0, opacity: 0.75 }}>刚才没有存上，请再说一遍</p>}
            </>
          )}
      </div>
    </div>
  );
}
