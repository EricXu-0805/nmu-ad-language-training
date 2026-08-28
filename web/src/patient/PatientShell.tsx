import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, DEVICE_CAPABILITY_UPDATED_EVENT, getDeviceCapability } from "../api";
import { PinPrompt, PIN_PROMPT_MANUAL_OPEN_EVENT } from "../components/PinPrompt";
import { bus } from "../sync/bus";
import { PATIENT_ACTIVATION_EVENT } from "../sync/messages";
import { useLiveCursor } from "../sync/useLiveCursor";
import { usePatientHeartbeat } from "../sync/usePatientHeartbeat";
import type { PatientPresenceScreen } from "../types";
import { autopilotPresenceScreen } from "./patientPresenceScreen.ts";
import { isSessionTerminalStatus } from "../sessionLifecycle";
import { Centered } from "./Centered";
import {
  bedsideSafetyIsLatched,
  latchBedsideSafetyStop,
  reconcileBedsideSafetyLatch,
  type BedsideSafetyLatch,
} from "./bedsideSafetyLatch";
import { canSignalBedsideActivation } from "./autopilotAdmission";
import { PatientAutopilotStage } from "./PatientAutopilotStage";
import { PatientStage } from "./PatientStage";
import { parsePatientSessionPlan, type PatientSessionPlan } from "./patientPlan";
import {
  acknowledgePatientPauseOutbox,
  createPatientPauseOutbox,
  loadPatientPauseOutbox,
  observePatientPauseOnServer,
  PATIENT_PAUSE_STORAGE_LOCK_NAME,
  patientPauseCanResolveAfterResume,
  patientPauseOutboxForStopSignal,
  patientPauseRetryDisposition,
  requirePatientPauseServerResolution,
  resolvePatientPauseOutbox,
  savePatientPauseOutbox,
  type PatientPauseOutbox,
} from "./patientPause";
import { RapportStage } from "./RapportStage";
import { ttsToggleTitle } from "./ttsEngineLabel";
import {
  clearTtsContext,
  currentVoiceName,
  setTtsContext,
  setTtsEnabled,
  speakSample,
  stopSpeaking,
  ttsEnabled,
} from "./tts";
import type { TtsPlaybackContextKey } from "./ttsContext";
import { usePatientAutopilot, type PatientAutopilotView } from "./usePatientAutopilot";
import { usePatientBinding } from "./usePatientBinding";

function withPatientPauseStorageLock<T>(
  operation: () => T,
): Promise<T> {
  if (!navigator.locks?.request) {
    return Promise.reject(new Error("浏览器不支持安全的暂停锁"));
  }
  return navigator.locks.request(
    PATIENT_PAUSE_STORAGE_LOCK_NAME,
    { mode: "exclusive" },
    operation,
  );
}

