import { useRef } from "react";
import { ImagePane } from "../components/ImagePane.tsx";
import { Centered } from "./Centered.tsx";
import {
  resolveExactAutopilotDisplayText,
  type ExactAutopilotDisplayText,
} from "./autopilotDisplayText.ts";
import type { PatientAutopilotView } from "./usePatientAutopilot.ts";

export function PatientAutopilotStage({
  autopilot,
  sessionId,
  activated,
  ttsOn,
  externallyPaused,
}: {
  autopilot: PatientAutopilotView;
  sessionId: string;
  activated: boolean;
  ttsOn: boolean;
  externallyPaused: boolean;
}) {
  const displayRef = useRef<ExactAutopilotDisplayText | null>(null);
  displayRef.current = autopilot.mode === "server"
    ? resolveExactAutopilotDisplayText(displayRef.current, autopilot.current)
    : null;
  if (autopilot.mode === "probing") {
    return (
      <Centered>
        <div className="target">正在确认安全连接</div>
        <p className="question" role="status">
          {autopilot.reason ?? "正在准备今天的练习…"}
        </p>
      </Centered>
    );
  }
  if (autopilot.mode === "blocked") {
    return (
      <Centered>
        <div className="target">我们先等一下</div>
        <p className="question" role="alert">{autopilot.reason ?? "请研究者查看设备状态"}</p>
      </Centered>
    );
  }

  const runtime = autopilot.runtime;
  const command = autopilot.current;
  if (runtime?.phase === "scope_completed") {
    return <Centered><div className="target">这一段完成了</div><p className="question">请稍候…</p></Centered>;
  }
  if (externallyPaused || runtime?.phase === "paused") {
    return (
      <Centered>
        <div className="target">我们先休息一下</div>
        <p className="question" role={externallyPaused ? "status" : "alert"}>
          {externallyPaused ? "练习已暂停，请稍候" : "自动流程已安全停止，请研究者处置"}
        </p>
      </Centered>
    );
  }
  if (!ttsOn) {
    return (
      <Centered>
        <div className="target">声音还没有开启</div>
        <p className="question" role="alert">请研究者点右上角“朗读 开”后继续</p>
      </Centered>
    );
  }
  if (!command) {
    return (
      <Centered>
        <div className="target">请稍候</div>
        <p className="question">正在理解刚才的回答…</p>
      </Centered>
    );
  }

  const speechText = displayRef.current?.text
    ?? "请稍候，研究者正在核对当前提示。";
  const status = !activated
    ? "点一下屏幕后开始"
    : autopilot.assetReadiness?.requestKey !== command.command_key
        || autopilot.assetReadiness.readiness === "loading"
      ? "正在准备题目图片"
    : runtime?.phase === "tts_playing"
      ? "正在为您朗读"
      : runtime?.phase === "recording"
        ? "正在听您说"
        : command.kind === "record" ? "正在准备麦克风" : "正在准备朗读";

  return (
    <div className="patient-stage">
      <div className="patient-stage-body">
        <div className="stage-image">
          <ImagePane
            sessionId={sessionId}
            requestKey={command.command_key}
            spotlight="none"
            compact={false}
            alt="题目图片"
            onReadinessChange={autopilot.reportAssetReadiness}
          />
        </div>
        <p className="question" aria-live="polite" aria-atomic="true">{speechText}</p>
        <div className="cue-slot" />
      </div>
      <div className="stage-mic" aria-live="polite">
        <p className="patient-status" role="status">{status}</p>
        {runtime?.phase === "recording" && (
          <button
            type="button"
            className="patient-primary-action patient-primary-action--secondary"
            onClick={autopilot.stopRecordingNow}
          >
            我说好了
          </button>
        )}
      </div>
    </div>
  );
}
