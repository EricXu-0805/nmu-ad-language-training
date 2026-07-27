import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { api, ApiError } from "../../api";
import {
  autopilotServerOwnsConsole,
  autopilotConsoleReducer,
  AutopilotControlOperationEpoch,
  completePlanAllowsAutopilotStart,
  initialAutopilotConsoleState,
  p0aConsoleEligibility,
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
import { Alert } from "../../components/Alert";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import type { Session } from "../../types";

export function ServerAutopilotControl({
  session,
  interactionBlocked,
  hasNamedAccount,
  operationalAutopilotReady,
  unsupportedOperationalPositions,
  patientMicOn,
  planPositionReady,
  onOwnershipChange,
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
  const isSimulation = session.is_simulation === true
    && session.data_classification === "simulation";
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
    onOwnershipChange(
      receipt.serverOwned,
      receipt.serverOwned ? receipt.status : "idle",
    );
  }, [onOwnershipChange, session.session_id]);

  useEffect(() => {
    dispatch({ type: "reset", sessionId: session.session_id });
    latestRevision.current = -1;
    latestReceipt.current = null;
    operationEpoch.current.invalidate();
    startInFlight.current = false;
    controlWriteInFlight.current = false;
    statusInFlight.current = false;
    setBedsideActivation(null);
    autoStartAttempted.current = false;
    setConfirmTakeover(false);
    setTakeoverBusy(false);
    if (!isSimulation) {
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
  }, [acceptReceipt, isSimulation, onOwnershipChange, session.session_id]);

  useEffect(() => {
    setProviderReadiness(null);
    setProviderReadinessError(null);
    setCanProbeProvider(false);
    if (!isSimulation || !hasNamedAccount) return undefined;
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
  }, [hasNamedAccount, isSimulation, session.session_id]);

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
        const error = "老人端安全收麦尚未得到服务器确认";
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
      // 400/401/403/404/422 都在写入前失败；409 可能表示服务器已经有 owner，
      // timeout/5xx 也可能是“已提交但回执丢失”，必须保持锁定等待权威查询。
      const rejectedBeforeWrite = error instanceof ApiError
        && ([400, 401, 403, 404, 422].includes(error.status)
          || readinessPrewrite);
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

  if (!isSimulation) return null;

  const takeover = async () => {
    const receipt = state.receipt;
    if (!receipt?.serverOwned || takeoverBusy) return;
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
      if (!latest.serverOwned) return;
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
  const canTakeover = state.receipt?.serverOwned === true
    && (paused || completed || serverFailed);
  const title = manual ? "AI 自动干预已完成审计接管"
    : active ? "当前冻结位置由服务器 AI 控制"
    : processing ? "AI 正在处理当前回答"
      : contentGap ? "AI 已停在缺少冻结内容的边界"
      : paused ? "AI 自动干预已安全暂停"
        : completed ? "当前可自动范围已完成"
          : serverFailed ? "AI 自动干预已进入失败锁定"
            : checking ? "正在核对服务器控制权"
          : uncertain ? "服务器状态待核实 · 人工控制已锁定"
            : "服务器 AI 自动干预 · 仅模拟且逐位置校验";

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
            {manual ? "已转为人工处置"
              : state.phase === "starting" ? "正在核对服务器门禁…"
              : checking ? "正在恢复服务器状态…"
              : active ? "服务器正在控制当前位置"
                : processing ? "服务器正在处理回答"
                  : paused ? "等待研究者处置"
                    : completed ? "当前可自动范围已完成"
                      : serverFailed ? "服务器失败·人工入口已锁定"
                      : uncertain ? "等待权威状态核实"
                : completePlanBlocked ? operationalAutopilotReady === null
                  ? "正在核对完整冻结计划"
                  : "完整冻结计划仍有自动协议缺口"
                : providerBlocked ? providerReadiness === null
                  ? "正在核对 AI 服务实测"
                  : "AI 服务实测未通过或已过期"
                : scopeBlocked ? "当前安排不支持服务器自动干预"
                  : accountBlocked ? "需要具名研究账号"
                  : runtimeBlocked ? "场次未处于可启动状态"
                    : rejected ? "重新核对并启动"
                      : "启动模拟 AI 自动干预"}
          </Button>
          {canTakeover && (
            <Button type="button" variant="danger" disabled={takeoverBusy}
              onClick={() => setConfirmTakeover(true)}>
              {takeoverBusy ? "正在核对收麦…" : "收麦后转为人工处置"}
            </Button>
          )}
          {canProbeProvider && !autopilotServerOwnsConsole(state) && (
            <Button type="button" variant="secondary" disabled={providerProbeBusy}
              onClick={() => { void runProviderProbe(); }}>
              {providerProbeBusy ? "正在实际检查 TTS→ASR→LLM…" : "执行 AI 服务合成检查"}
            </Button>
          )}
        </div>
      }
    >
      {manual ? (
        <>服务器已记录接管人、原因和状态版本，旧自动命令不再有效。如场次仍在暂停，请先核对老人状态再恢复人工流程。</>
      ) : active ? (
        <>服务器已签发当前设备命令。旧的本地推进、提示与录音入口保持关闭。</>
      ) : processing ? (
        <>录音已交给服务器做权威 ASR 与 operational 判类；完成前不会开放旧人工推进。</>
      ) : paused ? (
        contentGap ? (
          <>下一协议位置缺少冻结的 operational rubric 或自动交互协议，服务器已在边界停止且不会跳题。请由研究团队补齐并复核内容，或完成收麦后显式接管。</>
        ) : (
          <>服务器已保持当前题位与提示等级，旧人工入口仍锁定；后续需走显式接管流程。</>
        )
      ) : completed ? (
        <>本次冻结计划中所有可证明完整的模拟位置已经结束。旧人工入口不会自动恢复，需由服务器确认下一控制模式。</>
      ) : serverFailed ? (
        <>服务器保留控制权并停止推进，不会因刷新开放旧人工入口。{state.receipt?.lastErrorCode ? ` 错误码：${state.receipt.lastErrorCode}。` : ""}</>
      ) : checking ? (
        <>刷新后先从服务器恢复真实控制状态；查询完成前，旧标签页不能成为第二个驾驶员。</>
      ) : uncertain ? (
        <>启动请求可能已经在服务器提交，但响应未能确认。当前页不会假定“仍是人工模式”；请保持场次暂停或等待权威状态查询。</>
      ) : completePlanBlocked ? (
        operationalAutopilotReady === null ? (
          <>正在核对完整冻结计划的每一个题目和环节；核对完成前不会把控制权交给服务器 AI。</>
        ) : (
          <>服务器 AI 启动已禁用：源脚本当前有 {unsupportedOperationalPositions.length} 个交付缺口（包含尚未结构化的多要素环节）；服务器启动时还会再次逐一复核。请先由研究团队冻结评分边界、自动问句、提示、反馈和推进协议，不能启动后再中途交给现场人员补位。</>
        )
      ) : providerBlocked ? (
        <>
          {providerReadinessLabel(providerReadiness)}。只有具名管理员用固定合成内容实际通过 TTS→ASR，
          并对已配置 LLM 完成结构检查后，服务器才会取得控制权。
          {providerReadinessError ? ` 就绪状态读取失败：${providerReadinessError}。` : ""}
        </>
      ) : (
        <>管理员的最新合成检查已通过。点击后服务器还会核对具名研究账号、双重模拟开关、模拟档案、录音授权、实时场次和唯一配对设备。服务器只会按顺序执行内容完整的冻结协议位置；一旦缺少 rubric 或自动话术就会停在边界。</>
      )}
      {providerReadiness && (
        <details style={{ marginTop: 8 }}>
          <summary>{providerReadinessLabel(providerReadiness)}</summary>
          <div style={{ marginTop: 6 }}>
            TTS：{providerReadiness.tts.success ? "通过" : `未通过（${providerReadiness.tts.failureCode ?? "unknown"}）`}，
            ASR：{providerReadiness.asr.success ? "通过" : `未通过（${providerReadiness.asr.failureCode ?? "unknown"}）`}，
            LLM：{providerReadiness.llm.required ? "本运行合同必需" : "本运行合同非必需"}·
            {providerReadiness.llm.success ? "通过" : `未通过（${providerReadiness.llm.failureCode ?? "unknown"}）`}。
            {providerReadiness.checkedAt && providerReadiness.expiresAt
              ? ` 检查时间 ${new Date(providerReadiness.checkedAt).toLocaleString()}，有效至 ${new Date(providerReadiness.expiresAt).toLocaleString()}。`
              : " 尚未执行合成检查。"}
          </div>
        </details>
      )}
      {state.error && <div role="alert" style={{ marginTop: 6 }}>
        {uncertain ? "状态核实失败" : "启动未通过"}：{state.error}。
        {uncertain ? " 人工入口继续锁定，避免与可能已启动的服务器流程并行。" : " 服务器在写入前拒绝了请求。"}
      </div>}
    </Alert>
    <ConfirmDialog
      open={confirmTakeover}
      title="确认结束服务器控制并转为人工处置？"
      body="服务器仅会在老人端已完成收麦证明、当前命令已失效时放行。接管会写入不可覆盖的审计记录，不会把技术失败记为受试者作答。"
      confirmLabel="确认转为人工处置"
      onCancel={() => setConfirmTakeover(false)}
      onConfirm={() => { void takeover(); }}
    />
    </>
  );
}
