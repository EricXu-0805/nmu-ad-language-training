import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  type TechnicalPauseDraft,
  type TechnicalPauseRequest,
} from "../../api";
import { Alert } from "../../components/Alert";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { EnumSelect, Field, TextInput } from "../../components/Field";
import { StatusPill } from "../../components/StatusPill";
import { useToast } from "../../components/ToastContext";
import { useDialogFocusTrap } from "../../components/useDialogFocusTrap";
import { assertVersionsMatch, findDouble, findSingle, lookupCue, resolveFeedbackLine, useAutopilotProtocol, useItemBankBundle, type FbKey } from "../../content/bundle";
import { isBoundJournalTurnKey, useSessionJournal } from "../../hooks/useSessionJournal";
import { useSessionRuntime } from "../../hooks/useSessionRuntime";
import { shouldAutoRouteTrainingToWrapup } from "../../sessionLifecycle";
import { flushLiveWrites, useAudioSaved, useCursorWriter, usePatientRec, useSaveWatchdog } from "../../sync/useCursorWriter";
import { isPatientRecFailure } from "../../sync/messages";
import type { PatientPresenceView } from "../../sync/usePatientPresence";
import { formatRecordingClock, watchdogSecondsLeft } from "../../recordingClock";
import {
  buildExactTechnicalPauseRequest,
  exactTechnicalPauseRequestMatchesDraft,
  reconcilePendingTechnicalPauseForTakeover,
  technicalPauseDispositionIsUncertain,
} from "../../sync/technicalPauseRequest";
import type { AttemptEvent, AttemptProcessRequest, PlanItem, PlanTurn, Session, SessionPlan } from "../../types";
import { AuthenticatedAudio } from "../AuthenticatedAudio";
import { SessionControlBar } from "../SessionControlBar";
import { SessionAbortControl } from "../SessionAbortControl";
import { autopilotFailureKindForErrorCode, cueTypeForPrompt, decideAttemptProcessResult } from "./attemptEvidence";
import { answerTimeoutAction, canReleaseAutopilotFailure, makeAutopilotFailure, type AutopilotFailure, type AutopilotFailureKind } from "./autopilotSafety";
import { decideAudioSavedAutomation, retireLegacyAudioSavedAutoAdvancePreference } from "./audioSavedProgressSafety";
import { bedsideOperationIsCurrent, captureBedsideOperation, type BedsideOperationFence } from "./bedsideOperationFence";
import { ServerAutopilotControl } from "./ServerAutopilotControl";
import { ObserverConsole } from "./ObserverConsole.tsx";
import {
  manualResyncRequired,
  manualSurfaceLocked,
  observerAuthoritativePosition,
  observerResyncResultCurrent,
  type ManualResyncStatus,
  type ObserverResyncFence,
} from "./observerConsoleModel.ts";
import { deriveOwnershipTransition, runManualResyncTransaction } from "./observerResync.ts";
import type { AutopilotConsoleState } from "../../autopilot/startControl";
import {
  hasExactWeek2Single20Profile,
  operationalAutopilotReadyForSession,
} from "../../autopilot/demoProfile.ts";
import { assertPortraitFree, isRelationRole } from "./vm";

const AUTOPILOT_FAILURE_KINDS = new Set<AutopilotFailureKind>([
  "microphone", "audio", "upload", "asr", "classifier", "persistence",
]);

function autopilotFailureStorageKey(sessionId: string): string {
  return `nmu:console:apFailure:${sessionId}`;
}

function loadAutopilotFailure(sessionId: string): AutopilotFailure | null {
  try {
    const raw = localStorage.getItem(autopilotFailureStorageKey(sessionId));
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<AutopilotFailure>;
    if (!AUTOPILOT_FAILURE_KINDS.has(value.kind as AutopilotFailureKind)
        || typeof value.message !== "string"
        || typeof value.itemIdx !== "number"
        || typeof value.turnIdx !== "number"
        || typeof value.cueLevel !== "number") return null;
    return value as AutopilotFailure;
  } catch {
    return null;
  }
}

