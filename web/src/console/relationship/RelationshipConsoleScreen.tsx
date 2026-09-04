import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api";
import { Alert } from "../../components/Alert";
import { Button } from "../../components/Button";
import { StatusPill } from "../../components/StatusPill";
import { useToast } from "../../components/ToastContext";
import { useWeek1ReplyBank, useWeek1Script } from "../../content/bundle";
import { useSessionJournal } from "../../hooks/useSessionJournal";
import { useSessionRuntime } from "../../hooks/useSessionRuntime";
import { useAudioSaved, useCursorWriter, usePatientRec, useSaveWatchdog } from "../../sync/useCursorWriter";
import { isPatientRecFailure, type RapportBeat, type RecState } from "../../sync/messages";
import {
  afterReplyAction, autoAdvanceTarget, nextQuestionArmDelayMs, roundLabel,
  shouldAutoArmOnEntry, speechDelayMs,
} from "./rapportRounds";
import type { Session } from "../../types";
import { SessionControlBar } from "../SessionControlBar";
import { SessionAbortControl } from "../SessionAbortControl";

function defaultRapportFlags(sectionKey: string): { assentGate: boolean; containsDirectIdentifier: boolean } {
  return {
    assentGate: sectionKey === "认识机器人",
    containsDirectIdentifier: sectionKey === "自我介绍",
  };
}

