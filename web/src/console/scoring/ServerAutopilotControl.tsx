import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { api, ApiError } from "../../api";
import {
  AUTOPILOT_ADJUDICATION_REASONS,
  autopilotConflictCode,
  buildAutopilotAdjudicateRequest,
  buildAutopilotStartRequest,
  allowsAutopilotStartSelection,
  allowsBedsideDefaultStart,
  autopilotStartPreview,
  autopilotServerOwnsConsole,
  autopilotConsoleReducer,
  AutopilotControlOperationEpoch,
  completePlanAllowsAutopilotStart,
  initialAutopilotConsoleState,
  isPrewriteStartRejection,
  p0aConsoleEligibility,
  receiptAllowsAutopilotAdjudication,
  receiptAllowsAutopilotResume,
  receiptAllowsAutopilotTakeover,
  sameAutopilotStatusReceipt,
  type AutopilotAdjudicationKind,
  type AutopilotAdjudicationReason,
  type AutopilotConsoleState,
  type AutopilotStatusReceipt,
  type AutopilotStartOptions,
  type AutopilotStartSkipReason,
} from "../../autopilot/startControl";
import {
  canAutoStartServerAutopilot,
  latchBedsideActivation,
} from "../../autopilot/bedsideAutoStart";
import {
  isProviderReadinessPrewriteConflict,
  providerReadinessLabel,
  type ProviderReadiness,
} from "../../autopilot/providerReadiness";
import { PATIENT_ACTIVATION_EVENT } from "../../sync/messages";
import {
  autopilotWakeToken,
  nextServerOwnershipWake,
  PATIENT_AUTOPILOT_WAKE_EVENT,
} from "../../sync/autopilotWake";
import { Alert } from "../../components/Alert";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Field, TextInput } from "../../components/Field";
import type { JournalAttempt } from "../../hooks/useSessionJournal";
import type { Session, SessionPlan } from "../../types";
import { submitReviewedAdjudication } from "./autopilotAdjudication.ts";
import { hasExactWeek2Single20Profile } from "../../autopilot/demoProfile.ts";
import { autopilotAttemptView, type AutopilotAttemptView } from "./autopilotAttemptView.ts";
import { itemSeqLabel, itemSeqSummary } from "../itemNumbering.ts";

// 暂停原因用人话说一遍:研究者对着一串错误码不知道该去平板上看什么。
// 2026-09-13 演示:平板里留着上一场没传完的录音,自动带练一开麦就 recording_start_failed,
// 屏上只剩「已安全暂停」,钱凯只能说「进行不了训练」。
const AUTOPILOT_ERROR_HINTS: Record<string, string> = {
  recording_start_failed:
    "老人端麦克风没能启动。常见原因：平板里还留着上一场没传完的录音（系统会在下一次开麦前自动清掉），或浏览器没给麦克风权限。点「继续 AI 自动带练」再试一次；还不行就让老人端页面刷新一下。",
  microphone_denied: "老人端浏览器拒绝了麦克风权限，请在平板上允许后再点「继续 AI 自动带练」。",
  microphone_unavailable: "老人端找不到麦克风设备，请检查平板后再继续。",
  recording_upload_failed: "老人端录音上传或采集收据没有完成，检查网络后再点「继续 AI 自动带练」。",
  recording_runtime_failed: "老人端录音过程中出错（浏览器录音器异常），让老人端页面刷新一下再点「继续 AI 自动带练」。",
  device_command_timeout:
    "老人端没有在限定时间内回应。多半是老人端还在等点屏（屏上显示「点一下，接着听」），让老人点一下屏幕再点「继续 AI 自动带练」；如果屏幕黑了或页面关了，重新打开老人端页面。网络慢时也可能是老人端还在上传上一场没传完的录音，稍等再点「继续 AI 自动带练」。",
  audio_playback_failed: "老人端放不出这句引导语（拿不到音频、解码失败或浏览器拒绝播放）。检查平板音量和网络，让老人点一下屏幕，再点「继续 AI 自动带练」。",
  tts_cancelled: "引导语播放被打断（换页、切场或老人端被暂停）。确认老人端还在这一场，再点「继续 AI 自动带练」。",
  device_runtime_failed: "老人端自动流程内部出错，让老人端页面刷新一下再点「继续 AI 自动带练」。",
  intervention_completion_evidence_incomplete: "这一题的证据不完整（录音或判定没有落账），AI 不能替它宣布完成。点「转为人工操作」把这一题人工做完。",
  explicit_repeat_limit: "老人已多次要求重听，达到协议上限，AI 停下等你决定。点「继续 AI 自动带练」AI 会接着这一题（已经答过的，按最后一次回答给下一级提示或反馈）；也可以点「转为人工操作」人工完成。",
  autopilot_device_rotated: "老人端换了设备或重新配对，AI 已停下。确认新平板已配对到这一场后，点「继续 AI 自动带练」由 AI 接着当前题目；也可以「老人已答对」「跳过本题」，或「转为人工操作」。",
  // 研究者裁定(老人已答对/跳过本题)的写前 409:服务端在任何写入之前拒绝,
  // 提示常驻在卡片里,不折成会被下一次轮询抹掉的 uncertain。
  autopilot_adjudication_requires_pause: "先暂停，等平板收麦后再点。",
  autopilot_adjudication_saved: "现场决定已保存。AI 服务暂不可用，训练保持暂停；恢复后可继续 AI，或转为人工操作。",
  autopilot_adjudication_already_recorded: "这个环节的现场决定已经保存，不能重复裁定。请继续 AI 自动带练，或转为人工操作。",
  autopilot_adjudication_attempt_required: "这一题还没有录到老人的回答，AI 判不了「答对」；可以点「跳过本题」，或点「继续 AI 自动带练」让 AI 重问这一题。",
  autopilot_attempt_processing: "上一段录音还在判分，几秒后再点。",
  autopilot_attempt_failed: "上一段录音判分失败，AI 不能接着弹这一题；点「转为人工操作」把这一题人工完成。",
  autopilot_attempt_abandoned: "上一段录音的判分被暂停打断；可点「继续 AI 自动带练」，用已保存录音接着处理，或转为人工操作。",
  autopilot_resume_position_unresumable: "人工接管期间答过的这一题，AI 不能接着弹；请人工完成这一题后再切回。",
  autopilot_scope_completed: "本场可自动带练的题目已全部练完或裁定完毕；请直接进入场次收尾。",
  autopilot_revision_conflict: "服务器状态已更新，请核对当前题目与环节后重新操作。",
};
function autopilotErrorHint(code: string): string {
  return AUTOPILOT_ERROR_HINTS[code] ?? `错误码：${code}`;
}

