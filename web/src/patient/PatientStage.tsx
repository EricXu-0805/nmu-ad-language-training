import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { MicButton } from "../components/MicButton";
import { ImagePane, type Spotlight } from "../components/ImagePane";
import { turnKey } from "../lib/ids";
import type { CursorMsg } from "../sync/messages";
import { Centered } from "./Centered";
import { advanceFeedbackPlayback, initialFeedbackPlaybackState } from "./feedbackState";
import type { TaskPresentationExpectation } from "./presentationContent";
import { speak, stopSpeaking } from "./tts";
import { usePatientPresentation } from "./usePatientPresentation";
import { useVoxRecorder } from "./useVoxRecorder";
import { audioRecorderBlockCopy } from "./audioRecorderBlock";
import type { PatientSessionPlan } from "./patientPlan";
import type { TtsPlaybackContextKey } from "./ttsContext";
import {
  patientAssetRequirementReady,
  type PatientAssetReadinessEvent,
} from "./currentPatientAsset.ts";

// 一屏一图一环节:呈现→录音→沉默后逐级线索。超大/高对比/无倒计时/无对错。
// ★组件树无任何画像来源;线索文本只从版本锁定的 bundle 查表,null 就留空,绝不拼接兜底。
export function PatientStage({
  plan,
  cursor,
  sessionId,
  ttsContextKey,
  connectionReady = true,
  sessionPaused = false,
  sessionTerminal = false,
  registerImmediateDiscard,
}: {
  plan: PatientSessionPlan | null;
  cursor?: CursorMsg;
  sessionId: string;
  ttsContextKey: TtsPlaybackContextKey | null;
  connectionReady?: boolean;
  sessionPaused?: boolean;
  sessionTerminal?: boolean;
  registerImmediateDiscard?: (handler: (() => void) | null) => void;
}) {
  const item = plan && cursor ? plan.items[cursor.itemIdx] : undefined;
  const planTurn = item && cursor ? item.turns[cursor.turnIdx] : undefined;
  const role = planTurn?.response_role ?? "命名";
  const tk = item && planTurn ? turnKey(item.item_ref, planTurn.turn_seq) : "";
  const presentationExpected: TaskPresentationExpectation | null =
    plan && item && planTurn && cursor && !sessionTerminal
      ? {
          mode: "task",
          sessionId,
          itemBankVersionId: plan.item_bank_version_id,
          itemIdx: cursor.itemIdx,
          turnIdx: cursor.turnIdx,
          itemRef: item.item_ref,
          turnSeq: planTurn.turn_seq,
          responseRole: planTurn.response_role,
          cueLevel: cursor.cueLevel,
          feedbackKey: cursor.fbKey,
          feedbackItemRef: cursor.fbKey ? item.item_ref : undefined,
          feedbackSeq: cursor.fbSeq,
          // ★不钉 wseq:示意录音 arm/停止只前进 wseq、不改呈现内容。钉了它,每次 arm
          // 都会让已加载投影瞬间失配→mediaReady 掉一拍→录音器把刚 arm 的 recSeq
          // 当热麦风险永久拉黑,远程收音从此永不启动(彩排实测)。内容正确性仍由
          // item/turn/cue/feedback 字段逐项精确匹配兜底,旧转位响应照样被拒。
        }
      : null;
  const { presentation } = usePatientPresentation(presentationExpected);
  const taskPresentation = presentation?.mode === "task" ? presentation : null;
  const contentReady = taskPresentation !== null;
  const assetRequired = cursor?.screen === "present" || cursor?.screen === "record";
  const assetRequestKey = item?.item_ref ?? "no-current-item";
  const [assetState, setAssetState] = useState<PatientAssetReadinessEvent | null>(null);
  const onAssetReadinessChange = useCallback((event: PatientAssetReadinessEvent) => {
    setAssetState(event);
  }, []);
  const assetReady = patientAssetRequirementReady(
    assetRequired,
    assetState,
    assetRequestKey,
  );
  const mediaReady = contentReady && assetReady;
  const isPaused = sessionPaused || cursor?.screen === "paused";
  const suspended = !connectionReady || isPaused || sessionTerminal || !mediaReady;

  const {
    stopAndSave, discardForPatientPause, startNow, retrySave, saving, canRetry, recActive, micError,
    saveError, starting, remoteCommandBlocked, blockReason,
  } = useVoxRecorder({
    sessionId, recording: cursor?.recording, recSeq: cursor?.recSeq, commandSeq: cursor?.wseq,
    turnKey: tk, connectionReady, suspended: sessionPaused || !mediaReady,
    selfStartAllowed: mediaReady && cursor?.selfStart === true && !isPaused && !sessionTerminal && cursor?.screen !== "thanks" && cursor?.screen !== "done",
    stopRequested: sessionTerminal || isPaused || !mediaReady || cursor?.screen === "thanks" || cursor?.screen === "done",
  });
  useLayoutEffect(() => {
    registerImmediateDiscard?.(discardForPatientPause);
    return () => registerImmediateDiscard?.(null);
  }, [discardForPatientPause, registerImmediateDiscard]);
  const blockCopy = blockReason ? audioRecorderBlockCopy(blockReason) : null;

  const spotlight: Spotlight = role.startsWith("左") ? "left" : role.startsWith("右") ? "right" : role === "关系识别" ? "both" : "none";
  const question = role.includes("作用") ? "它是做什么用的呢？" : role === "关系识别" ? "它们之间有什么关系呢？" : "请看这张图片，这是什么？";
  // 线索/反馈不再从浏览器整包题库查找；只接受服务端当前游标的最小投影。
  const cueText = taskPresentation?.cue_text ?? null;
  const fbLine = taskPresentation?.feedback_text ?? null;
  const feedbackPlayback = useRef(initialFeedbackPlaybackState());

  // 换题时图片门禁会在同一 render 立即失效；paint 前停止上一题语音，
  // 绝不让旧题朗读跨进新图的加载窗口。
  useLayoutEffect(() => {
    if (!assetReady) stopSpeaking();
  }, [assetReady, assetRequestKey]);

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
    if (!suspended && cursor?.screen !== "thanks" && tk && ttsContextKey) {
      speak(question, { contextKey: ttsContextKey, tag: tk });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tk, question, resumeEpoch, contentReady, ttsContextKey]);
  // 自动驾驶反馈:独立一拍(位置还停在当前题、图片未换),fbSeq 变化才读。
  // 首次连接成功时的游标是历史基线,不重读；基线之后到达的第一个新 fbSeq 必须朗读。
  useEffect(() => {
    if (!contentReady) return;
    const transition = advanceFeedbackPlayback(feedbackPlayback.current, connectionReady, cursor?.fbSeq);
    feedbackPlayback.current = transition.state;
    if (transition.shouldSpeak && !suspended && cursor?.screen !== "thanks"
        && fbLine && ttsContextKey) {
      speak(fbLine, {
        contextKey: ttsContextKey,
        tag: `fb:${item?.item_ref ?? ""}:${cursor?.fbSeq ?? ""}`,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionReady, cursor?.fbSeq, suspended, contentReady, ttsContextKey]);
  useEffect(() => {
    if (!suspended && cursor?.screen !== "thanks" && cueText && ttsContextKey) {
      speak(cueText, { contextKey: ttsContextKey, tag: tk, enqueue: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cueText, tk, resumeEpoch, ttsContextKey]);
  // 收尾过渡屏:屏上是"今天辛苦了",小语不能同时还在念题(屏幕与语音互相矛盾的跳变)
  useEffect(() => {
    if (cursor?.screen === "thanks") stopSpeaking();
  }, [cursor?.screen]);

  if (sessionTerminal) {
    return <Centered><div className="target">今天辛苦了</div><p className="question">今天的练习已经结束</p></Centered>;
  }

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
  if (!contentReady) {
    return <Centered><p className="question">请稍等一下，马上就好。</p></Centered>;
  }

  return (
    <div className="patient-stage">
      <div className="patient-stage-body">
        <div className="stage-image" data-compact={cueText ? "true" : "false"}>
          <ImagePane
            sessionId={sessionId}
            requestKey={assetRequestKey}
            required={assetRequired}
            spotlight={spotlight}
            compact={!!cueText}
            alt="题目图片"
            onReadinessChange={onAssetReadinessChange}
          />
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
          : blockCopy
            ? (
              <div className="col" style={{ alignItems: "center", gap: "var(--sp-3)" }} role="alert">
                <p className="patient-status">{blockCopy.patient}</p>
                <p className="muted" style={{ maxWidth: "72ch", textAlign: "center" }}>
                  研究者处置：{blockCopy.researcher}
                </p>
              </div>
            )
          : (
            <>
              {!saveError && (
                <MicButton state={!mediaReady || remoteCommandBlocked ? "idle" : (cursor?.recording ?? "idle")} localActive={recActive}
                  selfStart={mediaReady && !remoteCommandBlocked && cursor?.selfStart === true} micError={micError} starting={mediaReady && starting}
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
