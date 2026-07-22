import { useEffect, useLayoutEffect } from "react";
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
}: {
  rapportStep?: RapportMsg;
  sessionId: string;
  ttsContextKey: TtsPlaybackContextKey | null;
  connectionReady?: boolean;
  sessionPaused?: boolean;
  sessionTerminal?: boolean;
}) {
  const presentationExpected: RapportPresentationExpectation | null =
    rapportStep && !sessionTerminal
      ? {
          mode: "rapport",
          sessionId,
          sectionKey: rapportStep.sectionKey,
          questionIdx: rapportStep.questionIdx,
          wseq: rapportStep.wseq,
        }
      : null;
  const { presentation, error } = usePatientPresentation(presentationExpected);
  const rapportPresentation = presentation?.mode === "rapport" ? presentation : null;
  const contentReady = rapportPresentation !== null;
  const isRobot = rapportPresentation?.speaker === "机器人";
  const qIdx = rapportStep?.questionIdx ?? 0;
  const text = isRobot
    ? rapportPresentation?.text ?? ""
    : "我们一起聊聊天，好吗？";

  const isPaused = sessionPaused || sessionTerminal || rapportStep?.paused === true;
  // 小语开口:机器人节话术变了就读;研究者节/脚本未就绪/校验失败一律不读。
  useLayoutEffect(() => {
    if (!(connectionReady && !isPaused && contentReady && isRobot && text && ttsContextKey)) {
      // A robot line may still be fetching when the next step belongs to the
      // researcher.  Invalidate it before paint so it cannot speak over the
      // human-led step.
      stopSpeaking();
    }
  }, [connectionReady, isPaused, contentReady, isRobot, text, ttsContextKey]);
  useEffect(() => {
    if (connectionReady && !isPaused && contentReady && isRobot && text && ttsContextKey) {
      speak(text, {
        contextKey: ttsContextKey,
        tag: `rapport:${rapportPresentation?.section_key ?? ""}:${qIdx}`,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionReady, isPaused, contentReady, isRobot, text, ttsContextKey]);

  const recording = rapportStep?.recording ?? "idle";
  const {
    stopAndSave, retrySave, saving, canRetry, recActive, micError, saveError,
    starting, remoteCommandBlocked, blockReason,
  } = useVoxRecorder({
    sessionId,
    recording: rapportStep?.recording,
    recSeq: rapportStep?.recSeq,
    commandSeq: rapportStep?.wseq,
    turnKey: `关系建立·${rapportStep?.sectionKey ?? ""}`,
    containsDirectIdentifier: rapportStep?.containsDirectIdentifier ?? false,
    connectionReady,
    suspended: sessionPaused || sessionTerminal || !contentReady,
    stopRequested: isPaused || sessionTerminal || !contentReady,
  });
  const blockCopy = blockReason ? audioRecorderBlockCopy(blockReason) : null;

  if (sessionTerminal) {
    return <Centered className="patient-rapport"><div className="target">今天辛苦了</div><p className="question">今天的交流已经结束</p></Centered>;
  }

  if (isPaused) {
    return (
      <Centered className="patient-rapport">
        <div className="target">我们先休息一下</div>
        <p className="question">准备好后，研究者会继续</p>
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
      <div className="patient-stage-body rapport-body"
        style={{ flex: "1 1 auto", minHeight: 0, width: "100%", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "var(--sp-4)" }}>
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
                  <p className="muted" style={{ maxWidth: "72ch", textAlign: "center" }}>
                    研究者处置：{blockCopy.researcher}
                  </p>
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
      )}
    </div>
  );
}

// 注:此文件不 import 任何评分模块;画像写入在 patientLocalProfile(仅本地)。
