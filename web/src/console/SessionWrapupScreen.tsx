import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { blobStore } from "../audio/blobStore";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/ToastContext";
import { useSessionJournal } from "../hooks/useSessionJournal";
import { useSessionRuntime } from "../hooks/useSessionRuntime";
import { ratioPct } from "../lib/format";
import { sessionAbortReasonLabel } from "../sessionAbort";
import {
  canExportCompletedSession,
  wrapupMode,
  type WrapupMode,
} from "../sessionLifecycle";
import { useAudioSaved } from "../sync/useCursorWriter";
import { useWeek1Script } from "../content/bundle";
import type {
  AudioAsset,
  ExportResult,
  ScoreReconstruction,
  Session,
  SessionPlan,
  SessionRuntimeState,
  SessionRuntimeStatus,
} from "../types";
import { ResearchReviewPanel } from "./ResearchReviewPanel";
import { SessionCloseoutPanel } from "./SessionCloseoutPanel";
import { ensureExportIntent, type ExportIntent } from "./exportIntent";
import type {
  SessionCloseoutOutcomeSummary,
  SessionCloseoutRecord,
  SessionCloseoutSaveRequest,
} from "./sessionCloseout";
import {
  completionIssueLabel,
  localCompletionGate,
  localInterventionCompletionGate,
  parseCompletionFailure,
  type CompletionFailureView,
} from "./sessionCompletion";