// week≥2 判分主屏:左题目游标列 + 右环节工作卡(转写→确认→AI初评→锁分)。
// 唯一游标写者,推进老人端;★本文件及本目录禁 import 任何画像源(oxlint 守卫 + vm 运行时断言)。
export function TrainingConsoleScreen({ session, hasNamedAccount, presence, onWrapup, onExit, onItemEventChange }: {
  session: Session; hasNamedAccount: boolean;
  // 由 ConsoleShell 的既有 usePatientPresence 轮询传入,本屏不新增网络轮询器。
  presence: PatientPresenceView;
  onWrapup: () => void; onExit?: () => void; onItemEventChange?: (id: number | null) => void;
}) {
  const toast = useToast();
  const exactDemoProfile = hasExactWeek2Single20Profile(session);
  const { bundle } = useItemBankBundle(session.week_no);
  const { journal, upsertItem, upsertTurn, upsertAudio, recordCueLevel, setCursor, hydrateFromServer } = useSessionJournal(session.session_id);
  const {
    postSession,
    postCursor,
    commitInteractionPresentation,
    beginSafetyPause,
    releaseSafetyPause,
    resetSession,
    syncError,
    retrySync,
  } = useCursorWriter(session.session_id);
  const runtimeControl = useSessionRuntime(session.session_id);
  const paused = runtimeControl.paused;
  const [pausePending, setPausePending] = useState(false);
  const [wrapupPending, setWrapupPending] = useState(false);
  const [recoveredLabel, setRecoveredLabel] = useState<string | null>(null);
  const recoveredOnce = useRef(false);
  const restoreTarget = useRef<{ itemIdx: number; turnIdx: number } | null>(null);
  const lastAppliedTurnK = useRef<string | null>(null);
  const handshakeSent = useRef(false);
  const interactionPresentationKeys = useRef<Map<string, string>>(new Map());
  const pendingTechnicalPause = useRef<TechnicalPauseRequest | null>(null);
  const pauseOperationInFlight = useRef(false);
  const stablePresentationKey = (operationKey: string): string => {
    const existing = interactionPresentationKeys.current.get(operationKey);
    if (existing) return existing;
    const uuid = globalThis.crypto?.randomUUID?.();
    if (!uuid) throw new Error("当前浏览器不能安全生成原子呈现幂等键");
    const key = `presentation:${uuid}`;
    interactionPresentationKeys.current.set(operationKey, key);
    return key;
  };
  const autoWrapupDispatched = useRef(false);
  useEffect(() => () => {
    // An exact request belongs only to this mounted session. The server keeps
    // its durable receipt; a new session must never inherit or mutate the body.
    pendingTechnicalPause.current = null;
    pauseOperationInFlight.current = false;
  }, [session.session_id]);

  const [plan, setPlan] = useState<SessionPlan | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [retryNonce, setRetryNonce] = useState(0);
  const [journalLoading, setJournalLoading] = useState(true);
  const [journalRecoveryError, setJournalRecoveryError] = useState<string | null>(null);
  const [recoveryApplied, setRecoveryApplied] = useState(false);
  const [itemIdx, setItemIdx] = useState(journal.cursor.itemIdx);
  const [turnIdx, setTurnIdx] = useState(journal.cursor.turnIdx);
  useEffect(() => retireLegacyAudioSavedAutoAdvancePreference(localStorage), []);
  // 自动驾驶:AI 听→判→固定话术反馈→提示升级→推进;人工随时可关(接管)。默认关。
  // 旧的浏览器本地自动驾驶不再从持久化开关恢复。服务器 P0a 必须由研究者
  // 在当前模拟场次显式启动；否则刷新会在无人确认时重新成为第二驾驶员。
  const [autoPilot, setAutoPilot] = useState(false);
  const [serverOwnership, setServerOwnership] = useState<{
    owned: boolean;
    phase: AutopilotConsoleState["phase"];
  }>(() => {
    const mustRecoverServerOwner = session.is_simulation === true
      && session.data_classification === "simulation";
    return {
      owned: mustRecoverServerOwner,
      phase: mustRecoverServerOwner ? "checking" : "idle",
    };
  });
  // 所有权回调可能在 React 提交这次 state 更新前就被再次调用（甚至在同一个
  // tick 里被批处理），而重同步事务的每个异步边界都要核对"此刻"的所有权——ref
  // 是唯一能保证时序的读取源，不能只靠下一次渲染才跑的 effect。
  const ownershipRef = useRef(serverOwnership);
  const manualResyncStatusRef = useRef<ManualResyncStatus>("idle");
  const [manualResync, setManualResyncState] = useState<{ status: ManualResyncStatus; error: string | null }>({ status: "idle", error: null });
  const setManualResync = useCallback((next: { status: ManualResyncStatus; error: string | null }) => {
    manualResyncStatusRef.current = next.status;
    setManualResyncState(next);
  }, []);
  // 是否发生过自动化暴露（服务器证实拥有过 / start 写已发出 / 写后状态不明）。
  // 初次挂载的只读 checking 询问不算：它得到明确 no-owner 时走既有恢复门。
  const automationExposure = useRef(false);
  const resyncFence = useRef<ObserverResyncFence>({ sessionId: session.session_id, epoch: 0 });
  // 释放后是否需要重同步，由回调同步 latch，effect 只负责消费——不能用
  // "上一次渲染看到的 owned" 判定下降沿：同一个 tick 内先 true 后 false 会被
  // React 合并成一次渲染，那时已经看不到中间的 true 了。
  const releaseNeedsResync = useRef(false);
  const planRef = useRef<SessionPlan | null>(null);
  planRef.current = plan;
  const onServerOwnershipChange = useCallback((owned: boolean, phase: AutopilotConsoleState["phase"]) => {
    const next = { owned, phase };
    const previousOwned = ownershipRef.current.owned;
    ownershipRef.current = next;
    const transition = deriveOwnershipTransition(
      automationExposure.current,
      next,
      manualResyncStatusRef.current === "idle",
    );
    automationExposure.current = transition.automationExposure;
    if (transition.invalidateResync) {
      // 服务器重新证明持有：同步作废在途/失败的重同步，防止旧 fence 下的
      // Promise 抢在下一次渲染跑到 effect 之前，就把人工面板重挂出来。
      resyncFence.current = { ...resyncFence.current, epoch: resyncFence.current.epoch + 1 };
      releaseNeedsResync.current = false;
      setManualResync({ status: "idle", error: null });
    } else if (manualResyncStatusRef.current === "idle"
        && manualResyncRequired(previousOwned, next, transition.automationExposure)) {
      // previousOwned 来自"上一次调用"时的 ref 快照，不是渲染批处理后才能看到
      // 的最终 state——同一个 tick 内先 true 后 false 也能正确捕捉下降沿。真正
      // 发起请求交给下面的 effect（那里才有这一次渲染最新的闭包）；这里先同步
      // latch 住 pending，防止本 tick 内紧接着又出现的重新持有被当成"仍是
      // idle，可以放行"。
      releaseNeedsResync.current = true;
      setManualResync({ status: "pending", error: null });
    }
    setServerOwnership(next);
  }, [setManualResync]);
  // 从 owned/uncertain 回到明确 no-owner 后的安全重同步：先证明服务器确实已经
  // 放手，再取当前精确 session 的最新 runtime + journal，全部核验通过后才用
  // 服务端 cursor 恢复人工位置；完成前人工面板不重挂，失败保持锁定可重试。全程
  // 不写 cursor——重挂后的既有效应才会补发游标。事务本身是注入依赖的纯编排
  // （observerResync.ts），这里只接线,并在最终 apply 前再核一次 fence/所有权。
  const runManualResync = async () => {
    // 所有权 ref 已经同步更新过：如果服务器已经重新证明持有，直接不发任何
    // status/runtime/journal 请求，人工面板继续锁定。
    if (ownershipRef.current.owned) return;
    const fence: ObserverResyncFence = {
      sessionId: session.session_id,
      epoch: resyncFence.current.epoch + 1,
    };
    resyncFence.current = fence;
    setManualResync({ status: "pending", error: null });
    const guard = {
      isLive: () => observerResyncResultCurrent(fence, resyncFence.current) && !ownershipRef.current.owned,
    };
    const outcome = await runManualResyncTransaction(fence.sessionId, guard, {
      fetchStatus: (sid) => api.autopilotStatus(sid),
      fetchRuntime: (sid) => api.getSessionRuntime(sid),
      fetchJournal: (sid) => api.sessionJournal(sid),
      getPlan: () => planRef.current,
      // 背景轮询与这里的直读共用同一个 hook 内的 per-session 单调下限：poll/
      // pause/resume 的回执在各自的 Promise continuation 里已经同步棘轮过，
      // 不依赖这次渲染是否已经跑过；直读到的 revision 也通过 reportRevision
      // 汇入同一份，而不是这个组件另开一份自己的、要等渲染才会更新的副本。
      getRuntimeRevisionFloor: () => runtimeControl.getRevisionFloor(),
      reportRuntimeRevision: (revision) => runtimeControl.reportRevision(fence.sessionId, revision),
    });
    if (outcome.kind === "stale") return;
    if (outcome.kind === "failed") {
      if (!guard.isLive()) return;
      setManualResync({ status: "failed", error: outcome.error instanceof ApiError ? outcome.error.detail : String(outcome.error) });
      return;
    }
    // 最终 apply 前再核一次 fence 与所有权：事务内部的最后一次核对与这里恢复
    // 执行之间仍隔着一次微任务调度，必须再确认一次才允许 hydrate。同样的道理，
    // 事务里对 revision 下限的最后一次核对，与这里的续行之间也隔着一次微任务
    // 调度——期间背景轮询完全可能把下限推得更高，所以这里要用当下最新的下限
    // 再核一次这次读到的 runtimeRevision，而不是只信事务内部那一次核对。
    if (!guard.isLive()) return;
    if (outcome.runtimeRevision < runtimeControl.getRevisionFloor()) {
      setManualResync({ status: "failed", error: "服务器返回的运行时版本落后于本页已知版本，拒绝据此恢复" });
      return;
    }
    hydrateFromServer(outcome.journal);
    setItemIdx(outcome.cursor.itemIdx);
    setTurnIdx(outcome.cursor.turnIdx);
    restoreTarget.current = outcome.cursor;
    lastAppliedTurnK.current = null; // 重挂时按最新 journal 重建工作卡
    setRecoveredLabel(`第 ${outcome.cursor.itemIdx + 1} 题 · 第 ${outcome.cursor.turnIdx + 1} 环节`);
    setManualResync({ status: "idle", error: null });
  };
  // 观察台锁定/解锁的唯一协调点。换场先清空观察台/重同步瞬时状态（fence 同时
  // 作废 A 场次的迟到结果）；释放且需要重同步的判定已经在 onServerOwnershipChange
  // 里逐次同步完成（releaseNeedsResync 是那边 latch 的事实），这里只负责在
  // session 未变时把它消费掉、真正发起请求——那时才有这一次渲染最新的闭包。
  useEffect(() => {
    if (resyncFence.current.sessionId !== session.session_id) {
      resyncFence.current = { sessionId: session.session_id, epoch: resyncFence.current.epoch + 1 };
      automationExposure.current = false;
      // 子组件对新场次的 (owned, phase) 上报可能晚一拍；先按锁定处理，
      // 等新场次的真实上报到达后再走正常判定。
      ownershipRef.current = { owned: true, phase: "checking" };
      releaseNeedsResync.current = false;
      if (manualResync.status !== "idle") setManualResync({ status: "idle", error: null });
      return;
    }
    if (releaseNeedsResync.current) {
      releaseNeedsResync.current = false;
      void runManualResync();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [manualResync.status, serverOwnership, session.session_id]);
  // D3:自动带练期间的权威位置只来自状态回执(服务端自动推进不写 live cursor);
  // 观察面与页首都用它,runtime cursor 只作无回执时的回退。
  const [apReceiptPosition, setApReceiptPosition] =
    useState<{ itemId: string; turnSeq: number } | null>(null);
  const onAutopilotReceiptPosition = useCallback(
    (position: { itemId: string; turnSeq: number } | null) => {
      setApReceiptPosition((current) => (
        current?.itemId === position?.itemId && current?.turnSeq === position?.turnSeq
          ? current : position
      ));
    }, []);
  const [operationalAutopilotReady, setOperationalAutopilotReady] = useState<boolean | null>(null);
  const [unsupportedOperationalPositions, setUnsupportedOperationalPositions] = useState<string[]>([]);
  const [operationalGapSummary, setOperationalGapSummary] = useState({
    rubric: 0,
    protocol: 0,
    sourceField: 0,
    unstructured: 0,
    sourceTotal: 0,
  });
  const autoPilotRef = useRef(autoPilot);
  autoPilotRef.current = autoPilot;
  const [apFailure, setApFailure] = useState<AutopilotFailure | null>(() => loadAutopilotFailure(session.session_id));
  const apFailureLatched = useRef(apFailure !== null);
  const apManualTakeoverTurn = useRef<string | null>(null);
  const failAutopilotRef = useRef<(
    kind: AutopilotFailureKind,
    message: string,
    evidence?: { errorCode?: string; attemptId?: number; alreadyRecorded?: boolean },
  ) => void>(() => {});
  const { protocol } = useAutopilotProtocol();
  const apTimers = useRef<{ arm?: ReturnType<typeof setTimeout>; answer?: ReturnType<typeof setTimeout>; act?: ReturnType<typeof setTimeout> }>({});
  const apCue1Path = useRef<"unknown" | "close" | "silence" | null>(null); // 第1次提示的进入路径(成功反馈按来路选变体,文档口径)
  const apBusy = useRef(false);
  const apLastRound = useRef<{
    rawAudioId: string;
    duration: number;
    attemptId: number;
    asrText: string;
    answerType: string;
    score: number | null;
    needsReview: boolean;
    containsTarget: boolean;
  } | null>(null);
  // 本轮是否仍在等回答:arm 后置真,收到本轮 audioSaved 后置假；答窗到点只负责收麦，
  // 仍须等待真实 audioSaved，防止把上传故障误判为沉默。
  // ★没有 VAD——"麦克风开了"(patientMicOn)不等于"老人在说话",故绝不用它当作答信号;
  // 用它做去重闸:自动收麦后迟到的旧录音回报(apAwaitingAnswer 已假)不再驱动状态机。
  const apAwaitingAnswer = useRef(false);
  const apMicSeenActive = useRef(false);
  // 录音资格 fail-closed:确认拿到患者档案前不允许 arm(获取失败若默认放行,
  // recording_allowed=false 的受试者会被开麦——合规护栏静默失效)。
  const [recStatus, setRecStatus] = useState<"loading" | "error" | "denied" | "allowed">("loading");
  // 真实研究只接受明确允许；只有显式模拟场次可在未评状态下演练录音链路。
  // 开麦真值只认服务端完整授权（同意/撤回/准入/场次状态）；
  // 模拟场次是否允许也由同一端点判定，前端不再自行放宽。
  const recordingEligible = recStatus === "allowed";
  const selfStart = recordingEligible;
  const [recState, setRecState] = useState<"idle" | "armed">("idle");
  // P0-3:老人端在场状态是录音入口的前置事实——不在线时示意录音必然落空,
  // 按钮禁用并给可见原因,不再让研究者对着空气录 18 秒。
  const patientLinkDown = presence.state === "offline" || presence.state === "unseen";
  // D5②:患者暂停闩未解开时患者端锁在暂停屏,此刻开录只会静默等到看门狗过期。
  const patientScreenPaused = presence.state === "online" && presence.screen === "paused";
  // P1-9:录音可见计时(arm 起计,回执/超时清零)与看门狗屏上倒计时。
  const [recArmedAt, setRecArmedAt] = useState<number | null>(null);
  const [watchdogUntil, setWatchdogUntil] = useState<number | null>(null);
  const [, setClockTick] = useState(0);
  useEffect(() => {
    if (recArmedAt === null && watchdogUntil === null) return;
    const timer = window.setInterval(() => setClockTick((n) => n + 1), 500);
    return () => window.clearInterval(timer);
  }, [recArmedAt, watchdogUntil]);
  const patientRec = usePatientRec(session.session_id);
  // 计时器与 armed 态同生共死:任何一处把 recState 收回 idle,计时即归零。
  useEffect(() => { if (recState !== "armed") setRecArmedAt(null); }, [recState]);
  const patientDeviceFailure = isPatientRecFailure(patientRec)
    && patientRec.sessionId === session.session_id ? patientRec : null;
  const recSeq = useRef(0);
  const watchdog = useSaveWatchdog(() => {
    setWatchdogUntil(null);
    setRecArmedAt(null);
    if (autoPilotRef.current) {
      failAutopilotRef.current("upload", "8 秒未收到录音保存回报，可能是麦克风、音频保存或上传失败。");
    } else {
      toast("8 秒未收到老人端录音回报——可能是停止太快（老人端还没开始录），或麦克风权限/网络问题。请检查老人端后重新示意录音。", "danger");
    }
  });
  // 看门狗守的是"哪个环节的回报":录音中跳题/暂停时守的是旧环节,回报到达时若按
  // "是否当前环节"判清,旧环节的成功回报清不掉它——保存明明成功还弹假警报。
  const watchdogFor = useRef<string | null>(null);
  const lastArmedTurnK = useRef<string | null>(null);
  const handledDeviceFailureId = useRef<string | null>(null);
  const cueWritePending = useRef(false);
  const bedsideOperationEpoch = useRef(0);
  const bedsideOperationBlocked = useRef(true);
  const armWatchdog = (tk: string | null) => {
    watchdogFor.current = tk;
    setWatchdogUntil(Date.now() + 8000);
    watchdog.start();
  };
  const [cueLevel, setCueLevel] = useState<0 | 1 | 2 | 3>(0);      // 已发给老人端的线索等级(只升不降)
  const [savingTurn, setSavingTurn] = useState(false);
  // 当前环节工作态
  const [work, setWork] = useState<WorkState>(emptyWork());
  const confirmationRequestRef = useRef<{
    turnId: number;
    expectedRevision: number;
    text: string;
    idempotencyKey: string;
  } | null>(null);
  const [pendingAudio, setPendingAudio] = useState<Record<string, { rawAudioId: string; duration: number }>>({});
  const [pendingAttempts, setPendingAttempts] = useState<Record<string, AttemptEvent>>({});

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
    setOperationalAutopilotReady(null);
    setUnsupportedOperationalPositions([]);
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
        // The account plan response is the runtime authority for the resolved
        // scope.  A paired-null session still reports canonical whole-source
        // readiness, while the exact demo profile reports only its frozen 20
        // positions.  The item-bank-wide 60 gaps therefore cannot mask a
        // valid demo projection.
        const operationalReady = operationalAutopilotReadyForSession(session, p);
        setOperationalAutopilotReady(operationalReady);
        // Canonical gap details remain useful operator-facing explanation;
        // readiness itself has already been decided by the strict, resolved
        // plan projection above.
        if (exactDemoProfile) {
          setUnsupportedOperationalPositions(operationalReady
            ? []
            : ["场次冻结的 20 题方案与服务器运行计划不一致"]);
          setOperationalGapSummary({
            rubric: 0,
            protocol: 0,
            sourceField: operationalReady ? 0 : 1,
            unstructured: 0,
            sourceTotal: 20,
          });
        } else {
          const structuredGaps = bank.unsupported_operational_positions
            ?? bank.unsupported_operational_rubrics
            ?? [];
          const unstructuredGaps = (bank.source_unstructured_positions ?? []).map(
            (row) => `未结构化多要素：${row.source_position_key}:${row.response_role}`,
          );
          setUnsupportedOperationalPositions([...structuredGaps, ...unstructuredGaps]);
          const counts = bank.unsupported_operational_position_counts_by_code ?? {};
          setOperationalGapSummary({
            rubric: counts.operational_rubric_unavailable ?? 0,
            // interaction_package_unavailable(2026-08-21 交互数据包缺失/无效)对
            // 操作者同义于「缺自动交互协议」,并入同一句解释。
            protocol: (counts.operational_protocol_unavailable ?? 0)
              + (counts.interaction_package_unavailable ?? 0),
            sourceField: counts.source_field_unavailable ?? 0,
            unstructured: bank.source_unstructured_position_count ?? unstructuredGaps.length,
            sourceTotal: bank.source_protocol_position_count ?? p.items.reduce(
              (total, item) => total + item.turns.length,
              0,
            ),
          });
        }
        if (!operationalReady) {
          autoPilotRef.current = false;
          setAutoPilot(false);
          try { localStorage.setItem("nmu:console:autopilot", "0"); } catch { /* 私隐模式 */ }
        }
        setPlan(p);
      })
      .catch((e) => setErr(String(e)));
  }, [exactDemoProfile, session, resetSession, retryNonce]);

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
  const terminal = runtimeControl.terminal;
  const interactionBlocked = terminal || paused || pausePending || wrapupPending || recoveryPending
    || Boolean(recoveryError) || Boolean(apFailure) || Boolean(patientDeviceFailure);
  // 服务器一旦可能取得控制权，旧的人工游标、提示、录音与本地自动推进全部停止。
  // `uncertain` 也必须锁定：POST 可能已提交但响应丢失，不能猜测仍是人工模式。
  // 观察台模式下人工面板整块不挂载（不仅 disabled）；重同步完成前同样保持锁定。
  // releaseNeedsResync.current 必须直接参与这个判定，不能只当 effect 的触发
  // 信号：它在 onServerOwnershipChange 里与 setServerOwnership 同步 latch，但
  // 这里仍需要它兜底——不依赖"这两个 setState 一定会被 React 批进同一次渲染"
  // 这个假设，否则会有一次 observerMode=false 的渲染，期间别的被动 effect
  // （例如按 itemIdx/turnIdx 补发游标那个）可能用本地旧位置抢先写一次游标。
  const serverOwned = serverOwnership.owned;
  const observerMode = manualSurfaceLocked(serverOwnership, manualResync.status) || releaseNeedsResync.current;
  const manualInteractionBlocked = interactionBlocked || observerMode;
  const retryRecovery = () => {
    setRetryNonce((n) => n + 1);
    void runtimeControl.refresh();
    retrySync();
    if (apFailure && !paused && !terminal) {
      // Retain and replay an ambiguous exact body unchanged. After a reload no
      // body exists, so reconstruct the closed client error code from the
      // persisted failure kind and issue one new CAS-bound command.
      void pauseTraining(
        true,
        pendingTechnicalPause.current
          ? undefined
          : { errorCode: `client_${apFailure.kind}` },
      );
    }
  };

  // The final TTS ACK may atomically close the bedside intervention; another
  // authorized tab may also close/abort/fail it. Polling any server terminal fact
  // must leave the bedside controls without a second local finish action.
  useEffect(() => {
    if (autoWrapupDispatched.current
        || runtimeControl.runtime?.sessionId !== session.session_id
        || !shouldAutoRouteTrainingToWrapup(runtimeControl.runtime.status)) return;
    autoWrapupDispatched.current = true;
    onWrapup();
  }, [onWrapup, runtimeControl.runtime, session.session_id]);

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

  // 周期重查服务端完整录音授权：不只看 recording_allowed，同意撤回、
  // 真实/模拟准入与运行时状态任一变化都必须收回已开的自助麦克风。
  useEffect(() => {
    // 服务端已接管、终态或人工面板尚未安全恢复时，通用录音
    // 授权不仅不会被使用，后端也必然 fail-closed。在任何请求/定时器
    // 之前停止这条人工轮询；明确释放并完成重同步后依赖变化会自动重启。
    if (manualInteractionBlocked) {
      setRecStatus("denied");
      return;
    }
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
  }, [manualInteractionBlocked, retryNonce, session.session_id]);

  const item: PlanItem | undefined = plan?.items[itemIdx];
  const planTurn: PlanTurn | undefined = item?.turns[turnIdx];
  const turnK = item && planTurn ? `${item.item_id}#${planTurn.turn_seq}` : "";
  // 观察面权威位置:回执优先,runtime 回退;映射不进冻结计划显示待同步。
  const observerPosition = observerAuthoritativePosition(
    apReceiptPosition, runtimeControl.runtime, session.session_id, plan);
  // 在途异步续体的环节身份校验:自动推进可在任何请求在途时跳环节,续体不校验会把
  // turnId/结果写进"下一环节"的工作卡——确认/锁分随之落到错误的 turn。
  const turnKRef = useRef(turnK);
  turnKRef.current = turnK;
  bedsideOperationBlocked.current = manualInteractionBlocked;
  const captureOperation = (requiresAutopilot = false): BedsideOperationFence =>
    captureBedsideOperation({
      sessionId: session.session_id,
      turnKey: turnK,
      epoch: bedsideOperationEpoch.current,
    }, requiresAutopilot);
  const operationIsCurrent = (fence: BedsideOperationFence): boolean =>
    bedsideOperationIsCurrent(fence, {
      sessionId: session.session_id,
      turnKey: turnKRef.current,
      epoch: bedsideOperationEpoch.current,
      blocked: bedsideOperationBlocked.current,
      autopilotActive: autoPilotRef.current,
    });
  const invalidateBedsideOperations = () => {
    bedsideOperationEpoch.current += 1;
    bedsideOperationBlocked.current = true;
  };
  const wasBedsideBlocked = useRef(manualInteractionBlocked);
  useEffect(() => {
    if (manualInteractionBlocked && !wasBedsideBlocked.current) {
      bedsideOperationEpoch.current += 1;
    }
    wasBedsideBlocked.current = manualInteractionBlocked;
  }, [manualInteractionBlocked]);
  useEffect(() => () => {
    bedsideOperationEpoch.current += 1;
    bedsideOperationBlocked.current = true;
  }, [session.session_id]);
  // 已锁环节不下发自助开录("锁定后不可再录"对自助路径同样生效)
  const selfStartOut = selfStart && !manualInteractionBlocked && !(journal.turns[turnK]?.locked ?? false);
  const patientMicOn = patientRec?.sessionId === session.session_id && patientRec.active === true;
  // 麦克风关闭的下降沿启动看门狗:自助路径的停麦后若迟迟等不到 audioSaved(保存失败),
  // 8 秒告警——否则自助模式保存失败对操作端零信号,老人的回答静默丢失。
  const prevMicOn = useRef(false);
  useEffect(() => {
    if (patientMicOn && apAwaitingAnswer.current) apMicSeenActive.current = true;
    if (prevMicOn.current && !patientMicOn && !patientDeviceFailure) {
      armWatchdog(patientRec?.turnKey ?? turnK);
    }
    prevMicOn.current = patientMicOn;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [patientMicOn, watchdog]);

  // 光标变化 → 重置工作态(从 journal 恢复布尔/已锁/线索级)+ 广播老人端
  // ★interactionBlocked 在依赖里只为"解锁边沿补发游标";位置没变时绝不重建工作卡——
  // 暂停/继续、重试同步成功都会翻转它,若重建会用 journal 已存值冲掉未保存的转写/确认草稿。
  useEffect(() => {
    if (!item || !planTurn || manualInteractionBlocked) return;
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
      confirmed: jt?.confirmedText ?? jt?.asrText ?? "",
      confirmationRevision: jt?.confirmationRevision ?? 0, ai: null,
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
                 selfStart: selfStartOut });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [itemIdx, turnIdx, plan, manualInteractionBlocked]);

  // 录音资格判定变化即补发游标:落地(→allowed/denied)给按钮,重查(→loading)收回按钮。
  // loading 也要发——资格重查期间老人端保留旧 selfStart=true 按钮就违反 fail-closed。
  useEffect(() => {
    if (!planTurn || manualInteractionBlocked) return;
    if (!recordingEligible) {
      if (recState === "armed" || patientMicOn) armWatchdog(patientRec?.turnKey ?? lastArmedTurnK.current ?? turnK);
      setRecState("idle");
      postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel,
                   recording: "idle", recSeq: recSeq.current, selfStart: false });
      if (autoPilotRef.current) {
        failAutopilotRef.current("microphone", "录音资格已撤回、变更或无法确认，麦克风已收回。");
      }
      return;
    }
    postCursor({ screen: recState === "armed" ? "record" : "present", itemIdx, turnIdx,
                 responseRole: planTurn.response_role, cueLevel,
                 recording: recState === "armed" ? "armed" : "idle", recSeq: recSeq.current, selfStart: selfStartOut });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recStatus, manualInteractionBlocked]);

  // ★收回游标:离开训练屏(收尾/新受试者/切屏)或 fail-closed 时,老人端的
  // "开始回答"按钮必须撤下——否则残留的 selfStart=true 让过渡期里任何人都能往本场次录音。
  // (控制台整页被强杀属残余风险:无人可发收回;README 已注明换人前先核对老人端画面。)
  const withdrawRef = useRef<() => void>(() => {});
  withdrawRef.current = () => {
    // 服务端接管时的老人端收麦由自动命令链负责；终态也已在
    // 同一服务端事务投影 thanks。此时卸载旧人工页不得再写一次旧游标。
    if (!planTurn || ownershipRef.current.owned || terminal) return;
    postCursor({ screen: "thanks", itemIdx, turnIdx, responseRole: planTurn.response_role,
                 cueLevel, recording: "idle", recSeq: recSeq.current, selfStart: false });
  };
  useEffect(() => () => withdrawRef.current(), []);
  useEffect(() => { if (err || recoveryError) withdrawRef.current(); }, [err, recoveryError]);

  // 老人端录完 → 建 turn 的音频入参在此暂存(按 turnKey)
  useAudioSaved((m) => {
    // 重放判定:操作端刷新后轮询会把最后一条回报再送一遍(内存 seen 集刷新即空)。
    // journal 持久化,里面已有的 rawAudioId 一律按重放处理:记录性动作照做(幂等),
    // 但绝不触发自动推进——否则每次刷新老人端都被顶走一题。
    const isReplay = Boolean(journal.audios[m.rawAudioId]);
    // 迟到回报(非当前环节)只做记账:watchdog 可能正在为"当前环节"守着,清了它
    // 会吞掉真正的超时告警;armed→idle 写回若以当前环节身份发出,会掐断刚示意的新录音。
    const isCurrent = m.turnKey === turnK;
    const automationAction = decideAudioSavedAutomation({
      sameSession: m.sessionId === session.session_id,
      sameTurn: isCurrent,
      alreadyRecorded: isReplay,
      safetyFailureLatched: apFailureLatched.current,
      manualTakeoverForTurn: apManualTakeoverTurn.current === turnK,
      adjudicationEnabled: autoPilot,
      interactionBlocked: manualInteractionBlocked,
    });
    // ★跨场次过滤:live state 会保留最近一条 audioSaved；别场次的回报若被记入本场
    // journal,会诱导跨场串绑——一律丢弃。
    if (automationAction === "ignore") return;
    setPendingAudio((prev) => ({ ...prev, [m.turnKey]: { rawAudioId: m.rawAudioId, duration: m.durationSeconds } }));
    // 记入 journal.audios,收尾屏音频闸门才能列出并驱动导出/校验/信度/删除。
    upsertAudio(m.rawAudioId, { turnKey: m.turnKey, containsDirectIdentifier: m.containsDirectIdentifier ?? false, isReliabilitySample: false, lastStatus: "recorded", durationSeconds: m.durationSeconds });
    // 看门狗按"守的环节"清:录音中跳题/暂停时守的是旧环节,旧环节回报到达即达成,
    // 不能因"非当前环节"漏清而弹保存成功的假警报;守当前环节时迟到旧报也清不掉它。
    if (!isReplay && m.turnKey === watchdogFor.current) {
      watchdog.clear();
      watchdogFor.current = null;
      setWatchdogUntil(null);
    }
    if (!isCurrent || isReplay) return;
    if (planTurn && !manualInteractionBlocked) {
      // 无论自停("我说好了",cursor 仍 armed)还是远端停(cursor 停在 stopped):都把 idle
      // 写回真值源——armed 残留会让老人端刷新后自动开麦;stopped 残留会让自助按钮永久失效。
      postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel, recording: "idle", recSeq: recSeq.current, selfStart: selfStartOut });
    }
    setRecState("idle");
    toast(`已收到老人端录音（${m.durationSeconds.toFixed(1)} 秒），可在下方试听`, "info");
    // audioSaved 只证明录音已持久化。它最多启动判定链；不能直接改变题位、提示或结束状态。
    if (automationAction === "adjudicate") void runAutoPilotOnAudio(m);
  });

  const advance = () => {
    if (!plan || !item || manualInteractionBlocked) return;
    if (turnIdx + 1 < item.turns.length) setTurnIdx(turnIdx + 1);
    else if (itemIdx + 1 < plan.items.length) { setItemIdx(itemIdx + 1); setTurnIdx(0); }
    else toast("已到最后一个环节,可前往场次收尾", "ok");
  };
  const retreat = () => {
    if (!plan || manualInteractionBlocked) return;
    if (turnIdx > 0) setTurnIdx(turnIdx - 1);
    else if (itemIdx > 0) {
      const prev = plan.items[itemIdx - 1];
      setItemIdx(itemIdx - 1);
      setTurnIdx(Math.max(0, prev.turns.length - 1));
    }
  };
  const atFirstTurn = itemIdx === 0 && turnIdx === 0;
  const atLastTurn = !!plan && !!item && turnIdx + 1 >= item.turns.length && itemIdx + 1 >= plan.items.length;
  const workflowStage = work.locked ? 4 : !work.savedAsr ? 1 : !work.savedConfirm ? 2 : !work.ai ? 3 : 4;

  // 示意老人端录音(VOX):arm→老人端自动开始录音;stop→老人端停止并保存后回传 audioSaved。
  // 携带当前 cueLevel,录音启停不清老人端已显示的线索。
  const armRecording = () => {
    if (!planTurn || manualInteractionBlocked || !recordingEligible) {
      if (!recordingEligible) toast("录音资格尚未明确允许，系统保持麦克风关闭", "warn");
      return;
    }
    // 自动驾驶的 arm 不拦:它有自己的失败通道;人工点按钮时设备不在线直接说明白。
    if (!autoPilotRef.current && (patientLinkDown || patientScreenPaused)) {
      toast(patientScreenPaused
        ? "老人端还停在暂停画面——等它恢复显示题目后再开始录音"
        : "老人端未连接——请先在老人端完成配对", "warn");
      return;
    }
    recSeq.current += 1; // 每次 arm 新序号:老人自停后 armed→armed 重发才能重触发老人端
    lastArmedTurnK.current = turnK;
    setRecState("armed");
    setRecArmedAt(Date.now());
    postCursor({ screen: "record", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel, recording: "armed", recSeq: recSeq.current, selfStart: selfStartOut });
  };
  const stopRecording = () => {
    if (!planTurn || manualInteractionBlocked) return;
    setRecState("idle");
    armWatchdog(lastArmedTurnK.current ?? turnK);
    postCursor({ screen: "record", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel, recording: "stopped", recSeq: recSeq.current, selfStart: selfStartOut });
  };

  async function commitCuePresentation(
    level: 1 | 2 | 3,
    fence: BedsideOperationFence,
    attemptId?: number,
  ): Promise<boolean> {
    if (!item || !planTurn) return false;
    const operationKey = `cue:${turnK}:${level}:${attemptId ?? "manual"}`;
    try {
      if (!operationIsCurrent(fence)) return false;
      const receipt = await commitInteractionPresentation({
        idempotency_key: stablePresentationKey(operationKey),
        interaction: {
          event_type: "cue_selected",
          item_id: item.item_id,
          turn_seq: planTurn.turn_seq,
          ...(attemptId ? { attempt_id: attemptId } : {}),
          prompt_level: level,
          cue_type: cueTypeForPrompt(level),
        },
        cursor: {
          sessionId: session.session_id,
          screen: "present",
          itemIdx,
          turnIdx,
          responseRole: planTurn.response_role,
          cueLevel: level,
          recording: "idle",
          recSeq: recSeq.current,
          selfStart: selfStartOut,
        },
      }, () => operationIsCurrent(fence));
      if (!receipt) return false;
      if (!operationIsCurrent(fence)) return false;
      interactionPresentationKeys.current.delete(operationKey);
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.status >= 400
          && error.status < 500 && error.status !== 408) {
        interactionPresentationKeys.current.delete(operationKey);
      }
      if (!operationIsCurrent(fence)) return false;
      toast(`提示证据未能写入：${error instanceof ApiError ? error.detail : String(error)}`, "danger");
      return false;
    }
  }

  // 发分级线索给老人端(0→3 只升不降)。必须先写只追加交互证据，
  // 账本失败时不下发话术、不消耗提示等级。
  const sendCue = async (level: 1 | 2 | 3) => {
    if (!planTurn || manualInteractionBlocked || level <= cueLevel || cueWritePending.current) return;
    const fence = captureOperation();
    cueWritePending.current = true;
    try {
      if (!await commitCuePresentation(level, fence)) return;
      if (!operationIsCurrent(fence)) return;
      setRecState("idle");
      setCueLevel(level);
      recordCueLevel(turnK, level);
    } finally {
      cueWritePending.current = false;
    }
  };

  function attemptRequest(audio: { rawAudioId: string; duration: number }, promptLevel = cueLevel): AttemptProcessRequest | null {
    if (!item || !planTurn) return null;
    return {
      item_id: item.item_id,
      turn_seq: planTurn.turn_seq,
      response_role: planTurn.response_role,
      raw_audio_id: audio.rawAudioId,
      prompt_level: promptLevel,
      cue_type: cueTypeForPrompt(promptLevel),
      duration_seconds: audio.duration,
    };
  }

  async function pauseManualEvidenceFailure(message: string, errorCode: string, alreadyRecorded = false) {
    toast(message, "danger");
    // attempt/process 的技术失败已在后端同一事务中写证据并暂停；
    // 其他客户端失败只能走新的单一原子命令，不再拆成 pause + append。
    await pauseTraining(
      true,
      alreadyRecorded ? undefined : { errorCode },
    );
  }

  async function ensureItemEvent(fence = captureOperation()): Promise<number | null> {
    if (!item || !operationIsCurrent(fence)) return null;
    const operationItem = item;
    const existing = journal.itemEvents[operationItem.item_id];
    if (existing) return existing.itemEventId;
    const ie = await api.createItem(session.session_id, {
      item_id: operationItem.item_id,
      task_type: operationItem.task_type,
      image_id: operationItem.image_id,
    });
    if (!operationIsCurrent(fence)) return null;
    upsertItem(operationItem.item_id, {
      itemEventId: ie.id,
      taskType: operationItem.task_type,
      imageId: operationItem.image_id ?? null,
    });
    onItemEventChange?.(ie.id);
    return ie.id;
  }

  // 三个异步动作共用一把在途锁:点了立刻有反馈、连点不重复提交
  const [busyOp, setBusyOp] = useState<null | "confirm" | "asr">(null);

  async function processManualAudio(
    audio: { rawAudioId: string; duration: number },
    opK: string,
    fence = captureOperation(),
  ): Promise<AttemptEvent | null> {
    if (!operationIsCurrent(fence)) return null;
    const cached = pendingAttempts[opK];
    if (cached?.raw_audio_id === audio.rawAudioId && cached.processing_status === "completed") return cached;
    const request = attemptRequest(audio);
    if (!request) return null;
    let result: Awaited<ReturnType<typeof api.processAttempt>>;
    try {
      result = await api.processAttempt(session.session_id, request);
    } catch (error) {
      if (!operationIsCurrent(fence)) return null;
      await pauseManualEvidenceFailure(
        `逐次 AI 处理请求失败：${error instanceof ApiError ? error.detail : String(error)}`,
        "client_attempt_request_failed",
      );
      return null;
    }
    if (!operationIsCurrent(fence)) return null;
    const decision = decideAttemptProcessResult(result, request, session.session_id, session.is_simulation);
    if (decision.kind === "technical_failure") {
      await pauseManualEvidenceFailure(
        `语音或判类引擎技术失败（${decision.errorCode}），本环节未推进。`,
        decision.errorCode,
        true,
      );
      return null;
    }
    if (decision.kind === "invalid") {
      await pauseManualEvidenceFailure(decision.message, "client_attempt_invalid_response");
      return null;
    }
    setPendingAttempts((previous) => ({ ...previous, [opK]: decision.attempt }));
    return decision.attempt;
  }

  async function tryLocalAsr() {
    const au = pendingAudio[turnK];
    if (!au || busyOp || manualInteractionBlocked) return;
    const opK = turnK; // 环节身份:自动推进可在请求在途时跳环节,续体只准写回发起时的环节
    const fence = captureOperation();
    setBusyOp("asr");
    try {
      const attempt = await processManualAudio(au, opK, fence);
      if (!attempt) return;
      if (!operationIsCurrent(fence)) return;
      setWork((w) => (w.savedAsr ? w : {
        ...w,
        asrText: attempt.asr_text ?? "",
        ai: {
          answerType: attempt.operational_answer_type ?? null,
          score: attempt.operational_score ?? null,
          needsReview: attempt.operational_needs_review === true,
        },
      }));
      toast(`逐次 AI 证据已完成（${attempt.asr_engine_version ?? "ASR"}）`, "ok");
    } catch (e) {
      if (operationIsCurrent(fence)) {
        await pauseManualEvidenceFailure(`处理逐次证据失败：${e instanceof ApiError ? e.detail : String(e)}`, "client_attempt_unexpected");
      }
    }
    finally { setBusyOp(null); }
  }

  async function saveTranscription() {
    if (!item || !planTurn || manualInteractionBlocked) return;
    if (savingTurn) return;                                          // 在途锁:防双击重复建 item/turn
    if (work.savedAsr && work.turnId) { toast("该环节转写已冻结,不可重写", "warn"); return; }
    const opK = turnK;
    const fence = captureOperation();
    const operationItem = item;
    const operationTurn = planTurn;
    setSavingTurn(true);
    try {
      const au = pendingAudio[turnK];
      if (!au) { toast("请先完成本环节录音", "warn"); return; }
      const attempt = await processManualAudio(au, opK, fence);
      if (!attempt || !operationIsCurrent(fence)) return;
      const ieid = await ensureItemEvent(fence);
      if (ieid == null || !operationIsCurrent(fence)) return;
      const te = await api.createTurn(ieid, {
        turn_seq: operationTurn.turn_seq, response_role: operationTurn.response_role,
        raw_audio_id: au.rawAudioId,
      });
      if (!operationIsCurrent(fence)) return;
      if (te.source_attempt_id !== attempt.id) throw new Error("Turn 未指向本次已完成 attempt");
      const authoritativeAsr = te.asr_text ?? attempt.asr_text ?? "";
      upsertTurn(operationItem.item_id, operationTurn.turn_seq, {
        turnId: te.id,
        responseRole: operationTurn.response_role,
        asrSaved: true,
        asrText: authoritativeAsr,
        cueLevel: (te.prompt_level ?? attempt.prompt_level) as 0 | 1 | 2 | 3,
      });
      // 环节已被自动推进跳走:journal 已正确记账(键在发起时捕获),但工作卡现在属于新环节,
      // 把旧环节的 turnId 写进去会让后续确认/锁分落到错误的 turn 上。
      if (!operationIsCurrent(fence)) return;
      // 确认文本预填 ASR 原文:多数环节只需微调而非整句重打
      setWork((w) => ({
        ...w,
        turnId: te.id,
        confirmationRevision: te.confirmation_revision,
        asrText: authoritativeAsr,
        savedAsr: true,
        confirmed: w.confirmed.trim() ? w.confirmed : authoritativeAsr,
        ai: {
          answerType: te.ai_answer_type ?? attempt.operational_answer_type ?? null,
          score: te.ai_score ?? attempt.operational_score ?? null,
          needsReview: te.ai_needs_review ?? attempt.operational_needs_review ?? false,
        },
      }));
      toast("转写已保存，不可再修改", "ok");
    } catch (e) {
      if (operationIsCurrent(fence)) {
        await pauseManualEvidenceFailure(
          `本环节证据没有保存成功：${e instanceof ApiError ? e.detail : String(e)}`,
          "client_turn_persistence_failed",
        );
      }
    }
    finally { setSavingTurn(false); }
  }

  async function persistWorkConfirmation(fence: BedsideOperationFence) {
    if (!item || !planTurn || work.turnId == null || !operationIsCurrent(fence)) return null;
    const operationItem = item;
    const operationTurn = planTurn;
    const operationTurnId = work.turnId;
    const text = work.confirmed.trim();
    let pending = confirmationRequestRef.current;
    if (!pending
        || pending.turnId !== operationTurnId
        || pending.expectedRevision !== work.confirmationRevision
        || pending.text !== text) {
      pending = {
        turnId: operationTurnId,
        expectedRevision: work.confirmationRevision,
        text,
        idempotencyKey: `turn-confirm-${crypto.randomUUID()}`,
      };
      confirmationRequestRef.current = pending;
    }
    try {
      const updated = await api.confirmTurn(operationTurnId, {
        confirmed_response_text: text,
        expected_revision: pending.expectedRevision,
        idempotency_key: pending.idempotencyKey,
      });
      if (!operationIsCurrent(fence)) return null;
      confirmationRequestRef.current = null;
      upsertTurn(operationItem.item_id, operationTurn.turn_seq, {
        turnId: operationTurnId,
        responseRole: operationTurn.response_role,
        confirmed: true,
        confirmedText: updated.confirmed_response_text ?? text,
        confirmationRevision: updated.confirmation_revision,
      });
      return updated;
    } catch (error) {
      if (!operationIsCurrent(fence)) throw error;
      if (error instanceof ApiError && error.status === 409
          && error.detailData !== null && typeof error.detailData === "object"
          && !Array.isArray(error.detailData)) {
        const code = (error.detailData as { code?: unknown }).code;
        if (code === "turn_confirmation_revision_conflict"
            || code === "turn_confirmation_replay_superseded"
            || code === "turn_confirmation_concurrency_conflict") {
          confirmationRequestRef.current = null;
          const refreshed = await api.sessionJournal(session.session_id);
          if (operationIsCurrent(fence)) hydrateFromServer(refreshed);
        }
      }
      throw error;
    }
  }

  async function saveConfirm(): Promise<boolean> {
    if (manualInteractionBlocked) return false;
    if (!item || !planTurn || work.turnId == null) { toast("请先保存转写", "warn"); return false; }
    if (!work.confirmed.trim()) { toast("确认文本为空,未提交(避免把已有确认覆盖成空串)", "warn"); return false; }
    if (busyOp) return false;
    const fence = captureOperation();
    setBusyOp("confirm");
    try {
      const updated = await persistWorkConfirmation(fence);
      if (!updated) return false;
      if (!operationIsCurrent(fence)) return false;
      setWork((w) => ({ ...w, savedConfirm: true, confirmationRevision: updated.confirmation_revision }));
      toast("确认文本已保存(未覆盖原文)", "ok");
      return true;
    } catch (e) {
      if (operationIsCurrent(fence)) toast(e instanceof ApiError ? e.detail : String(e), "danger");
      return false;
    }
    finally { setBusyOp((b) => (b === "confirm" ? null : b)); }
  }

  const [locking, setLocking] = useState(false);
  async function doLock(elementValue: number): Promise<boolean> {
    if (manualInteractionBlocked) return false;
    if (!item || !planTurn || work.turnId == null) { toast("请先保存转写", "warn"); return false; }
    if (locking) return false;
    const fence = captureOperation();
    const operationItem = item;
    const operationTurn = planTurn;
    const operationTurnId = work.turnId;
    setLocking(true);
    const payload = { element_value: elementValue };
    try {
      // 有改动未保存的确认文本:先替研究者存了再锁——锁定后 confirmed 不可改,不能让它无声丢失
      if (work.confirmed.trim() && !work.savedConfirm) {
        const updated = await persistWorkConfirmation(fence);
        if (!updated) return false;
        if (!operationIsCurrent(fence)) return false;
        setWork((w) => ({
          ...w, savedConfirm: true, confirmationRevision: updated.confirmation_revision,
        }));
      }
      if (!operationIsCurrent(fence)) return false;
      assertPortraitFree(payload as unknown as Record<string, unknown>); // ★锁分载荷画像守卫(前端第4层)
      await api.lockTurn(operationTurnId, payload);
      if (!operationIsCurrent(fence)) return false;
      upsertTurn(operationItem.item_id, operationTurn.turn_seq, {
        turnId: operationTurnId,
        responseRole: operationTurn.response_role,
        locked: true,
        elementValue,
        promptLevel: cueLevel,
      });
      setWork((w) => ({ ...w, locked: true }));
      // 锁定即收回老人端自助开录按钮("锁定后不可再录"对自助路径同样生效)
      if (operationIsCurrent(fence)) {
        postCursor({ screen: "present", itemIdx, turnIdx, responseRole: operationTurn.response_role, cueLevel, recording: "idle", recSeq: recSeq.current, selfStart: false });
      }
      toast(session.data_classification === "simulation" ? "模拟复核标注已锁定" : "研究评分已锁定", "ok");
      return true;
    } catch (e) {
      if (operationIsCurrent(fence)) toast(e instanceof ApiError ? e.detail : String(e), "danger");
      return false;
    }
    finally { setLocking(false); }
  }

  // ---------------- 自动驾驶状态机(文档协议:四分支→两级提示→告知答案,永不卡题) ----------------
  // 分工:处理器(收音回报/沉默)只判类和改状态;所有定时(开麦/沉默/终局播报后推进)统一由
  // 下面的 [turnK, cueLevel] 效应安排——避免"改状态的定时器被状态变化触发的 cleanup 清掉"。
  // AI 只判类驱动流程,永不锁分;反馈话术全部指向题库/协议固定文案(fbKey 查表,不产文本)。
  const apActive = operationalAutopilotReady === true && autoPilot && !manualInteractionBlocked && !!item && !!planTurn
    && recordingEligible;
  const clearApTimers = () => {
    for (const k of ["arm", "answer", "act"] as const) {
      const t = apTimers.current[k];
      if (t) clearTimeout(t);
      apTimers.current[k] = undefined;
    }
  };
  const persistAutoPilot = (enabled: boolean) => {
    autoPilotRef.current = enabled;
    setAutoPilot(enabled);
    try { localStorage.setItem("nmu:console:autopilot", enabled ? "1" : "0"); } catch { /* 私隐模式等 */ }
  };
  const disarmAutopilot = (allowSelfStart: boolean, writeSafeCursor = true) => {
    clearApTimers();
    apAwaitingAnswer.current = false;
    apMicSeenActive.current = false;
    apBusy.current = false;
    setRecState("idle");
    if (writeSafeCursor && planTurn) {
      postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role,
                   cueLevel, recording: "idle", recSeq: recSeq.current,
                   selfStart: allowSelfStart && selfStartOut });
    }
  };
  const serverOwnershipApplied = useRef(false);
  useEffect(() => {
    if (!serverOwned) {
      serverOwnershipApplied.current = false;
      return;
    }
    if (serverOwnershipApplied.current) return;
    serverOwnershipApplied.current = true;
    // 在 start 请求尚未确认时就先关闭第二驾驶员。若响应丢失，保持这个锁；
    // 后续只能由服务器状态查询/显式接管解除，不能靠刷新或本地猜测恢复。
    // Ownership has already been proven by GET/POST. Stop all local machinery,
    // but do not let this old tab emit a cursor write into the server-owned plane.
    disarmAutopilot(false, false);
    persistAutoPilot(false);
    try {
      localStorage.setItem("nmu:console:autopilot", "0");
    } catch { /* 私隐模式等 */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverOwned]);
  // 返回 true 或一句给研究者看的具体拒因。此前所有分支都折成同一句「老人端
  // 麦克风还未确认关闭」——2026-08-25 Eric 撞了一晚上却看不出真原因(实际是
  // 技术失败闩/暂停态挡的),拒因必须逐分支说真话,麦克风只在真是麦克风时提。
  const prepareServerOwnership = async (): Promise<true | string> => {
    if (patientMicOn) {
      const reason = "老人端麦克风仍在工作；请先完成保存，再启动自动带练";
      toast(reason, "danger");
      return reason;
    }
    if (apFailure) {
      return "本场早前发生过技术失败，故障锁还没解除——请先在红色横幅里点"
        + "「确认并人工接管」，处理后开新场再启动自动带练";
    }
    if (patientDeviceFailure) return "老人端设备故障还没解除，请先处理红色横幅里的设备提示";
    if (terminal) return "场次已结束，不能再启动自动带练";
    if (paused || pausePending) return "场次暂停中——请先点「继续本场」恢复，再启动自动带练";
    if (wrapupPending) return "正在进入收尾流程，不能启动自动带练";
    if (recoveryPending || Boolean(recoveryError)) {
      return "页面还没与服务器完成状态同步——稍等几秒，或刷新页面后再启动";
    }
    if (!planTurn) return "训练内容还没加载完成——稍等几秒再启动";
    serverOwnershipApplied.current = true;
    // The one awaited cursor write below is the pre-start handoff fact. Avoid a
    // second fire-and-forget write from disarmAutopilot.
    disarmAutopilot(false, false);
    persistAutoPilot(false);
    const accepted = await postCursor({
      screen: "present",
      itemIdx,
      turnIdx,
      responseRole: planTurn.response_role,
      cueLevel,
      recording: "stopped",
      recSeq: recSeq.current,
      selfStart: false,
    });
    if (!accepted) return "与服务器的交接写入未被确认——请再点一次启动";
    await flushLiveWrites();
    return true;
  };
  const failAutopilot = (
    kind: AutopilotFailureKind,
    message: string,
    evidence?: { errorCode?: string; attemptId?: number; alreadyRecorded?: boolean },
  ) => {
    if (apFailureLatched.current) return;
    apFailureLatched.current = true;
    // 清除故障横幅后，迟到的本题音频仍然不得触发自动推进。
    apManualTakeoverTurn.current = turnK;
    watchdog.clear();
    watchdogFor.current = null;
    disarmAutopilot(false);
    persistAutoPilot(false);
    const failure = makeAutopilotFailure({ itemIdx, turnIdx, cueLevel }, kind, message);
    setApFailure(failure);
    try { localStorage.setItem(autopilotFailureStorageKey(session.session_id), JSON.stringify(failure)); } catch { /* 私隐模式 */ }
    toast(`自动驾驶已安全暂停：${message}`, "danger");
    const errorCode = evidence?.errorCode ?? `client_${kind}`;
    // 先本地收麦和撤销旧写；未由 attempt/process 落账的失败随后只发
    // 一个 CAS + 幂等键绑定的原子技术暂停命令。
    void pauseTraining(
      true,
      evidence?.alreadyRecorded ? undefined : {
        errorCode,
        attemptId: evidence?.attemptId,
      },
    ).catch((error) => {
      const ledgerMessage = `安全暂停还没有得到服务器确认：${error instanceof ApiError ? error.detail : String(error)}`;
      setApFailure((current) => current ? { ...current, message: `${current.message}；${ledgerMessage}` } : current);
      toast(ledgerMessage, "danger");
    });
  };
  failAutopilotRef.current = failAutopilot;

  // 设备失败只消费服务器已提交的 patientRec 投影：同一事务已完成 technical_pause、
  // runtime pause 与关麦，本机只锁住 UI/计时器，绝不再追加 Attempt、提示或重复技术事件。
  useEffect(() => {
    if (!patientDeviceFailure || handledDeviceFailureId.current === patientDeviceFailure.failureId) return;
    handledDeviceFailureId.current = patientDeviceFailure.failureId;
    watchdog.clear();
    watchdogFor.current = null;
    clearApTimers();
    apAwaitingAnswer.current = false;
    apMicSeenActive.current = false;
    apBusy.current = false;
    apManualTakeoverTurn.current = patientDeviceFailure.turnKey;
    setRecState("idle");
    if (autoPilotRef.current && !apFailureLatched.current) {
      apFailureLatched.current = true;
      const failure = makeAutopilotFailure(
        { itemIdx, turnIdx, cueLevel },
        "microphone",
        "老人端录音设备未能启动；服务器已暂停场次，未按错误回答处理。",
      );
      setApFailure(failure);
      try { localStorage.setItem(autopilotFailureStorageKey(session.session_id), JSON.stringify(failure)); } catch { /* 私隐模式 */ }
    }
    autoPilotRef.current = false;
    setAutoPilot(false);
    try { localStorage.setItem("nmu:console:autopilot", "0"); } catch { /* 私隐模式 */ }
    toast("设备失败，场次已安全暂停。当前位置与提示等级保持不变。", "danger");
    // 只按新的服务器 failureId 处理一次；位置/提示值特意锁存在该次权威失败到达时。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [patientDeviceFailure?.failureId]);

  useEffect(() => {
    if (!terminal) return;
    clearApTimers();
    watchdog.clear();
    watchdogFor.current = null;
    apAwaitingAnswer.current = false;
    apMicSeenActive.current = false;
    apBusy.current = false;
    setRecState("idle");
    autoPilotRef.current = false;
    setAutoPilot(false);
    try { localStorage.setItem("nmu:console:autopilot", "0"); } catch { /* 私隐模式 */ }
    try { localStorage.removeItem(autopilotFailureStorageKey(session.session_id)); } catch { /* 私隐模式 */ }
    // complete/abort 已由服务器投影 idle/done 到老人端；终态后不再补发会被 409 的旧游标。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [terminal]);

  const acceptManualTakeover = async () => {
    if (!canReleaseAutopilotFailure(runtimeControl.runtime?.status)) {
      toast("服务器尚未确认安全暂停，故障锁不会解除，正在重试。", "danger");
      void pauseTraining(true);
      return;
    }
    if (pauseOperationInFlight.current) return;
    pauseOperationInFlight.current = true;
    setPausePending(true);
    try {
      const reconciled = await reconcilePendingTechnicalPauseForTakeover(
        runtimeControl.runtime?.status,
        pendingTechnicalPause.current,
        (exact) => api.commitTechnicalPause(session.session_id, exact),
      );
      pendingTechnicalPause.current = reconciled.pending;
      if (!reconciled.ready) {
        const detail = reconciled.error instanceof ApiError
          ? reconciled.error.detail : String(reconciled.error ?? "暂停回执未确认");
        toast(`原子技术暂停回执尚未对账：${detail}`, "danger");
        return;
      }
      apFailureLatched.current = false;
      setApFailure(null);
      try { localStorage.removeItem(autopilotFailureStorageKey(session.session_id)); } catch { /* 私隐模式 */ }
      toast("已进入人工接管，当前题目与提示等级保持不变。", "info");
    } finally {
      setPausePending(false);
      pauseOperationInFlight.current = false;
    }
  };
  // 朗读时长估计:无老人端"读完"回执,按字数估(云端语速 0.9)+固定余量;宁可多等不早开麦
  // (早开麦会把小语自己的声音录进去)。答窗宽限也用它:反馈/线索长句要多给作答时间。
  const speakMs = (t: string | null | undefined) => (t ? 2500 + t.length * 260 : 0);

  // 环节了结:建正式 turn(最后一轮音频/转写冻结;confirmed 留人工事后),持久化 AI 初评,推进
  async function apResolve(fbKey: FbKey | null, promptLevel: number) {
    if (!item || !planTurn) return;
    const fence = captureOperation(true);
    if (!operationIsCurrent(fence)) return;
    try {
      const round = apLastRound.current;
      if (!round) throw new Error("本环节没有可收口的 completed attempt");
      const ieid = await ensureItemEvent(fence);
      if (ieid == null || !operationIsCurrent(fence)) return;
      const te = await api.createTurn(ieid, {
        turn_seq: planTurn.turn_seq, response_role: planTurn.response_role,
        raw_audio_id: round.rawAudioId,
      });
      if (!operationIsCurrent(fence)) return;
      if (te.source_attempt_id !== round.attemptId) throw new Error("Turn 未指向本次 completed attempt");
      const authoritativeAsr = te.asr_text ?? round.asrText;
      upsertTurn(item.item_id, planTurn.turn_seq, {
        turnId: te.id,
        responseRole: planTurn.response_role,
        asrSaved: true,
        asrText: authoritativeAsr,
        cueLevel: promptLevel as 0 | 1 | 2 | 3,
      });
      setWork((w) => ({
        ...w,
        turnId: te.id,
        confirmationRevision: te.confirmation_revision,
        asrText: authoritativeAsr,
        savedAsr: true,
        confirmed: w.confirmed || authoritativeAsr,
        ai: {
          answerType: te.ai_answer_type ?? round.answerType,
          score: te.ai_score ?? round.score,
          needsReview: te.ai_needs_review ?? round.needsReview,
        },
      }));
      if (!operationIsCurrent(fence)) return;
      await apAdvance(fbKey);
    } catch (e) {
      if (!operationIsCurrent(fence)) return;
      failAutopilot("persistence", `自动保存本环节失败：${e instanceof ApiError ? e.detail : String(e)}`);
    }
  }

  // 推进两拍:beat-1 反馈话术在"当前题图片还在屏上"时先读(引用的是本题目标词,如"这就是胡萝卜");
  // 待反馈读完 beat-2 才换到下一题——否则老人看着新图听到上一题的答案。无反馈则直接推进。
  async function apAdvance(fbKey: FbKey | null) {
    if (!plan || !item || !planTurn || manualInteractionBlocked) return;
    const fence = captureOperation(true);
    if (!operationIsCurrent(fence)) return;
    clearApTimers();
    const round = apLastRound.current;
    const atEnd = turnIdx + 1 >= item.turns.length && itemIdx + 1 >= plan.items.length;
    const step = () => {
      if (!operationIsCurrent(fence)) return;
      if (atEnd) {
        postCursor({ screen: "thanks", itemIdx, turnIdx, responseRole: planTurn.response_role,
                     cueLevel, recording: "idle", recSeq: recSeq.current, selfStart: false });
        toast("最后一题完成,老人端已转入结束画面;可前往场次收尾", "ok");
      } else {
        advance();
      }
    };
    if (fbKey) {
      if (!round) {
        failAutopilot("persistence", "反馈话术缺少可追溯 attempt，系统未推进。");
        return;
      }
      try {
        if (!operationIsCurrent(fence)) return;
        const operationKey = `feedback:${turnK}:${round.attemptId}:${fbKey}`;
        const receipt = await commitInteractionPresentation({
          idempotency_key: stablePresentationKey(operationKey),
          interaction: {
            event_type: "feedback_selected",
            item_id: item.item_id,
            turn_seq: planTurn.turn_seq,
            attempt_id: round.attemptId,
            feedback_key: fbKey,
          },
          cursor: {
            sessionId: session.session_id,
            screen: "present",
            itemIdx,
            turnIdx,
            responseRole: planTurn.response_role,
            cueLevel,
            recording: "idle",
            recSeq: recSeq.current,
            selfStart: false,
            fbKey,
            fbItemId: item.item_id,
          },
        }, () => operationIsCurrent(fence));
        if (!receipt) return;
        if (!operationIsCurrent(fence)) return;
        interactionPresentationKeys.current.delete(operationKey);
      } catch (error) {
        const operationKey = `feedback:${turnK}:${round.attemptId}:${fbKey}`;
        if (error instanceof ApiError && error.status >= 400
            && error.status < 500 && error.status !== 408) {
          interactionPresentationKeys.current.delete(operationKey);
        }
        if (!operationIsCurrent(fence)) return;
        failAutopilot("persistence", `反馈证据写入失败：${error instanceof ApiError ? error.detail : String(error)}`);
        return;
      }
      if (!operationIsCurrent(fence)) return;
      const line = resolveFeedbackLine(bundle, protocol, fbKey, item.item_id);
      apTimers.current.act = setTimeout(step, speakMs(line) + 600);
    } else {
      step();
    }
    if (operationIsCurrent(fence)) apLastRound.current = null;
  }

  // 提示升级:单条 idle 游标原子下发(不走 apDisarm+sendCue 两次 post——后者读到未提交的
  // 陈旧 recState='armed',会让老人端在读线索时误显"正在准备麦克风")。arm 由效应随后重排。
  async function apEscalate(path: "unknown" | "close" | "silence") {
    const fence = captureOperation(true);
    if (!operationIsCurrent(fence)) return;
    apAwaitingAnswer.current = false;
    const next = Math.min(3, cueLevel + 1) as 1 | 2 | 3;
    if (!await commitCuePresentation(next, fence, apLastRound.current?.attemptId)) {
      if (!operationIsCurrent(fence)) return;
      failAutopilot("persistence", "提示已选择但证据未能落账，系统未下发话术。");
      return;
    }
    if (!operationIsCurrent(fence)) return;
    setRecState("idle");
    if (cueLevel === 0) apCue1Path.current = path;
    setCueLevel(next);
    recordCueLevel(turnK, next);
  }

  // 答窗到点:先确认麦克风真实启动过。未见 active 是技术故障,不得冒充"沉默"消耗提示；
  // 已启动则只下发收麦并等待 audioSaved，真正的静音也必须经过音频保存与 ASR 后才能判定。
  function apOnSilence() {
    if (!apActive || !item || !planTurn) return;
    if (answerTimeoutAction(apMicSeenActive.current) === "technical-failure") {
      failAutopilot("microphone", "作答窗口内未确认麦克风成功启动，系统没有把它当作沉默回答。");
      return;
    }
    setRecState("idle");
    armWatchdog(turnK);
    postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role,
                 cueLevel, recording: "idle", recSeq: recSeq.current, selfStart: false });
  }

  // 收音回报 → 转写 → 判类 → 分支(成功反馈按进入路径选变体;失败升级;终局推进)
  async function runAutoPilotOnAudio(m: { rawAudioId: string; durationSeconds: number }) {
    // apAwaitingAnswer 闸:只处理"正在等的这一轮"回报;自动收麦后迟到的旧录音回报一律忽略
    // (它 turnKey 仍等于当前环节,靠 turnKRef 挡不住,只能靠这个每轮独占标记)。
    if (!apAwaitingAnswer.current || apBusy.current || !item || !planTurn) return;
    apAwaitingAnswer.current = false;
    clearApTimers();
    apBusy.current = true;
    const fence = captureOperation(true);
    if (!operationIsCurrent(fence)) {
      apBusy.current = false;
      return;
    }
    try {
      if (!m.rawAudioId || !Number.isFinite(m.durationSeconds) || m.durationSeconds <= 0) {
        failAutopilot("audio", "收到的录音缺少有效时长或音频标识，系统未据此升级提示。");
        return;
      }
      const request = attemptRequest({ rawAudioId: m.rawAudioId, duration: m.durationSeconds });
      if (!request) {
        failAutopilot("persistence", "无法生成与冻结计划一致的 attempt 请求。");
        return;
      }
      let result: Awaited<ReturnType<typeof api.processAttempt>>;
      try {
        result = await api.processAttempt(session.session_id, request);
      } catch (e) {
        if (!operationIsCurrent(fence)) return;
        failAutopilot("asr", `逐次 AI 证据处理请求失败：${e instanceof ApiError ? e.detail : String(e)}`,
          { errorCode: "client_attempt_request_failed" });
        return;
      }
      if (!operationIsCurrent(fence)) return;
      const decision = decideAttemptProcessResult(result, request, session.session_id, session.is_simulation);
      if (decision.kind === "technical_failure") {
        failAutopilot(
          autopilotFailureKindForErrorCode(decision.errorCode),
          `逐次 AI 处理技术失败（${decision.errorCode}），系统未把它当作错误回答。`,
          { errorCode: decision.errorCode, attemptId: decision.attempt.id, alreadyRecorded: true },
        );
        return;
      }
      if (decision.kind === "invalid") {
        failAutopilot("persistence", decision.message, { errorCode: "client_attempt_invalid_response" });
        return;
      }
      const attempt = decision.attempt;
      apLastRound.current = {
        rawAudioId: attempt.raw_audio_id,
        duration: attempt.duration_seconds ?? m.durationSeconds,
        attemptId: attempt.id,
        asrText: attempt.asr_text ?? "",
        answerType: attempt.operational_answer_type ?? "",
        score: attempt.operational_score ?? null,
        needsReview: attempt.operational_needs_review === true,
        containsTarget: attempt.contains_target === true,
      };
      if (cueLevel >= 3) { void apResolve(null, 3); return; }   // 告知答案后不再判类,总是推进
      setWork((w) => ({ ...w, asrText: w.savedAsr ? w.asrText : (attempt.asr_text ?? ""),
        ai: {
          answerType: attempt.operational_answer_type ?? null,
          score: attempt.operational_score ?? null,
          needsReview: attempt.operational_needs_review === true,
        } }));
      // 成功=判"正确",或回答里包含了完整目标词(含目标词=说对了,如"是胡萝卜";
      // 而"萝卜"这种目标词的子串只判"部分正确"、contains_target 为假 → 按不准确升级提示)。
      const answerType = attempt.operational_answer_type ?? "";
      const ok = answerType === "正确" || attempt.contains_target === true;
      if (item.task_type === "单要素") {
        if (ok) {
          void apResolve(cueLevel === 0 ? "self"
            : cueLevel === 1 ? (`cued1_${apCue1Path.current ?? "close"}` as FbKey) : "cued2", cueLevel);
        } else {
          await apEscalate(answerType === "上位词或相关词" || answerType === "部分正确" ? "close" : "unknown");
        }
      } else if (planTurn.response_role.includes("命名")) {
        // 双要素命名:答错一次性纠正("这个我们刚刚见过,它叫X。"),不升级,直接下一环节
        void apResolve(ok ? null : planTurn.response_role.startsWith("左") ? "namefix_l" : "namefix_r", cueLevel);
      } else {
        // 作用/关系/关键要素只做“是否形成回答”的 operational 分类，不冒充研究对错。
        // 空回答先给一次固定提示；已有提示仍无回答才结束该环节，留待人工研究真值复核。
        if (answerType === "沉默" && cueLevel < 1) await apEscalate("silence");
        else void apResolve(null, cueLevel);
      }
    } catch (e) {
      if (!operationIsCurrent(fence)) return;
      failAutopilot("persistence", `自动处理出现未预期错误：${e instanceof ApiError ? e.detail : String(e)}`);
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
      const fence = captureOperation(true);
      apTimers.current.act = setTimeout(() => {
        if (operationIsCurrent(fence)) void apResolve(null, cueLevel);
      }, speakMs(text) + 800);
      return clearApTimers;
    }
    const qText = role.includes("作用") ? "它是做什么用的呢？" : role === "关系识别" ? "它们之间有什么关系呢？" : "请看这张图片，这是什么？";
    const cueText = cueLevel > 0 ? lookupCue(bundle, item.item_id, item.task_type, role, cueLevel) : null;
    const spoken = cueLevel > 0 ? cueText : qText;
    const fence = captureOperation(true);
    apTimers.current.arm = setTimeout(() => {
      if (!operationIsCurrent(fence)) return;
      apMicSeenActive.current = false;
      armRecording();
      apAwaitingAnswer.current = true;
      // 作答窗=文档沉默阈值(默认10s)+宽限;到点没等到"我说好了"即按沉默处理(收麦+升级)。
      // ★不随 patientMicOn 撤销——没有 VAD,"麦克风开着"不代表老人在说话;这条计时是唯一
      // 能在活麦沉默场景停麦、推进状态机的机制(真机麦克风验收的关键)。
      apTimers.current.answer = setTimeout(() => {
        if (operationIsCurrent(fence)) apOnSilence();
      }, ((protocol?.silence_seconds ?? 10) + 5) * 1000);
    }, speakMs(spoken));
    return clearApTimers;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turnK, cueLevel, apActive, work.locked]);

  // 换环节复位来路;关自动驾驶清干净(人工接管即刻生效)
  useEffect(() => { apCue1Path.current = null; }, [turnK]);
  useEffect(() => {
    if (!autoPilot) {
      bedsideOperationEpoch.current += 1;
      clearApTimers();
      apAwaitingAnswer.current = false;
      apLastRound.current = null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoPilot]);

  async function commitAtomicTechnicalPause(
    evidence?: { errorCode: string; attemptId?: number },
  ): Promise<boolean> {
    let exact = pendingTechnicalPause.current;
    if (exact && evidence) {
      const requestedDraft: TechnicalPauseDraft = {
        idempotency_key: exact.idempotency_key,
        error_code: evidence.errorCode,
        ...(evidence.attemptId ? { attempt_id: evidence.attemptId } : {}),
      };
      if (!exactTechnicalPauseRequestMatchesDraft(exact, requestedDraft)) {
        throw new Error("已在途的原子技术暂停与新故障事实不一致");
      }
    }
    if (!exact) {
      if (!evidence) {
        // Device/attempt failure already committed the authoritative pause.
        // Consume that truth only; never append a second technical_pause fact.
        const current = await runtimeControl.refresh();
        return current?.status === "paused";
      }
      const uuid = globalThis.crypto?.randomUUID?.();
      if (!uuid) throw new Error("当前浏览器不能安全生成技术暂停幂等键");
      const draft: TechnicalPauseDraft = {
        idempotency_key: `technical-pause:${uuid}`,
        error_code: evidence.errorCode,
        ...(evidence.attemptId ? { attempt_id: evidence.attemptId } : {}),
      };
      // Snapshot only after beginSafetyPause aborted older same-tab live writes.
      const snapshot = await api.getSessionRuntime(session.session_id);
      exact = buildExactTechnicalPauseRequest(
        draft, snapshot, session.session_id);
      pendingTechnicalPause.current = exact;
    }

    let receipt;
    try {
      receipt = await api.commitTechnicalPause(session.session_id, exact);
    } catch (firstError) {
      if (!technicalPauseDispositionIsUncertain(firstError)) {
        pendingTechnicalPause.current = null;
        throw firstError;
      }
      // The first response may have been lost after commit. Replay the byte-for-
      // byte same body once; changing revision/wseq under this key is forbidden.
      try {
        receipt = await api.commitTechnicalPause(session.session_id, exact);
      } catch (retryError) {
        if (!technicalPauseDispositionIsUncertain(retryError)) {
          pendingTechnicalPause.current = null;
        }
        throw retryError;
      }
    }
    pendingTechnicalPause.current = null;
    // Do not publish a cursor from a slow response. Polling applies only a
    // same-session, monotonic server runtime; the safety latch stays closed.
    void runtimeControl.refresh();
    return receipt.runtime.status === "paused";
  }

  async function pauseTraining(
    technicalFailure = false,
    evidence?: { errorCode: string; attemptId?: number },
  ) {
    if (pauseOperationInFlight.current || pausePending || paused) return;
    pauseOperationInFlight.current = true;
    invalidateBedsideOperations();
    clearApTimers();
    apAwaitingAnswer.current = false;
    // 不等任何普通 live 写：先在所有同源老人端闩住媒体并撤销旧写，
    // 再让服务器 pause 原子投影成为最终真值。
    beginSafetyPause();
    setPausePending(true);
    if (!technicalFailure && (recState === "armed" || patientMicOn)) {
      armWatchdog(patientRec?.turnKey ?? lastArmedTurnK.current ?? turnK);
    }
    setRecState("idle");
    try {
      const confirmed = technicalFailure
        ? await commitAtomicTechnicalPause(evidence)
        : Boolean(await runtimeControl.pause());
      if (confirmed) toast("本场训练已暂停，患者端麦克风保持关闭", "ok");
      else if (technicalFailure) {
        toast("服务端原子暂停尚未确认；本机仍保持安全锁定，请检查连接后重试同步。", "danger");
      }
    } finally {
      setPausePending(false);
      pauseOperationInFlight.current = false;
    }
  }

  async function resumeTraining() {
    const next = await runtimeControl.resume();
    if (next) {
      releaseSafetyPause();
      toast("已恢复到暂停前的位置", "ok");
    }
  }

  async function enterWrapup() {
    if (wrapupPending || manualInteractionBlocked || !planTurn) return;
    invalidateBedsideOperations();
    clearApTimers();
    apAwaitingAnswer.current = false;
    setWrapupPending(true);
    if (recState === "armed" || patientMicOn) {
      armWatchdog(patientRec?.turnKey ?? lastArmedTurnK.current ?? turnK);
    }
    const accepted = await postCursor({
      screen: "thanks",
      itemIdx,
      turnIdx,
      responseRole: planTurn.response_role,
      cueLevel,
      recording: "stopped",
      recSeq: recSeq.current,
      selfStart: false,
    });
    if (!accepted) {
      toast("服务器尚未确认收麦，本场仍停留在训练页，请重试同步。", "danger");
      setWrapupPending(false);
      return;
    }
    setRecState("idle");
    onWrapup();
  }

  if (err) return <FailClosed msg={err} onRetry={() => setRetryNonce((n) => n + 1)} onExit={onExit} />;
  if (!plan) return <p>加载会话计划…</p>;
  if (plan.items.length === 0) return <p>本场次无评分题(第 1 周应走关系建立控制台)。</p>;

  return (
    <div className="training-layout">
      {!observerMode && (
        <ItemRail plan={plan} itemIdx={itemIdx} lockedCount={countLocked(journal, plan)}
          onPick={(i) => { if (!manualInteractionBlocked) { setItemIdx(i); setTurnIdx(0); } }} journalTurns={journal.turns} disabled={manualInteractionBlocked} />
      )}

      <div className="training-main">
        <div className="training-page-header">
          <div>
            <div className="page-kicker">当前训练任务</div>
            <h2 className="page-title">{observerMode
              ? observerPosition?.itemLabel ?? "同步中…"
              : item?.item_id.replace(/^(SE|DE)_/, "") ?? "训练判分"}</h2>
            <p className="page-description">{observerMode ? "AI 正在自动带练，你可以随时安全暂停并接管。" : "当前为人工模式，由你逐步操作。"}</p>
          </div>
          <div className="row wrap" style={{ gap: 8, alignItems: "center" }}>
            {/* 常驻的老人端连接状态点:研究者不用翻顶栏就能知道设备在不在。 */}
            {presence.state !== "unsupported" && (
              <StatusPill tone={presence.state === "online" ? "ok"
                : presence.state === "offline" ? "danger"
                  : presence.state === "unseen" ? "warn" : "muted"}>
                {presence.state === "online" ? "老人端在线"
                  : presence.state === "offline" ? "老人端已断开"
                    : presence.state === "unseen" ? "老人端未连接"
                      : presence.state === "checking" ? "正在确认老人端" : "老人端状态未知"}
              </StatusPill>
            )}
          {/* 收尾前必先停录:否则本屏卸载后无人发 idle,老人端麦克风持续开着 */}
          {!observerMode && (
            <Button variant="ghost" disabled={manualInteractionBlocked}
              onClick={() => { void enterWrapup(); }}>
              {wrapupPending ? "正在确认收麦…" : "进入场次收尾"}
            </Button>
          )}
          </div>
        </div>

        <SessionControlBar paused={paused} loading={recoveryPending} busy={runtimeControl.busy || pausePending || wrapupPending}
          resumeBlocked={observerMode || Boolean(apFailure)} recoveredLabel={recoveredLabel}
          terminalStatus={runtimeControl.terminalStatus} onExit={onExit}
          error={recoveryError} onRetry={retryRecovery}
          onPause={() => void pauseTraining()} onResume={() => void resumeTraining()} />

        <div className="form-actions" style={{ justifyContent: "flex-end" }}>
          <SessionAbortControl sessionId={session.session_id}
            expectedRevision={runtimeControl.runtime?.revision ?? null}
            disabled={recoveryPending || runtimeControl.busy || pausePending || wrapupPending || terminal}
            onAborted={() => onWrapup()} />
        </div>

        {patientDeviceFailure && (
          <Alert tone="danger" title="设备失败，场次已安全暂停">
            老人端麦克风未能安全启动。系统已关闭录音入口并保持当前题位与提示等级；
            本次技术失败不会记作临床错误回答，也不会触发提示升级或自动推进。
          </Alert>
        )}

        {apFailure && (
          <Alert tone="danger" title="自动驾驶已安全暂停 · 需要研究者接管"
            actions={<Button variant="primary" disabled={pausePending} onClick={() => void acceptManualTakeover()}>
              {pausePending ? "正在确认安全暂停…" : paused ? "确认并人工接管" : "重新确认安全暂停"}
            </Button>}>
            {apFailure.message} 当前仍停在第 {apFailure.itemIdx + 1} 题、第 {apFailure.turnIdx + 1} 环节，
            提示等级保持 {apFailure.cueLevel} 级；麦克风和自动定时器均已关闭。
            {!paused && " 故障锁将持续到服务器确认场次已暂停。"}
          </Alert>
        )}

        {operationalAutopilotReady === false && (
          <Alert tone="warn" title={exactDemoProfile
            ? "20 题模拟计划核对未通过"
            : "完整场次自动化尚未获内容放行"}>
            {exactDemoProfile ? (
              <>服务器返回的 20 题运行计划与本场冻结方案不一致，系统已停止自动启动。请刷新后重试；若仍然出现，请退出当前场次并联系管理员。</>
            ) : (
              <>源脚本本周共有 {operationalGapSummary.sourceTotal || "待核"} 个训练环节；其中
                {operationalGapSummary.protocol > 0 && ` ${operationalGapSummary.protocol} 个已有判定目标但缺自动交互协议，`}
                {operationalGapSummary.rubric > 0 && ` ${operationalGapSummary.rubric} 个开放回答缺评分边界，`}
                {operationalGapSummary.sourceField > 0 && ` ${operationalGapSummary.sourceField} 个缺源字段，`}
                {operationalGapSummary.unstructured > 0 && ` ${operationalGapSummary.unstructured} 个多要素环节尚未结构化，`}
                均不会由 AI 猜测补齐。浏览器本地自动推进保持关闭，不能宣称全场或真实研究自动化已开放。</>
            )}
            {unsupportedOperationalPositions.length > 0 && (
              <details style={{ marginTop: 8 }}>
                <summary>查看源脚本的 {unsupportedOperationalPositions.length} 个交付缺口</summary>
                <ul>{unsupportedOperationalPositions.map((row) => <li key={row}>{row}</li>)}</ul>
              </details>
            )}
          </Alert>
        )}

        <ServerAutopilotControl
          session={session}
          interactionBlocked={interactionBlocked}
          hasNamedAccount={hasNamedAccount}
          operationalAutopilotReady={operationalAutopilotReady}
          unsupportedOperationalPositions={unsupportedOperationalPositions}
          patientMicOn={patientMicOn}
          planPositionReady={Boolean(planTurn)}
          onOwnershipChange={onServerOwnershipChange}
          onReceiptPosition={onAutopilotReceiptPosition}
          prepareOwnership={prepareServerOwnership}
        />

        {observerMode ? (
          // 服务端拥有/尚不能证伪拥有/重同步未完成：人工逐步操作面板整块不挂载，
          // 只读观察台的位置只来自服务端 runtime.cursor 与冻结计划映射。
          <ObserverConsole
            patientCode={session.patient_id}
            phase={serverOwnership.phase}
            resyncStatus={manualResync.status}
            resyncError={manualResync.error}
            position={observerPosition}
            onRetryResync={() => { void runManualResync(); }}
          />
        ) : (
          <>
        {item && planTurn && (
          <div className={`card col training-work-card${manualInteractionBlocked ? " session-paused-surface" : ""}`}>
            <div className="training-work-header">
              <div className="row wrap">
                <StatusPill tone="primary">{item.task_type}</StatusPill>
                <span>环节 {planTurn.turn_seq}/{item.turns.length}</span>
                <strong>{planTurn.response_role}</strong>
              </div>
              <div className="row wrap" style={{ gap: 8 }}>
                {work.locked && <StatusPill tone="ok">已锁定</StatusPill>}
                {/* 常驻导航:每个环节都能一键前进/回退(老人端随游标同步),不必等锁分或翻左侧题列 */}
                <Button onClick={retreat} disabled={manualInteractionBlocked || atFirstTurn}>上一环节</Button>
                <Button onClick={advance} disabled={manualInteractionBlocked || atLastTurn}>下一环节</Button>
                {/* 禁用原因可见短句:iPad 无 hover,title 等于没说 */}
                {atFirstTurn && !manualInteractionBlocked && <span className="muted">已是第一个环节</span>}
                {atLastTurn && !manualInteractionBlocked && <span className="muted">已是最后一个环节</span>}
              </div>
            </div>

            <WorkflowStepper current={workflowStage} />

            <ItemReference item={item} bundle={bundle} />

            {/* 分级线索(0→3 只升不降;内容取版本锁定题库,推老人端显示) */}
            {!manualInteractionBlocked && !work.locked && (
              <CueButtons bundle={bundle} itemId={item.item_id} taskType={item.task_type}
                role={planTurn.response_role} cueLevel={cueLevel} onSend={sendCue}
                patientOnline={presence.state === "online"}
                patientScreenPaused={patientScreenPaused}
                patientLinkDown={patientLinkDown} />
            )}

            {/* 录音示意(老人端 VOX)——fail-closed:资格未确认不放行 */}
            <div className="recording-panel">
              {patientDeviceFailure ? (
                <StatusPill tone="danger">设备失败 · 录音入口已关闭</StatusPill>
              ) : recoveryPending ? (
                <StatusPill tone="muted">正在恢复场次，录音入口保持关闭</StatusPill>
              ) : recoveryError ? (
                <StatusPill tone="danger">场次恢复失败，录音入口保持关闭</StatusPill>
              ) : paused ? (
                <StatusPill tone="warn">场次暂停中，患者端麦克风保持关闭</StatusPill>
              ) : recStatus === "denied" ? (
                <StatusPill tone="danger">服务器当前未授权录音 · 已保持关麦</StatusPill>
              ) : recStatus === "loading" ? (
                <Button disabled>录音资格确认中…</Button>
              ) : recStatus === "error" ? (
                <>
                  <StatusPill tone="warn">录音授权服务暂不可用，已保持关麦</StatusPill>
                  <Button onClick={() => setRetryNonce((n) => n + 1)}>重试</Button>
                </>
              ) : !recordingEligible ? (
                <StatusPill tone="danger">录音资格未评，真实场次保持关麦</StatusPill>
              ) : recState === "armed" || patientMicOn ? (
                <>
                  <Button variant="danger" size="lg" onClick={stopRecording}>停止老人端录音</Button>
                  {recArmedAt !== null && (
                    <StatusPill tone="warn">录音中 {formatRecordingClock(Date.now() - recArmedAt)}</StatusPill>
                  )}
                  {patientMicOn && recState !== "armed" && (
                    <StatusPill tone="danger">老人端麦克风开着(自助开录)</StatusPill>
                  )}
                </>
              ) : (
                <>
                  <Button variant="primary" size="lg" onClick={armRecording} disabled={work.locked || patientLinkDown || patientScreenPaused}>开始老人端录音</Button>
                  {patientLinkDown && !work.locked && (
                    <span className="muted">老人端未连接——请先在老人端完成配对</span>
                  )}
                  {patientScreenPaused && !patientLinkDown && !work.locked && (
                    <span className="muted">老人端还停在暂停画面——等它恢复显示题目后再开始录音</span>
                  )}
                </>
              )}
              {watchdogUntil !== null && recState !== "armed" && (
                <StatusPill tone="warn">
                  正在等老人端回传录音…还剩 {watchdogSecondsLeft(watchdogUntil, Date.now())} 秒
                </StatusPill>
              )}
              {pendingAudio[turnK] && (
                <>
                  <StatusPill tone="ok">
                    已收到录音 {Math.max(1, Object.values(journal.audios)
                      .filter((a) => a.turnKey === turnK).length)} 条{pendingAudio[turnK].duration > 0
                      ? `（最新 ${pendingAudio[turnK].duration.toFixed(1)} 秒）` : ""}
                  </StatusPill>
                  <AuthenticatedAudio rawAudioId={pendingAudio[turnK].rawAudioId} />
                  {!work.savedAsr && <Button variant="primary" size="lg" onClick={tryLocalAsr} disabled={manualInteractionBlocked || busyOp !== null}>{busyOp === "asr" ? "AI 处理中…" : "AI 转写并登记证据"}</Button>}
                </>
              )}
            </div>

            {/* 阶段A 转写 */}
            <section className={`workflow-panel${workflowStage === 1 ? " is-active" : ""}`}>
              <div className="workflow-panel-header">
                <div><span className="workflow-panel-number">1</span><strong>记录与转写</strong></div>
                {work.savedAsr && <StatusPill tone="ok">已冻结</StatusPill>}
              </div>
              <Field label="受试者回答" hint="原始转写不可修改；如需校正，请在下一步人工确认里填写。">
                <TextInput value={work.asrText} disabled
                  onChange={() => { /* 权威 ASR 不接受客户端改写 */ }}
                  placeholder={pendingAudio[turnK] ? "点击“AI 转写并登记证据”" : "请先完成本环节录音"} />
              </Field>
              {!work.savedAsr && <Button variant="primary" onClick={saveTranscription}
                disabled={manualInteractionBlocked || savingTurn || busyOp !== null || !pendingAudio[turnK]}>
                {savingTurn ? "正在登记证据…" : "冻结已核验转写"}
              </Button>}
              {!work.savedAsr && !pendingAudio[turnK] && !manualInteractionBlocked && (
                <span className="muted">要先完成本环节录音，才能冻结转写</span>
              )}
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
                  <TextInput value={work.confirmed} disabled={manualInteractionBlocked || work.locked}
                    onChange={(e) => setWork((w) => ({ ...w, confirmed: e.target.value, savedConfirm: false }))} placeholder="人工校正后的规范文本" />
                </Field>
                {!work.locked && (
                  <div className="row">
                    <Button onClick={() => void saveConfirm()} disabled={manualInteractionBlocked || busyOp !== null || work.savedConfirm}>
                      {busyOp === "confirm" ? "保存中…" : work.savedConfirm ? "已确认" : "保存人工确认"}
                    </Button>
                    {!work.savedConfirm && work.turnId != null && <span className="muted">有未保存修改</span>}
                  </div>
                )}
              </section>
            )}

            {/* 阶段C AI 运行证据 */}
            {work.savedAsr && (
              <section className={`workflow-panel${workflowStage === 3 ? " is-active" : ""}`}>
                <div className="workflow-panel-header">
                  <div><span className="workflow-panel-number">3</span><strong>AI 运行证据</strong></div>
                  <StatusPill tone="muted">AI 初评</StatusPill>
                </div>
                <p className="muted">AI 对本次回答的初步判断，仅供参考，不会自动写入研究评分。</p>
                {work.ai && (
                  <div className="row wrap">
                    <StatusPill tone="muted">{work.ai.answerType ?? "纯人工判定"}</StatusPill>
                    <span>初评分:{work.ai.score ?? "—"}</span>
                    {work.ai.needsReview && <StatusPill tone="warn">需人工复核</StatusPill>}
                  </div>
                )}
              </section>
            )}

            {/* 阶段D 人工锁分 */}
            {work.savedAsr && !work.locked && (
              <LockControl key={turnK} taskType={item.task_type} role={planTurn.response_role}
                suggestedPromptLevel={cueLevel} onLock={doLock} locking={locking || manualInteractionBlocked}
                simulation={session.data_classification === "simulation"}
                basis={<ItemReference item={item} bundle={bundle} />}
                confirmedText={work.confirmed}
                unsavedConfirm={Boolean(work.confirmed.trim()) && !work.savedConfirm}
                active={workflowStage === 4} />
            )}
            {work.locked && <div className="row wrap"><StatusPill tone="ok">{session.data_classification === "simulation" ? "本环节模拟复核标注已锁定" : "本环节研究评分已锁定"}</StatusPill><Button variant="primary" disabled={manualInteractionBlocked} onClick={advance}>进入下一环节</Button></div>}
          </div>
        )}
          </>
        )}
      </div>
    </div>
  );
}