// 裁定写前拒绝码:服务端明确未写入,可安全回退到刚重取的权威回执并常驻提示。
const ADJUDICATION_PREWRITE_CODES = new Set([
  "autopilot_adjudication_requires_pause",
  "autopilot_adjudication_attempt_required",
  "autopilot_adjudication_already_recorded",
  "autopilot_attempt_processing",
  "autopilot_attempt_failed",
  "autopilot_attempt_abandoned",
  "autopilot_scope_completed",
  "autopilot_revision_conflict",
]);
// 「继续」的写前拒绝码(收据 260 起题内续弹新增):同样在任何写入之前拒绝,提示要常驻,
// 不能折成 2.5 s 后被轮询抹掉的 uncertain。
const RESUME_PREWRITE_CODES = new Set([
  "autopilot_attempt_processing",
  "autopilot_attempt_failed",
  "autopilot_attempt_abandoned",
  "autopilot_resume_position_unresumable",
  "autopilot_scope_completed",
  "autopilot_revision_conflict",
]);

const ADJUDICATION_REASON_LABELS: Record<AutopilotAdjudicationReason, string> = {
  late_correct_after_window: "超时后才答对",
  asr_misrecognized: "识别错了，其实答对了",
  staff_judged_correct: "研究者在场判定答对",
  participant_declined: "老人不愿意答这题",
  asr_repeatedly_failed: "识别反复出错",
  trained_in_prior_sitting: "上一场已经练过这题",
  other: "其他",
};

interface AdjudicationDraft {
  reviewed: AutopilotStatusReceipt;
  positionLabel: string;
  kind: AutopilotAdjudicationKind;
  reason: AutopilotAdjudicationReason | null;
  note: string;
}

