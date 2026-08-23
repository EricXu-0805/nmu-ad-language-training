import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { api, ApiError } from "../../api";
import {
  autopilotServerOwnsConsole,
  autopilotConsoleReducer,
  AutopilotControlOperationEpoch,
  completePlanAllowsAutopilotStart,
  initialAutopilotConsoleState,
  isPrewriteStartRejection,
  p0aConsoleEligibility,
  receiptAllowsAutopilotTakeover,
  sameAutopilotStatusReceipt,
  type AutopilotConsoleState,
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
import type { Session } from "../../types";
import { hasExactWeek2Single20Profile } from "../../autopilot/demoProfile.ts";

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
}: {
  session: Session;
  interactionBlocked: boolean;
  hasNamedAccount: boolean;
  operationalAutopilotReady: boolean | null;
  unsupportedOperationalPositions: string[];
  patientMicOn: boolean;
  planPositionReady: boolean;
  onOwnershipChange: (owned: boolean, phase: AutopilotConsoleState["phase"]) => void;
  /** 权威回执里的只读位置投影(观察面/接管恢复展示用),无位置时回报 null。 */
  onReceiptPosition?: (position: { itemId: string; turnSeq: number } | null) => void;
  prepareOwnership: () => Promise<boolean>;
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
  const [providerReadiness, setProviderReadiness] = useState<ProviderReadiness | null>(null);
  const [providerReadinessError, setProviderReadinessError] = useState<string | null>(null);
  const [canProbeProvider, setCanProbeProvider] = useState(false);
  const [providerProbeBusy, setProviderProbeBusy] = useState(false);
  const acceptReceipt = useCallback((receipt: Awaited<ReturnType<typeof api.autopilotStatus>>) => {
    if (receipt.stateRevision < latestRevision.current) return;
    if (receipt.stateRevision === latestRevision.current && latestReceipt.current
        && !sameAutopilotStatusReceipt(latestReceipt.current, receipt)) {
      const error = "服务器返回了同版本但互相矛盾的控制状态";
      dispatch({ type: "status_uncertain", sessionId: session.session_id, error });
      onOwnershipChange(true, "uncertain");
      return;
    }
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

  const start = async () => {
    if (completePlanBlocked || providerReadiness?.startAllowed !== true
        || !eligibility.allowed
        || autopilotServerOwnsConsole(state) || startInFlight.current) return;
    startInFlight.current = true;
    controlWriteInFlight.current = true;
    operationEpoch.current.beginWrite();
    dispatch({ type: "start_requested", sessionId: session.session_id });
    // POST 的响应可能丢失；请求一发出就必须先收回旧人工控制，不能等绿色成功态。
    onOwnershipChange(true, "starting");
    try {
      if (!await prepareOwnership()) {
        const error = "老人端麦克风还未确认关闭";
        dispatch({ type: "start_rejected", sessionId: session.session_id, error });
        onOwnershipChange(false, "rejected");
        return;
      }
      const receipt = await api.startAutopilotP0a(session.session_id);
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
    state.phase, state.receipt]);

  if (!classificationProven) return null;

  const takeover = async () => {
    const receipt = state.receipt;
    if (!receiptAllowsAutopilotTakeover(receipt) || takeoverBusy) return;
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

  const scopeBlocked = !eligibility.allowed && eligibility.reason === "scope_unsupported";
  const accountBlocked = !eligibility.allowed && eligibility.reason === "account_required";
  const runtimeBlocked = !eligibility.allowed && eligibility.reason === "runtime_blocked";
  const active = state.phase === "waiting_tts" || state.phase === "waiting_recording";
  const processing = state.phase === "processing_attempt" || state.phase === "manual_draining";
  const paused = state.phase === "paused";
  const contentGap = paused
    && (state.receipt?.lastErrorCode === "operational_rubric_unavailable"
      || state.receipt?.lastErrorCode === "operational_protocol_unavailable");
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
              || autopilotServerOwnsConsole(state)}
            onClick={() => { void start(); }}
          >
            {manual ? "已转为人工操作"
              : state.phase === "starting" ? "正在核对启动条件…"
              : checking ? "正在恢复服务器状态…"
              : active ? "服务器正在控制当前位置"
                : processing ? "服务器正在处理回答"
                  : paused ? "等待研究者处理"
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
                      : isSimulation ? "启动 AI 自动带练（演练）"
                        : "启动 AI 自动带练"}
          </Button>
          {canTakeover && (
            <Button type="button" variant="danger" disabled={takeoverBusy}
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
        <>已转为人工操作，服务器已记录本次接管。如场次仍在暂停，请先确认老人状态再继续。</>
      ) : active ? (
        <>AI 正在进行当前环节；本页人工操作暂时关闭。</>
      ) : processing ? (
        <>服务器正在处理刚才的回答，请稍候。</>
      ) : paused ? (
        contentGap ? (
          <>下一题缺少自动训练内容，AI 已停下，不会跳题。请点「转为人工操作」继续。</>
        ) : (
          <>AI 已暂停，题目停在当前位置；要继续人工操作，请点「转为人工操作」。</>
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
      open={confirmTakeover}
      title="确认结束 AI 控制并转为人工操作？"
      body="服务器确认老人端麦克风已关闭后才会放行；接管会留下记录，技术故障不会算作老人答错。"
      confirmLabel="确认转为人工操作"
      onCancel={() => setConfirmTakeover(false)}
      onConfirm={() => { void takeover(); }}
    />
    </>
  );
}
