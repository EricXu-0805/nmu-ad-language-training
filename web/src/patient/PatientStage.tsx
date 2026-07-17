import { useEffect, useRef, useState } from "react";
import { MicButton } from "../components/MicButton";
import { ImagePane, type Spotlight } from "../components/ImagePane";
import { lookupCue, resolveFeedbackLine, useAutopilotProtocol, useItemBankBundle } from "../content/bundle";
import { turnKey } from "../lib/ids";
import type { CursorMsg } from "../sync/messages";
import type { SessionPlan } from "../types";
import { Centered } from "./Centered";
import { speak, stopSpeaking } from "./tts";
import { useVoxRecorder } from "./useVoxRecorder";

// 一屏一图一环节:呈现→录音→沉默后逐级线索。超大/高对比/无倒计时/无对错。
// ★组件树无任何画像来源;线索文本只从版本锁定的 bundle 查表,null 就留空,绝不拼接兜底。
export function PatientStage({ plan, cursor, sessionId, connectionReady = true, sessionPaused = false }: {
  plan: SessionPlan | null;
  cursor?: CursorMsg;
  sessionId: string;
  connectionReady?: boolean;
  sessionPaused?: boolean;
}) {
  const { bundle } = useItemBankBundle();
  const { protocol } = useAutopilotProtocol();

  const item = plan && cursor ? plan.items[cursor.itemIdx] : undefined;
  const planTurn = item && cursor ? item.turns[cursor.turnIdx] : undefined;
  const role = planTurn?.response_role ?? "命名";
  const tk = item && planTurn ? turnKey(item.item_id, planTurn.turn_seq) : "";
  const isPaused = sessionPaused || cursor?.screen === "paused";
  const suspended = !connectionReady || isPaused;

  const { stopAndSave, startNow, retrySave, saving, canRetry, recActive, micError, saveError, starting, remoteCommandBlocked } = useVoxRecorder({
    sessionId, recording: cursor?.recording, recSeq: cursor?.recSeq, commandSeq: cursor?.wseq,
    turnKey: tk, connectionReady, suspended: sessionPaused,
  });

  const spotlight: Spotlight = role.startsWith("左") ? "left" : role.startsWith("右") ? "right" : role === "关系识别" ? "both" : "none";
  const question = role.includes("作用") ? "它是做什么用的呢？" : role === "关系识别" ? "它们之间有什么关系呢？" : "请看这张图片，这是什么？";
  const cueText = item ? lookupCue(bundle, item.item_id, item.task_type, role, cursor?.cueLevel ?? 0) : null;
  // 自动驾驶反馈:游标只带键,文本本地查表(协议模板+题库目标词);缺内容返回 null,留空不兜底。
  const fbLine = cursor?.fbKey && cursor.fbItemId
    ? resolveFeedbackLine(bundle, protocol, cursor.fbKey, cursor.fbItemId)
    : null;
  const lastFbSeq = useRef<number | null>(null);   // null=未播种;挂载后首个 fbSeq 视为已消费,不重读

  // 小语开口:换环节读问句;线索到达/升级读线索(读的都是屏上原文)。
  // 线索用 enqueue:恢复/跳题时问句与线索同帧到达,排队读而不是让线索的 cancel 掐掉问句。
  // ★依赖只挂 tk/question/cueText + 恢复纪元,绝不挂 cursor.screen 全值——示意录音/收音回写
  // 都会翻转 screen,挂上它每次都把问句整句重读一遍,还会读进已示意的热麦克风(复审确证)。
  const [resumeEpoch, setResumeEpoch] = useState(0);
  const prevSuspended = useRef(false);
  useEffect(() => {
    if (prevSuspended.current && !suspended) setResumeEpoch((n) => n + 1); // 暂停/断线解除:补读当前句
    prevSuspended.current = suspended;
  }, [suspended]);
  useEffect(() => {
    if (!suspended && cursor?.screen !== "thanks" && tk) speak(question, { tag: tk });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tk, question, resumeEpoch]);
  // 自动驾驶反馈:独立一拍(位置还停在当前题、图片未换),fbSeq 变化才读。
  // ★挂载时用首个 fbSeq 播种(不朗读)——刷新恢复出的旧反馈游标绝不重读(它属于已过去的一拍)。
  useEffect(() => {
    const seq = cursor?.fbSeq;
    if (seq == null) return;
    if (lastFbSeq.current === null) { lastFbSeq.current = seq; return; }  // 播种:视为已消费
    if (seq === lastFbSeq.current) return;
    lastFbSeq.current = seq;
    if (!suspended && cursor?.screen !== "thanks" && fbLine) speak(fbLine, { tag: `fb:${cursor?.fbItemId ?? ""}:${seq}` });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cursor?.fbSeq]);
  useEffect(() => {
    if (!suspended && cursor?.screen !== "thanks" && cueText) speak(cueText, { tag: tk, enqueue: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cueText, tk, resumeEpoch]);
  // 收尾过渡屏:屏上是"今天辛苦了",小语不能同时还在念题(屏幕与语音互相矛盾的跳变)
  useEffect(() => {
    if (cursor?.screen === "thanks") stopSpeaking();
  }, [cursor?.screen]);

  if (isPaused) {
    return (
      <Centered>
        <div className="target">我们先休息一下</div>
        <p className="question">准备好后，研究者会继续</p>
      </Centered>
    );
  }

  // 收尾/结束的收回游标(screen:"thanks"):撤下作答界面,给平和的过渡屏
  if (cursor?.screen === "thanks") {
    return <Centered><div className="target">今天辛苦了</div><p className="question">请稍候…</p></Centered>;
  }
  if (!item || !planTurn) {
    return <Centered><p className="question">请稍候…</p></Centered>;
  }

  return (
    <div className="patient-stage">
      <div className="patient-stage-body">
        <div className="stage-image" data-compact={cueText ? "true" : "false"}>
          <ImagePane imageId={item.image_id} spotlight={spotlight} compact={!!cueText} alt="题目图片" />
        </div>
        <p className="question" aria-live="polite" aria-atomic="true">{question}</p>
        {/* 线索槽恒占位:线索出现/消失不再把问句和麦克风上下顶(布局零跳动) */}
        <div className="cue-slot" role="status" aria-live="polite" aria-atomic="true">
          {cueText && <p className="cue" style={{ maxWidth: "84vw", margin: 0 }}>{cueText}</p>}
        </div>
      </div>
      <div className="stage-mic" aria-busy={saving}>
        {saving
          ? <p className="patient-status" role="status" aria-live="polite">正在保存，请稍候</p>
          : (
            <>
              {!saveError && (
                <MicButton state={remoteCommandBlocked ? "idle" : (cursor?.recording ?? "idle")} localActive={recActive}
                  selfStart={!remoteCommandBlocked && cursor?.selfStart === true} micError={micError} starting={starting}
                  onStart={() => void startNow()} onStop={stopAndSave} />
              )}
              {saveError && (
                <div className="col" style={{ alignItems: "center", gap: "var(--sp-3)" }} role="alert">
                  <p className="patient-status">
                    {canRetry ? "刚才的回答还在本机，请再保存一次" : "刚才的回答没有完整保存，请找研究者看一看"}
                  </p>
                  {canRetry && (
                    <button type="button" className="patient-primary-action patient-primary-action--secondary"
                      onClick={() => void retrySave()}>
                      重新保存刚才的回答
                    </button>
                  )}
                </div>
              )}
            </>
          )}
      </div>
    </div>
  );
}