// 双终态收尾：现场只结束床旁干预；研究者事后复核/锁分后才最终完成；导出只属于最终完成态。
export function SessionWrapupScreen({
  session,
  reviewerId,
  canExport = false,
  canDeleteAudio = false,
  onBack,
  onDone,
  onRuntimeStatusChange,
  onCompletionBusyChange,
  onCloseoutGateChange,
}: {
  session: Session;
  reviewerId: string;
  canExport?: boolean;
  canDeleteAudio?: boolean;
  onBack?: () => void;
  onDone?: () => void;
  onRuntimeStatusChange?: (status: SessionRuntimeStatus) => void;
  onCompletionBusyChange?: (busy: boolean) => void;
  onCloseoutGateChange?: (blocked: boolean) => void;
}) {
  const toast = useToast();
  const runtimeControl = useSessionRuntime(session.session_id);
  const [runtimeOverride, setRuntimeOverride] = useState<SessionRuntimeState | null>(null);
  const { journal, upsertAudio, upsertTurn, hydrateFromServer } = useSessionJournal(session.session_id);
  const [journalRecovery, setJournalRecovery] = useState<"loading" | "ready" | "error">("loading");
  const [journalRecoveryError, setJournalRecoveryError] = useState<string | null>(null);
  const [journalRetry, setJournalRetry] = useState(0);
  const [scores, setScores] = useState<ScoreReconstruction | null>(null);
  const [scoresErr, setScoresErr] = useState<string | null>(null);
  const [plan, setPlan] = useState<SessionPlan | null>(null);
  const [planErr, setPlanErr] = useState<string | null>(null);
  const [exp, setExp] = useState<ExportResult | null>(null);
  const [exportBusy, setExportBusy] = useState(false);
  const [transitionBusy, setTransitionBusy] = useState(false);
  const [transitionFailure, setTransitionFailure] = useState<CompletionFailureView | null>(null);
  const [outcomeSummary, setOutcomeSummary] = useState<SessionCloseoutOutcomeSummary | null>(null);
  const [closeout, setCloseout] = useState<SessionCloseoutRecord | null>(null);
  const [closeoutLoadState, setCloseoutLoadState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [closeoutLoadError, setCloseoutLoadError] = useState<string | null>(null);
  const [closeoutRetryNonce, setCloseoutRetryNonce] = useState(0);
  const [closeoutEditorGate, setCloseoutEditorGate] = useState({
    dirty: false,
    submitting: false,
    reconciliation_required: false,
  });
  const [retryNonce, setRetryNonce] = useState(0);
  const [scoresRetryNonce, setScoresRetryNonce] = useState(0);
  const [gateEpoch, setGateEpoch] = useState(0);
  const [verifyingAll, setVerifyingAll] = useState(false);
  const mountedRef = useRef(true);
  const exportIntentRef = useRef<ExportIntent | null>(null);
  const transitionInFlight = useRef(false);
  const transitionRequest = useRef(0);

  const polledRuntime = runtimeControl.runtime?.sessionId === session.session_id
    ? runtimeControl.runtime
    : null;
  const currentRuntime = runtimeOverride
    && (!polledRuntime || runtimeOverride.revision >= polledRuntime.revision)
    ? runtimeOverride
    : polledRuntime;
  const runtimeStatus = currentRuntime?.status ?? session.runtime_status ?? null;
  const runtimeConfirmed = currentRuntime != null && !runtimeControl.error;
  const mode: WrapupMode | null = runtimeStatus == null ? null : wrapupMode(runtimeStatus);
  const closeoutReadyForLeave = closeout !== null
    && !closeoutEditorGate.dirty
    && !closeoutEditorGate.submitting
    && !closeoutEditorGate.reconciliation_required;
  const reviewAnalysisUnlocked = mode !== "review" || closeoutReadyForLeave;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      transitionRequest.current += 1;
      transitionInFlight.current = false;
      onCompletionBusyChange?.(false);
    };
  }, [onCompletionBusyChange, session.session_id]);

  useEffect(() => {
    onCloseoutGateChange?.(
      runtimeStatus === "intervention_completed" && !closeoutReadyForLeave,
    );
  }, [closeoutReadyForLeave, onCloseoutGateChange, runtimeStatus]);

  useEffect(() => () => onCloseoutGateChange?.(false), [onCloseoutGateChange]);

  // 现场结束时仍可能有最后一条 stop-and-save 回报在路上；只收同场次，绝不跨受试者混入。
  useAudioSaved((message) => {
    if (message.sessionId !== session.session_id || journal.audios[message.rawAudioId]) return;
    upsertAudio(message.rawAudioId, {
      turnKey: message.turnKey,
      containsDirectIdentifier: message.containsDirectIdentifier ?? false,
      isReliabilitySample: false,
      lastStatus: "recorded",
    });
    toast(`已补记最后一条录音：${message.rawAudioId.slice(0, 12)}…`, "info");
  });

  useEffect(() => {
    let cancelled = false;
    setJournalRecovery("loading");
    setJournalRecoveryError(null);
    api.sessionJournal(session.session_id)
      .then((remote) => {
        if (cancelled) return;
        if (remote.session.session_id !== session.session_id) {
          throw new Error("服务器返回了其他场次的记录，已拒绝恢复");
        }
        hydrateFromServer(remote);
        setJournalRecovery("ready");
      })
      .catch((error) => {
        if (cancelled) return;
        setJournalRecoveryError(error instanceof ApiError ? error.detail : String(error));
        setJournalRecovery("error");
      });
    return () => { cancelled = true; };
  }, [hydrateFromServer, journalRetry, session.session_id]);

  useEffect(() => {
    setScores(null);
    setScoresErr(null);
    api.sessionScores(session.session_id)
      .then(setScores)
      .catch((error) => setScoresErr(error instanceof ApiError ? error.detail : String(error)));
  }, [scoresRetryNonce, session.session_id]);

  useEffect(() => {
    setPlan(null);
    setPlanErr(null);
    api.sessionPlan(session.session_id, session.week_no, session.event_line)
      .then(setPlan)
      .catch((error) => setPlanErr(error instanceof ApiError ? error.detail : String(error)));
  }, [retryNonce, session.event_line, session.session_id, session.week_no]);

  useEffect(() => {
    if (mode !== "review" && mode !== "completed") {
      setOutcomeSummary(null);
      setCloseout(null);
      setCloseoutLoadState("idle");
      setCloseoutLoadError(null);
      return;
    }
    let cancelled = false;
    setOutcomeSummary(null);
    setCloseout(null);
    setCloseoutLoadState("loading");
    setCloseoutLoadError(null);
    void (async () => {
      try {
        const summary = await api.getSessionOutcomeSummary(session.session_id);
        if (cancelled) return;
        setOutcomeSummary(summary);
        try {
          const report = await api.getSessionCloseout(session.session_id);
          if (!cancelled) setCloseout(report);
        } catch (error) {
          if (!(error instanceof ApiError && error.status === 404)) throw error;
          if (!cancelled) setCloseout(null);
        }
        if (!cancelled) setCloseoutLoadState("ready");
      } catch (error) {
        if (cancelled) return;
        setCloseoutLoadError(error instanceof ApiError ? error.detail : String(error));
        setCloseoutLoadState("error");
      }
    })();
    return () => { cancelled = true; };
  }, [closeoutRetryNonce, mode, session.session_id]);

  const journalReady = journalRecovery === "ready";
  const planReady = plan !== null;
  const totalTurns = plan?.total_turns ?? 0;
  const lockedTurns = Object.values(journal.turns).filter((turn) => turn.locked).length;
  const audioIds = Object.keys(journal.audios);
  const untouched = (plan?.items ?? []).filter((item) => !journal.itemEvents[item.item_id]);
  const completionPct = totalTurns > 0 ? Math.round((lockedTurns / totalTurns) * 100) : null;
  // 第 1 周关系建立:本地只镜像「停在道别节」;道别节键取冻结脚本最后一节。
  const { script: week1Script, error: week1ScriptError } = useWeek1Script();
  const farewellKey = week1Script?.sections.length
    ? week1Script.sections[week1Script.sections.length - 1].key
    : null;
  const rapportStep = currentRuntime?.rapportStep ?? null;
  const rapportStatus = session.week_no !== 1 ? undefined
    : (farewellKey && rapportStep
      ? {
        atFarewell: rapportStep.sectionKey === farewellKey
          && (rapportStep.questionIdx ?? 0) === 0,
      }
      : null);
  const interventionGate = localInterventionCompletionGate({
    journalReady,
    planReady,
    weekNo: session.week_no,
    totalTurns,
    itemsMissingRecords: untouched.length,
    rapport: rapportStatus,
  });
  const completionGate = localCompletionGate({
    journalReady,
    planReady,
    weekNo: session.week_no,
    lockedTurns,
    totalTurns,
    rapport: rapportStatus,
  });
  const exportAllowed = currentRuntime != null && !runtimeControl.error && canExportCompletedSession({
    status: currentRuntime.status,
    strictCompletionGatePassed: completionGate.canRequest,
    roleAllowsExport: canExport,
  });
  const planHas = (taskType: string) => plan == null || plan.items.some((item) => item.task_type === taskType);
  const rawAudioIdsByTurnKey = Object.fromEntries(
    Object.entries(journal.turns).flatMap(([key, turn]) =>
      turn.rawAudioId ? [[key, turn.rawAudioId] as const] : []),
  );

  function beginTransition(): number | null {
    if (transitionInFlight.current) return null;
    const requestId = ++transitionRequest.current;
    transitionInFlight.current = true;
    setTransitionBusy(true);
    setTransitionFailure(null);
    onCompletionBusyChange?.(true);
    return requestId;
  }

  function endTransition(requestId: number) {
    if (!mountedRef.current || requestId !== transitionRequest.current) return;
    transitionInFlight.current = false;
    setTransitionBusy(false);
    onCompletionBusyChange?.(false);
  }

  async function finishIntervention() {
    if (!runtimeConfirmed || mode !== "bedside" || !interventionGate.canRequest) return;
    const requestId = beginTransition();
    if (requestId == null) return;
    try {
      const runtime = await api.finishIntervention(session.session_id);
      if (!mountedRef.current || requestId !== transitionRequest.current) return;
      if (runtime.status !== "intervention_completed") {
        setTransitionFailure({ message: "服务器未确认结束，本场仍保留在当前页。" });
        return;
      }
      setRuntimeOverride(runtime);
      onRuntimeStatusChange?.(runtime.status);
      toast("床旁干预已结束；老人端已停麦，请完成独立现场收尾后再切换下一位", "ok");
    } catch (error) {
      if (mountedRef.current && requestId === transitionRequest.current) {
        setTransitionFailure(parseCompletionFailure(error));
      }
    } finally {
      endTransition(requestId);
    }
  }

  async function saveCloseout(
    request: SessionCloseoutSaveRequest,
  ): Promise<SessionCloseoutRecord> {
    const saved = await api.saveSessionCloseout(session.session_id, request);
    setCloseout(saved);
    setCloseoutLoadError(null);
    setCloseoutLoadState("ready");
    toast(saved.idempotent ? "现场收尾已确认，无需重复保存" : "现场收尾已保存，可以切换下一位", "ok");
    return saved;
  }

  async function reconcileCloseout(): Promise<SessionCloseoutRecord | null> {
    try {
      const authoritative = await api.getSessionCloseout(session.session_id);
      setCloseout(authoritative);
      setCloseoutLoadError(null);
      setCloseoutLoadState("ready");
      return authoritative;
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        setCloseout(null);
        setCloseoutLoadError(null);
        setCloseoutLoadState("ready");
        return null;
      }
      throw error;
    }
  }

  async function completeResearchReview() {
    if (!runtimeConfirmed || mode !== "review" || !completionGate.canRequest
        || !closeoutReadyForLeave) return;
    const requestId = beginTransition();
    if (requestId == null) return;
    try {
      const runtime = await api.completeSession(session.session_id);
      if (!mountedRef.current || requestId !== transitionRequest.current) return;
      if (runtime.status !== "completed") {
        setTransitionFailure({ message: "服务器未确认最终完成，本场仍保留在本页。" });
        return;
      }
      setRuntimeOverride(runtime);
      onRuntimeStatusChange?.(runtime.status);
      toast("服务器已核对全部已锁定的评分；本场已最终完成", "ok");
    } catch (error) {
      if (mountedRef.current && requestId === transitionRequest.current) {
        setTransitionFailure(parseCompletionFailure(error));
      }
    } finally {
      endTransition(requestId);
    }
  }

  async function doExport() {
    if (!exportAllowed || exportBusy) {
      toast("只有已最终完成的场次能由数据管理员导出", "danger");
      return;
    }
    setExportBusy(true);
    try {
      exportIntentRef.current = ensureExportIntent(
        exportIntentRef.current, session.session_id,
      );
      // Keep this key across timeouts/ambiguous responses.  A retry is the same
      // export intent, never an accidental second batch.
      const result = await api.exportSession(
        session.session_id, exportIntentRef.current.idempotencyKey, true,
      );
      setExp(result);
      setGateEpoch((value) => value + 1);
      toast(`已生成去标识导出批次 ${result.batch_id}`, "ok");
    } catch (error) {
      toast(error instanceof ApiError ? error.detail : String(error), "danger");
    } finally {
      setExportBusy(false);
    }
  }

  async function verifyAll() {
    if (verifyingAll || !exportAllowed) return;
    setVerifyingAll(true);
    let ok = 0;
    let fail = 0;
    for (const id of audioIds) {
      try {
        const audio = await api.getAudio(id);
        if (audio.status === "exported") {
          await api.audioChecksum(id);
          ok += 1;
        }
      } catch {
        fail += 1;
      }
    }
    setGateEpoch((value) => value + 1);
    toast(`批量校验完成：${ok} 通过${fail ? `，${fail} 失败` : ""}`, fail ? "warn" : "ok");
    setVerifyingAll(false);
  }

  const header = headerForMode(mode, closeoutReadyForLeave);
  const primaryGate = mode === "bedside" ? interventionGate : completionGate;
  const statusTone = journalRecovery === "error" || planErr || runtimeControl.error
    ? "danger"
    : mode === "completed" ? "ok"
      : mode === "closed" ? "danger"
        : mode === "review" && !closeoutReadyForLeave ? "warn"
        : !runtimeConfirmed || !mode || !journalReady || !planReady ? "muted"
          : primaryGate.canRequest ? "ok" : "warn";
  const isDemo20 = session.is_simulation
    && session.autopilot_profile_version_id === "week2-single20-demo-v1";

  return (
    <div className="page-shell page-shell--medium">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">{header.kicker}</p>
          <h2 className="page-title">{header.title}</h2>
          <p className="page-description">{header.description}</p>
        </div>
        <div className="toolbar">
          <StatusPill tone={statusTone}>
            {!runtimeConfirmed ? "正在核对服务器场次状态" : mode === "completed" ? "最终完成 · 只读"
              : mode === "closed" ? terminalStatusLabel(runtimeStatus)
                : mode === "review" && !closeoutReadyForLeave ? "现场收尾待保存"
                : primaryGate.label}
          </StatusPill>
          {mode === "bedside" && onBack && (
            <Button disabled={transitionBusy} onClick={onBack}>返回{session.week_no === 1 ? "关系建立" : "训练"}</Button>
          )}
          {mode === "bedside" && (
            <Button variant="primary" disabled={transitionBusy || !runtimeConfirmed || !interventionGate.canRequest}
              onClick={() => { void finishIntervention(); }}>
              {transitionBusy ? "正在核对 AI 与录音证据…" : "结束床旁干预"}
            </Button>
          )}
          {mode === "review" && onDone && (
            <Button disabled={transitionBusy || !closeoutReadyForLeave}
              title={!closeoutReadyForLeave ? "请先保存当前现场收尾，并完成服务器核对" : undefined}
              onClick={onDone}>{closeoutReadyForLeave ? "切换下一位 · 研究复核稍后进行" : "先完成现场收尾"}</Button>
          )}
          {mode === "review" && (
            <Button variant="primary"
              disabled={transitionBusy || !runtimeConfirmed || !completionGate.canRequest || !closeoutReadyForLeave}
              onClick={() => { void completeResearchReview(); }}>
              {transitionBusy ? "正在请求最终核验…" : "锁定复核并最终完成"}
            </Button>
          )}
          {(mode === "completed" || mode === "closed") && onDone && (
            <Button onClick={onDone}>返回受试者列表</Button>
          )}
        </div>
      </header>

      {isDemo20 && (
        <p className="muted">本次是 20 题模拟演练，不代表真人训练结果。</p>
      )}

      {runtimeControl.error && (
        <Alert tone="danger" title="无法核对服务器场次状态"
          actions={<Button onClick={() => { void runtimeControl.refresh(); }}>重新同步</Button>}>
          {runtimeControl.error}。恢复前不能结束或完成本场。
        </Alert>
      )}

      {mode === "bedside" && (
        <p className="muted">结束前系统会自动核对每题都有 AI 记录和录音；人工确认和评分可事后再做。</p>
      )}
      {mode === "review" && (
        <p className="muted">老人端已结束，不能返回训练。</p>
      )}
      {mode === "closed" && (
        <Alert tone="danger" title={`${terminalStatusLabel(runtimeStatus)} · 只读`}>
          {runtimeStatus === "aborted" ? (
            <>
              <p>中止原因：{sessionAbortReasonLabel(currentRuntime?.endReason)}。已保存的记录保持只读。</p>
              <p>本次训练是否补做，由研究负责人决定。</p>
            </>
          ) : (
            "本场已异常结束，不能继续；请联系研究负责人核查。"
          )}
        </Alert>
      )}

      {session.week_no === 1 && (
        <p className="muted">第 1 周为关系建立场：完成道别并停止录音即可结束，无需逐题评分。</p>
      )}
      {session.week_no === 1 && week1ScriptError && (
        <Alert tone="danger" title="第 1 周脚本核对失败">
          {week1ScriptError}。暂时不能结束本场，请刷新页面重试。
        </Alert>
      )}

      {transitionFailure && (
        <CompletionFailureAlert failure={transitionFailure} bedside={mode === "bedside"} />
      )}

      {journalRecovery === "error" && (
        <Alert tone="danger" title="无法恢复服务器中的完整场次记录"
          actions={<Button onClick={() => setJournalRetry((value) => value + 1)}>重新加载记录</Button>}>
          {journalRecoveryError}。恢复前不能结束或导出本场。
        </Alert>
      )}
      {journalRecovery === "loading" && (
        <p className="muted">正在恢复本场记录，请稍候。</p>
      )}

      {mode === "review" && !closeoutReadyForLeave && (
        <Alert tone="warn" title="离开本场前，请先完成现场收尾">
          请选择「无额外观察」或记录观察并保存；保存后才能切换下一位。
        </Alert>
      )}

      {(mode === "review" || mode === "completed") && closeoutLoadState === "loading" && (
        <p className="muted">正在加载自动汇总与现场收尾…</p>
      )}
      {(mode === "review" || mode === "completed") && closeoutLoadState === "error" && (
        <Alert tone="danger" title="无法恢复自动汇总或现场收尾"
          actions={<Button onClick={() => setCloseoutRetryNonce((value) => value + 1)}>重新加载收尾</Button>}>
          {closeoutLoadError}。请重试加载。
        </Alert>
      )}
      {(mode === "review" || mode === "completed") && outcomeSummary && closeoutLoadState === "ready" && (
        <SessionCloseoutPanel
          outcomeSummary={outcomeSummary}
          closeout={closeout}
          busy={transitionBusy || mode === "completed"}
          onSave={saveCloseout}
          onReconcile={reconcileCloseout}
          onGateChange={setCloseoutEditorGate}
        />
      )}

      {reviewAnalysisUnlocked && (
        <section className="form-section">
          <div className="form-section-header">
            <div>
              <h3>{mode === "bedside" ? "床旁证据概览" : "研究复核完整度"}</h3>
              <p className="muted">
                {mode === "bedside"
                  ? "能否结束由服务器逐环节核对，缺项会明确列出。"
                  : "最终完成前，每个环节都需人工确认并锁定评分。"}
              </p>
            </div>
            <StatusPill tone={!journalReady || !planReady ? "muted" : primaryGate.canRequest ? "ok" : "warn"}>
              {!journalReady || !planReady ? "正在计算" : mode === "bedside" ? interventionGate.label
                : completionPct == null ? completionGate.label : `${completionPct}% 已锁定`}
            </StatusPill>
          </div>
          <div className="metric-grid">
            <div className="metric-card">
              <span className="metric-card__label">已有最终记录的环节</span>
              <strong className="metric-card__value">{Object.keys(journal.turns).length}<small> / {totalTurns || "—"}</small></strong>
            </div>
            <div className="metric-card">
              <span className="metric-card__label">人工已锁定</span>
              <strong className="metric-card__value">{lockedTurns}<small> / {totalTurns || "—"}</small></strong>
            </div>
            <div className="metric-card">
              <span className="metric-card__label">已登记录音</span>
              <strong className="metric-card__value">{audioIds.length}<small> 条</small></strong>
            </div>
          </div>

          {planErr && (
            <Alert tone="danger" title="无法加载本场计划"
              actions={<Button onClick={() => setRetryNonce((value) => value + 1)}>重新加载</Button>}>
              {planErr}。恢复前不能结束或导出本场。
            </Alert>
          )}
          {journalReady && untouched.length > 0 && (
            <Alert tone="warn" title={`有 ${untouched.length} 题还没有记录`}>
              缺少记录的题目会阻止结束，明细见下方折叠。
              <details style={{ marginTop: 8 }}>
                <summary>查看缺失题目</summary>
                <ul>{untouched.map((item) => <li key={item.item_id}>{item.item_id} · {item.task_type}</li>)}</ul>
              </details>
            </Alert>
          )}
        </section>
      )}

      {(mode === "review" || mode === "completed") && reviewAnalysisUnlocked && plan && journalReady && (
        <ResearchReviewPanel
          plan={plan}
          turns={journal.turns}
          reviewerId={reviewerId}
          readOnly={mode === "completed"}
          rawAudioIdsByTurnKey={rawAudioIdsByTurnKey}
          onUpsertTurn={upsertTurn}
          onChanged={() => setScoresRetryNonce((value) => value + 1)}
        />
      )}

      {(mode === "review" || mode === "completed") && reviewAnalysisUnlocked && (
        <section className="form-section">
          <div className="form-section-header">
            <div>
              <h3>研究评分结果</h3>
              <p className="muted">只统计已人工锁定的环节；AI 初评不计入。</p>
            </div>
          </div>
          {scoresErr ? (
            <div className="row wrap">
              <span style={{ color: "var(--c-danger)" }}>评分结果加载失败：{scoresErr}</span>
              <Button onClick={() => setScoresRetryNonce((value) => value + 1)}>重新加载</Button>
            </div>
          ) : !scores ? <p className="muted">正在加载评分结果…</p> : (
            <>
              <div className="metric-grid">
                {planHas("单要素") && <ScoreCard title="单要素命名正确率" summary={scores.single} metric="naming_accuracy" fmt={ratioPct} />}
                {planHas("双要素") && <ScoreCard title="双要素周得分" summary={scores.double} metric="weekly_de_score_percentile" />}
                {planHas("多要素") && <ScoreCard title="多要素关键要素率" summary={scores.multi} metric="weekly_me_score_percentile" />}
              </div>
              {scores.excluded_items.length > 0 && (
                <Alert tone="warn" title={`${scores.excluded_items.length} 项尚未计入研究评分`}>
                  这些题还没人工锁定，锁定后才能最终完成。
                </Alert>
              )}
            </>
          )}
        </section>
      )}

      {mode === "completed" && (
        <section className="form-section">
          <div className="form-section-header">
            <div>
              <h3>数据管理员导出、保全与治理</h3>
              <p className="muted">本区仅供数据管理员导出与管理录音，不能修改训练记录。</p>
            </div>
          </div>

          {!completionGate.canRequest && (
            <Alert tone="danger" title="记录核对不一致 · 导出已关闭">
              {completionGate.detail}。请联系研究负责人核查。
            </Alert>
          )}
          {!canExport && (
            <Alert tone="info" title="当前账号不能执行数据导出">
              研究者可查看；导出与录音管理需系统管理员或数据管理员账号。
            </Alert>
          )}

          {exportAllowed && (
            <>
              <div className="form-actions">
                <Button variant="primary" disabled={exportBusy} onClick={() => { void doExport(); }}>
                  {exportBusy ? "正在生成导出批次…" : "导出本场次（去标识）"}
                </Button>
              </div>
              {exp && (
                <div className="card" style={{ background: "var(--c-bg)" }}>
                  <div className="row wrap">
                    <StatusPill tone="ok">导出批次 {exp.batch_id}</StatusPill>
                    <StatusPill tone={exp.deidentified ? "ok" : "danger"}>{exp.deidentified ? "已去标识" : "包含标识信息"}</StatusPill>
                  </div>
                  <p>已生成：{Object.entries(exp.sheet_counts).map(([key, value]) => `${sheetLabels[key] ?? key}（${value}）`).join("、")}</p>
                  <p className="muted">共 {exp.files.length} 个文件 · 纳入 {exp.audio_touched.length} 条研究录音</p>
                </div>
              )}

              <div className="form-section-header" style={{ marginTop: 16 }}>
                <div>
                  <h3>研究录音状态</h3>
                  <p className="muted">整场导出后才可校验受控副本；原始录音不能绕过完整场次单独处理。</p>
                </div>
                {audioIds.length > 1 && (
                  <Button onClick={() => { void verifyAll(); }} disabled={verifyingAll}>
                    {verifyingAll ? "正在批量校验…" : "校验全部已导出录音"}
                  </Button>
                )}
              </div>
              {audioIds.length === 0
                ? <p className="muted">本场没有登记研究录音。</p>
                : audioIds.map((id) => <AudioGateRow key={id} rawAudioId={id}
                  sessionId={session.session_id} gateEpoch={gateEpoch}
                  canDelete={canDeleteAudio} />)}
            </>
          )}
        </section>
      )}
    </div>
  );
}

