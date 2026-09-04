import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { MicButton } from "../components/MicButton";
import { rapportTurnKey, type RapportMsg } from "../sync/messages";
import { Centered } from "./Centered";
import { audioRecorderBlockCopy } from "./audioRecorderBlock";
import type { RapportPresentationExpectation } from "./presentationContent";
import { speak, stopSpeaking } from "./tts";
import { usePatientPresentation } from "./usePatientPresentation";
import { onSpeechSettled, ttsEnabled } from "./tts";

// 播放收口信号的兜底上限。迟开麦研究者能手点补救,永不开麦不能。
// 云合成冷启的上界够用即可;等太久老人会以为没人理他,而迟开麦研究者能手点补救。
const SPEECH_SETTLE_TIMEOUT_MS = 12000;
// 第1周单段回答上限。自动带练要靠「说完了就往下走」:老人不按「我说好了」时,
// 到点自动停麦保存,机器人照常接话。第2-8周的作答窗是 15 秒;第1周是聊天,给宽些。
// 研究者随时能在控制台点「停止受试者端录音」提前收麦。
const RAPPORT_MAX_RECORDING_MS = 30_000;

type SpeechGate = { identity: string; settled: boolean } | null;
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
  // 「这一刻该显示/念哪一句」由位置+话拍+发声行唯一决定,**不含 wseq**。
  // wseq 每次写(含开麦)都被服务端重签,但它不改变内容。把它算进判据会同时
  // 造成两种伤害:①朗读被掐断并从头重念一遍,而这一遍常发生在麦克风已开之后,
  // 机器人自己的声音被录进老人的答句;②录音链把这次闪断当挂起,封存闩用这条
  // arm 自己的 recSeq 把它永久封死,麦克风再也打不开(六十轮存量缺陷)。
  // 换问/换拍/换发声行时身份真的变了,仍会先确认呈现再开麦(fail-closed 保留)。
  const utteranceId = rapportStep?.utteranceId;
  const contentIdentity =
    `${rapportStep?.sectionKey ?? ""}#${qIdx}#${beat}#${utteranceId ?? ""}`;
  const confirmedPositionRef = useRef<string | null>(null);
  if (contentReady) confirmedPositionRef.current = contentIdentity;
  const positionConfirmed = confirmedPositionRef.current === contentIdentity;
  // 已经念过的那一句(按内容身份)。开麦重签 wseq 不改变它,所以不会重念。
  const spokenIdentityRef = useRef<string | null>(null);
  // ★开麦闭环:小语这句还没播完就不放行麦克风。
  // 控制台那边的"等它说完"只能开环估时,而云合成冷启没有上界——抢话时机器人
  // 自己的声音会被录进老人的答句,再被云 ASR 当成老人说的话写进研究账本、
  // 喂进下一轮 prompt。播放收口(播完/失败/被打断)才是唯一可靠的信号。
  const speakingTagRef = useRef<string | null>(null);
  const [speechGate, setSpeechGate] = useState<SpeechGate>(null);
  useEffect(() => onSpeechSettled((tag) => {
    if (tag && tag === speakingTagRef.current) {
      setSpeechGate((gate: SpeechGate) => (gate && !gate.settled ? { ...gate, settled: true } : gate));
    }
  }), []);
  // 收口信号万一没来(引擎异常/订阅错过),不能把麦克风永久锁死:超时即放行。
  // 迟开麦是可恢复的(研究者能手点),永不开麦不是。
  useEffect(() => {
    if (!speechGate || speechGate.settled) return;
    const timer = window.setTimeout(() => {
      setSpeechGate((gate: SpeechGate) => (gate && !gate.settled ? { ...gate, settled: true } : gate));
    }, SPEECH_SETTLE_TIMEOUT_MS);
    return () => window.clearTimeout(timer);
  }, [speechGate]);
  // 小语开口:机器人节话术变了就读;研究者节/脚本未就绪/校验失败一律不读。
  useLayoutEffect(() => {
    // 断线/暂停/这一拍本就不该出声时必须立刻收声;此外只有「内容真的换了」
    // 才打断——仅 wseq 重签造成的呈现闪断不算换内容。
    const mustSilence = !connectionReady || isPaused
      || (contentReady && (!isRobot || !text || !ttsContextKey));
    if (mustSilence || contentIdentity !== spokenIdentityRef.current) {
      stopSpeaking();
    }
    if (!connectionReady || isPaused) spokenIdentityRef.current = null;
  }, [connectionReady, isPaused, contentReady, isRobot, text, ttsContextKey, contentIdentity]);
  useEffect(() => {
    if (!(connectionReady && !isPaused && contentReady && isRobot && text && ttsContextKey)) return;
    if (spokenIdentityRef.current === contentIdentity) return;   // 同一句不重念
    spokenIdentityRef.current = contentIdentity;
    const speechTag = `rapport:${rapportPresentation?.section_key ?? ""}:${qIdx}:${beat}`;
    speakingTagRef.current = speechTag;
    // 语音关着时没有播放可等,直接算收口。
    setSpeechGate({ identity: contentIdentity, settled: !ttsEnabled() });
    speak(text, {
      contextKey: ttsContextKey,
      tag: speechTag,
      // 回应拍带发声记录编号时按行取音——LLM 现编句只有这一条发声通道。
      fetchPath: beat === "reply" && utteranceId
        ? `/sessions/${encodeURIComponent(sessionId)}/rapport/utterances/${utteranceId}/tts`
        : undefined,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionReady, isPaused, contentReady, isRobot, text, ttsContextKey, contentIdentity]);

  // 小语正在念这一句:麦克风等它说完再开。
  // ★不能走 suspended:那是「永久挂起」语义,armed 到达时为真会让 useVoxRecorder
  // 的封存闩用**这条 arm 自己的 recSeq** 把它永久封死(六十轮那个坑的同一机制)。
  // 等播完是「临时等待」——对录音链先把 armed 压成 idle,播完再放行,recSeq 不变,
  // recording 的 idle→armed 边沿会正常触发开麦。
  const speechInFlight = speechGate !== null
    && speechGate.identity === contentIdentity && !speechGate.settled;
  const recording = rapportStep?.recording ?? "idle";
  const gatedRecording = speechInFlight && recording === "armed" ? "idle" : recording;
  const {
    stopAndSave, discardForPatientPause, retrySave, saving, canRetry, recActive, micError, saveError,
    starting, remoteCommandBlocked, blockReason,
  } = useVoxRecorder({
    sessionId,
    recording: gatedRecording,
    recSeq: rapportStep?.recSeq,
    commandSeq: rapportStep?.wseq,
    turnKey: rapportTurnKey(rapportStep?.sectionKey ?? "", rapportStep?.questionIdx ?? 0),
    containsDirectIdentifier: rapportStep?.containsDirectIdentifier ?? false,
    connectionReady,
    // error 是「呈现真的取不回来」(每秒重试仍失败),不是 wseq 重签造成的闪断:
    // 此时组件走 fail-closed 早返回,屏上只有「请稍等一下」、老人连停止按钮都
    // 没有——不挡住就是一支看不见的热麦。闪断(error 为空、只是在途)不受影响。
    suspended: sessionPaused || sessionTerminal || !positionConfirmed || Boolean(error),
    stopRequested: isPaused || sessionTerminal || !positionConfirmed,
    maxRecordingMs: RAPPORT_MAX_RECORDING_MS,
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