// week1 关系建立驱动屏:plan 无评分题,本就无判分 UI(与 scoring 零 import)。
// 逐节/逐问广播 rapportStep 推进老人端;录音同训练屏 arm/stop 模式(老人端 VOX 实采字节)。
// 自我介绍段 containsDirectIdentifier=true 随消息带到老人端登记(导出侧整段红线)。
// speaker="研究者" 的节是当面话术:老人端不朗读,此处只给研究者看提词。
export function RelationshipConsoleScreen({ session, onWrapup, onExit }: {
  session: Session;
  onWrapup: () => void;
  onExit?: () => void;
}) {
  const toast = useToast();
  const { script, error: scriptError } = useWeek1Script();
  const {
    postSession, postRapport, beginSafetyPause, releaseSafetyPause,
    resetSession, syncError, retrySync,
  } = useCursorWriter(session.session_id);
  const { journal, upsertAudio, hydrateFromServer } = useSessionJournal(session.session_id);
  const runtimeControl = useSessionRuntime(session.session_id);
  const paused = runtimeControl.paused;
  const [pausePending, setPausePending] = useState(false);
  const [wrapupPending, setWrapupPending] = useState(false);
  const [sectionIdx, setSectionIdx] = useState(0);
  const [qIdx, setQIdx] = useState(0);
  const [recoveredLabel, setRecoveredLabel] = useState<string | null>(null);
  const [journalRecoveryError, setJournalRecoveryError] = useState<string | null>(null);
  const [journalLoading, setJournalLoading] = useState(true);
  const [journalRetry, setJournalRetry] = useState(0);
  const [recoveryApplied, setRecoveryApplied] = useState(false);
  const [rapportFlags, setRapportFlags] = useState(() => defaultRapportFlags("认识机器人"));
  const recoveredOnce = useRef(false);
  const handshakeSent = useRef(false);
  const [recState, setRecState] = useState<RecState>("idle");
  // 一问两拍:ask=机器人问句,reply=老人答完后机器人说的那句。恢复一律回到 ask,
  // 老人重新听见的是问题本身,不是一句悬空的回应。
  const [beat, setBeat] = useState<RapportBeat>("ask");
  const [spokenReply, setSpokenReply] = useState<string | null>(null);
  // 当前回应拍指向的发声行。rapportStep 是整条覆盖写:任何一次省略 beat 的写
  // 都等于把这一拍打回 ask(老人端会把本问原问句重念一遍);而 beat=reply 却
  // 不带 utteranceId 的写会被服务端 422 拒(控制台以为关麦了,老人端还开着)。
  const replyBeatRef = useRef<{ beat: RapportBeat; utteranceId: number | null }>(
    { beat: "ask", utteranceId: null });
  const [replyMeta, setReplyMeta] = useState<string | null>(null);
  // 默认开:自动带练——问句念完自动开麦,老人说完系统自动回应(开放 AI 的节现编,
  // 身份问询的节照脚本),说完自动问下一问、问完自动进下一节。像第2-8周那样。
  const [autoReply, setAutoReply] = useState(true);
  const [replyPending, setReplyPending] = useState(false);
  // 机器人说完后的自动动作(开麦/续麦/换问/换节)用定时器等它说完;任何人为操作都取消。
  const [roundNote, setRoundNote] = useState<string | null>(null);
  const afterReplyTimer = useRef<number | null>(null);
  // 定时器回调来自旧渲染的闭包:动作函数与开关状态一律经 ref 取最新值。
  const latest = useRef({
    armNextRound: (_sk: string, _qi: number, _uid: number) => {},
    armRecording: () => {}, go: (_s: number, _q: number) => {},
    autoReply: true, autoOpenHere: false, questionCount: 0, sectionIdx: 0,
    interactionBlocked: false, askAt: (_q: number): string | null => null,
    sections: [] as { speaker: string | undefined; questionCount: number }[],
  });
  // 保持当前拍不变的那些写(老人自停回写 idle、研究者停止录音)必须原样带回
  // 当前拍与发声行,不能让默认值把话拍悄悄打回 ask。
  const currentBeatFields = () => (
    replyBeatRef.current.beat === "reply" && replyBeatRef.current.utteranceId !== null
      ? { beat: "reply" as const, utteranceId: replyBeatRef.current.utteranceId }
      : { beat: "ask" as const });
  const cancelAfterReply = () => {
    if (afterReplyTimer.current !== null) {
      window.clearTimeout(afterReplyTimer.current);
      afterReplyTimer.current = null;
    }
    setRoundNote(null);
  };
  const { bank: replyBank } = useWeek1ReplyBank();
  // 同一组连点两次不该说同一句;按组各记一个游标,轮着来。
  const replyCursor = useRef<Record<string, number>>({});
  const patientRec = usePatientRec(session.session_id);
  const patientDeviceFailure = isPatientRecFailure(patientRec)
    && patientRec.sessionId === session.session_id ? patientRec : null;
  // 与正式训练共用录音资格护栏：真实研究只接受明确允许；显式模拟场次才可用未评状态。
  const [recStatus, setRecStatus] = useState<"loading" | "error" | "denied" | "allowed">("loading");
  const recordingEligible = recStatus === "allowed";
  const [recRetry, setRecRetry] = useState(0);
  const section = script?.sections[sectionIdx];
  const isSelfIntro = section?.key === "自我介绍";
  const questions = section?.questions ?? [];
  const replyLine = section?.speaker === "机器人" ? (questions[qIdx]?.success ?? null) : null;
  const openReplyHere = Boolean(replyBank?.applies_to.some(
    (row) => row.section_key === section?.key && row.question_idx === qIdx));
  // AI 现编按节判定:录音只绑到节,节内含身份问询问位(自我介绍)整节不进云——
  // 那一节老人答完由机器人照冻结脚本回应(不转写、不出网),流程照样自动往下走。
  const autoOpenHere = Boolean(openReplyHere && questions.length > 0
    && questions.every((_q, i) => replyBank?.applies_to.some(
      (row) => row.section_key === section?.key && row.question_idx === i)));
  // 自动带练在每个机器人节都开得了:AI 现编(autoOpenHere)或脚本回应二选一。
  const autoModeHere = section?.speaker === "机器人" && questions.length > 0;
  const recSeq = useRef(0);
  // 自动回应回来时研究者可能已换问:回应只写回它出发时的那一问。
  const posRef = useRef("");
  posRef.current = `${section?.key ?? ""}#${qIdx}`;
  const recStateRef = useRef("idle");
  recStateRef.current = recState;
  // 三条回应入口共用的在途闩:state 异步,同帧竞态只有 ref 挡得住。
  const replyBusyRef = useRef(false);
  // 只有「这一问示意过录音」的收据才允许自动回应——同节迟到收据不冒名顶替。
  const lastArmedPosRef = useRef<string | null>(null);
  // ★必须在所有 early-return 之前:hook 写在条件 return 后面,脚本从"加载中"变"已加载"
  // 那一帧 hook 数量改变,React 抛错卸载整棵树——整页按钮全部失灵。
  const [proxyBusy, setProxyBusy] = useState(false);
  const watchdog = useSaveWatchdog(() =>
    toast("8 秒未收到老人端录音回报——可能没录上(麦克风权限/网络)。请检查老人端后重新示意录音。", "danger"));
  // 看门狗守"哪一节的回报":录音中换节/暂停守的是旧节,旧节回报到达即达成——
  // 若按"是否当前节"判清,保存成功也弹假警报。
  const watchdogFor = useRef<string | null>(null);
  const lastArmedKey = useRef<string | null>(null);
  const handledDeviceFailureId = useRef<string | null>(null);
  const armWatchdog = (k: string | null) => { watchdogFor.current = k; watchdog.start(); };

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    const check = (foreground: boolean) => {
      if (inFlight) return;
      inFlight = true;
      if (foreground) setRecStatus("loading");
      api.recordingAuthorization(session.session_id)
        .then((authorization) => {
          if (!cancelled) setRecStatus(authorization.allowed === true && authorization.runtime_status === "active" ? "allowed" : "denied");
        })
        .catch((error) => {
          if (!cancelled) setRecStatus(error instanceof ApiError && error.status >= 400 && error.status < 500 ? "denied" : "error");
        })
        .finally(() => { inFlight = false; });
    };
    check(true);
    const timer = window.setInterval(() => check(false), 5_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [session.session_id, recRetry]);

  const recoveryLoading = runtimeControl.loading || journalLoading;
  const recoveryError = runtimeControl.error ?? journalRecoveryError ?? syncError;
  const runtimeReady = runtimeControl.runtime?.sessionId === session.session_id;
  const recoveryPending = recoveryLoading || (!recoveryError && (!runtimeReady || !recoveryApplied));
  const terminal = runtimeControl.terminal;
  const interactionBlocked = terminal || paused || pausePending || wrapupPending || recoveryPending
    || Boolean(recoveryError) || Boolean(patientDeviceFailure);
  // 暂停/设备失败/终态/收尾一旦生效,待定的续麦或换问必须立刻作废:定时器回调
  // 持有的是旧闭包,里面的 interactionBlocked 永远是排定那一帧的值。
  useEffect(() => {
    if (interactionBlocked && afterReplyTimer.current !== null) {
      window.clearTimeout(afterReplyTimer.current);
      afterReplyTimer.current = null;
      setRoundNote(null);
    }
  }, [interactionBlocked]);
  const retryRecovery = () => {
    setJournalRetry((n) => n + 1);
    void runtimeControl.refresh();
    retrySync();
  };

  useEffect(() => {
    resetSession();
    recoveredOnce.current = false;
    handshakeSent.current = false;
    setRecoveryApplied(false);
    setRecoveredLabel(null);
    setSectionIdx(0);
    setQIdx(0);
    setBeat("ask");
    setRapportFlags(defaultRapportFlags("认识机器人"));
  }, [resetSession, session.session_id]);

  useEffect(() => {
    let cancelled = false;
    setJournalLoading(true);
    setJournalRecoveryError(null);
    api.sessionJournal(session.session_id)
      .then((remote) => {
        if (cancelled) return;
        if (remote.session.session_id !== session.session_id) {
          throw new Error("服务器返回了其他场次的记录，已拒绝恢复");
        }
        hydrateFromServer(remote);
        setJournalLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setJournalRecoveryError(e instanceof ApiError ? e.detail : String(e));
        setJournalLoading(false);
      });
    return () => { cancelled = true; };
  }, [hydrateFromServer, journalRetry, session.session_id]);

  useEffect(() => {
    if (!script || !runtimeReady || recoveryLoading || recoveryError || recoveredOnce.current) return;
    const saved = runtimeControl.runtime?.rapportStep;
    const nextSection = saved ? script.sections.findIndex((candidate) => candidate.key === saved.sectionKey) : 0;
    const safeSection = nextSection >= 0 ? nextSection : 0;
    const maxQuestion = Math.max(0, (script.sections[safeSection]?.questions?.length ?? 1) - 1);
    const safeQuestion = Math.min(Math.max(0, saved?.questionIdx ?? 0), maxQuestion);
    setSectionIdx(safeSection);
    setQIdx(safeQuestion);
    setBeat("ask");
    const restoredFlags = defaultRapportFlags(script.sections[safeSection]?.key ?? "");
    setRapportFlags({
      assentGate: saved?.assentGate ?? restoredFlags.assentGate,
      containsDirectIdentifier: saved?.containsDirectIdentifier ?? restoredFlags.containsDirectIdentifier,
    });
    if (safeSection > 0 || safeQuestion > 0 || runtimeControl.runtime?.status === "paused") {
      setRecoveredLabel(`「${script.sections[safeSection]?.key ?? "关系建立"}」第 ${safeQuestion + 1} 问`);
    }
    recoveredOnce.current = true;
    setRecoveryApplied(true);
  }, [recoveryError, recoveryLoading, runtimeControl.runtime, runtimeReady, script]);

  useEffect(() => {
    if (!script || interactionBlocked || handshakeSent.current) return;
    postSession({ sessionId: session.session_id, weekNo: session.week_no, eventLine: session.event_line, mode: "rapport", itemBankVersionId: session.item_bank_version_id });
    postRapport({
      sectionKey: script.sections[sectionIdx]?.key ?? "",
      questionIdx: qIdx,
      recording: "idle",
      recSeq: recSeq.current,
      ...rapportFlags,
    });
    handshakeSent.current = true;
  }, [interactionBlocked, postRapport, postSession, qIdx, rapportFlags, script, sectionIdx, session]);

  useEffect(() => {
    if (!script || interactionBlocked || recordingEligible) return;
    if (recState !== "idle") armWatchdog(lastArmedKey.current);
    setRecState("idle");
    postRapport({
      sectionKey: section?.key ?? "",
      questionIdx: qIdx,
      ...currentBeatFields(),
      recording: "idle",
      recSeq: recSeq.current,
      ...rapportFlags,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recStatus, interactionBlocked]);

  useEffect(() => {
    if (!terminal) return;
    watchdog.clear();
    watchdogFor.current = null;
    setRecState("idle");
    // 服务器已把终态投影为 idle/done，不再向只读场次补发旧 rapport。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [terminal]);

  // 权威设备失败已经由服务器原子暂停 runtime；关系建立无判分链，本机只清录音
  // 按钮/看门狗并冻结导航，不补写任何临床事件或推进指令。
  useEffect(() => {
    if (!patientDeviceFailure || handledDeviceFailureId.current === patientDeviceFailure.failureId) return;
    handledDeviceFailureId.current = patientDeviceFailure.failureId;
    watchdog.clear();
    watchdogFor.current = null;
    setRecState("idle");
    toast("设备失败，场次已安全暂停。当前对话位置保持不变。", "danger");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [patientDeviceFailure?.failureId]);

  // 卸载/换场次无条件收回 rapport 录音指令；即使本地 state 已不同步，也不留下 armed 热麦。
  const withdrawRef = useRef<() => void>(() => {});
  withdrawRef.current = () => {
    watchdog.clear();
    postRapport({
      sectionKey: section?.key ?? "",
      questionIdx: qIdx,
      ...currentBeatFields(),
      recording: "idle",
      recSeq: recSeq.current,
      ...rapportFlags,
    });
  };
  useEffect(() => () => withdrawRef.current(), []);
  // 定时器必须随组件一起消失:收尾/退出后旧实例的回调仍持有旧闭包,
  // 能把老人端麦克风打开而新实例屏上显示未录音(复核 P1 热麦路径)。
  useEffect(() => () => {
    if (afterReplyTimer.current !== null) window.clearTimeout(afterReplyTimer.current);
    afterReplyTimer.current = null;
  }, []);
  useEffect(() => { if (recoveryError) withdrawRef.current(); }, [recoveryError]);

  const replySourceLabel = (source: string, reason: string | null): string => {
    const base = source === "llm" ? "AI 现编"
      : source === "bank" ? "冻结句库"
        : source === "script" ? "冻结脚本" : "句库兜底";
    const why = reason === "asr_failed" ? "，没听清录音"
      : reason === "asr_empty" ? "，老人没说话"
        : reason === "llm_unavailable" ? "，AI 暂不可用"
          : reason === "cloud_not_authorized" ? "，受试者未授权云处理"
        : reason === "round_limit" ? "，本问已聊满，这段没有转写" : "";
    return `${base}${why}`;
  };
  const applyReply = async (
    u: { utteranceId: number; text: string; source: string; degradedReason: string | null;
         round?: number; maxRounds?: number; final?: boolean; invitesMore?: boolean },
    sk: string, qi: number, viaAuto = false,
  ) => {
    if (recStateRef.current !== "idle") return; // 已重新示意录音:不许回应句顶掉 armed
    cancelAfterReply();
    const beatBefore = replyBeatRef.current;
    setBeat("reply");
    replyBeatRef.current = { beat: "reply", utteranceId: u.utteranceId };
    setSpokenReply(u.text);
    setReplyMeta(replySourceLabel(u.source, u.degradedReason));
    const accepted = await postRapport({ sectionKey: sk, questionIdx: qi, beat: "reply", utteranceId: u.utteranceId, recording: "idle", recSeq: recSeq.current, ...rapportFlags });
    if (!accepted) {
      // 落库被拒=机器人没开口;「刚说了」的断言必须收回,不许屏上说谎。
      setSpokenReply(null);
      setReplyMeta(null);
      // 写被拒 = 机器人没开口:话拍指针一起回滚,否则后续任一"保持当前拍"的写
      // 会把这句已撤回的话补送给老人,而控制台上没有任何记录。
      setBeat(beatBefore.beat);
      replyBeatRef.current = beatBefore;
      toast("回应句没有送到老人端——检查场次状态后可点分组重试", "warn");
      return;
    }
    // 自动推进只接在自动回应之后:手点句库/手点脚本回应仍由研究者掌控节奏。
    if (!viaAuto || u.round === undefined || u.maxRounds === undefined
      || u.final === undefined) return;
    const now = latest.current;
    if (!now.autoReply) return;
    if (u.degradedReason === "round_limit") {
      // 研究者主动回到已聊满的问位(想让老人补一句):这一段录音不会被转写,
      // 机器人只说了句无关的收束话。不能当成"聊完了"自动把他推回下一问——
      // 那等于把他的操作撤销,而且屏上看不出这段被丢弃了。
      setRoundNote(`这一问已经聊满 ${u.maxRounds} 轮：刚才这段没有转写，`
        + "机器人只说了句收束话。要继续请点「下一问」。");
      return;
    }
    const action = afterReplyAction({
      autoMode: now.autoReply, final: u.final,
      invitesMore: u.invitesMore === true, qIdx: qi, questionCount: now.questionCount,
    });
    const posAt = posRef.current;
    // 脚本回应(不进云的问位)没有轮次可言,屏上不标「第 1 / 2 轮」误导研究者。
    const label = u.source === "script" ? "照脚本回应" : roundLabel(u.round, u.maxRounds);
    // 定时器回调持有的是排定那一帧的闭包:动作与闸门一律经 latest/ref 取最新值,
    // 并且必须自己再查一遍在途/阻断状态——否则暂停、收尾、卸载后它照样开麦。
    const stillOurs = () => (
      posRef.current === posAt
      && recStateRef.current === "idle"
      && !latest.current.interactionBlocked
      && !replyBusyRef.current
      && latest.current.autoReply
    );
    if (action === "rearm") {
      setRoundNote(`${label} · 小语说完会自动开麦，让老人接着说`);
      afterReplyTimer.current = window.setTimeout(() => {
        afterReplyTimer.current = null;
        if (!stillOurs()) return;
        setRoundNote(null);
        // 留在回应拍上开麦:老人屏继续显示小语刚说的那句。若打回 ask 拍,
        // 老人端会把本问的原问句再念一遍(还可能掐断正在播的追问)。
        latest.current.armNextRound(sk, qi, u.utteranceId);
      }, speechDelayMs(u.text));
    } else if (action === "advance") {
      setRoundNote(`${label} · 本问聊完，小语说完会自动问下一问`);
      afterReplyTimer.current = window.setTimeout(() => {
        afterReplyTimer.current = null;
        if (!stillOurs()) return;
        setRoundNote(null);
        // 换问后的自动开麦由 go 统一安排:前几轮已经把老人训练成「小语说完就该
        // 我说」,这里不开麦=老人对着关着的麦回答(钱凯最初报的那个现象)。
        latest.current.go(latest.current.sectionIdx, qi + 1);
      }, speechDelayMs(u.text));
    } else if (action === "section_done") {
      const target = autoAdvanceTarget(now.sections, now.sectionIdx);
      if (target === null) {
        setRoundNote(`${label} · 本节问完了，下一节要研究者当面说，请点「下一节」`);
        return;
      }
      setRoundNote(`${label} · 本节问完，小语说完会自动进下一节`);
      afterReplyTimer.current = window.setTimeout(() => {
        afterReplyTimer.current = null;
        if (!stillOurs()) return;
        setRoundNote(null);
        latest.current.go(target, 0);
      }, speechDelayMs(u.text));
    }
  };
  // 老人端录音落库回报 → 记入作业日志(收尾屏音频闸门据此列出)
  useAudioSaved((m) => {
    if (m.sessionId !== session.session_id) return; // 跨场次残留/迟到回报一律丢弃
    const expectedTurnKey = `关系建立·${section?.key ?? ""}`;
    const isReplay = Boolean(journal.audios[m.rawAudioId]);
    upsertAudio(m.rawAudioId, {
      turnKey: m.turnKey,
      containsDirectIdentifier: m.containsDirectIdentifier ?? false,
      isReliabilitySample: false,
      lastStatus: "recorded",
      durationSeconds: m.durationSeconds,
    });
    // 看门狗按"守的节"清:录音中换节/暂停守的是旧节,旧节的成功回报必须能清掉它。
    if (!isReplay && m.turnKey === watchdogFor.current) {
      watchdog.clear();
      watchdogFor.current = null;
    }
    // 上一问迟到或刷新重放的回报只记账；不收当前麦克风。
    if (m.turnKey !== expectedTurnKey || isReplay) return;
    if (recState !== "idle" && !interactionBlocked) {
      // 老人自己按"我说好了"停的:把 idle 写回镜像/服务端真值源,否则 armed 残留会让老人端刷新后自动开麦。
      postRapport({
        sectionKey: section?.key ?? "", questionIdx: qIdx,
        ...currentBeatFields(),
        recording: "idle", recSeq: recSeq.current, ...rapportFlags,
      });
    }
    setRecState("idle");
    toast(`录音已保存（${m.durationSeconds.toFixed(1)} 秒）${m.containsDirectIdentifier ? "。这段录音含直接身份信息，导出时会被拦下" : ""}`, "ok");
    // 自动带练:录音一落账,机器人自己接话。开放 AI 的节走「云 ASR → 现编」;
    // 含姓名/年龄等身份问询的节一个字节都不进云,照冻结脚本回应,流程照样往下走。
    // 失败不打断流程,研究者还能手点分组/脚本回应。
    if (autoReply && !interactionBlocked && !replyBusyRef.current
      && lastArmedPosRef.current === posRef.current
      && ((autoOpenHere && replyBank) || (!autoOpenHere && replyLine))) {
      const sk = section?.key ?? "";
      const qi = qIdx;
      const posAt = posRef.current;
      replyBusyRef.current = true;
      setReplyPending(true);
      const request = autoOpenHere
        ? { sectionKey: sk, questionIdx: qi, mode: "auto" as const, rawAudioId: m.rawAudioId }
        : { sectionKey: sk, questionIdx: qi, mode: "script" as const };
      api.rapportReplyCreate(session.session_id, request).then((u) => {
        if (posRef.current !== posAt) return;
        void applyReply(u, sk, qi, true);
      }).catch(() => {
        toast(autoOpenHere
          ? "自动回应没有生成——可以点下面的分组，让机器人照句库说一句"
          : "自动回应没有生成——可以点「让机器人回应」再试一次", "warn");
      }).finally(() => { replyBusyRef.current = false; setReplyPending(false); });
    }
  });

  if (scriptError) return <Alert tone="danger" title="第 1 周脚本校验失败">系统已安全暂停，不能使用未经校验的话术开场：{scriptError}</Alert>;
  if (!script) return <p>加载第 1 周脚本…</p>;

  // 统一推进:换节/换问都先停录(老人端收到 idle 自动收尾保存),再广播新指针。
  const go = (sIdx: number, q: number) => {
    if (interactionBlocked) return;
    cancelAfterReply();
    const s = script.sections[sIdx];
    const nextFlags = defaultRapportFlags(s?.key ?? "");
    if (recState !== "idle") armWatchdog(lastArmedKey.current);
    setRecState("idle");
    setSectionIdx(sIdx);
    setQIdx(q);
    setBeat("ask");
    replyBeatRef.current = { beat: "ask", utteranceId: null };
    setSpokenReply(null);
    setReplyMeta(null);
    setRapportFlags(nextFlags);
    postRapport({ sectionKey: s?.key ?? "", questionIdx: q, beat: "ask", recording: "idle", recSeq: recSeq.current, ...nextFlags });
    // 自动带练:进到一问就像第2-8周那样,问句念完自动开麦,不用研究者再点录音。
    // 研究者手点上一问/下一问/换节也走这里——他换到哪一问,老人就在哪一问作答。
    if (!shouldAutoArmOnEntry({
      autoMode: autoReply, speaker: s?.speaker, questionCount: s?.questions?.length ?? 0, qIdx: q,
    })) return;
    const pos = `${s?.key ?? ""}#${q}`;
    const ask = s?.questions?.[q]?.ask ?? null;
    setRoundNote("小语问完会自动开麦，让老人回答");
    afterReplyTimer.current = window.setTimeout(() => {
      afterReplyTimer.current = null;
      if (posRef.current !== pos || recStateRef.current !== "idle"
        || latest.current.interactionBlocked || !latest.current.autoReply
        || replyBusyRef.current) return;
      setRoundNote(null);
      latest.current.armRecording();
    }, nextQuestionArmDelayMs(ask));
  };

  const armRecording = () => {
    if (interactionBlocked) return;
    if (!recordingEligible) {
      toast(recStatus === "denied" ? "服务器当前未授权本场录音" : "录音授权尚未确认，系统保持关麦", "warn");
      return;
    }
    if (replyPending || replyBusyRef.current) return; // 回应在途不开麦:防 idle 写顶掉 armed
    cancelAfterReply();
    lastArmedPosRef.current = posRef.current;
    recSeq.current += 1; // 每次 arm 新序号:老人自停后 armed→armed 重发才能重触发老人端
    lastArmedKey.current = `关系建立·${section?.key ?? ""}`;
    setRecState("armed");
    setBeat("ask");
    replyBeatRef.current = { beat: "ask", utteranceId: null };
    setSpokenReply(null);
    setReplyMeta(null);
    postRapport({ sectionKey: section?.key ?? "", questionIdx: qIdx, beat: "ask", recording: "armed", recSeq: recSeq.current, ...rapportFlags });
  };
  // 多轮续麦:不改话拍、不动屏上那句回应,只把麦克风重新打开。
  // 走 armRecording 会 setBeat("ask") → 老人端 text 从回应句换回问句 → 朗读
  // effect 重跑,把本问原问句再念一遍并掐断正在播的追问(复核 P1)。
  const armNextRound = (sk: string, qi: number, utteranceId: number) => {
    // 放弃续麦必须留痕:老人面前是一句开放式追问,麦克风却没开——研究者
    // 看不见就会重现「老人说完没有回应」。
    if (interactionBlocked || replyPending || replyBusyRef.current) {
      setRoundNote("这一轮没有自动开麦（场次状态或回应在途），需要时请手动示意录音");
      return;
    }
    if (!recordingEligible) {
      toast(recStatus === "denied"
        ? "服务器当前未授权本场录音，自动开麦已取消"
        : "录音授权尚未确认，自动开麦已取消——请手动示意录音", "warn");
      setRoundNote("这一轮没有自动开麦，请手动示意录音");
      return;
    }
    cancelAfterReply();
    lastArmedPosRef.current = `${sk}#${qi}`;
    recSeq.current += 1;
    lastArmedKey.current = `关系建立·${sk}`;
    setRecState("armed");
    // 与 applyReply 同口径:写被拒 = 麦克风没开。有一条完全静默的拒绝路径
    // (409「已暂停」不弹 toast),不看返回值就会出现"控制台显示在录、老人端
    // 麦克风从未打开"——正是这个功能要消灭的那种沉默。
    void postRapport({
      sectionKey: sk, questionIdx: qi, beat: "reply", utteranceId,
      recording: "armed", recSeq: recSeq.current, ...rapportFlags,
    }).then((accepted) => {
      if (accepted) return;
      setRecState("idle");
      setRoundNote("自动开麦没有被服务器接受——请检查场次状态后手动示意录音");
    });
  };
  // 老人答完 → 机器人把冻结脚本里写好的那句回应读出来。只在关麦时可用,发出去的
  // recording 与当前状态一致,所以这一步不碰录音链,只换屏上那句话和朗读内容。
  const sayReply = () => {
    if (interactionBlocked || !replyLine || recState !== "idle" || replyPending
      || replyBusyRef.current) return;
    const sk = section?.key ?? "";
    const qi = qIdx;
    const posAt = posRef.current;
    replyBusyRef.current = true;
    setReplyPending(true);
    api.rapportReplyCreate(session.session_id, { sectionKey: sk, questionIdx: qi, mode: "script" })
      .then((u) => { if (posRef.current === posAt) void applyReply(u, sk, qi); })
      .catch(() => toast("回应句没有生成，请再试一次", "danger"))
      .finally(() => { replyBusyRef.current = false; setReplyPending(false); });
  };
  // 研究者点的是「刚才是哪种情况」,系统在那一组里轮一句。他刚听完老人说什么,
  // 这个判断比任何自动分类都准——第1周不判分、没有 attempt,录音本来就不转写,
  // 系统这一侧没有老人说了什么的文本。
  const sayGroupReply = (group: string) => {
    if (interactionBlocked || recState !== "idle" || !replyBank || replyPending
      || replyBusyRef.current) return;
    const pool = replyBank.replies.filter((r) => r.group === group);
    if (!pool.length) return;
    const n = (replyCursor.current[group] ?? -1) + 1;
    replyCursor.current[group] = n;
    const chosen = pool[n % pool.length];
    const sk = section?.key ?? "";
    const qi = qIdx;
    const posAt = posRef.current;
    replyBusyRef.current = true;
    setReplyPending(true);
    api.rapportReplyCreate(session.session_id, {
      sectionKey: sk, questionIdx: qi, mode: "bank", replyId: chosen.id,
    }).then((u) => { if (posRef.current === posAt) void applyReply(u, sk, qi); })
      .catch(() => toast("回应句没有生成，请再试一次", "danger"))
      .finally(() => { replyBusyRef.current = false; setReplyPending(false); });
  };
  latest.current = {
    armNextRound, armRecording, go, autoReply, autoOpenHere,
    questionCount: questions.length, sectionIdx, interactionBlocked,
    askAt: (q: number) => questions[q]?.ask ?? null,
    sections: script.sections.map((row) => ({
      speaker: row.speaker, questionCount: row.questions?.length ?? 0,
    })),
  };
  const stopRecording = () => {
    cancelAfterReply();
    setRecState("idle");
    armWatchdog(lastArmedKey.current ?? `关系建立·${section?.key ?? ""}`);
    postRapport({
      sectionKey: section?.key ?? "", questionIdx: qIdx,
      ...currentBeatFields(),
      recording: "idle", recSeq: recSeq.current, ...rapportFlags,
    });
  };

  async function recordProxyNaming() {
    // 关系建立周"代说物品名"属允许(无警告),仅记介入。在途锁防双击写重复记录。
    if (proxyBusy || interactionBlocked) return;
    setProxyBusy(true);
    try {
      await api.recordAbnormal(session.session_id, { intervention_type: "代说物品名", note: "关系建立周·允许" });
      toast("已记录介入:代说物品名(关系建立周允许)", "ok");
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
    finally { setProxyBusy(false); }
  }

  async function pauseRapport() {
    cancelAfterReply();
    if (pausePending || paused) return;
    beginSafetyPause();
    setPausePending(true);
    if (recState !== "idle") armWatchdog(lastArmedKey.current);
    setRecState("idle");
    try {
      const result = await runtimeControl.pause();
      if (result?.ok) toast("关系建立已暂停，患者端麦克风保持关闭", "ok");
      else if (result) toast(result.message, "danger");
    } finally {
      setPausePending(false);
    }
  }

  async function resumeRapport() {
    const result = await runtimeControl.resume();
    if (result?.ok) {
      releaseSafetyPause();
      toast("已恢复到暂停前的对话位置", "ok");
    } else if (result) {
      toast(result.message, "danger");
    }
  }

  async function enterWrapup() {
    cancelAfterReply();
    if (wrapupPending || interactionBlocked || !section) return;
    setWrapupPending(true);
    if (recState !== "idle") armWatchdog(lastArmedKey.current ?? `关系建立·${section.key}`);
    const accepted = await postRapport({
      sectionKey: section.key,
      questionIdx: qIdx,
      ...currentBeatFields(),
      recording: "idle",
      recSeq: recSeq.current,
      ...rapportFlags,
    });
    if (!accepted) {
      toast("服务器尚未确认收麦，本场仍停留在关系建立页，请重试同步。", "danger");
      setWrapupPending(false);
      return;
    }
    setRecState("idle");
    onWrapup();
  }

  return (
    <div className="page-shell page-shell--medium">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">步骤 3 / 4 · 第 1 周</p>
          <h2 className="page-title">关系建立</h2>
          <p className="page-description">按固定脚本与受试者进行轻松对话。本流程不判分，研究者只控制话术推进与合规录音。</p>
        </div>
        {/* 收尾前必先停录:否则本屏卸载后无人发 idle,老人端麦克风持续开着 */}
        <Button disabled={interactionBlocked} onClick={() => { void enterWrapup(); }}>
          {wrapupPending ? "正在确认收麦…" : "进入场次收尾"}
        </Button>
      </header>

      <SessionControlBar paused={paused} loading={recoveryPending} busy={runtimeControl.busy || pausePending || wrapupPending}
        terminalStatus={runtimeControl.terminalStatus} onExit={onExit}
        recoveredLabel={recoveredLabel} error={recoveryError} onRetry={retryRecovery}
        onPause={() => void pauseRapport()} onResume={() => void resumeRapport()} />

      <div className="form-actions" style={{ justifyContent: "flex-end" }}>
        <SessionAbortControl sessionId={session.session_id}
          expectedRevision={runtimeControl.runtime?.revision ?? null}
          disabled={recoveryPending || runtimeControl.busy || pausePending || wrapupPending || terminal}
          onAborted={() => onWrapup()} />
      </div>

      <section className={`form-section rapport-console-card${interactionBlocked ? " session-paused-surface" : ""}`}>
        <div className="form-section-header">
          <div>
            <p className="page-kicker">当前对话段落</p>
            <h3>{section?.key}</h3>
          </div>
          <div className="row wrap">
            <StatusPill tone="primary">第 {sectionIdx + 1} / {script.sections.length} 节</StatusPill>
            <StatusPill tone={section?.speaker === "机器人" ? "ok" : "muted"}>{section?.speaker === "机器人" ? "受试者端朗读" : "研究者当面说"}</StatusPill>
          </div>
        </div>
        {section?.note && <p className="muted">{section.note}</p>}
        {section?.line && <div className="rapport-script-line">{section.line}</div>}
        {questions.length > 0 && (
          <>
            <ol className="rapport-question-list">
              {questions.map((q, i) => (
                <li key={i} className={i === qIdx ? "is-active" : ""}>
                  <strong>{q.ask}</strong>
                  {q.success
                    ? <span>受试者答完后机器人说：{q.success}</span>
                    : <span className="muted">这一问脚本里没有写机器人的回应句</span>}
                </li>
              ))}
            </ol>
            <div className="toolbar rapport-question-actions">
              <div className="row">
                <Button disabled={interactionBlocked || replyPending || qIdx === 0} onClick={() => go(sectionIdx, qIdx - 1)}>上一问</Button>
                {!openReplyHere && (
                  <Button variant="primary" disabled={interactionBlocked || !replyLine || recState !== "idle" || replyPending}
                    title={replyLine
                      ? (recState === "idle" ? undefined : "先停止录音，再让机器人回应")
                      : "冻结脚本没有为这一问写回应句，要先由内容组补上"}
                    onClick={sayReply}>
                    {beat === "reply" ? "再说一次回应" : "让机器人回应"}
                  </Button>
                )}
                <Button disabled={interactionBlocked || replyPending || qIdx >= questions.length - 1} onClick={() => go(sectionIdx, qIdx + 1)}>下一问</Button>
              </div>
              <span className="muted">第 {qIdx + 1} / {questions.length} 问</span>
            </div>
            {autoModeHere && (
              <label className="row rapport-auto-toggle">
                <input type="checkbox" checked={autoReply}
                  onChange={(e) => { setAutoReply(e.target.checked); if (!e.target.checked) cancelAfterReply(); }} />
                <span>{autoOpenHere
                  ? "自动带练：问完自动开麦，老人说完 AI 听完现编接着聊，每问最多聊几轮后自动问下一问；听不清或 AI 不可用时改用句库"
                  : "自动带练：问完自动开麦，老人说完机器人照脚本回应后自动问下一问（本节含姓名、年龄等身份信息，回答不进云、不转写）"}</span>
              </label>
            )}
            {autoModeHere && !autoReply && (
              <p className="muted">已关自动带练：每一问要手点「开始受试者端录音」，老人说完再点回应。</p>
            )}
            {openReplyHere && replyBank && (
              <div className="rapport-reply-picker">
                <p className="muted">{autoOpenHere
                  ? "也可以手点：刚才是哪种情况？点一下，机器人就照句库说一句。"
                  : "这一问也可以手点句库：刚才是哪种情况？点一下，机器人就照句库说一句。"}</p>
                <div className="row wrap">
                  {replyBank.groups.map((g) => (
                    <Button key={g.key} variant={g.key === "接着聊" ? "primary" : undefined}
                      disabled={interactionBlocked || recState !== "idle" || replyPending}
                      title={recState === "idle" ? g.hint : "先停止录音，再让机器人回应"}
                      onClick={() => sayGroupReply(g.key)}>{g.key}</Button>
                  ))}
                </div>
                {replyPending && <p className="muted" role="status">小语正在想怎么回应…</p>}
                {roundNote && <p className="muted" role="status" aria-live="polite">{roundNote}</p>}
              </div>
            )}
            {spokenReply && (
              <p className="muted" role="status" aria-live="polite">
                机器人刚说了：{spokenReply}{replyMeta ? `（${replyMeta}）` : ""}
              </p>
            )}
          </>
        )}
        {isSelfIntro && <Alert tone="warn" title="本段录音可能包含直接身份信息">如启动录音，整段会被标记为含直接标识，并在导出时进入受控处理。</Alert>}

        {patientDeviceFailure && (
          <Alert tone="danger" title="设备失败，场次已安全暂停">
            老人端麦克风未能安全启动。系统已关闭录音入口并保持当前对话位置；
            本次技术失败不会生成临床错误记录，也不会推进到下一问。
          </Alert>
        )}

        {recStatus === "denied" && (
          <Alert tone="danger" title="本场录音已禁用">服务器当前未授权录音（可能是同意、撤回、准入或场次状态变化）。麦克风已收回。</Alert>
        )}
        {recStatus === "loading" && <StatusPill tone="muted">正在核对录音资格…</StatusPill>}
        {recStatus === "error" && (
          <Alert tone="danger" title="无法确认录音资格" actions={<Button onClick={() => setRecRetry((n) => n + 1)}>重新核对</Button>}>系统已保持麦克风关闭。</Alert>
        )}

        <div className="recording-panel">
          {patientDeviceFailure && <StatusPill tone="danger">设备失败 · 录音入口已关闭</StatusPill>}
          {paused && <StatusPill tone="warn">场次暂停中，患者端麦克风保持关闭</StatusPill>}
          {recoveryPending && <StatusPill tone="muted">正在恢复已保存记录</StatusPill>}
          {recoveryError && <StatusPill tone="danger">等待重新同步场次</StatusPill>}
          {!interactionBlocked && recordingEligible && (recState === "idle"
            ? <Button variant="primary" onClick={armRecording}>开始受试者端录音{isSelfIntro ? "（含标识）" : ""}</Button>
            : <Button variant="danger" onClick={stopRecording}>停止受试者端录音</Button>)}
          {recState !== "idle" && <StatusPill tone="danger">正在等待受试者端保存</StatusPill>}
          <Button onClick={recordProxyNaming} disabled={interactionBlocked || proxyBusy}>{proxyBusy ? "正在记录…" : "记录研究者代说物品名"}</Button>
        </div>
      </section>

      <div className="form-actions rapport-section-actions">
        <Button disabled={interactionBlocked || sectionIdx === 0} onClick={() => go(sectionIdx - 1, 0)}>上一节</Button>
        <Button variant="primary" disabled={interactionBlocked || sectionIdx >= script.sections.length - 1}
          onClick={() => go(sectionIdx + 1, 0)}>下一节</Button>
      </div>
    </div>
  );
}
