import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { MicButton, MicButtonSpace } from "../components/MicButton";
import { ImagePane, type Spotlight } from "../components/ImagePane";
import { turnKey } from "../lib/ids";
import type { CursorMsg } from "../sync/messages";
import { Centered } from "./Centered";
import type { TaskPresentationExpectation } from "./presentationContent";
import { onSpeechSettled, speak, stopSpeaking, ttsEnabled } from "./tts";
import { TaskSpeechGate } from "./taskSpeechGate";
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
  ttsOn = ttsEnabled(),
  connectionReady = true,
  sessionPaused = false,
  sessionTerminal = false,
  registerImmediateDiscard,
}: {
  plan: PatientSessionPlan | null;
  cursor?: CursorMsg;
  sessionId: string;
  ttsContextKey: TtsPlaybackContextKey | null;
  ttsOn?: boolean;
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
  const { presentation, error: presentationError } = usePatientPresentation(presentationExpected);
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
  const speechAllowed = !presentationError && assetReady && connectionReady && !isPaused && !sessionTerminal
    && cursor?.screen !== "thanks" && cursor?.screen !== "done" && !!ttsContextKey && !!tk;
  const question = role.includes("作用") ? "它是做什么用的呢？" : role === "关系识别" ? "它们之间有什么关系呢？" : "请看这张图片，这是什么？";
  const cueText = taskPresentation?.cue_text ?? null;
  const fbLine = taskPresentation?.feedback_text ?? null;
  const speechScope = JSON.stringify([sessionId, plan?.item_bank_version_id, tk, ttsContextKey]);
  // Keep only the already verified picture/question shell across same-position cue refreshes.
  // This does not preserve old cue text, or grant playback/recording before the exact projection arrives.
  const displayedScope = useRef<string | null>(null);
  const positionConfirmed = contentReady || displayedScope.current === speechScope;
  useLayoutEffect(() => {
    if (contentReady && !presentationError) displayedScope.current = speechScope;
  }, [contentReady, presentationError, speechScope]);
  const compactImage = !!cueText || (!contentReady && (cursor?.cueLevel ?? 0) > 0);
  const speechKey = JSON.stringify([speechScope, cursor?.cueLevel, cursor?.fbKey, cursor?.fbSeq]);
  const [speechGate] = useState(() => new TaskSpeechGate());
  const [speechRevision, setSpeechRevision] = useState(0);
  const [replayEpoch, setReplayEpoch] = useState(0);
  const replayAuthorization = useRef<string | null>(null);
  replayAuthorization.current = speechAllowed && mediaReady ? speechKey : null;
  const lastReplayEpoch = useRef(0);
  const speechStatus = speechGate.status(speechKey, ttsOn);
  const speechWaiting = !speechAllowed || !mediaReady || (speechStatus !== "played" && speechStatus !== "muted");
  // A pending prompt temporarily masks the arm; it must not permanently revoke recSeq.
  const initialRemote = useRef<{ recording: CursorMsg["recording"]; recSeq: CursorMsg["recSeq"]; commandSeq: CursorMsg["wseq"] } | null>(null);
  // The first real snapshot may arrive after mount; it is still historical authority.
  if (cursor && initialRemote.current === null) {
    initialRemote.current = { recording: cursor.recording, recSeq: cursor.recSeq, commandSeq: cursor.wseq };
  }
  const staleInitialArm = initialRemote.current !== null
    && cursor?.recording === initialRemote.current.recording
    && cursor?.recSeq === initialRemote.current.recSeq && cursor?.wseq === initialRemote.current.commandSeq;
  const gatedRecording = (speechWaiting || staleInitialArm)
    && (cursor?.recording === "armed" || cursor?.recording === "recording") ? "idle" : cursor?.recording;

  const {
    stopAndSave, discardForPatientPause, startNow, retrySave, saving, canRetry, recActive, micError,
    saveError, starting, remoteCommandBlocked, blockReason,
  } = useVoxRecorder({
    sessionId, recording: gatedRecording, recSeq: cursor?.recSeq, commandSeq: cursor?.wseq,
    turnKey: tk, connectionReady, suspended: isPaused || !assetReady,
    selfStartAllowed: !speechWaiting && cursor?.selfStart === true && !isPaused && !sessionTerminal && cursor?.screen !== "thanks" && cursor?.screen !== "done",
    stopRequested: sessionTerminal || isPaused || speechWaiting || cursor?.screen === "thanks" || cursor?.screen === "done",
  });
  useLayoutEffect(() => {
    registerImmediateDiscard?.(discardForPatientPause);
    return () => registerImmediateDiscard?.(null);
  }, [discardForPatientPause, registerImmediateDiscard]);
  const blockCopy = blockReason ? audioRecorderBlockCopy(blockReason) : null;

  const spotlight: Spotlight = role.startsWith("左") ? "left" : role.startsWith("右") ? "right" : role === "关系识别" ? "both" : "none";
  useLayoutEffect(() => onSpeechSettled((tag, outcome) => {
    if (speechGate.settle(tag, outcome)) setSpeechRevision(n => n + 1);
  }), [speechGate]);
  useLayoutEffect(() => {
    const replay = replayEpoch !== lastReplayEpoch.current;
    lastReplayEpoch.current = replayEpoch;
    const stopOld = speechGate.update(speechAllowed ? {
      scope: speechScope, key: speechKey, enabled: ttsOn, ready: mediaReady,
      question, cue: cueText, cueLevel: cursor?.cueLevel ?? 0,
      feedback: fbLine, feedbackSeq: cursor?.fbSeq,
    } : null, replay);
    // Revoke the receipt set before stopSpeaking emits an interruption callback.
    if (stopOld) stopSpeaking();
    setSpeechRevision(n => n + 1);
  }, [speechGate, speechAllowed, speechScope, speechKey, ttsOn, mediaReady, question, cueText, fbLine, cursor?.cueLevel, cursor?.fbSeq, replayEpoch]);
  useEffect(() => {
    // stopAndSave closes tracks asynchronously; saving must also finish before audio starts.
    if (!speechAllowed || !mediaReady || !ttsOn || !ttsContextKey || recActive || starting || saving) return;
    for (const line of speechGate.takePending()) {
      speak(line.text, { contextKey: ttsContextKey, tag: line.tag, enqueue: true });
    }
  }, [speechGate, speechAllowed, mediaReady, ttsOn, ttsContextKey, recActive, starting, saving, speechRevision]);
  useLayoutEffect(() => () => {
    speechGate.clear();
    stopSpeaking();
  }, [speechGate]);

  if (sessionTerminal) {
    return <Centered><div className="target">今天辛苦了</div><p className="question">今天的练习已经结束</p></Centered>;
  }

  if (isPaused) {
    return (
      <Centered>
        <div className="target">我们先休息一下</div>
        <p className="question">休息好了，工作人员会继续</p>
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
  if (presentationError || !positionConfirmed) {
    return <Centered><p className="question">请稍等一下，马上就好。</p></Centered>;
  }

  return (
    <div className="patient-stage">
      <div className="patient-stage-body">
        <div className="stage-image" data-compact={compactImage ? "true" : "false"}>
          <ImagePane
            sessionId={sessionId}
            requestKey={assetRequestKey}
            required={assetRequired}
            spotlight={spotlight}
            compact={compactImage}
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
      <div className="stage-mic patient-recorder-region" aria-busy={saving}>
        <MicButtonSpace />
        {saving
          ? <p className="patient-status" role="status" aria-live="polite">正在保存，请稍候</p>
          : speechWaiting && !recActive && !starting && !saveError
            ? (
              <div className="col" style={{ alignItems: "center", gap: "var(--sp-3)" }} role="status" aria-live="polite">
                <p className="patient-status">{speechStatus === "failed" ? "声音没播完，请重新听一遍" : "请先听小语说完，再回答"}</p>
                {speechStatus === "failed" && speechAllowed && mediaReady && (
                  <button type="button" className="patient-primary-action patient-primary-action--secondary"
                    onClick={() => {
                      if (replayAuthorization.current === speechKey && speechGate.status(speechKey, ttsOn) === "failed") {
                        setReplayEpoch(n => n + 1);
                      }
                    }}>重新听一遍</button>
                )}
              </div>
            )
          : blockCopy
            ? (
              <div className="col" style={{ alignItems: "center", gap: "var(--sp-3)" }} role="alert">
                <p className="patient-status">{blockCopy.patient}</p>
                <details className="muted" style={{ maxWidth: "72ch", fontSize: "var(--text-lg)" }}>
                  <summary>工作人员点这里看原因</summary>
                  <p style={{ margin: "var(--sp-2) 0 0", textAlign: "left" }}>{blockCopy.researcher}</p>
                </details>
              </div>
            )
          : (
            <>
              {!saveError && (
                <MicButton state={remoteCommandBlocked ? "idle" : (gatedRecording ?? "idle")} localActive={recActive}
                  selfStart={!speechWaiting && !remoteCommandBlocked && cursor?.selfStart === true} micError={micError} starting={mediaReady && starting}
                  onStart={() => void startNow()} onStop={stopAndSave} />
              )}
              {saveError && (
                <div className="col" style={{ alignItems: "center", gap: "var(--sp-3)" }} role="alert">
                  <p className="patient-status">
                    {canRetry ? "刚才的回答已经录好，请再点一次保存" : "刚才的回答没保存上，请找工作人员"}
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
