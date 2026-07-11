import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { EnumSelect, Field, TextInput } from "../../components/Field";
import { StatusPill } from "../../components/StatusPill";
import { useToast } from "../../components/Toast";
import { assertVersionsMatch, findDouble, findSingle, lookupCue, useItemBankBundle } from "../../content/bundle";
import { useSessionJournal } from "../../hooks/useSessionJournal";
import { useAudioSaved, useCursorWriter, usePatientRec, useSaveWatchdog } from "../../sync/useCursorWriter";
import type { PlanItem, PlanTurn, Session, SessionPlan } from "../../types";
import { assertPortraitFree, isRelationRole } from "./vm";

// week≥2 判分主屏:左题目游标列 + 右环节工作卡(转写→确认→AI初评→锁分)。
// 唯一游标写者,推进老人端;★本文件及本目录禁 import 任何画像源(oxlint 守卫 + vm 运行时断言)。
export function TrainingConsoleScreen({ session, onWrapup, onExit, onItemEventChange }: {
  session: Session; onWrapup: () => void; onExit?: () => void; onItemEventChange?: (id: number | null) => void;
}) {
  const toast = useToast();
  const { bundle } = useItemBankBundle();
  const { journal, upsertItem, upsertTurn, upsertAudio, recordCueLevel, setCursor } = useSessionJournal(session.session_id);
  const { postSession, postCursor, resetSession } = useCursorWriter();

  const [plan, setPlan] = useState<SessionPlan | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [retryNonce, setRetryNonce] = useState(0);
  const [itemIdx, setItemIdx] = useState(journal.cursor.itemIdx);
  const [turnIdx, setTurnIdx] = useState(journal.cursor.turnIdx);
  const [aiEnabled, setAiEnabled] = useState(true);
  // 收到老人端录音回报即自动推进下一环节——老人"点开始→说→我说好了"就能自走全场。
  // 关掉则回到研究者逐环节手动推进(需要当场转写/锁分时用)。
  const [autoAdvance, setAutoAdvance] = useState(() => {
    try { return localStorage.getItem("nmu:console:autoAdvance") !== "0"; } catch { return true; }
  });
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
  const [cueLevel, setCueLevel] = useState<0 | 1 | 2 | 3>(0);      // 已发给老人端的线索等级(只升不降)
  const [savingTurn, setSavingTurn] = useState(false);
  // 当前环节工作态
  const [work, setWork] = useState<WorkState>(emptyWork());
  const [pendingAudio, setPendingAudio] = useState<Record<string, { rawAudioId: string; duration: number }>>({});

  // 刷新恢复:journal.audios 持久有 turnKey→音频映射,重挂载时重建回放条,
  // 否则补录转写建 turn 时 raw_audio_id 静默断链。
  useEffect(() => {
    setPendingAudio((prev) => {
      const next = { ...prev };
      for (const [rid, a] of Object.entries(journal.audios)) {
        if (a.turnKey && !next[a.turnKey]) next[a.turnKey] = { rawAudioId: rid, duration: a.durationSeconds ?? 0 };
      }
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 拉取计划 + 版本三方断言(retryNonce:失败不再是死胡同,可点"重试"重跑)
  useEffect(() => {
    setErr(null);
    resetSession();
    api.sessionPlan(session.session_id, session.week_no, session.event_line)
      .then(async (p) => {
        const bank = await api.itemBank();
        // bundle 可能还没到,尽力断言 plan==后端;bundle 到位后再断言一次
        if (p.item_bank_version_id !== bank.version_id) {
          throw new Error(`题库版本不一致:计划=${p.item_bank_version_id} 后端=${bank.version_id}`);
        }
        setPlan(p);
        postSession({ sessionId: session.session_id, weekNo: session.week_no, eventLine: session.event_line, mode: "task", itemBankVersionId: p.item_bank_version_id });
      })
      .catch((e) => setErr(String(e)));
  }, [session, postSession, resetSession, retryNonce]);

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
  const selfStartOut = selfStart && !(journal.turns[turnK]?.locked ?? false);
  // 老人端麦克风真值(自助开录时操作端唯一感知渠道)
  const patientRec = usePatientRec(session.session_id);
  const patientMicOn = patientRec?.active === true;
  // 麦克风关闭的下降沿启动看门狗:自助路径的停麦后若迟迟等不到 audioSaved(保存失败),
  // 8 秒告警——否则自助模式保存失败对操作端零信号,老人的回答静默丢失。
  const prevMicOn = useRef(false);
  useEffect(() => {
    if (prevMicOn.current && !patientMicOn) watchdog.start();
    prevMicOn.current = patientMicOn;
  }, [patientMicOn, watchdog]);

  // 光标变化 → 重置工作态(从 journal 恢复布尔/已锁/线索级)+ 广播老人端
  useEffect(() => {
    if (!item || !planTurn) return;
    const jt = journal.turns[turnK];
    setWork({
      turnId: jt?.turnId ?? null, asrText: jt?.asrText ?? "",
      // 确认文本预填:优先已存确认,其次 ASR 原文(一场上百环节,不该每次整句重打)
      confirmed: jt?.confirmedText ?? jt?.asrText ?? "", ai: null,
      locked: jt?.locked ?? false, savedAsr: jt?.asrSaved ?? false, savedConfirm: jt?.confirmed ?? false,
    });
    if (recState === "armed") watchdog.start(); // 录音中直接跳题:老人端会自动收尾保存,回报也要有人等
    setRecState("idle");
    // 回访已发过线索的环节:恢复级别而非归零——老人端线索不被清屏,prompt_level 建议不记错
    const restoredCue = Math.min(3, Math.max(0, journal.cueLevels?.[turnK] ?? 0)) as 0 | 1 | 2 | 3;
    setCueLevel(restoredCue);
    setCursor(itemIdx, turnIdx);
    onItemEventChange?.(journal.itemEvents[item.item_id]?.itemEventId ?? null);
    postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel: restoredCue, recording: "idle", recSeq: recSeq.current,
                 selfStart: selfStart && !(jt?.locked ?? false) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [itemIdx, turnIdx, plan]);

  // 录音资格判定变化即补发游标:落地(→allowed/unrated/denied)给按钮,重查(→loading)收回按钮。
  // loading 也要发——资格重查期间老人端保留旧 selfStart=true 按钮就违反 fail-closed。
  useEffect(() => {
    if (!planTurn) return;
    postCursor({ screen: recState === "armed" ? "record" : "present", itemIdx, turnIdx,
                 responseRole: planTurn.response_role, cueLevel,
                 recording: recState === "armed" ? "armed" : "idle", recSeq: recSeq.current, selfStart: selfStartOut });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recStatus]);

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
  useEffect(() => { if (err) withdrawRef.current(); }, [err]);

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
    if (!isCurrent) return;
    watchdog.clear();
    if (recState === "armed" && planTurn) {
      // 老人自己按"我说好了"停的:把 idle 写回镜像/服务端真值源,否则 armed 残留会让老人端刷新后自动开麦。
      postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel, recording: "idle", recSeq: recSeq.current, selfStart: selfStartOut });
    }
    setRecState("idle");
    if (isReplay) return;
    toast(`老人端录音已保存(${m.durationSeconds.toFixed(1)}s)`, "info");
    // 自动推进:只认"当前环节"的首次回报;老人端由此获得"点开始→说→我说好了→下一题"
    // 的自走闭环,转写/锁分可事后回访补。研究者手上有在途操作/未保存的转写或确认改动时
    // 不跳——advance 会按 journal 重置工作卡,未存的字会被冲掉。
    if (!autoAdvance) return;
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
    if (!plan || !item) return;
    if (turnIdx + 1 < item.turns.length) setTurnIdx(turnIdx + 1);
    else if (itemIdx + 1 < plan.items.length) { setItemIdx(itemIdx + 1); setTurnIdx(0); }
    else toast("已到最后一个环节,可前往场次收尾", "ok");
  };
  const retreat = () => {
    if (!plan) return;
    if (turnIdx > 0) setTurnIdx(turnIdx - 1);
    else if (itemIdx > 0) {
      const prev = plan.items[itemIdx - 1];
      setItemIdx(itemIdx - 1);
      setTurnIdx(Math.max(0, prev.turns.length - 1));
    }
  };
  const atFirstTurn = itemIdx === 0 && turnIdx === 0;
  const atLastTurn = !!plan && !!item && turnIdx + 1 >= item.turns.length && itemIdx + 1 >= plan.items.length;

  // 示意老人端录音(VOX):arm→老人端自动开始录音;stop→老人端停止并保存后回传 audioSaved。
  // 携带当前 cueLevel,录音启停不清老人端已显示的线索。
  const armRecording = () => {
    if (!planTurn) return;
    recSeq.current += 1; // 每次 arm 新序号:老人自停后 armed→armed 重发才能重触发老人端
    setRecState("armed");
    postCursor({ screen: "record", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel, recording: "armed", recSeq: recSeq.current, selfStart: selfStartOut });
  };
  const stopRecording = () => {
    if (!planTurn) return;
    setRecState("idle");
    watchdog.start();
    postCursor({ screen: "record", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel, recording: "stopped", recSeq: recSeq.current, selfStart: selfStartOut });
  };

  // 发分级线索给老人端(0→3 只升不降;线索文本永远取自版本锁定题库,操作端不产话术)。
  const sendCue = (level: 1 | 2 | 3) => {
    if (!planTurn || level <= cueLevel) return;
    setCueLevel(level);
    recordCueLevel(turnK, level); // 持久化:回访本环节时恢复,不清老人端线索
    postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role,
                 cueLevel: level, recording: recState === "armed" ? "armed" : "idle", recSeq: recSeq.current, selfStart: selfStartOut });
  };

  async function ensureItemEvent(): Promise<number | null> {
    if (!item) return null;
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
    if (!au || busyOp) return;
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
    if (!item || !planTurn) return;
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
        postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel, recording: "idle", recSeq: recSeq.current, selfStart: false });
      }
      toast("已锁定评分", "ok");
      return true;
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); return false; }
    finally { setLocking(false); }
  }

  if (err) return <FailClosed msg={err} onRetry={() => setRetryNonce((n) => n + 1)} onExit={onExit} />;
  if (!plan) return <p>加载会话计划…</p>;
  if (plan.items.length === 0) return <p>本场次无评分题(第 1 周应走关系建立控制台)。</p>;

  return (
    <div className="row" style={{ alignItems: "flex-start", gap: "var(--sp-5)" }}>
      <ItemRail plan={plan} itemIdx={itemIdx} lockedCount={countLocked(journal, plan)}
        onPick={(i) => { setItemIdx(i); setTurnIdx(0); }} journalTurns={journal.turns} />

      <div className="col grow" style={{ maxWidth: 720 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>训练判分 · {item?.item_id}</h2>
          <div className="row">
            <label className="row" style={{ gap: 6 }}>评分人
              <TextInput value={reviewerId} onChange={(e) => setReviewerId(e.target.value)} style={{ width: 90 }} />
            </label>
            <label className="row" style={{ gap: 6 }}>
              <input type="checkbox" checked={aiEnabled} onChange={(e) => setAiEnabled(e.target.checked)} /> 动态 AI 初评
            </label>
            <label className="row" style={{ gap: 6 }} title="老人端点'我说好了'后自动跳下一环节;关掉则手动逐环节推进">
              <input type="checkbox" checked={autoAdvance} onChange={toggleAutoAdvance} /> 收音自动推进
            </label>
            {/* 收尾前必先停录:否则本屏卸载后无人发 idle,老人端麦克风持续开着 */}
            <Button variant="ghost" onClick={() => { if (recState === "armed") stopRecording(); onWrapup(); }}>场次收尾 →</Button>
          </div>
        </div>

        {item && planTurn && (
          <div className="card col">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <div><StatusPill tone="primary">{item.task_type}</StatusPill> 环节 {planTurn.turn_seq}/{item.turns.length} · <strong>{planTurn.response_role}</strong></div>
              <div className="row" style={{ gap: 8 }}>
                {work.locked && <StatusPill tone="ok">已锁定</StatusPill>}
                {/* 常驻导航:每个环节都能一键前进/回退(老人端随游标同步),不必等锁分或翻左侧题列 */}
                <Button onClick={retreat} disabled={atFirstTurn}>← 上一环节</Button>
                <Button onClick={advance} disabled={atLastTurn}>下一环节 →</Button>
              </div>
            </div>

            <ItemReference item={item} bundle={bundle} />

            {/* 分级线索(0→3 只升不降;内容取版本锁定题库,推老人端显示) */}
            {!work.locked && (
              <CueButtons bundle={bundle} itemId={item.item_id} taskType={item.task_type}
                role={planTurn.response_role} cueLevel={cueLevel} onSend={sendCue} />
            )}

            {/* 录音示意(老人端 VOX)——fail-closed:资格未确认不放行 */}
            <div className="row wrap" style={{ alignItems: "center" }}>
              {recStatus === "denied" ? (
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
                  <Button variant="danger" onClick={stopRecording}>■ 停止录音</Button>
                  {patientMicOn && recState !== "armed" && (
                    <StatusPill tone="danger">老人端麦克风开着(自助开录)</StatusPill>
                  )}
                </>
              ) : (
                <>
                  <Button onClick={armRecording} disabled={work.locked}>● 示意老人录音</Button>
                  {recStatus === "unrated" && <span className="muted">录音资格未评(按允许处理)</span>}
                </>
              )}
              {pendingAudio[turnK] && (
                <>
                  <StatusPill tone="ok">已收到录音 {pendingAudio[turnK].duration.toFixed(1)}s</StatusPill>
                  <audio controls preload="none" src={api.audioBlobUrl(pendingAudio[turnK].rawAudioId)} style={{ height: 36 }} />
                  {!work.savedAsr && <Button onClick={tryLocalAsr} disabled={busyOp !== null}>{busyOp === "asr" ? "转写中…" : "本地 ASR 转写"}</Button>}
                </>
              )}
            </div>

            {/* 阶段A 转写 */}
            <Field label="① ASR 转写(asr_text,保存即冻结,不覆盖)">
              <TextInput value={work.asrText} disabled={work.savedAsr}
                onChange={(e) => setWork((w) => ({ ...w, asrText: e.target.value }))}
                placeholder={pendingAudio[turnK] ? "老人端已录音,可回放后键入识别文本" : "键入听到/识别的回答"} />
            </Field>
            {!work.savedAsr && <Button onClick={saveTranscription} disabled={savingTurn}>{savingTurn ? "保存中…" : "保存转写"}</Button>}

            {/* 阶段B 确认 */}
            {work.savedAsr && (
              <>
                <div className="row">
                  <div className="grow"><div className="muted">asr_text(原文只读)</div><div className="card mono" style={{ background: "var(--c-diff)" }}>{work.asrText || "（空）"}</div></div>
                </div>
                <Field label="② 确认文本 confirmed(可改写,不动原文;已预填 ASR 原文)">
                  <TextInput value={work.confirmed} disabled={work.locked}
                    onChange={(e) => setWork((w) => ({ ...w, confirmed: e.target.value, savedConfirm: false }))} placeholder="人工校正后的规范文本" />
                </Field>
                {!work.locked && (
                  <div className="row">
                    <Button onClick={() => void saveConfirm()} disabled={busyOp !== null || work.savedConfirm}>
                      {busyOp === "confirm" ? "保存中…" : work.savedConfirm ? "✓ 已确认" : "保存确认"}
                    </Button>
                    {!work.savedConfirm && work.turnId != null && <span className="muted">有未保存修改</span>}
                  </div>
                )}
              </>
            )}

            {/* 阶段C AI 初评 */}
            {work.savedAsr && aiEnabled && (
              <div className="card" style={{ background: "var(--c-bg)" }}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <strong>③ AI 初评(仅辅助,永不锁分;保存确认后自动运行)</strong>
                  <Button onClick={() => void runAiJudge()} disabled={work.locked || busyOp !== null}>{busyOp === "ai" ? "评估中…" : "重新初评"}</Button>
                </div>
                {work.ai && (
                  <div className="row wrap">
                    <StatusPill tone="muted">{work.ai.answerType ?? "纯人工(无确定式口径)"}</StatusPill>
                    <span>初评分:{work.ai.score ?? "—"}</span>
                    {work.ai.needsReview && <StatusPill tone="warn">需人工复核</StatusPill>}
                  </div>
                )}
              </div>
            )}
            {work.savedAsr && !aiEnabled && <p className="muted">③ 动态 AI 初评已关闭 → 纯人工从零打分。</p>}

            {/* 阶段D 人工锁分 */}
            {work.savedAsr && !work.locked && (
              <LockControl key={turnK} taskType={item.task_type} role={planTurn.response_role}
                suggestedPromptLevel={cueLevel} onLock={doLock} locking={locking}
                basis={<ItemReference item={item} bundle={bundle} />}
                confirmedText={work.confirmed}
                unsavedConfirm={Boolean(work.confirmed.trim()) && !work.savedConfirm} />
            )}
            {work.locked && <div className="row"><StatusPill tone="ok">✓ 本环节锁定分已写入</StatusPill><Button variant="primary" onClick={advance}>下一环节 →</Button></div>}
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

// ---------------- 左：题目游标列 ----------------
function ItemRail({ plan, itemIdx, lockedCount, onPick, journalTurns }: {
  plan: SessionPlan; itemIdx: number; lockedCount: number;
  onPick: (i: number) => void; journalTurns: Record<string, { locked: boolean }>;
}) {
  return (
    <div className="item-rail">
      <div className="muted">进度 {lockedCount}/{plan.items.length} 题全锁</div>
      <div className="item-rail-list">
        {plan.items.map((it, i) => {
          const total = it.turns.length;
          const locked = it.turns.filter((t) => journalTurns[`${it.item_id}#${t.turn_seq}`]?.locked).length;
          const allLocked = locked === total;
          return (
            <button key={it.item_id} onClick={() => onPick(i)}
              style={{ textAlign: "left", padding: "6px 10px", borderRadius: 8,
                border: i === itemIdx ? "2px solid var(--c-primary)" : "1px solid var(--c-line)",
                background: i === itemIdx ? "var(--c-warn-bg)" : "var(--c-surface)" }}>
              <span style={{ marginRight: 6 }}>{allLocked ? "📌" : "📍"}</span>
              {it.item_id} {total > 1 && <span className="muted">({locked}/{total})</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ---------------- 题目参考(判分依据,零画像) ----------------
function ItemReference({ item, bundle }: { item: PlanItem; bundle: ReturnType<typeof useItemBankBundle>["bundle"] }) {
  if (!bundle) return null;
  if (item.task_type === "单要素") {
    const s = findSingle(bundle, item.item_id);
    if (!s) return null;
    return <div className="muted" style={{ fontSize: "0.9em" }}>目标词:<strong>{s.target_word}</strong> · 可接受:{s.acceptable_expressions.join("、") || "—"}</div>;
  }
  if (item.task_type === "双要素") {
    const d = findDouble(bundle, item.item_id);
    if (!d) return null;
    return (
      <div className="muted" style={{ fontSize: "0.9em" }}>
        左:<strong>{d.left_word}</strong> · 右:<strong>{d.right_word}</strong> · 关系线索:{d.relation_cue}
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
      ? [{ level: 1, label: "发线索1" }, { level: 2, label: "发线索2" }, { level: 3, label: "告知答案" }]
      : taskType === "双要素" && (role.includes("作用") || role === "关系识别")
        ? [{ level: 1, label: "发线索" }]
        : [];
  if (levels.length === 0) return null;
  const current = lookupCue(bundle, itemId, taskType, role, cueLevel);
  const tellText = lookupCue(bundle, itemId, taskType, role, 3);
  return (
    <div className="card col" style={{ background: "var(--c-bg)", gap: "var(--sp-2)" }}>
      <div className="row wrap">
        <strong>线索(当前 {cueLevel} 级)</strong>
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
      {current && <p className="muted" style={{ fontSize: "0.9em" }}>老人端正显示:{current}</p>}
      <ConfirmDialog open={confirmTell} title="告知答案?"
        body={`老人端将显示并朗读:「${tellText ?? ""}」。这是最高提示级,发出后本环节提示等级不可再降。`}
        confirmLabel="告知答案"
        onConfirm={() => { setConfirmTell(false); onSend(3); }}
        onCancel={() => setConfirmTell(false)} />
    </div>
  );
}

// ---------------- 按角色切换的锁分控件 ----------------
function LockControl({ taskType, role, suggestedPromptLevel = 0, onLock, locking = false, basis, confirmedText, unsavedConfirm }: {
  taskType: string; role: string; suggestedPromptLevel?: number;
  onLock: (v: number, promptLevel?: number) => Promise<boolean>;
  locking?: boolean; basis?: React.ReactNode; confirmedText?: string; unsavedConfirm?: boolean;
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
    <div className="card col" style={{ background: "var(--c-bg)" }}>
      <strong>④ 人工锁分(研究数据真值 · 一旦锁定不可改)</strong>
      {single && (
        <Field label="提示等级 prompt_level(0 自发 / 1 轻提示 / 2 明确或语音 / 3 告知答案)">
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
    </div>
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
    <div onClick={onCancel} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.4)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 950 }}>
      <div className="card col fade-in" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 500 }}>
        <h3>确认锁定</h3>
        <p>环节「{role}」→ 锁定为 <strong>{label}</strong>{promptLevel != null ? ` · 提示等级 ${promptLevel}` : ""}。</p>
        {/* 判分依据摆在眼前,不用取消弹窗滚回去翻 */}
        {basis && <div className="card" style={{ background: "var(--c-bg)", padding: "var(--sp-3)" }}>{basis}</div>}
        {confirmedText?.trim() && <p className="muted">确认文本:「{confirmedText}」</p>}
        {unsavedConfirm && <p style={{ color: "var(--c-warn)" }}>确认文本有未保存修改:锁定时将自动保存这一版。</p>}
        <p className="muted">锁定后本环节不可再改 confirmed / 不可重复锁。</p>
        <div className="row" style={{ justifyContent: "flex-end" }}>
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
    <div className="card col" style={{ background: "var(--c-danger-bg)", border: "2px solid var(--c-danger)", color: "var(--c-danger)", maxWidth: 720 }}>
      <h3>训练屏 fail-closed</h3>
      <p>{msg}</p>
      <p className="muted">题库版本不一致或计划加载失败时,拒开训练以保护数据完整性。后端恢复后点重试即可,不必换受试者。</p>
      <div className="row">
        {onRetry && <Button variant="primary" onClick={onRetry}>重试</Button>}
        {onExit && <Button onClick={onExit}>← 返回建场次</Button>}
      </div>
    </div>
  );
}