// 老人端外壳:旧流程仍是只读游标显示器；只有服务器证明 P0a 活跃后，
// 才互斥切换到独立的设备命令/ACK 运行器。
// 无 session 超时、无自动黑屏、对沉默与错误宽容(永不报错、永不闪红)。
export function PatientShell() {
  const { session, cursor, rapportStep, connection } = useLiveCursor();
  const { bound: patientBindingActive } = usePatientBinding();
  const terminal = isSessionTerminalStatus(session?.runtimeStatus);
  const connectionReady = connection === "connected";
  const [patientActivated, setPatientActivated] = useState(false);
  const [ttsOn, setTtsOn] = useState(ttsEnabled());
  const [safetyLatch, setSafetyLatch] = useState<BedsideSafetyLatch | null>(null);
  const [patientPauseLatch, setPatientPauseLatch] = useState<PatientPauseOutbox | null>(() => (
    typeof window === "undefined" ? null : loadPatientPauseOutbox(window.localStorage)
  ));
  const [patientPauseStorageFailed, setPatientPauseStorageFailed] = useState(false);
  const safetyPaused = bedsideSafetyIsLatched(safetyLatch, session?.sessionId ?? null);
  const patientPauseLatched = patientPauseLatch !== null
    && patientPauseLatch.sessionId === session?.sessionId
    && patientPauseLatch.state !== "resolved";
  const effectivePaused = session?.paused === true || safetyPaused || patientPauseLatched;
  const patientPauseStopped = session?.paused === true || patientPauseLatched || safetyPaused;
  // plan 必须与 sessionId 成对保存。新场次握手到达的首帧就拒用旧 plan，
  // 不能等 effect 清理后才撤下上一位受试者的题目。
  const [loadedPlan, setLoadedPlan] = useState<{ sessionId: string; plan: PatientSessionPlan } | null>(null);
  const plan = session && loadedPlan?.sessionId === session.sessionId ? loadedPlan.plan : null;
  const autopilot = usePatientAutopilot({
    sessionId: session && session.mode !== "rapport" && !terminal ? session.sessionId : null,
    activated: patientActivated,
    ttsOn,
    connectionReady,
    sessionPaused: effectivePaused,
    serverPaused: session?.paused === true,
    sessionTerminal: terminal,
  });
  const stopAutopilotMediaRef = useRef(autopilot.stopMediaNow);
  stopAutopilotMediaRef.current = autopilot.stopMediaNow;
  const stopAutopilotForPatientPauseRef = useRef(autopilot.stopForPatientPauseNow);
  stopAutopilotForPatientPauseRef.current = autopilot.stopForPatientPauseNow;
  const discardLegacyRecordingRef = useRef<(() => void) | null>(null);
  const registerImmediateDiscard = useCallback((handler: (() => void) | null) => {
    discardLegacyRecordingRef.current = handler;
  }, []);

  const stopLocallyForPatientPause = useCallback((
    sessionId: string,
    serverPaused = false,
  ) => {
    stopSpeaking();
    clearTtsContext();
    stopAutopilotForPatientPauseRef.current();
    discardLegacyRecordingRef.current?.();
    setSafetyLatch((current) => reconcileBedsideSafetyLatch(
      current?.sessionId === sessionId
        ? current : latchBedsideSafetyStop(sessionId, sessionId),
      sessionId,
      serverPaused,
    ));
  }, []);

  const requestPatientPause = useCallback(() => {
    const sessionId = session?.sessionId;
    if (!sessionId || terminal || patientPauseStopped) return;

    // Safety ordering is intentional: everything above the first async API
    // continuation is local and synchronous. No network/storage failure can
    // leave speech or a microphone running after pointerdown.
    stopLocallyForPatientPause(sessionId);

    const persistOneIntent = () => {
      const persisted = loadPatientPauseOutbox(window.localStorage, sessionId);
      const pending = persisted && persisted.state !== "resolved"
        ? persisted : createPatientPauseOutbox(sessionId);
      const saved = persisted === pending
        || savePatientPauseOutbox(window.localStorage, pending);
      if (!saved) {
        // Without a durable exact key there is no safe retry/cross-tab epoch.
        // Keep only the synchronous reduction latch and tell the patient to
        // call staff; do not fabricate an outbox or submit an unrepeatable API.
        setPatientPauseStorageFailed(true);
        setPatientPauseLatch(null);
        return;
      }
      setPatientPauseStorageFailed(false);
      setPatientPauseLatch(pending);
      bus.post({
        type: "patientPauseStop",
        sessionId,
        idempotencyKey: pending.idempotencyKey,
      });
    };
    if (navigator.locks?.request) {
      void navigator.locks.request(
        PATIENT_PAUSE_STORAGE_LOCK_NAME,
        { mode: "exclusive" },
        () => { persistOneIntent(); },
      ).catch(() => {
        // Never perform an unlocked read/modify/write on the cross-tab slot.
        // The synchronous physical safety latch remains in force and staff are
        // told that this browser cannot durably deliver the request.
        setPatientPauseStorageFailed(true);
      });
    } else {
      // Device preflight treats Web Locks as required.  The fallback remains
      // local-only and reduction-only; it never mutates the shared outbox.
      setPatientPauseStorageFailed(true);
    }
  }, [patientPauseStopped, session?.sessionId, stopLocallyForPatientPause, terminal]);

  // A refresh or same-tab navigation must restore the local safety latch before
  // any delayed server response. A different live session never inherits it.
  useLayoutEffect(() => {
    const sessionId = session?.sessionId;
    setPatientPauseStorageFailed(false);
    setPatientPauseLatch(sessionId
      ? loadPatientPauseOutbox(window.localStorage, sessionId)
      : null);
  }, [session?.sessionId]);

  const [capabilityEpoch, setCapabilityEpoch] = useState(0);
  useEffect(() => {
    const updated = () => setCapabilityEpoch((value) => value + 1);
    window.addEventListener(DEVICE_CAPABILITY_UPDATED_EVENT, updated);
    return () => window.removeEventListener(DEVICE_CAPABILITY_UPDATED_EVENT, updated);
  }, []);

  // Pending is a tiny origin-local outbox containing no bearer or patient text.
  // Offline/auth/server failures retain both the local latch and exact key;
  // reconnect, reopen, or re-pair retries the same request.
  // An ACK remains persisted until LiveState independently projects paused, so
  // a refresh in the response/poll gap cannot reopen media.
  useEffect(() => {
    const pending = patientPauseLatch;
    const sessionId = session?.sessionId;
    if (!pending || pending.sessionId !== sessionId) return;
    if (pending.state === "resolved") return;
    let cancelled = false;
    const advanceShared = (operation: () => PatientPauseOutbox) => (
      withPatientPauseStorageLock(operation).then((next) => {
        if (cancelled) return next;
        setPatientPauseLatch((current) => (
          current?.sessionId === pending.sessionId
            && current.idempotencyKey === pending.idempotencyKey
            ? next : current
        ));
        return next;
      })
    );
    const markStorageFailure = () => {
      if (!cancelled) setPatientPauseStorageFailed(true);
    };
    if (terminal) {
      // Terminal is reduction-only and permanent, but a lagging tab may still
      // need the shared key to stop immediately before its next live poll.
      if (pending.state !== "server_pause_observed"
          && Number.isSafeInteger(session?.wseq) && Number(session?.wseq) >= 1) {
        void advanceShared(() => observePatientPauseOnServer(
          window.localStorage, pending, Number(session?.wseq))).then(() => {
          if (!cancelled) setPatientPauseStorageFailed(false);
        }).catch(markStorageFailure);
      }
      return () => { cancelled = true; };
    }
    if (session?.paused === true) {
      // liveSeq in the API receipt is a different clock.  Persist only this
      // SessionMsg.wseq, then compare it with a later SessionMsg.wseq so a
      // closed/reopened tab cannot mistake an old active frame for resume.
      if (pending.state !== "server_pause_observed"
          && Number.isSafeInteger(session.wseq) && Number(session.wseq) >= 1) {
        void advanceShared(() => observePatientPauseOnServer(
          window.localStorage, pending, Number(session.wseq))).then(() => {
          if (!cancelled) setPatientPauseStorageFailed(false);
        }).catch(markStorageFailure);
      }
      return () => { cancelled = true; };
    }
    if (patientPauseCanResolveAfterResume(
      pending,
      session?.wseq,
      false,
      terminal,
    )) {
      // Only the authoritative paused -> resumed transition retires the key.
      // A lagging tab's old active frame cannot suppress another tab's stop.
      void advanceShared(() => resolvePatientPauseOutbox(
        window.localStorage, pending, session?.wseq, false, terminal,
      )).catch(markStorageFailure);
      return () => { cancelled = true; };
    }
    if (!connectionReady || pending.state !== "pending") return;

    let retryTimer: number | null = null;
    let retryDelayMs = 3_000;
    const scheduleRetry = () => {
      retryTimer = window.setTimeout(submit, retryDelayMs);
      retryDelayMs = Math.min(retryDelayMs * 2, 30_000);
    };
    const submit = () => {
      void api.patientPause(pending.sessionId, pending.idempotencyKey).then(
        () => {
          void advanceShared(() => acknowledgePatientPauseOutbox(
            window.localStorage, pending)).catch(markStorageFailure);
        },
        (error: unknown) => {
        if (cancelled) return;
        const disposition = patientPauseRetryDisposition(error);
        if (disposition === "wait_for_capability") {
          // The capability update event is the only retry trigger for a dead
          // pairing.  A fixed timer must not hammer an expired credential.
          return;
        }
        if (disposition === "retry") {
          scheduleRetry();
          return;
        }
        // A stable rejection cannot release media and must not be retried for
        // ever.  Keep the local reduction latch until an independent server
        // paused projection (and later an authorized resume) resolves it.
        void advanceShared(() => requirePatientPauseServerResolution(
          window.localStorage, pending)).catch(markStorageFailure);
        },
      );
    };
    submit();
    return () => {
      cancelled = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
    };
  }, [patientPauseLatch, session?.sessionId, session?.paused, session?.wseq, terminal,
    connectionReady, capabilityEpoch]);

  useLayoutEffect(() => bus.subscribe((message) => {
    if (message.sessionId !== session?.sessionId) return;
    if (message.type === "patientPauseStop") {
      // A background tab may receive this before its own live poll sees the
      // pause, but must reject it after the shared exact key has completed an
      // authoritative paused -> resumed transition.
      const pending = patientPauseOutboxForStopSignal(
        window.localStorage, message.sessionId, message.idempotencyKey);
      // The sender persists before broadcasting.  Missing, superseded, or
      // resolved keys are stale/unrecoverable hints and must not recreate a
      // pause epoch after an authorized resume.
      if (!pending) return;
      stopLocallyForPatientPause(message.sessionId, session.paused === true);
      setPatientPauseLatch(pending);
      return;
    }
    if (message.type !== "safetyStop") return;
    // BroadcastChannel 的安全停是减权事件：先同步撤销两套语音/媒体执行器，
    // 再闩住 React 舞台，旧 cursor 即使晚到也无法重新开麦。
    clearTtsContext();
    stopAutopilotMediaRef.current();
    setSafetyLatch(() => reconcileBedsideSafetyLatch(
      latchBedsideSafetyStop(session.sessionId, message.sessionId),
      session.sessionId,
      session.paused === true,
    ));
  }), [session?.sessionId, session?.paused, stopLocallyForPatientPause]);

  useLayoutEffect(() => {
    if (safetyLatch?.sessionId === session?.sessionId
        && safetyLatch?.observedServerPause === true
        && session?.paused !== true
        && !patientPauseLatched) {
      // A local-only fallback (for example storage failure) has now observed
      // the full authoritative paused -> resumed sequence. It can clear both
      // its reduction latch and the old delivery warning without an outbox.
      setPatientPauseStorageFailed(false);
    }
    setSafetyLatch((current) => reconcileBedsideSafetyLatch(
      current,
      session?.sessionId ?? null,
      session?.paused === true,
    ));
  }, [session?.sessionId, session?.paused, safetyLatch?.sessionId,
    safetyLatch?.observedServerPause, patientPauseLatched]);

  // 切场即清空激活态与已发信号记录:旧场次的激活绝不能替新场次说话。
  // 点击激活在发生的那一刻就绑定当时的 sessionId(ref 同步写,不等 setState),
  // 切场后的那一次 commit 里即使 patientActivated 还是旧值也发不出信号。
  const activatedForSession = useRef<string | null>(null);
  const activationSignaledFor = useRef<string | null>(null);
  useEffect(() => {
    setPatientActivated(false);
    activatedForSession.current = null;
    activationSignaledFor.current = null;
  }, [session?.sessionId]);

  // 老人一次明确点击 + 朗读开 + 连接就绪 → 对本窗操作端发一次 exact-session 激活信号。
  // 同一场次同一激活生命周期只发一次;它只是本机触发条件,操作端仍走全套门禁,
  // 服务端照常 fail-closed 重验——这里不产生任何授权或使用证据。
  useEffect(() => {
    const sessionId = session?.sessionId ?? null;
    if (!canSignalBedsideActivation({
      sessionId,
      sessionMode: session ? session.mode : null,
      sessionTerminal: terminal,
      activatedForSessionId: patientActivated ? activatedForSession.current : null,
      ttsOn,
      connectionReady,
      alreadySignaledSessionId: activationSignaledFor.current,
    })) return;
    activationSignaledFor.current = sessionId;
    window.dispatchEvent(new CustomEvent(PATIENT_ACTIVATION_EVENT, { detail: { sessionId } }));
  }, [session, terminal, patientActivated, ttsOn, connectionReady]);

  // useLayoutEffect:paint 前设好 34px 尺度,避免老人端首帧 16px→34px 全屏字号跳变
  useLayoutEffect(() => {
    document.documentElement.dataset.scale = "patient";
    return () => {
      clearTtsContext();
      // 仅清理自己仍持有的值，避免路由切换时覆盖另一端刚设置的尺度。
      if (document.documentElement.dataset.scale === "patient") {
        delete document.documentElement.dataset.scale;
      }
    };
  }, []);

  // 收到 session 握手 → 自取 plan;拉取失败每 3 秒悄悄重试直到成功——
  // 老人端是哑显示器,没有可点的恢复入口,后端抖一下不能让它永远停在"请稍候"。
  useEffect(() => {
    // Server-owned/probing mode consumes only current-command projections. Do
    // not prefetch the legacy multi-item plan into that control plane.
    if (!session || terminal || session.mode === "rapport" || autopilot.mode !== "legacy") {
      setLoadedPlan(null);
      return;
    }
    const sessionId = session.sessionId;
    setLoadedPlan(null);
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      api.patientSessionPlan(sessionId, session.weekNo, session.eventLine)
        .then((raw) => parsePatientSessionPlan(raw))
        .then((p) => { if (!cancelled) setLoadedPlan({ sessionId, plan: p }); })
        .catch(() => { if (!cancelled) timer = window.setTimeout(load, 3000); });
    };
    load();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.sessionId, session?.weekNo, session?.eventLine, session?.mode, terminal, autopilot.mode]);

  const autopilotScreen: PatientPresenceScreen = autopilotPresenceScreen({
    runtimePhase: autopilot.runtime?.phase,
    mode: autopilot.mode,
    blockedCalm: autopilot.blockedCalm,
  });
  const currentScreen: PatientPresenceScreen = !session
    ? "waiting"
    : terminal
      ? "complete"
      : effectivePaused
      ? "paused"
      : session.mode === "rapport"
      ? rapportStep?.paused === true ? "paused" : "rapport"
      : autopilot.mode !== "legacy"
        ? autopilotScreen
      : cursor?.screen === "thanks"
        ? "thanks"
        : cursor?.screen === "paused"
          ? "paused"
        : cursor?.screen === "done"
          ? "complete"
          : !plan || !cursor
            ? "loading"
            : cursor.screen === "record"
              ? "record"
              : cursor.screen === "present"
                ? "present"
                : cursor.screen === "idle"
                  ? "waiting"
                  : "waiting";
  const commandSeq = session?.mode === "rapport"
    ? rapportStep?.wseq
    : autopilot.mode !== "legacy" ? autopilot.current?.command_seq : cursor?.wseq;
  usePatientHeartbeat(session?.sessionId, currentScreen, commandSeq ?? session?.wseq);

  // Generic/legacy TTS gets an exact replay namespace.  It is deliberately
  // absent while a new item is loading, during pause/disconnect/terminal views,
  // and whenever the server-owned command runner controls media.
  const ttsContextKey: TtsPlaybackContextKey | null = !session || terminal
      || effectivePaused || !connectionReady
    ? null
    : session.mode === "rapport" && currentScreen === "rapport"
      && rapportStep?.sectionKey !== undefined
      ? `session:${session.sessionId}|plane:rapport|section:${rapportStep.sectionKey}|question:${rapportStep.questionIdx}`
      : session.mode !== "rapport" && autopilot.mode === "legacy"
        && plan && cursor && (currentScreen === "present" || currentScreen === "record")
        ? `session:${session.sessionId}|plane:legacy|item:${cursor.itemIdx}|turn:${cursor.turnIdx}`
        : null;

  useLayoutEffect(() => {
    setTtsContext(ttsContextKey);
  }, [ttsContextKey]);

  useLayoutEffect(() => {
    if (!connectionReady || ["paused", "complete", "loading", "waiting", "thanks"].includes(currentScreen)) {
      stopSpeaking();
    }
  }, [connectionReady, currentScreen]);

  // capabilityEpoch 每次能力变化都会自增触发重渲染,这里读到的配对状态因此新鲜。
  void capabilityEpoch;
  const devicePaired = patientBindingActive || getDeviceCapability() !== null;

  let body: React.ReactNode;
  if (!session) {
    body = (
      <Centered>
        <div className="target">您好</div>
        <p className="question">准备好了我们就开始</p>
        {patientBindingActive && (
          <p className="question" role="status" aria-live="polite">
            已连接 · 等待训练开始
          </p>
        )}
        {!devicePaired && (
          // 「暂不配对」之后的可见找回路径:不弹错、不吓老人,一个安静的入口,
          // 工作人员点它重新打开配对框——绝不依赖整页刷新。
          <button type="button" className="patient-pair-entry"
            onClick={() => window.dispatchEvent(new Event(PIN_PROMPT_MANUAL_OPEN_EVENT))}>
            连接这台平板
            <small>请工作人员点这里完成配对</small>
          </button>
        )}
      </Centered>
    );
  } else if (terminal) {
    body = <Centered><div className="target">今天辛苦了</div><p className="question">今天的练习已经结束</p></Centered>;
  } else if (session.mode === "rapport") {
    // 暂停不卸载舞台:卸载会丢掉保存失败的 pendingSave 与重试入口,恢复后该段回答
    // 从流程里无声消失。舞台保持挂载,休息屏由舞台自己渲染,录音经 suspended 立即停麦。
    body = <RapportStage key={session.sessionId} rapportStep={rapportStep} sessionId={session.sessionId}
      ttsContextKey={ttsContextKey}
      connectionReady={connectionReady} sessionPaused={effectivePaused} sessionTerminal={terminal}
      registerImmediateDiscard={registerImmediateDiscard} />;
  } else {
    body = autopilot.mode === "legacy"
      ? <PatientStage key={session.sessionId} plan={plan} cursor={cursor} sessionId={session.sessionId}
          ttsContextKey={ttsContextKey}
          connectionReady={connectionReady} sessionPaused={effectivePaused} sessionTerminal={terminal}
          registerImmediateDiscard={registerImmediateDiscard} />
      : <PatientAutopilotStage
          key={session.sessionId}
          autopilot={autopilot}
          sessionId={session.sessionId}
          activated={patientActivated}
          ttsOn={ttsOn}
          externallyPaused={effectivePaused}
        />;
  }
  return <>
    {body}
    {session && !connectionReady && (
      <div className="patient-connection-notice" role="status" aria-live="polite" aria-atomic="true">
        <div className="patient-connection-notice__mark" aria-hidden="true">语</div>
        <div className="patient-connection-notice__copy">
          <strong>{connection === "connecting" ? "正在连接" : "正在重新连接"}</strong>
          <span>请稍候，我们会接着刚才的内容</span>
        </div>
      </div>
    )}
    {session && !terminal && (
      <button
        type="button"
        className={`patient-pause-button${patientPauseStopped ? " is-latched" : ""}`}
        aria-pressed={patientPauseStopped}
        aria-label={patientPauseStorageFailed
          ? "已经停下了，请马上喊工作人员"
          : patientPauseStopped ? "练习已停止，请等待工作人员" : "暂停练习"}
        disabled={patientPauseStopped}
        onPointerDown={requestPatientPause}
        // Pointer clicks have already stopped media on pointerdown. A click
        // with detail=0 is the keyboard/assistive-technology activation path.
        onClick={(event) => { if (event.detail === 0) requestPatientPause(); }}
      >
        <span>{session.paused === true
          ? "练习已暂停"
          : patientPauseStopped ? "已经停下了" : "暂停练习"}</span>
        <small>{patientPauseStorageFailed
          ? "没能通知到系统，请马上喊工作人员"
          : session.paused === true
          ? "请等待工作人员处理"
          : patientPauseLatched
            ? patientPauseLatch?.state === "server_resolution_required"
                ? "已经停下了，请等工作人员来"
                : "已经停下了，请喊工作人员"
            : safetyPaused
              ? "请等待工作人员处理"
              : "需要休息或帮助时点这里"}</small>
      </button>
    )}
    <TapToStart tapped={patientActivated} onTap={() => {
      activatedForSession.current = session?.sessionId ?? null;
      setPatientActivated(true);
      // 这一次点击同时把朗读打开,主流程不再需要第二次触碰。
      // 角落的朗读钮仍可随时关掉。
      if (!ttsOn) {
        setTtsEnabled(true);
        setTtsOn(true);
      }
    }} />
    <TtsToggle on={ttsOn} onChange={setTtsOn} sampleContextKey={ttsContextKey}
      mode={autopilot.mode} />
    <PinPrompt />
  </>;
}