function headerForMode(
  mode: WrapupMode | null,
  closeoutReadyForLeave: boolean,
): { kicker: string; title: string; description: string } {
  if (mode === "bedside") {
    return {
      kicker: "现场流程 · 结束床旁干预",
      title: "核对 AI 与录音证据后结束老人端",
      description: "不要求老人等待人工转写、确认或锁分；结束后工作人员独立补现场收尾。",
    };
  }
  if (mode === "review") {
    if (!closeoutReadyForLeave) {
      return {
        kicker: "现场流程 · 必填收尾",
        title: "先保存现场收尾，再切换下一位",
        description: "老人端已经结束；这一步只记录可直接观察到的事实，保存后才进入事后研究复核。",
      };
    }
    return {
      kicker: "事后流程 · 研究复核",
      title: "人工确认回答并锁定评分",
      description: "老人端已经结束；研究者在后台逐环节复核 AI 证据，全部锁定后才最终完成。",
    };
  }
  if (mode === "completed") {
    return {
      kicker: "数据后台 · 最终完成",
      title: "只读查看与受控导出",
      description: "本场记录已只读；导出由数据管理员执行。",
    };
  }
  if (mode === "closed") {
    return {
      kicker: "已关闭 · 只读",
      title: "本场次已关闭",
      description: "系统不会恢复题目推进、录音或锁分。",
    };
  }
  return {
    kicker: "场次状态核对",
    title: "正在确认场次状态",
    description: "确认完成前暂不能进行任何操作。",
  };
}

