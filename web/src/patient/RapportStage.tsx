import { useEffect } from "react";
import { useWeek1Script } from "../content/bundle";
import { MicButton } from "../components/MicButton";
import type { RapportMsg } from "../sync/messages";
import { Centered } from "./Centered";
import { speak } from "./tts";
import { useVoxRecorder } from "./useVoxRecorder";

// 第1周关系建立屏:对话模式(米色暖调、机器人头像、无图无线索梯无评分光环),刻意区别于任务模式防串屏。
// 显示操作端广播的固定话术;画像采集仅落本地(patientLocalProfile),此屏不 POST。
// 录音同任务屏 VOX:操作端 arm 才录;自我介绍段 containsDirectIdentifier 随消息带入登记(导出侧整段红线)。
// TTS 只给 speaker="机器人" 的节开口;"研究者"节是当面话术,屏上只显欢迎语、不朗读。
export function RapportStage({ rapportStep, sessionId, connectionReady = true, sessionPaused = false }: {
  rapportStep?: RapportMsg;
  sessionId: string;
  connectionReady?: boolean;
  sessionPaused?: boolean;
}) {
  const { script, error } = useWeek1Script();
  const section = script?.sections.find((s) => s.key === rapportStep?.sectionKey);
  const isRobot = section?.speaker === "机器人";
  const qIdx = rapportStep?.questionIdx ?? 0;
  const text = isRobot
    ? section?.questions?.[Math.min(qIdx, Math.max(0, (section.questions?.length ?? 1) - 1))]?.ask
      ?? section?.line
      ?? script?.generic_fallback_line
      ?? ""
    : "我们一起聊聊天，好吗？";

  const isPaused = sessionPaused || rapportStep?.paused === true;
  // 小语开口:机器人节话术变了就读;研究者节/脚本未就绪/校验失败一律不读。
  useEffect(() => {
    if (connectionReady && !isPaused && script && isRobot && text) {
      speak(text, { tag: `rapport:${section?.key ?? ""}:${qIdx}` });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionReady, isPaused, script, isRobot, text]);

  const recording = rapportStep?.recording ?? "idle";
  const { stopAndSave, retrySave, saving, canRetry, recActive, micError, saveError, starting, remoteCommandBlocked } = useVoxRecorder({
    sessionId,
    recording: rapportStep?.recording,
    recSeq: rapportStep?.recSeq,
    commandSeq: rapportStep?.wseq,
    turnKey: `关系建立·${rapportStep?.sectionKey ?? ""}`,
    containsDirectIdentifier: rapportStep?.containsDirectIdentifier ?? false,
    connectionReady,
    suspended: sessionPaused,
  });

  if (isPaused) {
    return (
      <Centered className="patient-rapport">
        <div className="target">我们先休息一下</div>
        <p className="question">准备好后，研究者会继续</p>
      </Centered>
    );
  }

  if (error) {
    // fail-closed:脚本 schema 校验失败,拒绝凭错误内容开场(对老人仍显平静文案)
    return (
      <Centered className="patient-rapport">
        <p className="question">请稍等一下，马上就好。</p>
      </Centered>
    );
  }

  const effectiveRecording = remoteCommandBlocked ? "idle" : recording;
  const recordingRequested = effectiveRecording === "armed" || effectiveRecording === "recording";
  const showRecorder = recordingRequested || recActive || saving || saveError;

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
            : (
              <>
                {!saveError && (recordingRequested || recActive) && (
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