// 点击式启动:浏览器要求页面有过一次点击才放行语音(user activation)。
// 与其让研究者记住"先摸一下屏幕"这条隐性规则,不如给一个明确的可点落点;
// 点击本身即授予激活,tts 的 pointerdown 补读会把当前该读的话读出来。
function TapToStart({ tapped, onTap }: { tapped: boolean; onTap(): void }) {
  if (tapped) return null;
  return (
    <button type="button" className="tap-overlay" aria-label="点一下，开始" onClick={onTap}>
      <div className="tap-overlay-inner">
        <div className="tap-overlay-mark" aria-hidden="true">语</div>
        <div>点一下，开始</div>
        <div className="tap-overlay-sub">开始后会听到声音</div>
      </div>
    </button>
  );
}

// 语音开关(设备本地设置,研究者代点):角落小钮,不进老人的视觉主线。
// 点开即试读当前屏显文本——既给浏览器用户激活,也当场听到用的是哪个音色。
function TtsToggle({ on, onChange, sampleContextKey, mode }: {
  on: boolean;
  onChange(on: boolean): void;
  sampleContextKey: TtsPlaybackContextKey | null;
  mode: PatientAutopilotView["mode"];
}) {
  return (
    <button
      type="button"
      className="tts-toggle"
      aria-label={on ? "关闭语音朗读" : "开启语音朗读"}
      aria-pressed={on}
      // 只有 legacy 平面才由本机音色出声，也只有它才可以读本机音色名。
      title={ttsToggleTitle(on, mode, mode === "legacy" ? currentVoiceName() : null)}
      onClick={() => {
        const next = !on;
        setTtsEnabled(next);
        onChange(next);
        // P0a owns its own receipt-bearing player. A legacy sample here would
        // overlap the server command and create an unacknowledged second voice.
        if (next && sampleContextKey) speakSample(sampleContextKey);
      }}
    >
      <span className="tts-toggle-label" aria-hidden="true">朗读</span>
      <span className="tts-toggle-state" aria-hidden="true">{on ? "开" : "关"}</span>
    </button>
  );
}
