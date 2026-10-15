import { useRef, useState } from "react";
import { ImagePane } from "../components/ImagePane.tsx";
import { Centered } from "./Centered.tsx";
import { capturePhaseMatches } from "./autopilotCapturePresentation.ts";
import {
  resolveExactAutopilotDisplayText,
  type ExactAutopilotDisplayText,
} from "./autopilotDisplayText.ts";
import { patientStimulusKey } from "./patientAssetMediaGate.ts";
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
  const [answerNowKey, setAnswerNowKey] = useState<string | null>(null);
  displayRef.current = autopilot.mode === "server"
    ? resolveExactAutopilotDisplayText(displayRef.current, autopilot.current)
    : null;
  if (autopilot.mode === "probing") {
    return (
      <Centered>
        <div className="target">正在准备</div>
        <p className="question" role="status">
          {autopilot.reason ? "请稍等一下" : "正在准备今天的练习…"}
        </p>
        {autopilot.reason && (
          <details className="muted" style={{ maxWidth: "72ch", fontSize: "var(--text-lg)" }}>
            <summary>工作人员点这里看原因</summary>
            <p style={{ margin: "var(--sp-2) 0 0", textAlign: "left" }}>{autopilot.reason}</p>
          </details>
        )}
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
          {autopilot.blockedCalm ? autopilot.reason ?? "练习已暂停，请稍候" : "请找工作人员"}
        </p>
        {!autopilot.blockedCalm && autopilot.reason && (
          <details className="muted" style={{ maxWidth: "72ch", fontSize: "var(--text-lg)" }}>
            <summary>工作人员点这里看原因</summary>
            <p style={{ margin: "var(--sp-2) 0 0", textAlign: "left" }}>{autopilot.reason}</p>
          </details>
        )}
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
          {calm ? "练习已暂停，请稍候" : "练习先停一下，请找工作人员"}
        </p>
        {!calm && (
          <details className="muted" style={{ maxWidth: "72ch", fontSize: "var(--text-lg)" }}>
            <summary>工作人员点这里看原因</summary>
            <p style={{ margin: "var(--sp-2) 0 0", textAlign: "left" }}>自动流程已安全停止，请研究者处置</p>
          </details>
        )}
      </Centered>
    );
  }
  if (!ttsOn) {
    return (
      <Centered>
        <div className="target">声音还没有开启</div>
        <p className="question" role="alert">请工作人员打开右上角的「朗读」开关</p>
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
    ?? "请稍等一下";
  // 题图按 item/turn 取、按 item/turn 判就绪：提问→录音换的是 command_key，
  // 不是题，同一张图不再下载第二遍。
  const stimulusKey = patientStimulusKey(command);
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
  const bargeIn = autopilot.bargeIn?.sessionId === sessionId
    && autopilot.bargeIn.itemRef === command.item_ref
    && autopilot.bargeIn.turnSeq === command.turn_seq
    && autopilot.bargeIn.attemptSeq === command.attempt_seq
    && autopilot.bargeIn.controlGeneration === command.control_generation
    && autopilot.bargeIn.runnerGeneration === command.runner_generation
    ? autopilot.bargeIn : null;
  // 「从头回答」只对被打断的那一句提问/线索和紧接着的录音说;同一回答位置上的
  // 反馈、报答案、重播的提问都是别的朗读命令,照常显示。
  const voiceInterrupted = bargeIn?.phase === "interrupted"
    && (command.kind === "record"
      ? command.payload.presentation_purpose === "question"
        || command.payload.presentation_purpose === "cue"
      : command.command_key === bargeIn.commandKey);
  // 检测只在这一句提问/线索真正播放时活着;残留的 monitoring 观察不算在听。
  const voiceMonitoring = bargeIn?.phase === "monitoring"
    && command.kind === "tts" && command.state === "started"
    && bargeIn.commandKey === command.command_key
    && runtime?.phase === "tts_playing";
  const persisting = localPhase?.phase === "persisting";
  const awaitingAnswerProcessing = runtime?.phase === "waiting_server_after_record";
  const awaitingAfterSpeech = runtime?.phase === "waiting_server_after_tts";
  const purpose = command.kind === "tts"
    ? command.payload.purpose : command.payload.presentation_purpose;
  const answerPrompt = purpose === "question" || purpose === "cue";
  const imageReady = autopilot.assetReadiness?.requestKey === stimulusKey
    && autopilot.assetReadiness.readiness === "ready";
  const status = !activated
    ? "点一下屏幕后开始"
    : !imageReady
      ? "正在准备题目图片"
    : awaitingAnswerProcessing
      ? "正在理解刚才的回答，请稍候"
    : persisting
      ? "录好了，正在保存"
    : listening
      ? voiceInterrupted ? "正在听您说，请从头完整说一次" : "正在听您说"
    : voiceInterrupted
      ? "朗读已停止，看到「正在听您说」后从头回答"
    : answerNowKey === command.command_key
      ? "朗读已停止，正在准备麦克风"
    : awaitingAfterSpeech
      ? answerPrompt ? "正在准备麦克风" : "请稍候，马上继续"
    : runtime?.phase === "tts_playing"
      ? "正在为您朗读"
      : command.kind === "record" ? "正在准备麦克风" : "正在准备朗读";
  // 提问/线索在播、以及播完到真实开麦之间那一小段，老人常常已经开口，那句话录不
  // 进去(2026-09-17 养老院实测)。屏上给一句平静的提示；真实 onstart 一到，
  // listening 接管，换成"正在听您说"。反馈/报答案句不是在等回答，不提示。
  const listenFirstCue = activated && imageReady && !listening && !persisting
    && (purpose === "question" || purpose === "cue")
    && (command.kind === "record"
      ? runtime?.phase === "record_ready"
      : runtime?.phase === "tts_ready" || runtime?.phase === "tts_playing"
        || runtime?.phase === "waiting_server_after_tts");

  // V2:线索级(prompt_level>0)话术比首问长得多——题图收紧一档(与 legacy
  // PatientStage 的 cueText 规则同义),长文本与贴底收音区互不遮挡。
  const compactImage = (displayRef.current?.promptLevel ?? command.prompt_level) > 0;
  const canAnswerNow = activated && imageReady && command.kind === "tts"
    && command.state === "started" && answerNowKey !== command.command_key
    && !voiceInterrupted && runtime?.phase === "tts_playing"
    && (purpose === "question" || purpose === "cue");
  return (
    <div className="patient-stage">
      <div className="patient-stage-body">
        <div className="stage-image" data-compact={compactImage ? "true" : "false"}>
          <ImagePane
            sessionId={sessionId}
            requestKey={stimulusKey}
            spotlight="none"
            compact={compactImage}
            alt="题目图片"
            onReadinessChange={autopilot.reportAssetReadiness}
          />
        </div>
        <p className="question" aria-live="polite" aria-atomic="true">{speechText}</p>
        {/* 提示槽恒占位：提示出现/消失不把问句和麦克风上下顶。不出声，只是屏上一句。 */}
        <div className="cue-slot">
          {listenFirstCue && (
            <p className="patient-optional-hint" data-cue="listen-first" style={{ margin: 0 }}>
              {voiceInterrupted ? "刚才的开头还没有录下，请稍候再完整说一次"
                : voiceMonitoring ? "想回答时可以开口，或点「现在回答」"
                : awaitingAfterSpeech || command.kind === "record"
                  ? "看到「正在听您说」后再回答" : "请听完再回答"}
            </p>
          )}
        </div>
      </div>
      <div className="stage-mic patient-autopilot-controls" aria-live="polite">
        {/* 每个阶段都保留状态、按钮和说明三行的空间。占位只有不可见的排版空格，
            不包含按钮、不进读屏或焦点序列；按最长现有文案预留，窄屏换行也一致。 */}
        <div className="patient-control-row patient-control-status-row">
          <span className="patient-status patient-layout-reserve" aria-hidden="true">
            {"\u3000".repeat(24)}
          </span>
          <p className="patient-status" role="status">{status}</p>
        </div>
        <div className="patient-control-row patient-control-action-row">
          <span className="patient-primary-action patient-layout-reserve" aria-hidden="true">
            {"\u3000".repeat(9)}
          </span>
          {canAnswerNow && (
            <button type="button" className="patient-primary-action patient-primary-action--secondary"
              onClick={() => {
                if (autopilot.answerNow?.()) setAnswerNowKey(command.command_key);
              }} aria-describedby="autopilot-answer-now-hint">
              现在回答
            </button>
          )}
          {/* 不点也会在作答窗口尽头自动收麦；这个按钮仅供提前结束。 */}
          {listening && (
            <button
              type="button"
              className="patient-primary-action patient-primary-action--secondary"
              onClick={autopilot.stopRecordingNow}
              aria-describedby="autopilot-stop-optional"
            >
              说完了可以点这里
            </button>
          )}
        </div>
        <div className="patient-control-row patient-control-hint-row">
          <span className="patient-optional-hint patient-layout-reserve" aria-hidden="true">
            {"\u3000".repeat(24)}
          </span>
          {canAnswerNow && (
            <p className="patient-optional-hint" id="autopilot-answer-now-hint">
              点这里停止朗读，看到「正在听您说」后回答
            </p>
          )}
          {listening && (
            <p className="patient-optional-hint" id="autopilot-stop-optional">
              不点也可以，我们会自动继续
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
