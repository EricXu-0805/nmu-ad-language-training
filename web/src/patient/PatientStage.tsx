import { MicButton } from "../components/MicButton";
import { ImagePane, type Spotlight } from "../components/ImagePane";
import { lookupCue, useItemBankBundle } from "../content/bundle";
import { turnKey } from "../lib/ids";
import type { CursorMsg } from "../sync/messages";
import type { SessionPlan } from "../types";
import { Centered } from "./Centered";
import { useVoxRecorder } from "./useVoxRecorder";

// 一屏一图一环节:呈现→录音→沉默后逐级线索。超大/高对比/无倒计时/无对错。
// ★组件树无任何画像来源;线索文本只从版本锁定的 bundle 查表,null 就留空,绝不拼接兜底。
export function PatientStage({ plan, cursor, sessionId }: { plan: SessionPlan | null; cursor?: CursorMsg; sessionId: string }) {
  const { bundle } = useItemBankBundle();

  const item = plan && cursor ? plan.items[cursor.itemIdx] : undefined;
  const planTurn = item && cursor ? item.turns[cursor.turnIdx] : undefined;
  const role = planTurn?.response_role ?? "命名";
  const tk = item && planTurn ? turnKey(item.item_id, planTurn.turn_seq) : "";

  const { stopAndSave } = useVoxRecorder({ sessionId, recording: cursor?.recording, recSeq: cursor?.recSeq, turnKey: tk });

  if (!item || !planTurn) {
    return <Centered><p className="question">请稍候…</p></Centered>;
  }

  const spotlight: Spotlight = role.startsWith("左") ? "left" : role.startsWith("右") ? "right" : role === "关系识别" ? "both" : "none";
  const question = role.includes("作用") ? "它是做什么用的呢？" : role === "关系识别" ? "它们之间有什么关系呢？" : "请看这张图片，这是什么？";
  const cueText = lookupCue(bundle, item.item_id, item.task_type, role, cursor?.cueLevel ?? 0);

  return (
    <Centered>
      <ImagePane imageId={item.image_id} spotlight={spotlight} alt="题目图片" />
      <p className="question">{question}</p>
      {cueText && <p className="cue card" style={{ maxWidth: "80vw" }}>{cueText}</p>}
      <MicButton state={cursor?.recording ?? "idle"} onStop={stopAndSave} />
    </Centered>
  );
}