export function ServerAutopilotControl({
  session,
  interactionBlocked,
  hasNamedAccount,
  operationalAutopilotReady,
  unsupportedOperationalPositions,
  patientMicOn,
  planPositionReady,
  onOwnershipChange,
  onReceiptPosition,
  prepareOwnership,
  attempts,
  hasExistingEvidence,
  plan,
}: {
  session: Session;
  plan: SessionPlan | null;
  interactionBlocked: boolean;
  hasNamedAccount: boolean;
  operationalAutopilotReady: boolean | null;
  unsupportedOperationalPositions: string[];
  patientMicOn: boolean;
  planPositionReady: boolean;
  onOwnershipChange: (owned: boolean, phase: AutopilotConsoleState["phase"]) => void;
  /** 权威回执里的只读位置投影(观察面/接管恢复展示用),无位置时回报 null。 */
  onReceiptPosition?: (position: { itemId: string; turnSeq: number } | null) => void;
  prepareOwnership: () => Promise<true | string>;
  /** journal 的 attempts 投影(服务器持有期间由训练台定时补取),只给「AI 听到了什么」面板;null = 还没取到过。 */
  attempts: readonly JournalAttempt[] | null;
  /** 已恢复的题目/环节/录音/提示账本中是否存在人工或自动证据；服务端仍最终复核。 */
  hasExistingEvidence: boolean;
}) {
  const [state, dispatch] = useReducer(
    autopilotConsoleReducer,
    session.session_id,
    initialAutopilotConsoleState,
  );
  const eligibility = p0aConsoleEligibility(session, interactionBlocked, hasNamedAccount);
  const completePlanBlocked = !completePlanAllowsAutopilotStart(
    operationalAutopilotReady,
  );
  const exactDemoProfile = hasExactWeek2Single20Profile(session);
  const resolvedPlanLabel = exactDemoProfile ? "20 题模拟计划" : "完整训练计划";
  const isSimulation = session.is_simulation === true
    && session.data_classification === "simulation";
  const isRealResearch = session.is_simulation === false
    && session.data_classification === "research";
  // 分类不可证明（legacy_unknown/缺失/错配）时整个控件不渲染，也不轮询。
  const classificationProven = isSimulation || isRealResearch;
  const startInFlight = useRef(false);
  const controlWriteInFlight = useRef(false);
  const statusInFlight = useRef(false);
  const latestRevision = useRef(-1);
  const latestReceipt = useRef<Awaited<ReturnType<typeof api.autopilotStatus>> | null>(null);
  const operationEpoch = useRef(new AutopilotControlOperationEpoch());
  // 床旁激活信号:锁存 exact-session 的一次激活;自动写请求每个激活周期最多一次。
  // attempted 用同步 ref 而非 state——StrictMode 双跑同一 effect 时,第二跑必须
  // 立刻看到第一跑已尝试,不能等 setState 落地。
  const [bedsideActivation, setBedsideActivation] = useState<string | null>(null);
  const autoStartAttempted = useRef(false);
  // 已发出的 serverOwned 唤醒 token:同 session+同权威版本重复轮询不反复唤醒。
  const lastWakeToken = useRef<string | null>(null);
  const [confirmTakeover, setConfirmTakeover] = useState(false);
  const [takeoverBusy, setTakeoverBusy] = useState(false);
  const [confirmResume, setConfirmResume] = useState(false);
  const [resumeBusy, setResumeBusy] = useState(false);
  const [adjudication, setAdjudication] = useState<AdjudicationDraft | null>(null);
  const [adjudicateBusy, setAdjudicateBusy] = useState(false);
  const [adjudicationError, setAdjudicationError] = useState<string | null>(null);
  // 裁定写前被拒的提示,常驻到下一次裁定或服务器版本前进为止(D1 同一纪律)。
  const [controlNotice, setControlNotice] = useState<{ label: string; text: string } | null>(null);
  const [providerReadiness, setProviderReadiness] = useState<ProviderReadiness | null>(null);
  const [providerReadinessError, setProviderReadinessError] = useState<string | null>(null);
  const [canProbeProvider, setCanProbeProvider] = useState(false);
  const [providerProbeBusy, setProviderProbeBusy] = useState(false);
  const [startOrder, setStartOrder] = useState(1);
  const [startSkipReason, setStartSkipReason] = useState<AutopilotStartSkipReason | null>(null);
  const [startSkipNote, setStartSkipNote] = useState("");
  const [startSelectionError, setStartSelectionError] = useState<string | null>(null);
  const [startSelectionEdited, setStartSelectionEdited] = useState(false);
  const startSelectionEditedRef = useRef(false);
  const [confirmStartPosition, setConfirmStartPosition] = useState<AutopilotStartOptions | null>(null);
  const startPreview = autopilotStartPreview(plan, startOrder);
  const canSelectStart = allowsAutopilotStartSelection(
    state.receipt, attempts === null ? null : attempts.length, hasExistingEvidence,
  ) && autopilotStartPreview(plan, 1) !== null;
  const acceptReceipt = useCallback((receipt: Awaited<ReturnType<typeof api.autopilotStatus>>) => {
    if (receipt.stateRevision < latestRevision.current) return;
    if (receipt.stateRevision === latestRevision.current && latestReceipt.current
        && !sameAutopilotStatusReceipt(latestReceipt.current, receipt)) {
      const error = "服务器返回了同版本但互相矛盾的控制状态";
      dispatch({ type: "status_uncertain", sessionId: session.session_id, error });
      onOwnershipChange(true, "uncertain");
      return;
    }
    // 服务器版本前进 = 裁定被拒时的题位已经翻篇,旧提示不再成立。
    if (receipt.stateRevision > latestRevision.current) setControlNotice(null);
    latestRevision.current = receipt.stateRevision;
    latestReceipt.current = receipt;
    dispatch({ type: "status_received", sessionId: session.session_id, receipt });
    onReceiptPosition?.(
      receipt.positionItemId !== null && receipt.positionTurnSeq !== null
        ? { itemId: receipt.positionItemId, turnSeq: receipt.positionTurnSeq }
        : null,
    );
    onOwnershipChange(
      receipt.serverOwned,
      receipt.serverOwned ? receipt.status : "idle",
    );
    // 权威回执证明服务器已持有当前场次 → 对同窗患者端发一次性唤醒,让被
    // autopilot_not_active 闩在 legacy 的老人端重新探测一次并进入既有 server
    // runner。POST /start 回执与响应丢失后的 /status 对账都汇入本收口;
    // serverOwned=false 与 checking/rejected/uncertain(无权威回执)发不出唤醒。
    // 唤醒不是授权、不是使用证据,患者端仍由 capability 与服务端权威验证。
    const wake = nextServerOwnershipWake(lastWakeToken.current, session.session_id, receipt);
    if (wake) {
      lastWakeToken.current = autopilotWakeToken(wake);
      window.dispatchEvent(new CustomEvent(PATIENT_AUTOPILOT_WAKE_EVENT, { detail: wake }));
    }
  }, [onOwnershipChange, onReceiptPosition, session.session_id]);

  useEffect(() => {
    dispatch({ type: "reset", sessionId: session.session_id });
    latestRevision.current = -1;
    latestReceipt.current = null;
    onReceiptPosition?.(null);
    operationEpoch.current.invalidate();
    startInFlight.current = false;
    controlWriteInFlight.current = false;
    statusInFlight.current = false;
    setBedsideActivation(null);
    autoStartAttempted.current = false;
    lastWakeToken.current = null;
    setConfirmTakeover(false);
    setTakeoverBusy(false);
    setConfirmResume(false);
    setResumeBusy(false);
    setAdjudication(null);
    setAdjudicateBusy(false);
    setAdjudicationError(null);
    setControlNotice(null);
    setStartOrder(1);
    setStartSkipReason(null);
    setStartSkipNote("");
    setStartSelectionError(null);
    setStartSelectionEdited(false);
    startSelectionEditedRef.current = false;
    setConfirmStartPosition(null);
    if (!classificationProven) {
      onOwnershipChange(false, "idle");
      return undefined;
    }

    // Refresh is fail-closed: old manual controls stay locked until the
    // account-only status route proves the server has no owner.
    onOwnershipChange(true, "checking");
    let cancelled = false;
    const refreshStatus = async () => {
      if (cancelled || controlWriteInFlight.current || statusInFlight.current) return;
      statusInFlight.current = true;
      const readEpoch = operationEpoch.current.captureRead();
      try {
        const receipt = await api.autopilotStatus(session.session_id);
        if (!cancelled && !controlWriteInFlight.current
            && operationEpoch.current.accepts(readEpoch)) acceptReceipt(receipt);
      } catch (error) {
        if (!cancelled && !controlWriteInFlight.current
            && operationEpoch.current.accepts(readEpoch)) {
          const message = error instanceof ApiError ? error.detail
            : error instanceof Error ? error.message : String(error);
          dispatch({ type: "status_uncertain", sessionId: session.session_id, error: message });
          onOwnershipChange(true, "uncertain");
        }
      } finally {
        statusInFlight.current = false;
      }
    };
    void refreshStatus();
    const timer = window.setInterval(() => { void refreshStatus(); }, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [acceptReceipt, classificationProven, onOwnershipChange, onReceiptPosition,
    session.session_id]);

  useEffect(() => {
    setProviderReadiness(null);
    setProviderReadinessError(null);
    setCanProbeProvider(false);
    if (!classificationProven || !hasNamedAccount) return undefined;
    let cancelled = false;
    let inFlight = false;
    const refreshReadiness = async () => {
      if (cancelled || inFlight) return;
      inFlight = true;
      try {
        const next = await api.providerReadiness();
        if (!cancelled) {
          setProviderReadiness(next);
          setProviderReadinessError(null);
        }
      } catch (error) {
        if (!cancelled) {
          setProviderReadiness(null);
          setProviderReadinessError(error instanceof ApiError ? error.detail
            : error instanceof Error ? error.message : String(error));
        }
      } finally {
        inFlight = false;
      }
    };
    void api.authMe().then((identity) => {
      if (!cancelled) setCanProbeProvider(identity.role === "admin");
    }).catch(() => {
      if (!cancelled) setCanProbeProvider(false);
    });
    void refreshReadiness();
    const timer = window.setInterval(() => { void refreshReadiness(); }, 30_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [classificationProven, hasNamedAccount, session.session_id]);

  const runProviderProbe = async () => {
    if (!canProbeProvider || providerProbeBusy || autopilotServerOwnsConsole(state)) return;
    setProviderProbeBusy(true);
    setProviderReadinessError(null);
    try {
      setProviderReadiness(await api.probeProviderReadiness());
    } catch (error) {
      setProviderReadiness(null);
      setProviderReadinessError(error instanceof ApiError ? error.detail
        : error instanceof Error ? error.message : String(error));
    } finally {
      setProviderProbeBusy(false);
    }
  };

  const start = async (options?: AutopilotStartOptions) => {
    if (completePlanBlocked || providerReadiness?.startAllowed !== true
        || !eligibility.allowed
        || autopilotServerOwnsConsole(state) || startInFlight.current) return;
    if ((options?.startPresentationOrder ?? 1) !== startOrder
        || (startOrder > 1 && (!canSelectStart || !startPreview))) {
      setStartSelectionError("起始题目或本场记录已变化，请核对后重新选择；已开始的场次请使用继续训练。");
      return;
    }
    try {
      buildAutopilotStartRequest(session.session_id, options);
    } catch (error) {
      setStartSelectionError(error instanceof Error ? error.message : String(error));
      return;
    }
    setConfirmStartPosition(null);
    setStartSelectionError(null);
    startInFlight.current = true;
    controlWriteInFlight.current = true;
    operationEpoch.current.beginWrite();
    dispatch({ type: "start_requested", sessionId: session.session_id });
    // POST 的响应可能丢失；请求一发出就必须先收回旧人工控制，不能等绿色成功态。
    onOwnershipChange(true, "starting");
    try {
      const prepared = await prepareOwnership();
      if (prepared !== true) {
        // 拒因由 prepareOwnership 逐分支给出真话;这里绝不折成统一的麦克风文案。
        dispatch({ type: "start_rejected", sessionId: session.session_id, error: prepared });
        onOwnershipChange(false, "rejected");
        return;
      }
      const receipt = await api.startAutopilotP0a(session.session_id, options);
      acceptReceipt(receipt);
    } catch (error) {
      const message = error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
      const readinessPrewrite = isProviderReadinessPrewriteConflict(error);
      if (readinessPrewrite) {
        try {
          setProviderReadiness(await api.providerReadiness());
          setProviderReadinessError(null);
        } catch (refreshError) {
          setProviderReadiness(null);
          setProviderReadinessError(refreshError instanceof ApiError ? refreshError.detail
            : refreshError instanceof Error ? refreshError.message : String(refreshError));
        }
      }
      // 400/401/403/404/422 都在写入前失败；确定性门禁 409(部署开关/授权/范围)
      // 同样在写入前拒绝(D1:拒因要常驻,不折成会被轮询抹掉的 uncertain)。
      // 幂等/revision/CAS 类 409 可能表示服务器已经有 owner，timeout/5xx 也可能
      // 是“已提交但回执丢失”，必须保持锁定等待权威查询。
      const rejectedBeforeWrite = error instanceof ApiError
        && ([400, 401, 403, 404, 422].includes(error.status)
          || readinessPrewrite
          || isPrewriteStartRejection(error));
      dispatch({
        type: rejectedBeforeWrite ? "start_rejected" : "status_uncertain",
        sessionId: session.session_id,
        error: message,
      });
      onOwnershipChange(!rejectedBeforeWrite, rejectedBeforeWrite ? "rejected" : "uncertain");
    } finally {
      startInFlight.current = false;
      controlWriteInFlight.current = false;
    }
  };

  // 接收床旁激活信号(窗内事件——不 import 老人端源码)。只锁存与当前场次完全
  // 匹配的 sessionId;旧场次迟到事件、空值和重复事件都不会改变已锁存的状态。
  useEffect(() => {
    const onActivation = (event: Event) => {
      const sessionId = (event as CustomEvent<{ sessionId?: unknown }>).detail?.sessionId;
      setBedsideActivation((current) =>
        latchBedsideActivation(current, sessionId, session.session_id));
    };
    window.addEventListener(PATIENT_ACTIVATION_EVENT, onActivation);
    return () => window.removeEventListener(PATIENT_ACTIVATION_EVENT, onActivation);
  }, [session.session_id]);

  const startRef = useRef(start);
  startRef.current = start;

  // 老人端一次明确激活后,替研究者按一次既有的「启动」——同一条 start 链路,
  // 不新建第二条。信号先到、门禁未明时只等待;checking/starting/uncertain/
  // 服务器已持有/写前被拒 一律不自动发起,写请求每个激活周期最多一次。
  useEffect(() => {
    // 选择起点属于研究者操作：即使后来选回1，也要明确重置才恢复床旁自动启动。
    // ref 在原生选框获得焦点时即锁住，防激活信号早于 React 的状态更新。
    if (!allowsBedsideDefaultStart(startOrder, startSelectionEditedRef.current, confirmStartPosition !== null)) return;
    if (!canAutoStartServerAutopilot({
      sessionId: session.session_id,
      latchedActivationSessionId: bedsideActivation,
      eligibilityAllowed: eligibility.allowed,
      completePlanBlocked,
      providerStartAllowed: providerReadiness?.startAllowed === true,
      phase: state.phase,
      receiptProvesNoOwner: state.receipt !== null && !state.receipt.serverOwned,
      interactionBlocked,
      patientMicOn,
      planPositionReady,
      startInFlight: startInFlight.current,
      alreadyAttempted: autoStartAttempted.current,
    })) return;
    autoStartAttempted.current = true;
    void startRef.current();
  }, [bedsideActivation, completePlanBlocked, eligibility.allowed, interactionBlocked,
    patientMicOn, planPositionReady, providerReadiness, session.session_id,
    state.phase, state.receipt, startOrder, startSelectionEdited, confirmStartPosition]);

  if (!classificationProven) return null;

  const takeover = async () => {
    const receipt = state.receipt;
    if (!receiptAllowsAutopilotTakeover(receipt) || takeoverBusy || controlWriteInFlight.current) return;
    setConfirmTakeover(false);
    setTakeoverBusy(true);
    controlWriteInFlight.current = true;
    operationEpoch.current.beginWrite();
    // Releasing the server owner is itself a write whose response can be lost.
    // Keep the console locked until the authoritative receipt proves manual mode.
    onOwnershipChange(true, "uncertain");
    try {
      const latest = await api.autopilotStatus(session.session_id);
      acceptReceipt(latest);
      if (!receiptAllowsAutopilotTakeover(latest)) return;
      const next = await api.takeoverAutopilot(
        session.session_id,
        latest.stateRevision,
      );
      acceptReceipt(next);
    } catch (error) {
      const message = error instanceof ApiError ? error.detail
        : error instanceof Error ? error.message : String(error);
      dispatch({
        type: "status_uncertain",
        sessionId: session.session_id,
        error: message,
      });
      onOwnershipChange(true, "uncertain");
    } finally {
      setTakeoverBusy(false);
      controlWriteInFlight.current = false;
    }
  };

  // 与 takeover 同一收口范式:写前重取权威状态,写后收权威回执;失败折 uncertain
  // fail-closed。恢复成功后 serverOwned 仍为 true,不触发人工面重挂。
  const resume = async () => {
    if (!receiptAllowsAutopilotResume(state.receipt) || resumeBusy || controlWriteInFlight.current) return;
    setConfirmResume(false);
    setResumeBusy(true);
    controlWriteInFlight.current = true;
    operationEpoch.current.beginWrite();
    onOwnershipChange(true, "uncertain");
    try {
      const latest = await api.autopilotStatus(session.session_id);
      acceptReceipt(latest);
      if (!receiptAllowsAutopilotResume(latest)) return;
      try {
        const next = await api.resumeAutopilot(
          session.session_id,
          latest.stateRevision,
        );
        acceptReceipt(next);
      } catch (error) {
        const code = autopilotConflictCode(error);
        if (code === null || !RESUME_PREWRITE_CODES.has(code)) throw error;
        acceptReceipt(latest);
        setControlNotice({ label: "未能继续", text: autopilotErrorHint(code) });
      }
    } catch (error) {
      const message = error instanceof ApiError ? error.detail
        : error instanceof Error ? error.message : String(error);
      dispatch({
        type: "status_uncertain",
        sessionId: session.session_id,
        error: message,
      });
      onOwnershipChange(true, "uncertain");
    } finally {
      setResumeBusy(false);
      controlWriteInFlight.current = false;
    }
  };

  // Drafts retain the exact paused state the researcher saw. A status refresh or
  // revision conflict must never move that decision to another question/answer.
  const openAdjudication = (kind: AutopilotAdjudicationKind) => {
    const receipt = state.receipt;
    if (controlWriteInFlight.current || !receipt || !receiptAllowsAutopilotAdjudication(receipt)) return;
    const targetItem = plan?.items.find(row => row.item_id === receipt.positionItemId);
    const targetTurn = targetItem?.turns.find(row => row.turn_seq === receipt.positionTurnSeq);
    if (!targetItem || !targetTurn) return;
    setAdjudicationError(null);
    setAdjudication({
      kind, reason: null, note: "", reviewed: { ...receipt },
      positionLabel: `第 ${targetItem.presentation_order} 题 · ${targetItem.task_type} · 第 ${targetTurn.turn_seq} 环节（${targetTurn.response_role}）`,
    });
  };
  const adjudicate = async () => {
    const draft = adjudication;
    if (!draft?.reason || adjudicateBusy || controlWriteInFlight.current) return;
    const reason = draft.reason;
    // Local validation has no uncertain server outcome. Keep the editable draft
    // visible if copied notes contain control characters or exceed the contract.
    try {
      buildAutopilotAdjudicateRequest(
        session.session_id, draft.reviewed.stateRevision, draft.kind, reason, draft.note,
      );
    } catch (error) {
      setAdjudicationError(error instanceof Error ? error.message : String(error));
      return;
    }
    setAdjudicationError(null);
    setAdjudication(null);
    setAdjudicateBusy(true);
    setControlNotice(null);
    controlWriteInFlight.current = true;
    operationEpoch.current.beginWrite();
    onOwnershipChange(true, "uncertain");
    try {
      try {
        const result = await submitReviewedAdjudication({
          reviewed: draft.reviewed,
          readStatus: () => api.autopilotStatus(session.session_id),
          write: revision => api.adjudicateAutopilot(
            session.session_id, revision, draft.kind, reason, draft.note,
          ),
          acceptReceipt,
        });
        if (result === "changed") setControlNotice({
          label: "裁定未记录",
          text: "打开确认框后训练状态已改变。请核对当前题目、环节和回答，再重新选择；刚才的决定没有提交到新位置。",
        });
      } catch (error) {
        const code = autopilotConflictCode(error);
        if (code === null || !ADJUDICATION_PREWRITE_CODES.has(code)) throw error;
        setControlNotice({ label: "裁定未记录", text: autopilotErrorHint(code) });
      }
    } catch (error) {
      const message = error instanceof ApiError ? error.detail
        : error instanceof Error ? error.message : String(error);
      dispatch({
        type: "status_uncertain",
        sessionId: session.session_id,
        error: message,
      });
      onOwnershipChange(true, "uncertain");
    } finally {
      setAdjudicateBusy(false);
      controlWriteInFlight.current = false;
    }
  };

  const scopeBlocked = !eligibility.allowed && eligibility.reason === "scope_unsupported";
  const accountBlocked = !eligibility.allowed && eligibility.reason === "account_required";
  const runtimeBlocked = !eligibility.allowed && eligibility.reason === "runtime_blocked";
  const active = state.phase === "waiting_tts" || state.phase === "waiting_recording";
  const processing = state.phase === "processing_attempt" || state.phase === "manual_draining";
  const paused = state.phase === "paused";
  const contentGap = paused
    && (state.receipt?.lastErrorCode === "operational_rubric_unavailable"
      || state.receipt?.lastErrorCode === "operational_protocol_unavailable");
  // 最后一题被裁定后没有下一位可续:服务端留在安全暂停并把它写进 lastErrorCode,
  // 这里收起「继续/裁定」,只留人工接管与去收尾的提示。
  const scopeExhausted = paused && state.receipt?.lastErrorCode === "autopilot_scope_completed";
  const adjudicationSaved = paused && state.receipt?.lastErrorCode === "autopilot_adjudication_saved";
  const completed = state.phase === "scope_completed";
  const uncertain = state.phase === "uncertain";
  const checking = state.phase === "checking";
  const serverFailed = state.phase === "failed";
  const rejected = state.phase === "rejected";
  const providerBlocked = providerReadiness?.startAllowed !== true;
  const manual = state.receipt?.scopeKey === "p0a_sim_first_single_v1"
    && state.receipt.mode === "manual";
  const canTakeover = receiptAllowsAutopilotTakeover(state.receipt)
    && (paused || completed || serverFailed);
  // 裁定按钮与「继续 AI 自动带练」同一份收麦证明;内容缺口的暂停不许跳题。
  const adjudicationPositionKnown = plan?.items.some(row => row.item_id === state.receipt?.positionItemId
    && row.turns.some(turn => turn.turn_seq === state.receipt?.positionTurnSeq)) === true;
  const canAdjudicate = receiptAllowsAutopilotAdjudication(state.receipt)
    && adjudicationPositionKnown && !contentGap && !scopeExhausted;
  const controlBusy = resumeBusy || takeoverBusy || adjudicateBusy;
  const startSelectionBlocked = startOrder > 1
    && (!canSelectStart || !startPreview || startSkipReason === null);
  const markStartSelectionEdited = () => {
    startSelectionEditedRef.current = true;
    setStartSelectionEdited(true);
    setStartSelectionError(null);
  };
  const requestStart = () => {
    if (startOrder === 1) { void start(); return; }
    const options: AutopilotStartOptions = {
      startPresentationOrder: startOrder, skipReasonCode: startSkipReason, skipNote: startSkipNote,
    };
    try {
      buildAutopilotStartRequest(session.session_id, options);
      if (!canSelectStart || !startPreview) throw new Error("本场已有记录，不能重新指定训练起点");
      setStartSelectionError(null);
      setConfirmStartPosition(options);
    } catch (error) {
      setStartSelectionError(error instanceof Error ? error.message : String(error));
    }
  };
  // 「AI 听到了什么」:只在服务器持有且在题位上(带练中/处理中/AI 自己暂停)时展示,
  // 位置来自权威回执,回答来自 journal attempts 投影;人工接管态由人工面自己判分。
  const heardPosition = state.receipt?.serverOwned
    && state.receipt.positionItemId !== null && state.receipt.positionTurnSeq !== null
    ? { itemId: state.receipt.positionItemId, turnSeq: state.receipt.positionTurnSeq }
    : null;
  const heardVisible = heardPosition !== null && (active || processing || paused);
  const heard = heardVisible && attempts !== null ? autopilotAttemptView(attempts, heardPosition) : null;
  const title = manual ? "AI 自动带练已转为人工接管"
    : active ? "AI 正在控制当前环节"
    : processing ? "AI 正在处理当前回答"
      : contentGap ? "下一题内容不全，AI 已停下"
      : paused ? "AI 自动带练已安全暂停"
        : completed ? "当前可自动范围已完成"
          : serverFailed ? "AI 自动带练已进入失败锁定"
            : checking ? "正在核对服务器控制权"
          : uncertain ? "服务器状态待核实 · 人工控制已锁定"
            : isSimulation ? "AI 自动带练（演练）" : "AI 自动带练";

  return (
    <>
    <Alert
      tone={manual ? "warn" : active || processing || completed ? "ok" : paused || uncertain || serverFailed || rejected ? "danger" : "warn"}
      title={title}
      actions={
        <div className="row wrap">
          <Button
            type="button"
            variant={autopilotServerOwnsConsole(state) ? "secondary" : "primary"}
            disabled={manual || completePlanBlocked || !eligibility.allowed
              || providerBlocked
              || startSelectionBlocked
              || autopilotServerOwnsConsole(state)}
            onClick={requestStart}
          >
            {manual ? "已转为人工操作"
              : state.phase === "starting" ? "正在核对启动条件…"
              : checking ? "正在恢复服务器状态…"
              : active ? "服务器正在控制当前位置"
                : processing ? "服务器正在处理回答"
                  : paused ? "AI 已安全暂停"
                    : completed ? "当前可自动范围已完成"
                      : serverFailed ? "服务器失败·人工入口已锁定"
                      : uncertain ? "等待权威状态核实"
                : completePlanBlocked ? operationalAutopilotReady === null
                  ? `正在核对${resolvedPlanLabel}`
                  : `${resolvedPlanLabel}的题目内容未配齐`
                : providerBlocked ? providerReadiness === null
                  ? "正在核对 AI 服务实测"
                  : "AI 服务实测未通过或已过期"
                : scopeBlocked ? "当前安排不支持 AI 自动带练"
                  : accountBlocked ? "需要具名研究账号"
                  : runtimeBlocked ? "场次未处于可启动状态"
                    : rejected ? "重新核对并启动"
                      : startOrder > 1 ? `确认从第 ${startOrder} 题开始`
                      : isSimulation ? "启动 AI 自动带练（演练）"
                        : "启动 AI 自动带练"}
          </Button>
          {receiptAllowsAutopilotResume(state.receipt) && !contentGap && !scopeExhausted && (
            <Button type="button" variant="primary" disabled={controlBusy}
              onClick={() => setConfirmResume(true)}>
              {resumeBusy ? "正在恢复 AI 自动带练…"
                : manual ? "切回 AI 自动带练" : "继续 AI 自动带练"}
            </Button>
          )}
          {canAdjudicate && (
            <>
              <Button type="button" variant="secondary" disabled={controlBusy}
                onClick={() => openAdjudication("confirmed_correct")}>
                {adjudicateBusy ? "正在记录裁定…" : "老人已答对"}
              </Button>
              <Button type="button" variant="secondary" disabled={controlBusy}
                onClick={() => openAdjudication("skip_item")}>
                跳过本题
              </Button>
            </>
          )}
          {canTakeover && (
            <Button type="button" variant="danger" disabled={controlBusy}
              onClick={() => setConfirmTakeover(true)}>
              {takeoverBusy ? "正在确认麦克风已关闭…" : "转为人工操作"}
            </Button>
          )}
          {canProbeProvider && !autopilotServerOwnsConsole(state) && (
            <Button type="button" variant="secondary" disabled={providerProbeBusy}
              onClick={() => { void runProviderProbe(); }}>
              {providerProbeBusy ? "正在检查 AI 服务…" : "检查 AI 服务"}
            </Button>
          )}
        </div>
      }
    >
      {manual ? (
        <>已转为人工操作，服务器已记录本次接管。要回到自动模式，点「切回 AI 自动带练」，AI 会从当前未完成的题目接着做；人工接管期间答过的题目 AI 不能接着弹，请先人工完成这一题再切回。如场次仍在暂停，请先确认老人状态再继续。</>
      ) : active ? (
        <>AI 正在进行当前环节；本页人工操作暂时关闭。</>
      ) : processing ? (
        <>服务器正在处理刚才的回答，请稍候。</>
      ) : paused ? (
        contentGap ? (
          <>下一题缺少自动训练内容，AI 已停下，不会跳题。请点「转为人工操作」继续。</>
        ) : scopeExhausted ? (
          <>{autopilotErrorHint("autopilot_scope_completed")}如需补做，请点「转为人工操作」。</>
        ) : adjudicationSaved ? (
          <>{autopilotErrorHint("autopilot_adjudication_saved")}</>
        ) : (
          <>
            AI 已安全暂停，题目停在当前位置。点「继续 AI 自动带练」由 AI 接着当前题目（已经答过的，按最后一次回答给下一级提示或反馈）；或点「转为人工操作」由你人工继续本场。
            {canAdjudicate && (
              <div style={{ marginTop: "var(--sp-1)" }}>
                老人其实答对了、或这题不该再问，可点「老人已答对」结束当前环节，或「跳过本题」结束本题剩余环节。保存决定后会尝试继续；AI 服务不可用时保持暂停。AI 自己的判定原样保留，你的决定连同账号名和原因一起记录；研究评分仍以事后复核为准。
              </div>
            )}
            {state.receipt?.lastErrorCode && (
              <div style={{ marginTop: "var(--sp-1)" }}>
                {autopilotErrorHint(state.receipt.lastErrorCode)}
              </div>
            )}
          </>
        )
      ) : completed ? (
        <>{isSimulation ? "本次模拟训练" : "本场训练"}的自动部分已完成；如需继续，请点「转为人工操作」。</>
      ) : serverFailed ? (
        <>
          AI 自动训练出错停止，页面保持锁定；请联系研究团队处理。
          {state.receipt?.lastErrorCode && (
            <details style={{ marginTop: "var(--sp-2)" }}>
              <summary>技术详情</summary>
              <div style={{ marginTop: "var(--sp-1)" }}>错误码：{state.receipt.lastErrorCode}</div>
            </details>
          )}
        </>
      ) : checking ? (
        <>正在向服务器确认控制状态，请稍候。</>
      ) : uncertain ? (
        <>启动结果待确认，页面已锁定，请稍候。</>
      ) : completePlanBlocked ? (
        operationalAutopilotReady === null ? (
          <>正在核对训练内容，请稍候。</>
        ) : (
          <>训练内容还有 {unsupportedOperationalPositions.length} 处未配齐，AI 自动带练不可启动，请人工操作。</>
        )
      ) : providerBlocked ? (
        <>
          {providerReadinessLabel(providerReadiness)}。请管理员先点「检查 AI 服务」，通过后才能启动。
          {providerReadinessError ? ` 检查状态读取失败：${providerReadinessError}。` : ""}
        </>
      ) : (
        <>
          AI 服务检查已通过，可以启动。
          <details style={{ marginTop: "var(--sp-2)" }}>
            <summary>技术详情</summary>
            <div style={{ marginTop: "var(--sp-1)" }}>
              启动时服务器会自动核对：研究账号、{isSimulation ? "模拟档案与开关" : "研究档案、部署开关与云处理授权"}、录音授权、场次状态、配对设备；缺少内容的环节会自动停下，不会跳题。
            </div>
          </details>
        </>
      )}
      {controlNotice && (
        <div role="alert" style={{ marginTop: "var(--sp-1)" }}>
          {controlNotice.label}：{controlNotice.text}
        </div>
      )}
      {(canSelectStart || (startOrder > 1 && !autopilotServerOwnsConsole(state))) && (
        <div className="col" style={{ gap: "var(--sp-2)", marginTop: "var(--sp-2)" }}>
          <Field label="本场从哪一题开始" hint="仅新场次可选。题号来自本场冻结计划；之前的题会留下跳过记录，不算本场已完成的回答。">
            <select className="form-control" value={startOrder}
              disabled={!canSelectStart || startInFlight.current}
              onFocus={markStartSelectionEdited}
              onChange={(event) => {
                markStartSelectionEdited();
                setStartOrder(Number(event.target.value));
                setStartSkipReason(null);
                setStartSkipNote("");
              }}>
              {plan?.items.map((item) => {
                const seq = itemSeqLabel(item.task_type, item.presentation_order);
                return <option key={item.item_id} value={item.presentation_order}>
                  {seq ? itemSeqSummary(item.task_type, seq) : `第 ${item.presentation_order} 题`} · {item.item_id}
                </option>;
              })}
            </select>
          </Field>
          {startOrder > 1 && startPreview && (
            <>
              <div>从第 {startOrder} 题开始；前面的 {startPreview.skippedItems.length} 题、{startPreview.skippedTurns} 个环节将记录为跳过。</div>
              <details>
                <summary>核对将跳过的题目</summary>
                <ul>{startPreview.skippedItems.map((item) => <li key={item.item_id}>
                  第 {item.presentation_order} 题 · {item.task_type} · {item.item_id}（{item.turns.length} 个环节）
                </li>)}</ul>
              </details>
              <Field label="跳过前面题目的原因" required>
                <select className="form-control" value={startSkipReason ?? ""} disabled={!canSelectStart}
                  onChange={(event) => {
                    markStartSelectionEdited();
                    setStartSkipReason(event.target.value === "" ? null : event.target.value as AutopilotStartSkipReason);
                  }}>
                  <option value="">请选择原因</option>
                  <option value="trained_in_prior_sitting">上一场已经练过</option>
                  <option value="other">其他</option>
                </select>
              </Field>
              <Field label="跳过备注（可不填，200 字以内）">
                <TextInput value={startSkipNote} maxLength={200} disabled={!canSelectStart}
                  onChange={(event) => { markStartSelectionEdited(); setStartSkipNote(event.target.value); }} />
              </Field>
            </>
          )}
          {startSelectionEdited && (
            <div className="row wrap">
              <span>正在核对训练起点，请由研究者确认启动；老人端点击不会自动开始。</span>
              <Button variant="ghost" disabled={!canSelectStart} onClick={() => {
                setStartOrder(1); setStartSkipReason(null); setStartSkipNote("");
                setStartSelectionError(null); setConfirmStartPosition(null);
                startSelectionEditedRef.current = false; setStartSelectionEdited(false);
              }}>{autoStartAttempted.current ? "重置为第 1 题" : "重置为第 1 题并恢复床旁自动启动"}</Button>
            </div>
          )}
          {autoStartAttempted.current && (
            <div>本场已尝试过自动启动，重置起点后仍需由研究者手动点击启动；老人端点击不会自动重试。</div>
          )}
          {startSelectionError && <div role="alert">未启动：{startSelectionError}</div>}
          {!canSelectStart && <div role="alert">本场记录已变化，指定起点已关闭；请核对现有进度。</div>}
        </div>
      )}
      {heardVisible && <HeardPanel view={heard} loaded={attempts !== null} />}
      {isRealResearch && (
        <div style={{ marginTop: 6, fontSize: "0.9em", opacity: 0.85 }}>
          训练引导语为研究初版，尚未经临床定稿；请按研究方案核对后使用。
        </div>
      )}
      {providerReadiness && (
        <details style={{ marginTop: 8 }}>
          <summary>{providerReadinessLabel(providerReadiness)}</summary>
          <div style={{ marginTop: 6 }}>
            TTS：{providerReadiness.tts.success ? "通过" : `未通过（${providerReadiness.tts.failureCode ?? "unknown"}）`}，
            ASR：{providerReadiness.asr.success ? "通过" : `未通过（${providerReadiness.asr.failureCode ?? "unknown"}）`}，
            LLM：{providerReadiness.llm.required ? "必需" : "非必需"}·
            {providerReadiness.llm.success ? "通过" : `未通过（${providerReadiness.llm.failureCode ?? "unknown"}）`}。
            {providerReadiness.checkedAt && providerReadiness.expiresAt
              ? ` 检查时间 ${new Date(providerReadiness.checkedAt).toLocaleString()}，有效至 ${new Date(providerReadiness.expiresAt).toLocaleString()}。`
              : " 尚未执行合成检查。"}
          </div>
        </details>
      )}
      {state.error && <div role="alert" style={{ marginTop: 6 }}>
        {uncertain ? "状态核实失败" : "启动未通过"}：{state.error}。
        {uncertain ? " 人工入口继续锁定，避免与可能已启动的服务器流程并行。" : " 服务器在写入前拒绝了请求，未产生任何记录。"}
      </div>}
      {/* D1:拒因不许无痕消失——权威 no-owner 回执把强横幅降级为持久提示,
          直到再次点启动或服务器真的持有。 */}
      {!state.error && state.lastStartRejection && (
        <div role="alert" style={{ marginTop: 6 }}>
          上次启动被拒：{state.lastStartRejection}。处理后可重新点「启动 AI 自动带练」。
        </div>
      )}
    </Alert>
    <ConfirmDialog
      open={confirmStartPosition !== null}
      title={`确认从第 ${confirmStartPosition?.startPresentationOrder ?? startOrder} 题开始训练？`}
      body={confirmStartPosition && startPreview && (
        <div className="col" style={{ gap: "var(--sp-2)" }}>
          <strong>起始题：第 {startPreview.startItem.presentation_order} 题 · {startPreview.startItem.task_type} · {startPreview.startItem.item_id}</strong>
          <div>前面的 {startPreview.skippedItems.length} 题、{startPreview.skippedTurns} 个环节将留下署名跳过记录；不会补造回答、录音或正确分数。启动后不能更改这次选择。</div>
          <div>原因：{confirmStartPosition.skipReasonCode === "trained_in_prior_sitting" ? "上一场已经练过" : "其他"}</div>
          {confirmStartPosition.skipNote?.trim() && <div>备注：{confirmStartPosition.skipNote.trim()}</div>}
          <div>请确认老人已准备好，启动后 AI 会从上面这道题开始提问。</div>
        </div>
      )}
      confirmLabel="确认跳过前面题目并开始"
      confirmVariant="primary"
      confirmDisabled={startSelectionBlocked || !eligibility.allowed || providerBlocked || completePlanBlocked}
      onCancel={() => setConfirmStartPosition(null)}
      onConfirm={() => { if (confirmStartPosition) void start(confirmStartPosition); }}
    />
    <ConfirmDialog
      open={confirmTakeover}
      title="确认结束 AI 控制并转为人工操作？"
      body="服务器确认老人端麦克风已关闭后才会放行；接管会留下记录，技术故障不会算作老人答错。"
      confirmLabel="确认转为人工操作"
      onCancel={() => setConfirmTakeover(false)}
      onConfirm={() => { void takeover(); }}
    />
    <ConfirmDialog
      open={confirmResume}
      title={manual ? "确认切回 AI 自动带练？" : "确认继续 AI 自动带练？"}
      body="AI 会接着当前题目；处理未完的录音会用已保存的录音继续判分，不会重新开麦。已经判完的回答会给下一级提示或反馈，需要新回答时再开启麦克风。请先确认老人已准备好继续，恢复会留下记录。"
      confirmLabel={manual ? "确认切回 AI" : "确认继续 AI"}
      onCancel={() => setConfirmResume(false)}
      onConfirm={() => { void resume(); }}
    />
    <ConfirmDialog
      open={adjudication !== null}
      title={adjudication?.kind === "skip_item" ? "确认跳过本题？" : "确认老人已答对？"}
      body={adjudication && (
        <div className="col" style={{ gap: "var(--sp-2)" }}>
          <strong>{adjudication.positionLabel}</strong>
          {adjudicationError && <div role="alert">裁定未记录：{adjudicationError}</div>}
          <div>
            {adjudication.kind === "skip_item"
              ? "系统会结束本题剩余环节并记录跳过；已经完成的环节保留。保存后尝试继续下一题，AI 服务不可用时保持暂停。"
              : "系统会记录你确认当前环节答对。保存后尝试继续下一环节，AI 服务不可用时保持暂停；同一题的其他环节仍需训练。"}
            AI 自己的判定原样保留；你的决定连同账号名和下面选的原因一起记录，研究评分仍以事后复核为准。
          </div>
          <fieldset className="col" style={{ gap: "var(--sp-1)", border: 0, padding: 0, margin: 0 }}>
            <legend className="field__label">原因</legend>
            {AUTOPILOT_ADJUDICATION_REASONS[adjudication.kind].map((code) => (
              <label key={code} className="toggle-field">
                <input type="radio" name="autopilot-adjudication-reason" value={code}
                  checked={adjudication.reason === code}
                  onChange={() => setAdjudication({ ...adjudication, reason: code })} />
                {ADJUDICATION_REASON_LABELS[code]}
              </label>
            ))}
          </fieldset>
          <Field label="备注（可不填，200 字以内）">
            <TextInput value={adjudication.note} maxLength={200}
              onChange={(event) => setAdjudication({ ...adjudication, note: event.target.value })} />
          </Field>
        </div>
      )}
      confirmLabel={adjudication?.kind === "skip_item" ? "确认跳过本题" : "确认老人已答对"}
      confirmVariant={adjudication?.kind === "skip_item" ? "danger" : "primary"}
      busy={adjudicateBusy}
      confirmDisabled={!adjudication?.reason}
      onCancel={() => setAdjudication(null)}
      onConfirm={() => { void adjudicate(); }}
    />
    </>
  );
}

// 2026-09-17 养老院实测:研究者看不到 ASR 听成了什么(螺母→刘世茂、茶杯→查呗),
// 只能猜 AI 为什么判错。这里只展示,不参与控制判定;判类是运营决策,不是研究评分。
function HeardPanel({ view, loaded }: { view: AutopilotAttemptView | null; loaded: boolean }) {
  if (!loaded) {
    return (
      <div style={{ marginTop: 6, fontSize: "0.9em", opacity: 0.85 }}>
        AI 听到的：还没取到本场的回答记录，稍等。
      </div>
    );
  }
  if (!view) {
    return (
      <div style={{ marginTop: 6, fontSize: "0.9em", opacity: 0.85 }}>
        AI 听到的：这一题还没有录到老人的回答。
      </div>
    );
  }
  const heard = view.heard.kind === "pending" ? "转写中…"
    : view.heard.kind === "silence" ? "没有识别到语音"
      : `「${view.heard.text}」`;
  const verdict = view.verdict.kind === "pending" ? "判分中…"
    : view.verdict.kind === "failed"
      ? `处理失败（技术原因${view.verdict.errorCode ? `：${view.verdict.errorCode}` : ""}）`
      : `${view.verdict.answerType}${view.verdict.score !== null ? ` · ${view.verdict.score} 分` : ""}${view.verdict.needsReview ? " · 建议复核" : ""}`;
  return (
    <div style={{ marginTop: 6, fontSize: "0.9em" }} aria-live="polite">
      <div><strong>AI 听到的</strong>（本题第 {view.attemptSeq} 次回答 · {view.promptLabel}）</div>
      <div>识别：{heard}</div>
      <div>AI 判类：{verdict}<span style={{ opacity: 0.7 }}>（仅供参考，研究评分以事后复核为准）</span></div>
    </div>
  );
}
