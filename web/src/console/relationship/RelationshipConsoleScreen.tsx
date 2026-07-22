import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api";
import { Alert } from "../../components/Alert";
import { Button } from "../../components/Button";
import { StatusPill } from "../../components/StatusPill";
import { useToast } from "../../components/ToastContext";
import { useWeek1Script } from "../../content/bundle";
import { useSessionJournal } from "../../hooks/useSessionJournal";
import { useSessionRuntime } from "../../hooks/useSessionRuntime";
import { useAudioSaved, useCursorWriter, usePatientRec, useSaveWatchdog } from "../../sync/useCursorWriter";
import { isPatientRecFailure, type RecState } from "../../sync/messages";
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
  const recSeq = useRef(0);
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
      recording: "idle",
      recSeq: recSeq.current,
      ...rapportFlags,
    });
  };
  useEffect(() => () => withdrawRef.current(), []);
  useEffect(() => { if (recoveryError) withdrawRef.current(); }, [recoveryError]);

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
      postRapport({ sectionKey: section?.key ?? "", questionIdx: qIdx, recording: "idle", recSeq: recSeq.current, ...rapportFlags });
    }
    setRecState("idle");
    toast(`录音已保存${m.containsDirectIdentifier ? "(含直接标识,导出将红线)" : ""}:${m.rawAudioId.slice(0, 12)}… · ${m.durationSeconds.toFixed(1)}s`, "ok");
  });

  if (scriptError) return <Alert tone="danger" title="第 1 周脚本校验失败">系统已安全暂停，不能使用未经校验的话术开场：{scriptError}</Alert>;
  if (!script) return <p>加载第 1 周脚本…</p>;

  // 统一推进:换节/换问都先停录(老人端收到 idle 自动收尾保存),再广播新指针。
  const go = (sIdx: number, q: number) => {
    if (interactionBlocked) return;
    const s = script.sections[sIdx];
    const nextFlags = defaultRapportFlags(s?.key ?? "");
    if (recState !== "idle") armWatchdog(lastArmedKey.current);
    setRecState("idle");
    setSectionIdx(sIdx);
    setQIdx(q);
    setRapportFlags(nextFlags);
    postRapport({ sectionKey: s?.key ?? "", questionIdx: q, recording: "idle", recSeq: recSeq.current, ...nextFlags });
  };

  const armRecording = () => {
    if (interactionBlocked) return;
    if (!recordingEligible) {
      toast(recStatus === "denied" ? "服务器当前未授权本场录音" : "录音授权尚未确认，系统保持关麦", "warn");
      return;
    }
    recSeq.current += 1; // 每次 arm 新序号:老人自停后 armed→armed 重发才能重触发老人端
    lastArmedKey.current = `关系建立·${section?.key ?? ""}`;
    setRecState("armed");
    postRapport({ sectionKey: section?.key ?? "", questionIdx: qIdx, recording: "armed", recSeq: recSeq.current, ...rapportFlags });
  };
  const stopRecording = () => {
    setRecState("idle");
    armWatchdog(lastArmedKey.current ?? `关系建立·${section?.key ?? ""}`);
    postRapport({ sectionKey: section?.key ?? "", questionIdx: qIdx, recording: "idle", recSeq: recSeq.current, ...rapportFlags });
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
    if (pausePending || paused) return;
    beginSafetyPause();
    setPausePending(true);
    if (recState !== "idle") armWatchdog(lastArmedKey.current);
    setRecState("idle");
    try {
      const next = await runtimeControl.pause();
      if (next) toast("关系建立已暂停，患者端麦克风保持关闭", "ok");
    } finally {
      setPausePending(false);
    }
  }

  async function resumeRapport() {
    const next = await runtimeControl.resume();
    if (next) {
      releaseSafetyPause();
      toast("已恢复到暂停前的对话位置", "ok");
    }
  }

  async function enterWrapup() {
    if (wrapupPending || interactionBlocked || !section) return;
    setWrapupPending(true);
    if (recState !== "idle") armWatchdog(lastArmedKey.current ?? `关系建立·${section.key}`);
    const accepted = await postRapport({
      sectionKey: section.key,
      questionIdx: qIdx,
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
                  {q.success && <span>回答后说：{q.success}</span>}
                </li>
              ))}
            </ol>
            <div className="toolbar rapport-question-actions">
              <div className="row"><Button disabled={interactionBlocked || qIdx === 0} onClick={() => go(sectionIdx, qIdx - 1)}>上一问</Button><Button disabled={interactionBlocked || qIdx >= questions.length - 1} onClick={() => go(sectionIdx, qIdx + 1)}>下一问</Button></div>
              <span className="muted">第 {qIdx + 1} / {questions.length} 问</span>
            </div>
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
