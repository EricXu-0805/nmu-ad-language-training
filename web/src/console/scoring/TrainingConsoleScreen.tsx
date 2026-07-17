import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { EnumSelect, Field, TextInput } from "../../components/Field";
import { StatusPill } from "../../components/StatusPill";
import { useToast } from "../../components/ToastContext";
import { assertVersionsMatch, findDouble, findSingle, lookupCue, resolveFeedbackLine, useAutopilotProtocol, useItemBankBundle, type FbKey } from "../../content/bundle";
import { isBoundJournalTurnKey, useSessionJournal } from "../../hooks/useSessionJournal";
import { useSessionRuntime } from "../../hooks/useSessionRuntime";
import { flushLiveWrites, useAudioSaved, useCursorWriter, usePatientRec, useSaveWatchdog } from "../../sync/useCursorWriter";
import type { PlanItem, PlanTurn, Session, SessionPlan } from "../../types";
import { AuthenticatedAudio } from "../AuthenticatedAudio";
import { SessionControlBar } from "../SessionControlBar";
import { assertPortraitFree, isRelationRole } from "./vm";

// week≥2 判分主屏:左题目游标列 + 右环节工作卡(转写→确认→AI初评→锁分)。
// 唯一游标写者,推进老人端;★本文件及本目录禁 import 任何画像源(oxlint 守卫 + vm 运行时断言)。
export function TrainingConsoleScreen({ session, onWrapup, onExit, onItemEventChange }: {
  session: Session; onWrapup: () => void; onExit?: () => void; onItemEventChange?: (id: number | null) => void;
}) {
  const toast = useToast();
  const { bundle } = useItemBankBundle();
  const { journal, upsertItem, upsertTurn, upsertAudio, recordCueLevel, setCursor, hydrateFromServer } = useSessionJournal(session.session_id);
  const { postSession, postCursor, resetSession, syncError, retrySync } = useCursorWriter(session.session_id);
  const runtimeControl = useSessionRuntime(session.session_id);
  const paused = runtimeControl.paused;
  const [pausePending, setPausePending] = useState(false);
  const [recoveredLabel, setRecoveredLabel] = useState<string | null>(null);
  const recoveredOnce = useRef(false);
  const restoreTarget = useRef<{ itemIdx: number; turnIdx: number } | null>(null);
  const lastAppliedTurnK = useRef<string | null>(null);
  const handshakeSent = useRef(false);

  const [plan, setPlan] = useState<SessionPlan | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [retryNonce, setRetryNonce] = useState(0);
  const [journalLoading, setJournalLoading] = useState(true);
  const [journalRecoveryError, setJournalRecoveryError] = useState<string | null>(null);
  const [recoveryApplied, setRecoveryApplied] = useState(false);
  const [itemIdx, setItemIdx] = useState(journal.cursor.itemIdx);
  const [turnIdx, setTurnIdx] = useState(journal.cursor.turnIdx);
  const [aiEnabled, setAiEnabled] = useState(true);
  // 收到老人端录音回报即自动推进下一环节——老人"点开始→说→我说好了"就能自走全场。
  // 关掉则回到研究者逐环节手动推进(需要当场转写/锁分时用)。
  const [autoAdvance, setAutoAdvance] = useState(() => {
    try { return localStorage.getItem("nmu:console:autoAdvance") !== "0"; } catch { return true; }
  });
  // 自动驾驶:AI 听→判→固定话术反馈→提示升级→推进;人工随时可关(接管)。默认关。
  const [autoPilot, setAutoPilot] = useState(() => {
    try { return localStorage.getItem("nmu:console:autopilot") === "1"; } catch { return false; }
  });
  const toggleAutoPilot = () => setAutoPilot((v) => {
    const n = !v;
    try { localStorage.setItem("nmu:console:autopilot", n ? "1" : "0"); } catch { /* 私隐模式等 */ }
    return n;
  });
  const autoPilotRef = useRef(autoPilot);
  autoPilotRef.current = autoPilot;
  const { protocol } = useAutopilotProtocol();
  const apTimers = useRef<{ arm?: ReturnType<typeof setTimeout>; answer?: ReturnType<typeof setTimeout>; act?: ReturnType<typeof setTimeout> }>({});
  const apCue1Path = useRef<"unknown" | "close" | "silence" | null>(null); // 第1次提示的进入路径(成功反馈按来路选变体,文档口径)
  const apBusy = useRef(false);
  const apLastRound = useRef<{ rawAudioId: string; duration: number; asrText: string | null } | null>(null);
  // 本轮是否仍在等回答:arm 后置真,收到本轮 audioSaved 或答窗到点即置假。
  // ★没有 VAD——"麦克风开了"(patientMicOn)不等于"老人在说话",故绝不用它当作答信号;
  // 用它做去重闸:自动收麦后迟到的旧录音回报(apAwaitingAnswer 已假)不再驱动状态机。
  const apAwaitingAnswer = useRef(false);
  const fbSeqCounter = useRef(0);
  const toggleAutoAdvance = () => setAutoAdvance((v) => {
    const n = !v;
    try { localStorage.setItem("nmu:console:autoAdvance", n ? "1" : "0"); } catch { /* 私隐模式等 */ }
    return n;
  });
  const [reviewerId, setReviewerId] = useState("R1");
  // 录音资格 fail-closed:确认拿到患者档案前不允许 arm(获取失败若默认放行,
  // recording_allowed=false 的受试者会被开麦——合规护栏静默失效)。
  const [recStatus, setRecStatus] = useState<"loading" | "error" | "denied" | "allowed" | "unrated">("loading");
  // 下发老人端的自助开录资格:与"示意录音"同一判定(unrated 按允许);loading/error/denied 一律 false。
  const selfStart = recStatus === "allowed" || recStatus === "unrated";
  const [recState, setRecState] = useState<"idle" | "armed">("idle");
  const recSeq = useRef(0);
  const watchdog = useSaveWatchdog(() =>
    toast("8 秒未收到老人端录音回报——可能没录上(麦克风权限/网络)。请检查老人端后重新示意录音。", "danger"));
  // 看门狗守的是"哪个环节的回报":录音中跳题/暂停时守的是旧环节,回报到达时若按
  // "是否当前环节"判清,旧环节的成功回报清不掉它——保存明明成功还弹假警报。
  const watchdogFor = useRef<string | null>(null);
  const lastArmedTurnK = useRef<string | null>(null);
  const armWatchdog = (tk: string | null) => { watchdogFor.current = tk; watchdog.start(); };
  const [cueLevel, setCueLevel] = useState<0 | 1 | 2 | 3>(0);      // 已发给老人端的线索等级(只升不降)
  const [savingTurn, setSavingTurn] = useState(false);
  // 当前环节工作态
  const [work, setWork] = useState<WorkState>(emptyWork());
  const [pendingAudio, setPendingAudio] = useState<Record<string, { rawAudioId: string; duration: number }>>({});

  // 刷新恢复:journal.audios 持久有 turnKey→音频映射,重挂载时重建回放条,
  // 否则补录转写建 turn 时 raw_audio_id 静默断链。
  // ★同环节重录取"最后一遍"(与实时路径一致——audioSaved 处理器就是后到覆盖):
  // 先到先占会在刷新后把旧一遍录音绑回转写/回放。journal.audios 按登记序迭代,末=最新。
  useEffect(() => {
    setPendingAudio((prev) => {
      const next = { ...prev };
      for (const [rid, a] of Object.entries(journal.audios)) {
        if (isBoundJournalTurnKey(a.turnKey) && /#\d+$/.test(a.turnKey)) {
          next[a.turnKey] = { rawAudioId: rid, duration: a.durationSeconds ?? 0 };
        }
      }
      return next;
    });
  }, [journal.audios]);

  // 计划与 journal 分开恢复：计划错误走整屏 fail-closed；journal 错误仍保留页面骨架，
  // 但由 SessionControlBar 关闭一切现场推进并提供统一重试。
  useEffect(() => {
    setErr(null);
    setPlan(null);
    resetSession();
    recoveredOnce.current = false;
    restoreTarget.current = null;
    lastAppliedTurnK.current = null; // 换场/重载:同名 turnKey 也必须重建工作卡
    handshakeSent.current = false;
    setRecoveryApplied(false);
    setRecoveredLabel(null);
    Promise.all([
      api.sessionPlan(session.session_id, session.week_no, session.event_line),
      api.itemBank(),
    ])
      .then(([p, bank]) => {
        // bundle 可能还没到,尽力断言 plan==后端;bundle 到位后再断言一次
        if (p.item_bank_version_id !== bank.version_id) {
          throw new Error(`题库版本不一致:计划=${p.item_bank_version_id} 后端=${bank.version_id}`);
        }
        setPlan(p);
      })
      .catch((e) => setErr(String(e)));
  }, [session, resetSession, retryNonce]);

  useEffect(() => {
    let cancelled = false;
    setJournalLoading(true);
    setJournalRecoveryError(null);
    api.sessionJournal(session.session_id)
      .then((remoteJournal) => {
        if (cancelled) return;
        if (remoteJournal.session.session_id !== session.session_id) {
          throw new Error("服务器返回了其他场次的记录，已拒绝恢复");
        }
        hydrateFromServer(remoteJournal);
        setJournalLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setJournalRecoveryError(e instanceof ApiError ? e.detail : String(e));
        setJournalLoading(false);
      });
    return () => { cancelled = true; };
  }, [hydrateFromServer, retryNonce, session.session_id]);

  const recoveryLoading = runtimeControl.loading || journalLoading;
  const recoveryError = runtimeControl.error ?? journalRecoveryError ?? syncError;
  const runtimeReady = runtimeControl.runtime?.sessionId === session.session_id;
  const recoveryPending = recoveryLoading || (!recoveryError && (!runtimeReady || !recoveryApplied));
  const interactionBlocked = paused || pausePending || recoveryPending || Boolean(recoveryError);
  const retryRecovery = () => {
    setRetryNonce((n) => n + 1);
    void runtimeControl.refresh();
    retrySync();
  };

  // 服务端运行时位置优先于浏览器缓存。只在首次恢复时应用，避免轮询或本地推进被旧值拉回。
  useEffect(() => {
    if (!plan || !runtimeReady || recoveryLoading || recoveryError || recoveredOnce.current) return;
    const saved = runtimeControl.runtime?.cursor;
    const nextItem = saved?.itemIdx ?? journal.cursor.itemIdx;
    const nextTurn = saved?.turnIdx ?? journal.cursor.turnIdx;
    const safeItem = Math.min(Math.max(0, nextItem), Math.max(0, plan.items.length - 1));
    const turns = plan.items[safeItem]?.turns ?? [];
    const safeTurn = Math.min(Math.max(0, nextTurn), Math.max(0, turns.length - 1));
    setItemIdx(safeItem);
    setTurnIdx(safeTurn);
    restoreTarget.current = { itemIdx: safeItem, turnIdx: safeTurn };
    if (safeItem > 0 || safeTurn > 0 || runtimeControl.runtime?.status === "paused") {
      setRecoveredLabel(`第 ${safeItem + 1} 题 · 第 ${safeTurn + 1} 环节`);
    }
    recoveredOnce.current = true;
    setRecoveryApplied(true);
  }, [journal.cursor.itemIdx, journal.cursor.turnIdx, plan, recoveryError, recoveryLoading, runtimeControl.runtime, runtimeReady]);

  // HTTP 写入由 useCursorWriter 串行排队：先完成 session 握手，再落首个 cursor。
  useEffect(() => {
    if (!plan || interactionBlocked || handshakeSent.current) return;
    postSession({ sessionId: session.session_id, weekNo: session.week_no, eventLine: session.event_line, mode: "task", itemBankVersionId: plan.item_bank_version_id });
    handshakeSent.current = true;
  }, [interactionBlocked, plan, postSession, session]);

  useEffect(() => {
    if (plan && bundle) {
      try { assertVersionsMatch(bundle.item_bank_version_id, plan.item_bank_version_id, plan.item_bank_version_id); }
      catch (e) { setErr(String(e)); }
    }
  }, [plan, bundle]);

  // 受试者是否允许录音——不允许则禁用"示意录音"(护栏:no-recording 受试者不得被 arm)
  useEffect(() => {
    setRecStatus("loading");
    api.getPatient(session.patient_id)
      .then((pt) => setRecStatus(pt.recording_allowed === false ? "denied" : pt.recording_allowed === true ? "allowed" : "unrated"))
      .catch(() => setRecStatus("error"));
  }, [session.patient_id, retryNonce]);

  const item: PlanItem | undefined = plan?.items[itemIdx];
  const planTurn: PlanTurn | undefined = item?.turns[turnIdx];
  const turnK = item && planTurn ? `${item.item_id}#${planTurn.turn_seq}` : "";
  // 在途异步续体的环节身份校验:自动推进可在任何请求在途时跳环节,续体不校验会把
  // turnId/结果写进"下一环节"的工作卡——确认/锁分随之落到错误的 turn。
  const turnKRef = useRef(turnK);
  turnKRef.current = turnK;
  // 已锁环节不下发自助开录("锁定后不可再录"对自助路径同样生效)
  const selfStartOut = selfStart && !interactionBlocked && !(journal.turns[turnK]?.locked ?? false);
  // 老人端麦克风真值(自助开录时操作端唯一感知渠道)
  const patientRec = usePatientRec(session.session_id);
  const patientMicOn = patientRec?.active === true;
  // 麦克风关闭的下降沿启动看门狗:自助路径的停麦后若迟迟等不到 audioSaved(保存失败),
  // 8 秒告警——否则自助模式保存失败对操作端零信号,老人的回答静默丢失。
  const prevMicOn = useRef(false);
  useEffect(() => {
    if (prevMicOn.current && !patientMicOn) armWatchdog(patientRec?.turnKey ?? turnK);
    prevMicOn.current = patientMicOn;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [patientMicOn, watchdog]);

  // 光标变化 → 重置工作态(从 journal 恢复布尔/已锁/线索级)+ 广播老人端
  // ★interactionBlocked 在依赖里只为"解锁边沿补发游标";位置没变时绝不重建工作卡——
  // 暂停/继续、重试同步成功都会翻转它,若重建会用 journal 已存值冲掉未保存的转写/确认草稿。
  useEffect(() => {
    if (!item || !planTurn || interactionBlocked) return;
    const target = restoreTarget.current;
    if (target && (target.itemIdx !== itemIdx || target.turnIdx !== turnIdx)) return;
    restoreTarget.current = null;
    if (lastAppliedTurnK.current === turnK) {
      // 暂停/恢复解除,位置未变:只补发游标恢复老人端按钮(恢复投影 selfStart=false),工作卡不动
      postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel,
                   recording: "idle", recSeq: recSeq.current, selfStart: selfStartOut });
      return;
    }
    lastAppliedTurnK.current = turnK;
    const jt = journal.turns[turnK];
    setWork({
      turnId: jt?.turnId ?? null, asrText: jt?.asrText ?? "",
      // 确认文本预填:优先已存确认,其次 ASR 原文(一场上百环节,不该每次整句重打)
      confirmed: jt?.confirmedText ?? jt?.asrText ?? "", ai: null,
      locked: jt?.locked ?? false, savedAsr: jt?.asrSaved ?? false, savedConfirm: jt?.confirmed ?? false,
    });
    if (recState === "armed") armWatchdog(lastArmedTurnK.current); // 录音中直接跳题:老人端会自动收尾保存,回报也要有人等
    setRecState("idle");
    // 回访已发过线索的环节:恢复级别而非归零——老人端线索不被清屏,prompt_level 建议不记错
    const savedCursor = runtimeControl.runtime?.cursor;
    const runtimeCue = savedCursor?.itemIdx === itemIdx && savedCursor.turnIdx === turnIdx
      ? savedCursor.cueLevel ?? 0
      : 0;
    const restoredCue = Math.min(3, Math.max(
      0,
      journal.cueLevels?.[turnK] ?? 0,
      jt?.cueLevel ?? 0,
      jt?.promptLevel ?? 0,
      runtimeCue,
    )) as 0 | 1 | 2 | 3;
    setCueLevel(restoredCue);
    setCursor(itemIdx, turnIdx);
    onItemEventChange?.(journal.itemEvents[item.item_id]?.itemEventId ?? null);
    // 自动驾驶的反馈不在此下发:它是"上一题图片还在屏上"时先读的独立一拍(apAdvance beat-1),
    // 推进到新题的这条游标绝不带 fbKey——否则老人看着新图听到上一题的"这就是胡萝卜"。
    postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel: restoredCue, recording: "idle", recSeq: recSeq.current,
                 selfStart: selfStart && !(jt?.locked ?? false) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [itemIdx, turnIdx, plan, interactionBlocked]);

  // 录音资格判定变化即补发游标:落地(→allowed/unrated/denied)给按钮,重查(→loading)收回按钮。
  // loading 也要发——资格重查期间老人端保留旧 selfStart=true 按钮就违反 fail-closed。
  useEffect(() => {
    if (!planTurn || interactionBlocked) return;
    postCursor({ screen: recState === "armed" ? "record" : "present", itemIdx, turnIdx,
                 responseRole: planTurn.response_role, cueLevel,
                 recording: recState === "armed" ? "armed" : "idle", recSeq: recSeq.current, selfStart: selfStartOut });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recStatus, interactionBlocked]);

  // ★收回游标:离开训练屏(收尾/新受试者/切屏)或 fail-closed 时,老人端的
  // "开始回答"按钮必须撤下——否则残留的 selfStart=true 让过渡期里任何人都能往本场次录音。
  // (控制台整页被强杀属残余风险:无人可发收回;README 已注明换人前先核对老人端画面。)
  const withdrawRef = useRef<() => void>(() => {});
  withdrawRef.current = () => {
    if (!planTurn) return;
    postCursor({ screen: "thanks", itemIdx, turnIdx, responseRole: planTurn.response_role,
                 cueLevel, recording: "idle", recSeq: recSeq.current, selfStart: false });
  };
  useEffect(() => () => withdrawRef.current(), []);
  useEffect(() => { if (err || recoveryError) withdrawRef.current(); }, [err, recoveryError]);

  // 老人端录完 → 建 turn 的音频入参在此暂存(按 turnKey)
  useAudioSaved((m) => {
    // ★跨场次过滤:live state 里 audioSaved 残留到下次握手才清(且 PUT 是 fire-and-forget,
    // 失败可能永存)。别场次的回报若被记入本场 journal,会诱导跨场串绑——一律丢弃。
    if (m.sessionId !== session.session_id) return;
    // 重放判定:操作端刷新后轮询会把最后一条回报再送一遍(内存 seen 集刷新即空)。
    // journal 持久化,里面已有的 rawAudioId 一律按重放处理:记录性动作照做(幂等),
    // 但绝不触发自动推进——否则每次刷新老人端都被顶走一题。
    const isReplay = Boolean(journal.audios[m.rawAudioId]);
    // 迟到回报(非当前环节)只做记账:watchdog 可能正在为"当前环节"守着,清了它
    // 会吞掉真正的超时告警;armed→idle 写回若以当前环节身份发出,会掐断刚示意的新录音。
    const isCurrent = m.turnKey === turnK;
    setPendingAudio((prev) => ({ ...prev, [m.turnKey]: { rawAudioId: m.rawAudioId, duration: m.durationSeconds } }));
    // 记入 journal.audios,收尾屏音频闸门才能列出并驱动导出/校验/信度/删除。
    upsertAudio(m.rawAudioId, { turnKey: m.turnKey, containsDirectIdentifier: m.containsDirectIdentifier ?? false, isReliabilitySample: false, lastStatus: "recorded", durationSeconds: m.durationSeconds });
    // 看门狗按"守的环节"清:录音中跳题/暂停时守的是旧环节,旧环节回报到达即达成,
    // 不能因"非当前环节"漏清而弹保存成功的假警报;守当前环节时迟到旧报也清不掉它。
    if (!isReplay && m.turnKey === watchdogFor.current) {
      watchdog.clear();
      watchdogFor.current = null;
    }
    if (!isCurrent || isReplay) return;
    if (planTurn && !interactionBlocked) {
      // 无论自停("我说好了",cursor 仍 armed)还是远端停(cursor 停在 stopped):都把 idle
      // 写回真值源——armed 残留会让老人端刷新后自动开麦;stopped 残留会让自助按钮永久失效。
      postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel, recording: "idle", recSeq: recSeq.current, selfStart: selfStartOut });
    }
    setRecState("idle");
    toast(`老人端录音已保存(${m.durationSeconds.toFixed(1)}s)`, "info");
    // 自动驾驶接管:转写→判类→分支(反馈/提示升级/推进)全自动,人工只看卡住
    if (autoPilot) {
      if (!interactionBlocked) void runAutoPilotOnAudio(m);
      return;
    }
    // 自动推进:只认"当前环节"的首次回报;老人端由此获得"点开始→说→我说好了→下一题"
    // 的自走闭环,转写/锁分可事后回访补。研究者手上有在途操作/未保存的转写或确认改动时
    // 不跳——advance 会按 journal 重置工作卡,未存的字会被冲掉。
    if (!autoAdvance || interactionBlocked) return;
    const jt = journal.turns[turnK];
    const confirmBaseline = jt?.confirmedText ?? jt?.asrText ?? "";
    const dirty = busyOp !== null || savingTurn || locking
      || (work.asrText.trim() !== "" && !work.savedAsr)
      || (work.confirmed !== "" && work.confirmed !== confirmBaseline && !work.savedConfirm);
    if (dirty) { toast("录音已收,但本环节有未保存的编辑/在途操作,未自动跳转", "warn"); return; }
    const atEnd = plan && item && turnIdx + 1 >= item.turns.length && itemIdx + 1 >= plan.items.length;
    if (atEnd && planTurn) {
      // 末环节:游标不动的话老人端会停在末题、按钮常驻,可无限重复录音——转"完成"过渡屏
      postCursor({ screen: "thanks", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel, recording: "idle", recSeq: recSeq.current, selfStart: false });
      toast("最后一个环节已收音,老人端已转入结束画面;可前往场次收尾", "ok");
    } else {
      advance();
    }
  });

  const advance = () => {
    if (!plan || !item || interactionBlocked) return;
    if (turnIdx + 1 < item.turns.length) setTurnIdx(turnIdx + 1);
    else if (itemIdx + 1 < plan.items.length) { setItemIdx(itemIdx + 1); setTurnIdx(0); }
    else toast("已到最后一个环节,可前往场次收尾", "ok");
  };
  const retreat = () => {
    if (!plan || interactionBlocked) return;
    if (turnIdx > 0) setTurnIdx(turnIdx - 1);
    else if (itemIdx > 0) {
      const prev = plan.items[itemIdx - 1];
      setItemIdx(itemIdx - 1);
      setTurnIdx(Math.max(0, prev.turns.length - 1));
    }
  };
  const atFirstTurn = itemIdx === 0 && turnIdx === 0;
  const atLastTurn = !!plan && !!item && turnIdx + 1 >= item.turns.length && itemIdx + 1 >= plan.items.length;
  const workflowStage = work.locked ? 4 : !work.savedAsr ? 1 : !work.savedConfirm ? 2 : aiEnabled && !work.ai ? 3 : 4;

  // 示意老人端录音(VOX):arm→老人端自动开始录音;stop→老人端停止并保存后回传 audioSaved。
  // 携带当前 cueLevel,录音启停不清老人端已显示的线索。
  const armRecording = () => {
    if (!planTurn || interactionBlocked) return;
    recSeq.current += 1; // 每次 arm 新序号:老人自停后 armed→armed 重发才能重触发老人端
    lastArmedTurnK.current = turnK;
    setRecState("armed");
    postCursor({ screen: "record", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel, recording: "armed", recSeq: recSeq.current, selfStart: selfStartOut });
  };
  const stopRecording = () => {
    if (!planTurn) return;
    setRecState("idle");
    armWatchdog(lastArmedTurnK.current ?? turnK);
    postCursor({ screen: "record", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel, recording: "stopped", recSeq: recSeq.current, selfStart: selfStartOut });
  };

  // 发分级线索给老人端(0→3 只升不降;线索文本永远取自版本锁定题库,操作端不产话术)。
  const sendCue = (level: 1 | 2 | 3) => {
    if (!planTurn || interactionBlocked || level <= cueLevel) return;
    setCueLevel(level);
    recordCueLevel(turnK, level); // 持久化:回访本环节时恢复,不清老人端线索
    postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role,
                 cueLevel: level, recording: recState === "armed" ? "armed" : "idle", recSeq: recSeq.current, selfStart: selfStartOut });
  };

  async function ensureItemEvent(): Promise<number | null> {
    if (!item || interactionBlocked) return null;
    const existing = journal.itemEvents[item.item_id];
    if (existing) return existing.itemEventId;
    const ie = await api.createItem(session.session_id, { item_id: item.item_id, task_type: item.task_type, image_id: item.image_id });
    upsertItem(item.item_id, { itemEventId: ie.id, taskType: item.task_type, imageId: item.image_id ?? null });
    onItemEventChange?.(ie.id);
    return ie.id;
  }

  // 三个异步动作共用一把在途锁:点了立刻有反馈、连点不重复提交
  const [busyOp, setBusyOp] = useState<null | "confirm" | "ai" | "asr">(null);

  async function tryLocalAsr() {
    const au = pendingAudio[turnK];
    if (!au || busyOp || interactionBlocked) return;
    const opK = turnK; // 环节身份:自动推进可在请求在途时跳环节,续体只准写回发起时的环节
    setBusyOp("asr");
    try {
      const r = await api.asrTranscribe(au.rawAudioId);
      if (turnKRef.current !== opK) return;
      if (r.degraded || r.asr_text == null) {
        toast(`本地 ASR 引擎未接(${r.engine_version}),请人工转写`, "info");
        return;
      }
      // 引擎产文只预填,仍走人工确认→冻结的正常链路
      setWork((w) => (w.savedAsr ? w : { ...w, asrText: r.asr_text ?? w.asrText }));
      toast(`ASR 预填完成(${r.engine_version},置信度 ${r.asr_confidence ?? "—"})`, "ok");
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
    finally { setBusyOp(null); }
  }

  async function saveTranscription() {
    if (!item || !planTurn || interactionBlocked) return;
    if (savingTurn) return;                                          // 在途锁:防双击重复建 item/turn
    if (work.savedAsr && work.turnId) { toast("该环节转写已冻结,不可重写", "warn"); return; }
    const opK = turnK;
    setSavingTurn(true);
    try {
      const ieid = await ensureItemEvent();
      if (ieid == null) return;
      const au = pendingAudio[turnK];
      const te = await api.createTurn(ieid, {
        turn_seq: planTurn.turn_seq, response_role: planTurn.response_role,
        asr_text: work.asrText, raw_audio_id: au?.rawAudioId ?? null, duration_seconds: au?.duration ?? null,
      });
      upsertTurn(item.item_id, planTurn.turn_seq, { turnId: te.id, responseRole: planTurn.response_role, asrSaved: true, asrText: work.asrText });
      // 环节已被自动推进跳走:journal 已正确记账(键在发起时捕获),但工作卡现在属于新环节,
      // 把旧环节的 turnId 写进去会让后续确认/锁分落到错误的 turn 上。
      if (turnKRef.current !== opK) return;
      // 确认文本预填 ASR 原文:多数环节只需微调而非整句重打
      setWork((w) => ({ ...w, turnId: te.id, savedAsr: true, confirmed: w.confirmed.trim() ? w.confirmed : w.asrText }));
      toast("转写已保存并冻结", "ok");
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
    finally { setSavingTurn(false); }
  }

  async function saveConfirm(): Promise<boolean> {
    if (interactionBlocked) return false;
    if (!item || !planTurn || work.turnId == null) { toast("请先保存转写", "warn"); return false; }
    if (!work.confirmed.trim()) { toast("确认文本为空,未提交(避免把已有确认覆盖成空串)", "warn"); return false; }
    if (busyOp) return false;
    const opK = turnK;
    setBusyOp("confirm");
    try {
      await api.confirmTurn(work.turnId, work.confirmed);
      upsertTurn(item.item_id, planTurn.turn_seq, { turnId: work.turnId, responseRole: planTurn.response_role, confirmed: true, confirmedText: work.confirmed });
      if (turnKRef.current !== opK) return true; // 已跳环节:journal 已记账,不写新环节的工作卡
      setWork((w) => ({ ...w, savedConfirm: true }));
      toast("确认文本已保存(未覆盖原文)", "ok");
      // 判分口径以 confirmed 优先:确认一落地就自动跑初评,省一次手点("动态"名副其实)
      if (aiEnabled) void runAiJudge(true);
      return true;
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); return false; }
    finally { setBusyOp((b) => (b === "confirm" ? null : b)); }
  }

  async function runAiJudge(auto = false) {
    if (interactionBlocked) return;
    if (work.turnId == null) { toast("请先保存转写", "warn"); return; }
    if (!auto && busyOp) return;
    const opK = turnK;
    if (!auto) setBusyOp("ai");
    try {
      const te = await api.aiJudgeTurn(work.turnId);
      if (turnKRef.current !== opK) return;
      setWork((w) => ({ ...w, ai: { answerType: te.ai_answer_type ?? null, score: te.ai_score ?? null, needsReview: !!te.ai_needs_review } }));
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
    finally { if (!auto) setBusyOp((b) => (b === "ai" ? null : b)); }
  }

  const [locking, setLocking] = useState(false);
  async function doLock(elementValue: number, promptLevel?: number): Promise<boolean> {
    if (interactionBlocked) return false;
    if (!item || !planTurn || work.turnId == null) { toast("请先保存转写", "warn"); return false; }
    if (locking) return false;
    const opK = turnK;
    setLocking(true);
    const payload = { reviewer_id: reviewerId, element_value: elementValue, prompt_level: promptLevel ?? null };
    try {
      // 有改动未保存的确认文本:先替研究者存了再锁——锁定后 confirmed 不可改,不能让它无声丢失
      if (work.confirmed.trim() && !work.savedConfirm) {
        await api.confirmTurn(work.turnId, work.confirmed);
        upsertTurn(item.item_id, planTurn.turn_seq, { turnId: work.turnId, responseRole: planTurn.response_role, confirmed: true, confirmedText: work.confirmed });
        if (turnKRef.current === opK) setWork((w) => ({ ...w, savedConfirm: true }));
      }
      assertPortraitFree(payload as unknown as Record<string, unknown>); // ★锁分载荷画像守卫(前端第4层)
      await api.lockTurn(work.turnId, payload);
      upsertTurn(item.item_id, planTurn.turn_seq, { turnId: work.turnId, responseRole: planTurn.response_role, locked: true, elementValue, promptLevel });
      if (turnKRef.current === opK) {
        setWork((w) => ({ ...w, locked: true }));
        // 锁定即收回老人端自助开录按钮("锁定后不可再录"对自助路径同样生效)
        if (!interactionBlocked) {
          postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel, recording: "idle", recSeq: recSeq.current, selfStart: false });
        }
      }
      toast("已锁定评分", "ok");
      return true;
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); return false; }
    finally { setLocking(false); }
  }

  // ---------------- 自动驾驶状态机(文档协议:四分支→两级提示→告知答案,永不卡题) ----------------
  // 分工:处理器(收音回报/沉默)只判类和改状态;所有定时(开麦/沉默/终局播报后推进)统一由
  // 下面的 [turnK, cueLevel] 效应安排——避免"改状态的定时器被状态变化触发的 cleanup 清掉"。
  // AI 只判类驱动流程,永不锁分;反馈话术全部指向题库/协议固定文案(fbKey 查表,不产文本)。
  const apActive = autoPilot && !interactionBlocked && !!item && !!planTurn
    && (recStatus === "allowed" || recStatus === "unrated");
  const clearApTimers = () => {
    for (const k of ["arm", "answer", "act"] as const) {
      const t = apTimers.current[k];
      if (t) clearTimeout(t);
      apTimers.current[k] = undefined;
    }
  };
  // 朗读时长估计:无老人端"读完"回执,按字数估(云端语速 0.9)+固定余量;宁可多等不早开麦
  // (早开麦会把小语自己的声音录进去)。答窗宽限也用它:反馈/线索长句要多给作答时间。
  const speakMs = (t: string | null | undefined) => (t ? 2500 + t.length * 260 : 0);

  // 收麦并把老人端 idle 写回真值(armed/stopped 残留会自动重开麦/锁死自助按钮)。
  function apDisarm() {
    apAwaitingAnswer.current = false;
    setRecState("idle");
    if (planTurn) {
      postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role,
                   cueLevel, recording: "idle", recSeq: recSeq.current, selfStart: selfStartOut });
    }
  }

  // 环节了结:建正式 turn(最后一轮音频/转写冻结;confirmed 留人工事后),持久化 AI 初评,推进
  async function apResolve(fbKey: FbKey | null, promptLevel: number) {
    if (!item || !planTurn) return;
    const opK = turnK;
    try {
      const ieid = await ensureItemEvent();
      if (ieid == null || turnKRef.current !== opK) return;
      const round = apLastRound.current;
      const te = await api.createTurn(ieid, {
        turn_seq: planTurn.turn_seq, response_role: planTurn.response_role,
        asr_text: round?.asrText ?? null, raw_audio_id: round?.rawAudioId ?? null,
        duration_seconds: round?.duration ?? null, prompt_level: promptLevel,
      });
      upsertTurn(item.item_id, planTurn.turn_seq, { turnId: te.id, responseRole: planTurn.response_role,
        asrSaved: true, asrText: round?.asrText ?? "", cueLevel: promptLevel as 0 | 1 | 2 | 3 });
      void api.aiJudgeTurn(te.id).catch(() => undefined);   // 初评持久化失败不阻推进(人工可回访重跑)
      if (turnKRef.current !== opK) return;
      setWork((w) => ({ ...w, turnId: te.id, asrText: round?.asrText ?? "", savedAsr: true }));
      apAdvance(fbKey);
    } catch (e) {
      toast(`自动驾驶建档失败,请人工接管本环节:${e instanceof ApiError ? e.detail : String(e)}`, "danger");
    }
  }

  // 推进两拍:beat-1 反馈话术在"当前题图片还在屏上"时先读(引用的是本题目标词,如"这就是胡萝卜");
  // 待反馈读完 beat-2 才换到下一题——否则老人看着新图听到上一题的答案。无反馈则直接推进。
  function apAdvance(fbKey: FbKey | null) {
    if (!plan || !item || !planTurn || interactionBlocked) return;
    clearApTimers();
    apLastRound.current = null;
    const atEnd = turnIdx + 1 >= item.turns.length && itemIdx + 1 >= plan.items.length;
    const step = () => {
      if (atEnd) {
        postCursor({ screen: "thanks", itemIdx, turnIdx, responseRole: planTurn.response_role,
                     cueLevel, recording: "idle", recSeq: recSeq.current, selfStart: false });
        toast("最后一题完成,老人端已转入结束画面;可前往场次收尾", "ok");
      } else {
        advance();
      }
    };
    if (fbKey) {
      const line = resolveFeedbackLine(bundle, protocol, fbKey, item.item_id);
      postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role,
                   cueLevel, recording: "idle", recSeq: recSeq.current, selfStart: false,
                   fbKey, fbItemId: item.item_id, fbSeq: ++fbSeqCounter.current });
      apTimers.current.act = setTimeout(step, speakMs(line) + 600);
    } else {
      step();
    }
  }

  // 提示升级:单条 idle 游标原子下发(不走 apDisarm+sendCue 两次 post——后者读到未提交的
  // 陈旧 recState='armed',会让老人端在读线索时误显"正在准备麦克风")。arm 由效应随后重排。
  function apEscalate(path: "unknown" | "close" | "silence") {
    apAwaitingAnswer.current = false;
    setRecState("idle");
    if (cueLevel === 0) apCue1Path.current = path;
    const next = Math.min(3, cueLevel + 1) as 1 | 2 | 3;
    setCueLevel(next);
    recordCueLevel(turnK, next);
    if (planTurn) {
      postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role,
                   cueLevel: next, recording: "idle", recSeq: recSeq.current, selfStart: selfStartOut });
    }
  }

  // 答窗到点:老人在作答窗内没点"我说好了"(痴呆人群常见:困惑/不理解按钮)。没有 VAD,
  // 只能按"作答窗超时=沉默"处理——先物理收麦(绝不留热麦),再按文档沉默分支走。
  function apOnSilence() {
    if (!apActive || !item || !planTurn) return;
    apAwaitingAnswer.current = false;
    apDisarm();                                            // 收回老人端麦克风(热麦红线)
    if (item.task_type === "单要素") { apEscalate("silence"); return; }
    // 双要素:文档未定义沉默——v1 按"不能回答"处理(已在协议 notes 报备待课题组拍板)
    if (planTurn.response_role.includes("命名")) {
      void apResolve(planTurn.response_role.startsWith("左") ? "namefix_l" : "namefix_r", cueLevel);
    } else if (cueLevel < 1) {
      apEscalate("silence");   // 作用/关系:给功能/关系讲解;读完推进由效应终局分支接管
    } else {
      void apResolve(null, cueLevel);
    }
  }

  // 收音回报 → 转写 → 判类 → 分支(成功反馈按进入路径选变体;失败升级;终局推进)
  async function runAutoPilotOnAudio(m: { rawAudioId: string; durationSeconds: number }) {
    // apAwaitingAnswer 闸:只处理"正在等的这一轮"回报;自动收麦后迟到的旧录音回报一律忽略
    // (它 turnKey 仍等于当前环节,靠 turnKRef 挡不住,只能靠这个每轮独占标记)。
    if (!apAwaitingAnswer.current || apBusy.current || !item || !planTurn) return;
    apAwaitingAnswer.current = false;
    clearApTimers();
    apBusy.current = true;
    const opK = turnK;
    try {
      const tr = await api.asrTranscribe(m.rawAudioId);
      if (turnKRef.current !== opK || !autoPilotRef.current) return;
      if (tr.degraded || tr.asr_text == null) {
        toast(`ASR 引擎不可用(${tr.engine_version}),自动驾驶让位:请人工转写/推进本环节`, "warn");
        return;
      }
      apLastRound.current = { rawAudioId: m.rawAudioId, duration: m.durationSeconds, asrText: tr.asr_text };
      if (cueLevel >= 3) { void apResolve(null, 3); return; }   // 告知答案后不再判类,总是推进
      const cls = await api.judgeClassify(item.item_id, planTurn.response_role, tr.asr_text);
      if (turnKRef.current !== opK || !autoPilotRef.current) return;
      setWork((w) => ({ ...w, asrText: w.savedAsr ? w.asrText : (tr.asr_text ?? ""),
        ai: { answerType: cls.answer_type, score: cls.ai_score, needsReview: cls.needs_review } }));
      // 成功=判"正确",或回答里包含了完整目标词(含目标词=说对了,如"是胡萝卜";
      // 而"萝卜"这种目标词的子串只判"部分正确"、contains_target 为假 → 按不准确升级提示)。
      const ok = cls.answer_type === "正确" || cls.contains_target === true;
      if (item.task_type === "单要素") {
        if (ok) {
          void apResolve(cueLevel === 0 ? "self"
            : cueLevel === 1 ? (`cued1_${apCue1Path.current ?? "close"}` as FbKey) : "cued2", cueLevel);
        } else {
          apEscalate(cls.answer_type === "上位词或相关词" || cls.answer_type === "部分正确" ? "close" : "unknown");
        }
      } else if (planTurn.response_role.includes("命名")) {
        // 双要素命名:答错一次性纠正("这个我们刚刚见过,它叫X。"),不升级,直接下一环节
        void apResolve(ok ? null : planTurn.response_role.startsWith("左") ? "namefix_l" : "namefix_r", cueLevel);
      } else {
        void apResolve(null, cueLevel);   // 作用/关系:开放式,能答即继续(文档口径),对错人工事后
      }
    } catch (e) {
      toast(`自动驾驶处理失败,请人工接管本环节:${e instanceof ApiError ? e.detail : String(e)}`, "danger");
    } finally {
      apBusy.current = false;
    }
  }

  // 唯一的定时安排点:换环节/线索升级时,算好"读完再动"的等待
  useEffect(() => {
    if (!apActive || !item || !planTurn) return;
    if (work.locked || journal.turns[turnK]?.asrSaved) return;   // 已办环节(含回访)不自动化
    const role = planTurn.response_role;
    const isDoubleAux = item.task_type === "双要素" && !role.includes("命名");
    if (cueLevel >= 3 || (isDoubleAux && cueLevel >= 1)) {
      // 终局播报(告知答案/双要素讲解):读完即建档推进,不再收音
      const text = lookupCue(bundle, item.item_id, item.task_type, role, cueLevel);
      apTimers.current.act = setTimeout(() => { void apResolve(null, cueLevel); }, speakMs(text) + 800);
      return clearApTimers;
    }
    const qText = role.includes("作用") ? "它是做什么用的呢？" : role === "关系识别" ? "它们之间有什么关系呢？" : "请看这张图片，这是什么？";
    const cueText = cueLevel > 0 ? lookupCue(bundle, item.item_id, item.task_type, role, cueLevel) : null;
    const spoken = cueLevel > 0 ? cueText : qText;
    apTimers.current.arm = setTimeout(() => {
      armRecording();
      apAwaitingAnswer.current = true;
      // 作答窗=文档沉默阈值(默认10s)+宽限;到点没等到"我说好了"即按沉默处理(收麦+升级)。
      // ★不随 patientMicOn 撤销——没有 VAD,"麦克风开着"不代表老人在说话;这条计时是唯一
      // 能在活麦沉默场景停麦、推进状态机的机制(真机麦克风验收的关键)。
      apTimers.current.answer = setTimeout(apOnSilence, ((protocol?.silence_seconds ?? 10) + 5) * 1000);
    }, speakMs(spoken));
    return clearApTimers;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turnK, cueLevel, apActive, work.locked]);

  // 换环节复位来路;关自动驾驶清干净(人工接管即刻生效)
  useEffect(() => { apCue1Path.current = null; }, [turnK]);
  useEffect(() => {
    if (!autoPilot) { clearApTimers(); apAwaitingAnswer.current = false; apLastRound.current = null; }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoPilot]);

  async function pauseTraining() {
    if (pausePending || paused) return;
    setPausePending(true);
    if (recState === "armed" || patientMicOn) {
      armWatchdog(patientRec?.turnKey ?? lastArmedTurnK.current ?? turnK);
    }
    if (planTurn) {
      // bus 先行收麦，不等 pause HTTP 往返；后端的场次暂停仍是最终真值。
      postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role,
                   cueLevel, recording: "idle", recSeq: recSeq.current, selfStart: false });
    }
    setRecState("idle");
    try {
      // 先排空已入队的 live 写再发 pause:pause 直连 HTTP,若先落地会把排队中的
      // 收麦游标 409 掉,控制条随即锁死在"重新同步"循环里(复审确证的死锁)。
      await flushLiveWrites();
      const next = await runtimeControl.pause();
      if (next) toast("本场训练已暂停，患者端麦克风保持关闭", "ok");
    } finally {
      setPausePending(false);
    }
  }

  async function resumeTraining() {
    const next = await runtimeControl.resume();
    if (next) toast("已恢复到暂停前的位置", "ok");
  }

  if (err) return <FailClosed msg={err} onRetry={() => setRetryNonce((n) => n + 1)} onExit={onExit} />;
  if (!plan) return <p>加载会话计划…</p>;
  if (plan.items.length === 0) return <p>本场次无评分题(第 1 周应走关系建立控制台)。</p>;

  return (
    <div className="training-layout">
      <ItemRail plan={plan} itemIdx={itemIdx} lockedCount={countLocked(journal, plan)}
        onPick={(i) => { if (!interactionBlocked) { setItemIdx(i); setTurnIdx(0); } }} journalTurns={journal.turns} disabled={interactionBlocked} />

      <div className="training-main">
        <div className="training-page-header">
          <div>
            <div className="page-kicker">当前训练任务</div>
            <h2 className="page-title">{item?.item_id.replace(/^(SE|DE)_/, "") ?? "训练判分"}</h2>
            <p className="page-description">按“转写、确认、辅助初评、人工锁分”顺序完成本环节。</p>
          </div>
          {/* 收尾前必先停录:否则本屏卸载后无人发 idle,老人端麦克风持续开着 */}
          <Button variant="ghost" disabled={interactionBlocked} onClick={() => {
            if (interactionBlocked) return;
            if (recState === "armed") stopRecording();
            onWrapup();
          }}>进入场次收尾</Button>
        </div>

        <SessionControlBar paused={paused} loading={recoveryPending} busy={runtimeControl.busy || pausePending} recoveredLabel={recoveredLabel}
          error={recoveryError} onRetry={retryRecovery}
          onPause={() => void pauseTraining()} onResume={() => void resumeTraining()} />

        <div className="toolbar training-toolbar" aria-label="训练设置">
          <label className="toolbar-field">评分人
            <TextInput value={reviewerId} disabled={interactionBlocked} onChange={(e) => setReviewerId(e.target.value)} style={{ width: 96 }} />
          </label>
          <label className="toggle-field">
            <input type="checkbox" checked={aiEnabled} disabled={interactionBlocked} onChange={(e) => setAiEnabled(e.target.checked)} />
            <span>启用辅助初评</span>
          </label>
          <label className="toggle-field" title="老人端点'我说好了'后自动跳下一环节;关掉则手动逐环节推进">
            <input type="checkbox" checked={autoAdvance} disabled={interactionBlocked} onChange={toggleAutoAdvance} />
            <span>收音后自动推进</span>
          </label>
          <label className="toggle-field" title="AI 全自动:自动开麦收音→云转写→判类→固定话术反馈→提示逐级升级→推进;随时可关(人工接管)。需已配云语音 Key">
            <input type="checkbox" checked={autoPilot} disabled={interactionBlocked} onChange={toggleAutoPilot} />
            <span>自动驾驶</span>
          </label>
          {autoPilot && <StatusPill tone="primary">自动驾驶中 · AI 判类仅驱动流程,锁分仍需人工</StatusPill>}
        </div>

        {item && planTurn && (
          <div className={`card col training-work-card${interactionBlocked ? " session-paused-surface" : ""}`}>
            <div className="training-work-header">
              <div className="row wrap">
                <StatusPill tone="primary">{item.task_type}</StatusPill>
                <span>环节 {planTurn.turn_seq}/{item.turns.length}</span>
                <strong>{planTurn.response_role}</strong>
              </div>
              <div className="row wrap" style={{ gap: 8 }}>
                {work.locked && <StatusPill tone="ok">已锁定</StatusPill>}
                {/* 常驻导航:每个环节都能一键前进/回退(老人端随游标同步),不必等锁分或翻左侧题列 */}
                <Button onClick={retreat} disabled={interactionBlocked || atFirstTurn}>上一环节</Button>
                <Button onClick={advance} disabled={interactionBlocked || atLastTurn}>下一环节</Button>
              </div>
            </div>

            <WorkflowStepper current={workflowStage} />

            <ItemReference item={item} bundle={bundle} />

            {/* 分级线索(0→3 只升不降;内容取版本锁定题库,推老人端显示) */}
            {!interactionBlocked && !work.locked && (
              <CueButtons bundle={bundle} itemId={item.item_id} taskType={item.task_type}
                role={planTurn.response_role} cueLevel={cueLevel} onSend={sendCue} />
            )}

            {/* 录音示意(老人端 VOX)——fail-closed:资格未确认不放行 */}
            <div className="recording-panel">
              {recoveryPending ? (
                <StatusPill tone="muted">正在恢复场次，录音入口保持关闭</StatusPill>
              ) : recoveryError ? (
                <StatusPill tone="danger">场次恢复失败，录音入口保持关闭</StatusPill>
              ) : paused ? (
                <StatusPill tone="warn">场次暂停中，患者端麦克风保持关闭</StatusPill>
              ) : recStatus === "denied" ? (
                <StatusPill tone="danger">该受试者不允许录音 · 由研究者现场听记</StatusPill>
              ) : recStatus === "loading" ? (
                <Button disabled>录音资格确认中…</Button>
              ) : recStatus === "error" ? (
                <>
                  <StatusPill tone="warn">录音资格未确认(患者档案获取失败)</StatusPill>
                  <Button onClick={() => setRetryNonce((n) => n + 1)}>重试</Button>
                </>
              ) : recState === "armed" || patientMicOn ? (
                <>
                  <Button variant="danger" onClick={stopRecording}>停止老人端录音</Button>
                  {patientMicOn && recState !== "armed" && (
                    <StatusPill tone="danger">老人端麦克风开着(自助开录)</StatusPill>
                  )}
                </>
              ) : (
                <>
                  <Button onClick={armRecording} disabled={work.locked}>开始老人端录音</Button>
                  {recStatus === "unrated" && <span className="muted">录音资格未评(按允许处理)</span>}
                </>
              )}
              {pendingAudio[turnK] && (
                <>
                  <StatusPill tone="ok">已收到录音 {pendingAudio[turnK].duration.toFixed(1)}s</StatusPill>
                  <AuthenticatedAudio rawAudioId={pendingAudio[turnK].rawAudioId} />
                  {!work.savedAsr && <Button onClick={tryLocalAsr} disabled={interactionBlocked || busyOp !== null}>{busyOp === "asr" ? "转写中…" : "本地 ASR 转写"}</Button>}
                </>
              )}
            </div>

            {/* 阶段A 转写 */}
            <section className={`workflow-panel${workflowStage === 1 ? " is-active" : ""}`}>
              <div className="workflow-panel-header">
                <div><span className="workflow-panel-number">1</span><strong>记录与转写</strong></div>
                {work.savedAsr && <StatusPill tone="ok">已冻结</StatusPill>}
              </div>
              <Field label="受试者回答" hint="保存后保留原始转写，不会被后续人工确认覆盖。">
                <TextInput value={work.asrText} disabled={interactionBlocked || work.savedAsr}
                  onChange={(e) => setWork((w) => ({ ...w, asrText: e.target.value }))}
                  placeholder={pendingAudio[turnK] ? "回放录音后输入听到的回答" : "输入现场听到的回答"} />
              </Field>
              {!work.savedAsr && <Button variant="primary" onClick={saveTranscription} disabled={interactionBlocked || savingTurn}>{savingTurn ? "正在保存…" : "保存原始转写"}</Button>}
            </section>

            {/* 阶段B 确认 */}
            {work.savedAsr && (
              <section className={`workflow-panel${workflowStage === 2 ? " is-active" : ""}`}>
                <div className="workflow-panel-header">
                  <div><span className="workflow-panel-number">2</span><strong>人工确认</strong></div>
                  {work.savedConfirm && <StatusPill tone="ok">已确认</StatusPill>}
                </div>
                <div className="source-transcript"><span>原始转写</span><strong className="mono">{work.asrText || "（空）"}</strong></div>
                <Field label="确认后的回答" hint="可校正识别错误；原始转写会继续保留。">
                  <TextInput value={work.confirmed} disabled={interactionBlocked || work.locked}
                    onChange={(e) => setWork((w) => ({ ...w, confirmed: e.target.value, savedConfirm: false }))} placeholder="人工校正后的规范文本" />
                </Field>
                {!work.locked && (
                  <div className="row">
                    <Button onClick={() => void saveConfirm()} disabled={interactionBlocked || busyOp !== null || work.savedConfirm}>
                      {busyOp === "confirm" ? "保存中…" : work.savedConfirm ? "已确认" : "保存人工确认"}
                    </Button>
                    {!work.savedConfirm && work.turnId != null && <span className="muted">有未保存修改</span>}
                  </div>
                )}
              </section>
            )}

            {/* 阶段C AI 初评 */}
            {work.savedAsr && aiEnabled && (
              <section className={`workflow-panel${workflowStage === 3 ? " is-active" : ""}`}>
                <div className="workflow-panel-header">
                  <div><span className="workflow-panel-number">3</span><strong>辅助初评</strong></div>
                  <Button onClick={() => void runAiJudge()} disabled={interactionBlocked || work.locked || busyOp !== null}>{busyOp === "ai" ? "评估中…" : "重新初评"}</Button>
                </div>
                <p className="muted">只作为人工判断参考，不会写入正式分。</p>
                {work.ai && (
                  <div className="row wrap">
                    <StatusPill tone="muted">{work.ai.answerType ?? "纯人工(无确定式口径)"}</StatusPill>
                    <span>初评分:{work.ai.score ?? "—"}</span>
                    {work.ai.needsReview && <StatusPill tone="warn">需人工复核</StatusPill>}
                  </div>
                )}
              </section>
            )}
            {work.savedAsr && !aiEnabled && <p className="muted">辅助初评已关闭，本环节完全由人工判分。</p>}

            {/* 阶段D 人工锁分 */}
            {work.savedAsr && !work.locked && (
              <LockControl key={turnK} taskType={item.task_type} role={planTurn.response_role}
                suggestedPromptLevel={cueLevel} onLock={doLock} locking={locking || interactionBlocked}
                basis={<ItemReference item={item} bundle={bundle} />}
                confirmedText={work.confirmed}
                unsavedConfirm={Boolean(work.confirmed.trim()) && !work.savedConfirm}
                active={workflowStage === 4} />
            )}
            {work.locked && <div className="row wrap"><StatusPill tone="ok">本环节正式分已锁定</StatusPill><Button variant="primary" disabled={interactionBlocked} onClick={advance}>进入下一环节</Button></div>}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------- 工作态 ----------------
interface WorkState {
  turnId: number | null; asrText: string; confirmed: string;
  ai: { answerType: string | null; score: number | null; needsReview: boolean } | null;
  locked: boolean; savedAsr: boolean; savedConfirm: boolean;
}
const emptyWork = (): WorkState => ({ turnId: null, asrText: "", confirmed: "", ai: null, locked: false, savedAsr: false, savedConfirm: false });

function countLocked(journal: ReturnType<typeof useSessionJournal>["journal"], plan: SessionPlan): number {
  return plan.items.filter((it) => it.turns.every((t) => journal.turns[`${it.item_id}#${t.turn_seq}`]?.locked)).length;
}

function WorkflowStepper({ current }: { current: number }) {
  const steps = ["记录转写", "人工确认", "辅助初评", "人工锁分"];
  return (
    <div className="workflow-stepper" aria-label="当前环节工作进度">
      {steps.map((label, index) => {
        const step = index + 1;
        return (
          <div key={label} className={`workflow-step${step === current ? " is-active" : ""}${step < current ? " is-complete" : ""}`}>
            <span>{step}</span>
            <strong>{label}</strong>
          </div>
        );
      })}
    </div>
  );
}

// ---------------- 左：题目游标列 ----------------
function ItemRail({ plan, itemIdx, lockedCount, onPick, journalTurns, disabled = false }: {
  plan: SessionPlan; itemIdx: number; lockedCount: number;
  onPick: (i: number) => void; journalTurns: Record<string, { locked: boolean }>; disabled?: boolean;
}) {
  const progress = plan.items.length ? Math.round((lockedCount / plan.items.length) * 100) : 0;
  return (
    <aside className="item-rail" aria-label="题目进度">
      <div className="item-rail-header">
        <div>
          <span className="page-kicker">题目进度</span>
          <strong>{lockedCount}/{plan.items.length} 已完成</strong>
        </div>
        <span className="mono muted">{progress}%</span>
      </div>
      <div className="progress-track" aria-hidden><span style={{ width: `${progress}%` }} /></div>
      <div className="item-rail-list">
        {plan.items.map((it, i) => {
          const total = it.turns.length;
          const locked = it.turns.filter((t) => journalTurns[`${it.item_id}#${t.turn_seq}`]?.locked).length;
          const allLocked = locked === total;
          const displayName = it.item_id.replace(/^(SE|DE)_/, "");
          return (
            <button key={it.item_id} onClick={() => onPick(i)} disabled={disabled}
              aria-current={i === itemIdx ? "step" : undefined}
              className={`item-rail-button${i === itemIdx ? " is-active" : ""}${allLocked ? " is-complete" : ""}`}>
              <span className="item-rail-index">{i + 1}</span>
              <span className="item-rail-copy">
                <strong>{displayName}</strong>
                <span>{it.task_type}{total > 1 ? ` · ${locked}/${total} 环节` : allLocked ? " · 已锁定" : " · 待完成"}</span>
              </span>
            </button>
          );
        })}
      </div>
    </aside>
  );
}

// ---------------- 题目参考(判分依据,零画像) ----------------
function ItemReference({ item, bundle }: { item: PlanItem; bundle: ReturnType<typeof useItemBankBundle>["bundle"] }) {
  if (!bundle) return null;
  if (item.task_type === "单要素") {
    const s = findSingle(bundle, item.item_id);
    if (!s) return null;
    return <div className="item-reference"><span>判分目标</span><strong>{s.target_word}</strong><span>可接受表达：{s.acceptable_expressions.join("、") || "暂无补充"}</span></div>;
  }
  if (item.task_type === "双要素") {
    const d = findDouble(bundle, item.item_id);
    if (!d) return null;
    return (
      <div className="item-reference">
        <span>左侧</span><strong>{d.left_word}</strong><span>右侧</span><strong>{d.right_word}</strong><span>关系线索：{d.relation_cue}</span>
      </div>
    );
  }
  return null;
}

// ---------------- 分级线索控件(推老人端;文本只来自题库,缺文本禁发) ----------------
function CueButtons({ bundle, itemId, taskType, role, cueLevel, onSend }: {
  bundle: ReturnType<typeof useItemBankBundle>["bundle"];
  itemId: string; taskType: string; role: string;
  cueLevel: 0 | 1 | 2 | 3; onSend: (level: 1 | 2 | 3) => void;
}) {
  // 告知答案=最高提示级,发出即不可逆(prompt_level 只升不降):红色区分 + 二次确认防误触
  const [confirmTell, setConfirmTell] = useState(false);
  // 单要素命名:三级;双要素 作用/关系:一级(题库仅一条角色线索);其余无预置线索。
  const levels: { level: 1 | 2 | 3; label: string }[] =
    taskType === "单要素"
      ? [{ level: 1, label: "发送轻提示" }, { level: 2, label: "发送明确提示" }, { level: 3, label: "告知答案" }]
      : taskType === "双要素" && (role.includes("作用") || role === "关系识别")
        ? [{ level: 1, label: "发送提示" }]
        : [];
  if (levels.length === 0) return null;
  const current = lookupCue(bundle, itemId, taskType, role, cueLevel);
  const tellText = lookupCue(bundle, itemId, taskType, role, 3);
  return (
    <div className="cue-panel">
      <div className="row wrap">
        <strong>分级线索</strong>
        <StatusPill tone={cueLevel === 0 ? "muted" : "warn"}>当前 {cueLevel} 级</StatusPill>
        {levels.map(({ level, label }) => {
          const text = lookupCue(bundle, itemId, taskType, role, level);
          const isTell = taskType === "单要素" && level === 3;
          return (
            <Button key={level} disabled={level <= cueLevel || text == null}
              variant={isTell ? "danger" : "ghost"}
              title={text == null ? "题库缺此级线索文本(待内容组补)" : undefined}
              onClick={() => (isTell ? setConfirmTell(true) : onSend(level))}>
              {label}{text == null ? "(缺文本)" : ""}
            </Button>
          );
        })}
      </div>
      {current && <p className="muted">老人端正在显示：{current}</p>}
      <ConfirmDialog open={confirmTell} title="告知答案?"
        body={`老人端将显示并朗读:「${tellText ?? ""}」。这是最高提示级,发出后本环节提示等级不可再降。`}
        confirmLabel="告知答案"
        onConfirm={() => { setConfirmTell(false); onSend(3); }}
        onCancel={() => setConfirmTell(false)} />
    </div>
  );
}

// ---------------- 按角色切换的锁分控件 ----------------
function LockControl({ taskType, role, suggestedPromptLevel = 0, onLock, locking = false, basis, confirmedText, unsavedConfirm, active = false }: {
  taskType: string; role: string; suggestedPromptLevel?: number;
  onLock: (v: number, promptLevel?: number) => Promise<boolean>;
  locking?: boolean; basis?: React.ReactNode; confirmedText?: string; unsavedConfirm?: boolean; active?: boolean;
}) {
  const [promptLevel, setPromptLevel] = useState<string | null>(String(suggestedPromptLevel));
  const [confirmVal, setConfirmVal] = useState<{ v: number; label: string } | null>(null);

  // 线索升级发生在锁分之后填的场景:跟随建议(研究者仍可改)。
  useEffect(() => { setPromptLevel(String(suggestedPromptLevel)); }, [suggestedPromptLevel]);

  const relation = isRelationRole(role);
  const single = taskType === "单要素";

  const choices: { v: number; label: string; tone: "ok" | "warn" | "danger" }[] = relation
    ? [{ v: 1, label: "识别(1)", tone: "ok" }, { v: 0.5, label: "部分(0.5)", tone: "warn" }, { v: 0, label: "未识别(0)", tone: "danger" }]
    : [{ v: 1, label: single ? "命名正确(1)" : "正确(1)", tone: "ok" }, { v: 0, label: single ? "未正确(0)" : "错误(0)", tone: "danger" }];

  return (
    <section className={`workflow-panel${active ? " is-active" : ""}`}>
      <div className="workflow-panel-header">
        <div><span className="workflow-panel-number">4</span><strong>人工锁分</strong></div>
        <StatusPill tone="warn">锁定后不可修改</StatusPill>
      </div>
      <p className="muted">请以人工确认文本和题目判分依据为准；辅助初评不会自动锁分。</p>
      {single && (
        <Field label="本环节最高提示等级" hint="0 自发回答 · 1 轻提示 · 2 明确或语音提示 · 3 已告知答案">
          <EnumSelect options={["0", "1", "2", "3"]} value={promptLevel} onChange={setPromptLevel} allowEmpty={false} />
        </Field>
      )}
      <div className="row wrap">
        {choices.map((c) => (
          <Button key={c.v} variant={c.tone === "ok" ? "primary" : "ghost"} disabled={locking}
            onClick={() => setConfirmVal({ v: c.v, label: c.label })}>
            {c.label}
          </Button>
        ))}
      </div>

      {confirmVal && (
        <LockConfirm label={confirmVal.label} role={role} promptLevel={single ? Number(promptLevel ?? 0) : undefined}
          basis={basis} confirmedText={confirmedText} unsavedConfirm={unsavedConfirm} locking={locking}
          onCancel={() => { if (!locking) setConfirmVal(null); }}
          onConfirm={async () => {
            // 在途保持弹窗打开、按钮禁用;成功才关——绝不出现"点了没反应再点一次"的双锁
            const ok = await onLock(confirmVal.v, single ? Number(promptLevel ?? 0) : undefined);
            if (ok) setConfirmVal(null);
          }} />
      )}
    </section>
  );
}

function LockConfirm({ label, role, promptLevel, basis, confirmedText, unsavedConfirm, locking, onConfirm, onCancel }: {
  label: string; role: string; promptLevel?: number;
  basis?: React.ReactNode; confirmedText?: string; unsavedConfirm?: boolean; locking?: boolean;
  onConfirm: () => void; onCancel: () => void;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => { const t = setTimeout(() => setArmed(true), 400); return () => clearTimeout(t); }, []);
  return (
    <div className="dialog-backdrop" onClick={onCancel}>
      <div className="dialog-panel fade-in" role="dialog" aria-modal="true" aria-labelledby="lock-confirm-title" onClick={(e) => e.stopPropagation()}>
        <div className="dialog-header"><h3 id="lock-confirm-title">确认正式锁分</h3></div>
        <p>环节「{role}」将锁定为 <strong>{label}</strong>{promptLevel != null ? ` · 提示等级 ${promptLevel}` : ""}。</p>
        {/* 判分依据摆在眼前,不用取消弹窗滚回去翻 */}
        {basis && <div className="item-reference">{basis}</div>}
        {confirmedText?.trim() && <p className="muted">确认文本:「{confirmedText}」</p>}
        {unsavedConfirm && <p style={{ color: "var(--c-warn)" }}>确认文本有未保存修改:锁定时将自动保存这一版。</p>}
        <p className="muted">锁定后，本环节的确认文本和评分均不可再次修改。</p>
        <div className="dialog-actions">
          <Button onClick={onCancel} disabled={locking}>取消</Button>
          <Button variant="primary" disabled={!armed || locking} onClick={onConfirm}>
            {locking ? "锁定中…" : armed ? "确认锁定" : "请稍候…"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function FailClosed({ msg, onRetry, onExit }: { msg: string; onRetry?: () => void; onExit?: () => void }) {
  return (
    <div className="alert alert--danger" role="alert" style={{ maxWidth: 760 }}>
      <div className="col">
      <h3>本场训练已安全暂停</h3>
      <p>{msg}</p>
      <p>题库版本不一致或计划加载失败时，系统不会继续呈现题目，以保护研究数据。恢复后可直接重试，无需更换受试者。</p>
      <div className="row wrap">
        {onRetry && <Button variant="primary" onClick={onRetry}>重试</Button>}
        {onExit && <Button onClick={onExit}>返回选人</Button>}
      </div>
      </div>
    </div>
  );
}