// ---------------- 工作态 ----------------
interface WorkState {
  turnId: number | null; asrText: string; confirmed: string;
  confirmationRevision: number;
  ai: { answerType: string | null; score: number | null; needsReview: boolean } | null;
  locked: boolean; savedAsr: boolean; savedConfirm: boolean;
}
const emptyWork = (): WorkState => ({ turnId: null, asrText: "", confirmed: "", confirmationRevision: 0, ai: null, locked: false, savedAsr: false, savedConfirm: false });

function countLocked(journal: ReturnType<typeof useSessionJournal>["journal"], plan: SessionPlan): number {
  return plan.items.filter((it) => it.turns.every((t) => journal.turns[`${it.item_id}#${t.turn_seq}`]?.locked)).length;
}

function WorkflowStepper({ current }: { current: number }) {
  const steps = ["记录转写", "人工确认", "AI 运行证据", "人工复核锁定"];
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
function CueButtons({ bundle, itemId, taskType, role, cueLevel, onSend, patientOnline, patientScreenPaused, patientLinkDown }: {
  bundle: ReturnType<typeof useItemBankBundle>["bundle"];
  itemId: string; taskType: string; role: string;
  cueLevel: 0 | 1 | 2 | 3; onSend: (level: 1 | 2 | 3) => void;
  patientOnline: boolean; patientScreenPaused: boolean; patientLinkDown: boolean;
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
  // 禁用原因可见短句(iPad 无 hover,不用 title):只解释"此刻按不下去的那颗"。
  const maxLevel = levels[levels.length - 1].level;
  const nextEntry = levels.find(({ level }) => level === cueLevel + 1) ?? null;
  const nextText = nextEntry
    ? lookupCue(bundle, itemId, taskType, role, nextEntry.level)
    : null;
  const lockedLabels = levels
    .filter(({ level }) => level > cueLevel + 1)
    .map(({ label }) => `「${label}」`);
  const disabledNote = cueLevel >= maxLevel
    ? "已是最高提示级，不能再升"
    : nextEntry && nextText == null
      ? `「${nextEntry.label}」的提示语暂未配置，本环节请口头提示`
      : lockedLabels.length > 0
        ? `提示只能逐级升：先发「${nextEntry?.label}」，${lockedLabels.join("")}才会解锁`
        : null;
  return (
    <div className="cue-panel">
      <div className="row wrap">
        <strong>分级线索</strong>
        <StatusPill tone={cueLevel === 0 ? "muted" : "warn"}>当前 {cueLevel} 级</StatusPill>
        {levels.map(({ level, label }) => {
          const text = lookupCue(bundle, itemId, taskType, role, level);
          const isTell = taskType === "单要素" && level === 3;
          return (
            <Button key={level} size="lg" disabled={level !== cueLevel + 1 || text == null}
              variant={isTell ? "danger" : "secondary"}
              onClick={() => (isTell ? setConfirmTell(true) : onSend(level))}>
              {label}{text == null ? "(缺文本)" : ""}
            </Button>
          );
        })}
      </div>
      {disabledNote && <p className="muted">{disabledNote}</p>}
      {/* 「正在显示」只有设备在线时才可断言;不在线时如实说没送到。
          在线但停在暂停屏时(D4②),患者看到的是暂停画面,不是线索。 */}
      {current && (
        <p className="muted">{patientScreenPaused
          ? `老人端正在显示暂停画面；恢复后会显示：${current}`
          : patientOnline
          ? `老人端正在显示：${current}`
          : patientLinkDown
            ? `已发送，但老人端此刻不在线，连接后会显示：${current}`
            : `已发送线索（老人端连接状态待确认）：${current}`}</p>
      )}
      <ConfirmDialog open={confirmTell} title="告知答案?"
        body={`老人端将显示并朗读:「${tellText ?? ""}」。这是最高提示级,发出后本环节提示等级不可再降。`}
        confirmLabel="告知答案"
        onConfirm={() => { setConfirmTell(false); onSend(3); }}
        onCancel={() => setConfirmTell(false)} />
    </div>
  );
}

// ---------------- 按角色切换的锁分控件 ----------------
function LockControl({ taskType, role, suggestedPromptLevel = 0, onLock, locking = false, simulation = false, basis, confirmedText, unsavedConfirm, active = false }: {
  taskType: string; role: string; suggestedPromptLevel?: number;
  onLock: (v: number, promptLevel?: number) => Promise<boolean>;
  locking?: boolean; simulation?: boolean; basis?: React.ReactNode; confirmedText?: string; unsavedConfirm?: boolean; active?: boolean;
}) {
  const [promptLevel, setPromptLevel] = useState<string | null>(String(suggestedPromptLevel));
  const [confirmVal, setConfirmVal] = useState<{ v: number; label: string } | null>(null);

  // 线索升级发生在锁分之后填的场景:跟随建议(研究者仍可改)。
  useEffect(() => { setPromptLevel(String(suggestedPromptLevel)); }, [suggestedPromptLevel]);

  const relation = isRelationRole(role);
  const single = taskType === "单要素";

  // 关系识别 0.5 档已于 2026-08-19 取消（钱凯口径：必须正确才给分）。
  const choices: { v: number; label: string; tone: "ok" | "warn" | "danger" }[] = relation
    ? [{ v: 1, label: "识别(1)", tone: "ok" }, { v: 0, label: "未识别(0)", tone: "danger" }]
    : [{ v: 1, label: single ? "命名正确(1)" : "正确(1)", tone: "ok" }, { v: 0, label: single ? "未正确(0)" : "错误(0)", tone: "danger" }];

  return (
    <section className={`workflow-panel${active ? " is-active" : ""}`}>
      <div className="workflow-panel-header">
        <div><span className="workflow-panel-number">4</span><strong>{simulation ? "模拟复核标注" : "人工研究评分"}</strong></div>
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
          simulation={simulation}
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

function LockConfirm({ label, role, promptLevel, simulation = false, basis, confirmedText, unsavedConfirm, locking, onConfirm, onCancel }: {
  label: string; role: string; promptLevel?: number;
  simulation?: boolean; basis?: React.ReactNode; confirmedText?: string; unsavedConfirm?: boolean; locking?: boolean;
  onConfirm: () => void; onCancel: () => void;
}) {
  const [armed, setArmed] = useState(false);
  const safeCancel = () => { if (!locking) onCancel(); };
  const panelRef = useDialogFocusTrap<HTMLDivElement>({
    open: true,
    onCancel: safeCancel,
    initialFocus: "first-button",
    focusKey: armed,
  });
  useEffect(() => { const t = setTimeout(() => setArmed(true), 400); return () => clearTimeout(t); }, []);
  return (
    <div className="dialog-backdrop" onClick={safeCancel}>
      <div ref={panelRef} className="dialog-panel fade-in" role="dialog" aria-modal="true"
        aria-labelledby="lock-confirm-title" aria-busy={locking || undefined} tabIndex={-1}
        onClick={(e) => e.stopPropagation()}>
        <div className="dialog-header"><h3 id="lock-confirm-title">{simulation ? "确认模拟复核标注" : "确认研究评分锁定"}</h3></div>
        <p>环节「{role}」将锁定为 <strong>{label}</strong>{promptLevel != null ? ` · 提示等级 ${promptLevel}` : ""}。</p>
        {/* 判分依据摆在眼前,不用取消弹窗滚回去翻 */}
        {basis && <div className="item-reference">{basis}</div>}
        {confirmedText?.trim() && <p className="muted">确认文本:「{confirmedText}」</p>}
        {unsavedConfirm && <p style={{ color: "var(--c-warn)" }}>确认文本有未保存修改:锁定时将自动保存这一版。</p>}
        <p className="muted">锁定后，本环节的确认文本和评分均不可再次修改。</p>
        <div className="dialog-actions">
          <Button onClick={safeCancel} disabled={locking}>取消</Button>
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