function terminalStatusLabel(status: SessionRuntimeStatus | null): string {
  if (status === "aborted") return "场次已中止";
  if (status === "failed") return "场次已异常结束";
  if (status === "intervention_completed") return "干预已结束 · 待研究复核";
  if (status === "completed") return "场次已最终完成";
  return "场次状态未知";
}

function CompletionFailureAlert({ failure, bedside }: { failure: CompletionFailureView; bedside: boolean }) {
  return (
    <Alert tone="danger" title={bedside ? "服务器拒绝结束床旁干预" : "服务器拒绝最终完成"}>
      <p>{failure.message}</p>
      {failure.assessment && (
        <>
          {failure.assessment.protocol === "rapport" ? (
            <p>
              服务器核对：{failure.assessment.atFarewell ? "已停在道别节" : "未停在道别节"}，
              录音指令{failure.assessment.recordingIdle ? "已收回" : "未收回"}，
              录音 {failure.assessment.audioTotal ?? "—"} 段中
              {failure.assessment.audioVerified ?? "—"} 段通过核验。
            </p>
          ) : (
          <p>
            服务器核对：计划 {failure.assessment.expectedTurns ?? "—"} 个环节，
            匹配 {failure.assessment.matchedTurns ?? "—"} 个，
            AI 完成 {failure.assessment.completedAttemptTurns ?? "—"} 个，
            录音证据 {failure.assessment.audioEvidencedTurns ?? "—"} 个，
            人工锁定 {failure.assessment.lockedTurns ?? "—"} 个。
          </p>
          )}
          {failure.assessment.issues.length > 0 && (
            <details>
              <summary style={{ cursor: "pointer", fontWeight: 600 }}>
                查看 {failure.assessment.issues.length} 条阻断原因
              </summary>
              <ul>
                {failure.assessment.issues.map((issue, index) => (
                  <li key={`${issue.code ?? "issue"}-${issue.itemId ?? "session"}-${issue.turnSeq ?? "none"}-${index}`}>
                    {completionIssueLabel(issue)}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </>
      )}
      本场仍保留在当前页面，不会误切换受试者或宣布完成。
    </Alert>
  );
}

function ScoreCard({
  title,
  summary,
  metric,
  fmt,
}: {
  title: string;
  summary: Record<string, unknown> | null;
  metric: string;
  fmt?: (value: number) => string;
}) {
  const value = summary ? (summary[metric] as number | undefined) : undefined;
  return (
    <div className="metric-card">
      <span className="metric-card__label">{title}</span>
      {summary
        ? <strong className="metric-card__value">{value == null ? "—" : fmt ? fmt(value) : `${value.toFixed(1)}%`}</strong>
        : <span className="muted">暂无已锁定题</span>}
    </div>
  );
}

const sheetLabels: Record<string, string> = {
  session: "场次信息",
  turns: "环节记录",
  attempts: "AI 处理记录",
  interactions: "交互记录",
  item_scores: "题目评分",
  scales: "评估量表",
  legacy_unverified_scales: "待核对历史量表",
  abnormal: "异常记录",
  audio_manifest: "录音清单",
  repeat_audio_manifest: "重复录音清单",
};

const audioStatusLabel: Record<AudioAsset["status"], string> = {
  recorded: "待整场导出",
  exported: "已导出，待校验",
  checksum_verified: "校验通过，待信度复核",
  reliability_review_done: "信度复核完成",
  deletable: "可删除采集原件",
  deleted: "采集原件已删（受控副本按策略保留）",
};

function AudioGateRow({ rawAudioId, sessionId, gateEpoch = 0, canDelete = false }: {
  rawAudioId: string;
  sessionId: string;
  gateEpoch?: number;
  canDelete?: boolean;
}) {
  const toast = useToast();
  const [audio, setAudio] = useState<AudioAsset | null>(null);
  const [loadState, setLoadState] = useState<"loading" | "ok" | "missing" | "error">("loading");
  const [rowBusy, setRowBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const refresh = useCallback(() => {
    setLoadState("loading");
    api.getAudio(rawAudioId)
      .then((next) => { setAudio(next); setLoadState("ok"); })
      .catch((error) => {
        setAudio(null);
        setLoadState(error instanceof ApiError && error.status === 404 ? "missing" : "error");
      });
  }, [rawAudioId]);
  useEffect(() => { refresh(); }, [gateEpoch, refresh]);

  async function step(operation: () => Promise<AudioAsset>) {
    if (rowBusy) return;
    setRowBusy(true);
    try {
      setAudio(await operation());
      toast("录音保全闸门已推进", "ok");
    } catch (error) {
      toast(error instanceof ApiError ? error.detail : String(error), "danger");
      refresh();
    } finally {
      setRowBusy(false);
    }
  }

  async function remove() {
    if (rowBusy) return;
    setRowBusy(true);
    try {
      await api.deleteAudio(rawAudioId, sessionId);
      let localCacheCleared = true;
      try {
        const outcome = await blobStore.del(rawAudioId);
        localCacheCleared = outcome === "deleted" || outcome === "missing";
      } catch { localCacheCleared = false; }
      toast(
        localCacheCleared
          ? "服务器采集原件与当前浏览器同 ID 缓存已清理"
          : "服务器采集原件已删除，但当前浏览器缓存清理失败",
        localCacheCleared ? "ok" : "warn",
      );
      refresh();
    } catch (error) {
      toast(error instanceof ApiError ? error.detail : String(error), "danger");
    } finally {
      setRowBusy(false);
    }
  }

  if (loadState === "loading") return <div className="row muted">{rawAudioId.slice(0, 16)}…：读取状态中…</div>;
  if (loadState === "error") return <div className="row muted">{rawAudioId.slice(0, 16)}…：状态获取失败 <Button onClick={refresh}>重试</Button></div>;
  if (loadState === "missing" || !audio) return <div className="row muted">{rawAudioId.slice(0, 16)}…：服务端未登记</div>;
  const tone = audio.status === "deletable" ? "ok" : audio.status === "deleted" ? "muted" : "primary";
  return (
    <div className="row" style={{ justifyContent: "space-between", borderBottom: "1px solid var(--c-line-soft)", paddingBottom: 8 }}>
      <div className="col" style={{ gap: 2 }}>
        <span className="mono">{rawAudioId.slice(0, 16)}…</span>
        <span className="muted">{audio.is_reliability_sample ? "信度样本" : "常规"}{audio.contains_direct_identifier ? " · 含直接标识" : ""}</span>
      </div>
      <div className="row wrap">
        <StatusPill tone={tone}>{audioStatusLabel[audio.status]}</StatusPill>
        {rowBusy && <span className="muted">处理中…</span>}
        {audio.status === "recorded" && <span className="muted">请先执行整场去标识导出</span>}
        {audio.status === "exported" && (
          <Button disabled={rowBusy} onClick={() => { void step(() => api.audioChecksum(rawAudioId)); }}>校验导出副本</Button>
        )}
        {audio.status === "checksum_verified" && audio.is_reliability_sample && (
          <Button disabled={rowBusy} onClick={() => { void step(() => api.audioReliabilityReview(rawAudioId)); }}>完成人工信度复核</Button>
        )}
        {audio.status === "deletable" && canDelete && (
          <Button variant="danger" disabled={rowBusy} onClick={() => setConfirmDelete(true)}>删除采集原件</Button>
        )}
        {audio.status === "deletable" && !canDelete && (
          <span className="muted">删除需由系统管理员执行</span>
        )}
      </div>
      <ConfirmDialog open={canDelete && confirmDelete} title="删除这条研究录音采集原件？"
        body={`${rawAudioId.slice(0, 16)}… 已通过整场导出、校验${audio.is_reliability_sample ? "与信度复核" : ""}。此操作不可恢复；受控导出副本仍按研究保留策略保存。`}
        confirmLabel="确认删除采集原件"
        onConfirm={() => { setConfirmDelete(false); void remove(); }}
        onCancel={() => setConfirmDelete(false)} />
    </div>
  );
}
