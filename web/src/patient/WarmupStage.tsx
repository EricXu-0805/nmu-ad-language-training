import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { RapportMsg } from "../sync/messages";
import { warmupIdentity } from "../week1Warmup";
import { Centered } from "./Centered";
import { onSpeechSettled, speak, stopSpeaking, ttsEnabled } from "./tts";
import { usePatientPresentation } from "./usePatientPresentation";

/** Optional conversation has no recording hook, playback advancement or scoring path. */
export function WarmupStage({ sessionId, rapportStep, ttsContextKey, allowedIdentity,
  connectionReady, sessionPaused, registerImmediateDiscard }: {
  sessionId: string;
  rapportStep: RapportMsg;
  ttsContextKey: string | null;
  allowedIdentity: string | null;
  connectionReady: boolean;
  sessionPaused: boolean;
  registerImmediateDiscard?: (handler: (() => void) | null) => void;
}) {
  const warmup = rapportStep.warmup;
  const identity = warmupIdentity(warmup);
  const paused = sessionPaused || rapportStep.paused === true;
  const { presentation, error } = usePatientPresentation(warmup ? {
    mode: "rapport", sessionId, sectionKey: rapportStep.sectionKey,
    questionIdx: rapportStep.questionIdx, beat: rapportStep.beat ?? "ask",
    wseq: rapportStep.wseq, warmup,
  } : null);
  const content = presentation?.mode === "rapport" && presentation.warmup ? presentation : null;
  const consumed = useRef<string | null>(null);
  const blockedThroughSeq = useRef(0);
  const current = useRef(warmup);
  current.current = warmup;
  const [failed, setFailed] = useState<string | null>(null);
  const [locallyStopped, setLocallyStopped] = useState(0);
  const activeTag = useRef<string | null>(null);
  const silence = () => {
    blockedThroughSeq.current = Math.max(blockedThroughSeq.current, current.current?.seq ?? 0);
    setLocallyStopped(blockedThroughSeq.current);
    activeTag.current = null;
    stopSpeaking();
  };
  useLayoutEffect(() => {
    registerImmediateDiscard?.(silence);
    const hidden = () => { if (document.visibilityState !== "visible") silence(); };
    window.addEventListener("pagehide", silence);
    document.addEventListener("visibilitychange", hidden);
    return () => {
      registerImmediateDiscard?.(null);
      window.removeEventListener("pagehide", silence);
      document.removeEventListener("visibilitychange", hidden);
      activeTag.current = null;
      stopSpeaking();
    };
    // Stable session stage; silence reads the latest command through current.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [registerImmediateDiscard]);
  useEffect(() => onSpeechSettled((tag, outcome) => {
    if (tag === activeTag.current && outcome !== "played") setFailed(identity);
  }), [identity]);
  const ready = Boolean(warmup && warmup.beat !== "done" && content?.speaker === "机器人"
    && content.text && !error && connectionReady && !paused && ttsContextKey
    && identity === allowedIdentity && warmup.seq > blockedThroughSeq.current);
  useLayoutEffect(() => {
    if (!ready) stopSpeaking();
  }, [ready, identity]);
  useEffect(() => {
    if (!ready || !identity || !content?.text || !ttsContextKey || consumed.current === identity) return;
    consumed.current = identity;
    if (document.visibilityState !== "visible" || !ttsEnabled()) {
      setFailed(identity);
      return;
    }
    const tag = `warmup:${sessionId}:${identity}`;
    activeTag.current = tag;
    setFailed(null);
    speak(content.text, { contextKey: ttsContextKey, tag });
    // The shared player bounds startup and playback separately. Do not count
    // slow synthesis/download against a second fixed speaking deadline here.
    return () => { activeTag.current = null; stopSpeaking(); };
  }, [ready, identity, content?.text, sessionId, ttsContextKey]);
  if (paused) return <Centered className="patient-rapport"><div className="target">我们先休息一下</div><p className="question">休息好了，工作人员会继续</p></Centered>;
  if (!connectionReady || error || !content) return <Centered className="patient-rapport"><p className="question">请稍等一下，马上就好。</p></Centered>;
  if (warmup?.beat === "done") return <Centered className="patient-rapport"><div className="target">谢谢您和我聊聊天</div><p className="question">准备好了，工作人员会继续</p></Centered>;
  const waiting = allowedIdentity !== identity || locallyStopped >= (warmup?.seq ?? 0);
  return <div className="patient-stage patient-rapport" style={{ background: "#F3ECD9" }}>
    <div className="patient-stage-body rapport-body">
      <div className="rapport-brand" aria-hidden="true">语</div>
      <p className="question" aria-live="polite" aria-atomic="true" style={{ maxWidth: "80vw" }}>{content.text}</p>
      {(waiting || failed === identity) && <p className="cue" role="status">请稍等，工作人员会继续</p>}
    </div>
  </div>;
}
