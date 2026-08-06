import { useRef } from "react";
import { ImagePane } from "../components/ImagePane.tsx";
import { Centered } from "./Centered.tsx";
import { capturePhaseMatches } from "./autopilotCapturePresentation.ts";
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
    // blockedCalm:runtime 被服务器收走(收尾/暂停/中止)不是设备故障,平静档;
    // 其余 blocked(设备绑定/状态不一致/图片失败)保持告警档。
    return (
      <Centered>
        <div className="target">我们先等一下</div>
        <p className="question" role={autopilot.blockedCalm ? "status" : "alert"}>
          {autopilot.reason ?? "请研究者查看设备状态"}
        </p>
      </Centered>
    );
  }

  const runtime = autopilot.runtime;
  const command = autopilot.current;
  if (runtime?.phase === "scope_completed") {
    return <Centered><div className="target">这一段完成了</div><p className="question">请稍候…</p></Centered>;
  }
  if (externallyPaused || runtime?.phase === "paused") {
    // runtime_released:服务器收走了本 runtime 世代(收尾/暂停/中止),不是设备
    // 故障——配平静文案,结局由 live 通道呈现;只有真正的设备侧判死才告警。
    const calm = externallyPaused || runtime?.pause_reason === "runtime_released";
    return (
      <Centered>
        <div className="target">我们先休息一下</div>
        <p className="question" role={calm ? "status" : "alert"}>
          {calm ? "练习已暂停，请稍候" : "自动流程已安全停止，请研究者处置"}
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
  // 两端都以浏览器自己的事实为准，不等服务器 runtime。
  // 开录那一端：真实 onstart 之后 record_started 还要走一整个网络往返，服务器
  // 此刻仍是 waiting_recording；等它才显示"正在听您说"，就是白白吃掉老人的
  // 作答时间。收麦那一端：麦克风已经物理关闭、字节还在保存上传时，服务器
  // 仍合法地是 recording，这时说"正在听您说"等于请老人对着已关的麦克风继续讲。
  const localPhase = capturePhaseMatches(
    autopilot.localCapturePhase, sessionId, command.command_key)
    ? autopilot.localCapturePhase
    : null;
  const listening = localPhase?.phase === "listening";
  const persisting = localPhase?.phase === "persisting";
  const status = !activated
    ? "点一下屏幕后开始"
    : autopilot.assetReadiness?.requestKey !== command.command_key
        || autopilot.assetReadiness.readiness === "loading"
      ? "正在准备题目图片"
    : persisting
      ? "已收音，正在保存，请稍候"
    : listening
      ? "正在听您说"
    : runtime?.phase === "tts_playing"
      ? "正在为您朗读"
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
        {listening && (
          <>
            {/* 服务器托管的自动流程里这个按钮只是提前结束，不是完成链路的必要条件：
                不点，到作答窗口尽头也会自动收麦并继续。文案必须把这点说清楚，
                否则老人会以为不点就卡住。人工/legacy 平面的麦克风按钮不受影响。 */}
            <button
              type="button"
              className="patient-primary-action patient-primary-action--secondary"
              onClick={autopilot.stopRecordingNow}
              aria-describedby="autopilot-stop-optional"
            >
              说完了可以点这里
            </button>
            <p className="patient-optional-hint" id="autopilot-stop-optional">
              不点也可以，我们会自动继续
            </p>
          </>
        )}
      </div>
    </div>
  );
}
