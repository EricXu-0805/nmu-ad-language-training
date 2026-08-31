import { useEffect, useLayoutEffect, useRef } from "react";
import { MicButton } from "../components/MicButton";
import type { RapportMsg } from "../sync/messages";
import { Centered } from "./Centered";
import { audioRecorderBlockCopy } from "./audioRecorderBlock";
import type { RapportPresentationExpectation } from "./presentationContent";
import { speak, stopSpeaking } from "./tts";
import { usePatientPresentation } from "./usePatientPresentation";
import { useVoxRecorder } from "./useVoxRecorder";
import type { TtsPlaybackContextKey } from "./ttsContext";

// 第1周关系建立屏:对话模式(米色暖调、机器人头像、无图无线索梯无评分光环),刻意区别于任务模式防串屏。
// 显示操作端广播的固定话术;画像采集仅落本地(patientLocalProfile),此屏不 POST。
// 录音同任务屏 VOX:操作端 arm 才录;自我介绍段 containsDirectIdentifier 随消息带入登记(导出侧整段红线)。
// TTS 只给 speaker="机器人" 的节开口;"研究者"节是当面话术,屏上只显欢迎语、不朗读。
export function RapportStage({
  rapportStep,
  sessionId,
  ttsContextKey,
  connectionReady = true,
  sessionPaused = false,
  sessionTerminal = false,
  registerImmediateDiscard,
}: {
  rapportStep?: RapportMsg;
  sessionId: string;
  ttsContextKey: TtsPlaybackContextKey | null;
  connectionReady?: boolean;
  sessionPaused?: boolean;
  sessionTerminal?: boolean;
  registerImmediateDiscard?: (handler: (() => void) | null) => void;
}) {
  const presentationExpected: RapportPresentationExpectation | null =
    rapportStep && !sessionTerminal
      ? {
          mode: "rapport",
          sessionId,
          sectionKey: rapportStep.sectionKey,
          questionIdx: rapportStep.questionIdx,
          beat: rapportStep.beat ?? "ask",
          wseq: rapportStep.wseq,
        }
      : null;
  const { presentation, error } = usePatientPresentation(presentationExpected);
  const rapportPresentation = presentation?.mode === "rapport" ? presentation : null;
  const contentReady = rapportPresentation !== null;
  const isRobot = rapportPresentation?.speaker === "机器人";
  const qIdx = rapportStep?.questionIdx ?? 0;
  const beat = rapportStep?.beat ?? "ask";
  const text = isRobot
    ? rapportPresentation?.text ?? ""
    : "我们一起聊聊天，好吗？";

  const isPaused = sessionPaused || sessionTerminal || rapportStep?.paused === true;
  // 录音链的挂起判据只认「题位已确认」,不认 wseq 级新鲜度:每次 arm 都会重签
  // wseq → 呈现期望必刷新 → contentReady 必闪断;若把这次闪断当挂起,
  // useVoxRecorder 的封存闩会用**这一条 arm 自己的 recSeq** 把它永久封死,
  // 麦克风永远打不开(真机走查抓出的存量缺陷)。换题/换拍仍会先确认再开麦。
  const positionKey = `${rapportStep?.sectionKey ?? ""}#${qIdx}#${beat}`;
  const confirmedPositionRef = useRef<string | null>(null);
  if (contentReady) confirmedPositionRef.current = positionKey;
  const positionConfirmed = confirmedPositionRef.current === positionKey;
  // 小语开口:机器人节话术变了就读;研究者节/脚本未就绪/校验失败一律不读。
  useLayoutEffect(() => {
    if (!(connectionReady && !isPaused && contentReady && isRobot && text && ttsContextKey)) {
      // A robot line may still be fetching when the next step belongs to the
      // researcher.  Invalidate it before paint so it cannot speak over the
      // human-led step.
      stopSpeaking();
    }
  }, [connectionReady, isPaused, contentReady, isRobot, text, ttsContextKey]);
  const utteranceId = rapportStep?.utteranceId;
  useEffect(() => {
    if (connectionReady && !isPaused && contentReady && isRobot && text && ttsContextKey) {
      speak(text, {
        contextKey: ttsContextKey,
        tag: `rapport:${rapportPresentation?.section_key ?? ""}:${qIdx}:${beat}`,
        // 回应拍带发声记录编号时按行取音——LLM 现编句只有这一条发声通道。
        fetchPath: beat === "reply" && utteranceId
          ? `/sessions/${encodeURIComponent(sessionId)}/rapport/utterances/${utteranceId}/tts`
          : undefined,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionReady, isPaused, contentReady, isRobot, text, ttsContextKey, utteranceId]);

  const recording = rapportStep?.recording ?? "idle";
  const {
    stopAndSave, discardForPatientPause, retrySave, saving, canRetry, recActive, micError, saveError,
    starting, remoteCommandBlocked, blockReason,
  } = useVoxRecorder({
    sessionId,
    recording: rapportStep?.recording,
    recSeq: rapportStep?.recSeq,
    commandSeq: rapportStep?.wseq,
    turnKey: `关系建立·${rapportStep?.sectionKey ?? ""}`,
    containsDirectIdentifier: rapportStep?.containsDirectIdentifier ?? false,
    connectionReady,
    suspended: sessionPaused || sessionTerminal || !positionConfirmed,
    stopRequested: isPaused || sessionTerminal || !positionConfirmed,
  });
  useLayoutEffect(() => {
    registerImmediateDiscard?.(discardForPatientPause);
    return () => registerImmediateDiscard?.(null);
  }, [discardForPatientPause, registerImmediateDiscard]);
  const blockCopy = blockReason ? audioRecorderBlockCopy(blockReason) : null;

  if (sessionTerminal) {
    return <Centered className="patient-rapport"><div className="target">今天辛苦了</div><p className="question">今天的交流已经结束</p></Centered>;
  }

  if (isPaused) {
    return (
      <Centered className="patient-rapport">
        <div className="target">我们先休息一下</div>
        <p className="question">休息好了，工作人员会继续</p>
      </Centered>
    );
  }

  if (error || !contentReady) {
    // fail-closed:最小投影未就绪/校验失败，拒绝凭本地整包脚本或旧问句开场。
    return (
      <Centered className="patient-rapport">
        <p className="question">请稍等一下，马上就好。</p>
      </Centered>
    );
  }

  const effectiveRecording = remoteCommandBlocked ? "idle" : recording;
  const recordingRequested = effectiveRecording === "armed" || effectiveRecording === "recording";
  const showRecorder = recordingRequested || recActive || saving || saveError || micError || blockCopy !== null;

  return (
    <div className="patient-stage patient-rapport" style={{ background: "#F3ECD9" }}>
      <div className="patient-stage-body rapport-body">
        <div className="rapport-brand" aria-hidden="true">语</div>
        <p className="question" aria-live="polite" aria-atomic="true" style={{ maxWidth: "80vw" }}>{text}</p>
        {rapportStep?.assentGate && (
          <p className="cue" role="status" aria-live="polite" style={{ color: "var(--c-primary)" }}>等您点头同意，我们再开始</p>
        )}
      </div>
      {showRecorder && (
        <div className="stage-mic" aria-busy={saving}>
          {saving
            ? <p className="patient-status" role="status" aria-live="polite">正在保存，请稍候</p>
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
                {!saveError && (recordingRequested || recActive || micError) && (
                  <MicButton state={effectiveRecording} localActive={recActive} micError={micError} starting={starting} onStop={stopAndSave} />
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
      )}
    </div>
  );
}

// 注:此文件不 import 任何评分模块;画像写入在 patientLocalProfile(仅本地)。
