import { useCallback, useEffect, useRef, useState } from "react";
import {
  DEVICE_CAPABILITY_UPDATED_EVENT,
  DEVICE_PAIR_REQUIRED_EVENT,
  selectDeviceCredential,
} from "../api.ts";
import { DurableAutopilotAckDelivery } from "./autopilotAckDelivery.ts";
import {
  PatientAutopilotController,
  deviceCapabilityAllowsAutopilot,
} from "./autopilotController.ts";
import {
  canStartAutopilotRunner,
  shouldBootstrapAutopilotRunner,
  shouldSchedulePassiveAutopilotPoll,
} from "./autopilotHookRuntimeGate.ts";
import { autopilotHttpTransport } from "./autopilotHttpTransport.ts";
import {
  BrowserAutopilotRecordingExecutor,
  browserAutopilotRecoveryDependencies,
} from "./autopilotRecordingExecutor.ts";
import {
  restoreAutopilotRuntimeAfterRefresh,
  type AutopilotDrainedAck,
} from "./autopilotRecordingRecovery.ts";
import {
  autopilotNextTickDelayMs,
  type AutopilotRuntimeState,
} from "./autopilotRuntime.ts";
import { BrowserAutopilotSpeechExecutor } from "./autopilotSpeechExecutor.ts";
import { browserAutopilotSpeechPorts } from "./autopilotBrowserSpeechPorts.ts";
import {
  acknowledgeExactAutopilotDrain,
  fetchExactAutopilotDrainTarget,
} from "./autopilotMediaTransport.ts";
import { browserAutopilotMediaDependencies } from "./autopilotBrowserMediaDependencies.ts";
import {
  captureIdentityIsNewer,
  sameCaptureIdentity,
  type LocalAutopilotCaptureEvent,
  type LocalAutopilotCapturePhase,
} from "./autopilotCapturePresentation.ts";
import {
  AutopilotPageLifecycle,
  browserAutopilotLifecycleHost,
} from "./autopilotPageLifecycle.ts";
import type { NextCommandProjection } from "./autopilotProtocol.ts";
import { stopSpeaking } from "./tts.ts";
import {
  canProbePatientAutopilot,
  canRunPatientAutopilotMedia,
  resolvePatientAutopilotVisibleMode,
  type PatientAutopilotMode,
} from "./autopilotAdmission.ts";
import {
  acquireAutopilotOwnerLease,
  type AutopilotOwnerLease,
} from "./autopilotOwnerLease.ts";
import { AutopilotExecutionFence } from "./autopilotExecutionFence.ts";
import {
  acknowledgeDrainAfterShutdown,
  assertExactDrainTransition,
} from "./autopilotDrainCoordinator.ts";
import {
  classifyAutopilotDrainFailure,
  drainRetryDelayMs,
} from "./autopilotDrainRetryPolicy.ts";
import { settleAutopilotServerExit } from "./autopilotServerExitCoordinator.ts";
import {
  exactAutopilotApiCode,
  planAutopilotProbeFailure,
} from "./autopilotProbePolicy.ts";
import type { PatientAssetReadinessEvent } from "./currentPatientAsset.ts";
import { PatientAssetMediaGate } from "./patientAssetMediaGate.ts";
import {
  PATIENT_AUTOPILOT_WAKE_EVENT,
  PatientProbeWakeCoordinator,
} from "../sync/autopilotWake.ts";

export type { PatientAutopilotMode } from "./autopilotAdmission.ts";

interface ServerContext {
  delivery: DurableAutopilotAckDelivery;
  current: NextCommandProjection | null;
  runtime: AutopilotRuntimeState;
  ownerLease: AutopilotOwnerLease;
  ownerGeneration: number;
  controller: PatientAutopilotController | null;
  fence: AutopilotExecutionFence;
  drainedCommandKey: string | null;
  exitPromise: Promise<void> | null;
}

let nextAutopilotOwnerGeneration = 0;

export interface PatientAutopilotView {
  mode: PatientAutopilotMode;
  runtime: AutopilotRuntimeState | null;
  current: NextCommandProjection | null;
  reason: string | null;
  /** 仅 mode==="blocked" 时有意义:平静档(status)还是告警档(alert)。 */
  blockedCalm: boolean;
  assetReadiness: PatientAssetReadinessEvent | null;
  /** Browser-only: bytes captured, still saving. Never a server phase. */
  localCapturePhase: LocalAutopilotCapturePhase | null;
  reportAssetReadiness(event: PatientAssetReadinessEvent): void;
  stopMediaNow(): void;
  stopForPatientPauseNow(): void;
  stopRecordingNow(): void;
}

function blockedReason(error: unknown): string {
  const code = exactAutopilotApiCode(error);
  if (code === "autopilot_command_device_mismatch"
      || code === "autopilot_command_device_rotated") {
    return "自动流程已绑定其他设备，请研究者处置";
  }
  if (code === "autopilot_state_invalid" || code === "autopilot_command_invalid") {
    return "自动流程状态不一致，已安全停止";
  }
  if (code === "autopilot_runtime_inactive") {
    // 服务器收走了本 runtime 世代(收尾/暂停/中止),不是设备故障;与控制器的
    // runtime_released 暂停同一档平静文案,结局由 live 会话通道呈现。
    return "练习已暂停，请稍候";
  }
  return "自动流程暂时无法确认，请研究者查看";
}

/** blocked 屏的紧急度:只有 runtime 被服务器收走这一族用平静档(role=status)。 */
function blockedIsCalm(error: unknown): boolean {
  return exactAutopilotApiCode(error) === "autopilot_runtime_inactive";
}

async function loadServerRuntime(
  sessionId: string,
  delivery: DurableAutopilotAckDelivery,
  drained?: AutopilotDrainedAck | null,
): Promise<{ current: NextCommandProjection | null; runtime: AutopilotRuntimeState }> {
  const runtime = await restoreAutopilotRuntimeAfterRefresh({
    sessionId,
    next: (id) => autopilotHttpTransport.next(id),
    delivery,
    deps: browserAutopilotRecoveryDependencies,
    drained,
  });
  return { current: runtime.command, runtime };
}

export function usePatientAutopilot(input: {
  sessionId: string | null;
  activated: boolean;
  ttsOn: boolean;
  connectionReady: boolean;
  sessionPaused: boolean;
  sessionTerminal: boolean;
}): PatientAutopilotView {
  const [capabilityRevision, setCapabilityRevision] = useState(0);
  const [mode, setMode] = useState<PatientAutopilotMode>("legacy");
  const [probeEpoch, setProbeEpoch] = useState(0);
  const [resolvedProbeKey, setResolvedProbeKey] = useState("");
  const [server, setServer] = useState<ServerContext | null>(null);
  const [reason, setReason] = useState<string | null>(null);
  // blocked 屏的紧急度:true=平静档(runtime 被服务器收走),false=告警档。
  // 每个 setMode("blocked") 位点都必须显式定它,不许继承上一次的值。
  const [blockedCalm, setBlockedCalm] = useState(false);
  const [assetReadiness, setAssetReadiness] = useState<PatientAssetReadinessEvent | null>(null);
  const assetGateRef = useRef<PatientAssetMediaGate | null>(null);
  if (assetGateRef.current === null) assetGateRef.current = new PatientAssetMediaGate();
  const wakeCoordinatorRef = useRef<PatientProbeWakeCoordinator | null>(null);
  if (wakeCoordinatorRef.current === null) {
    wakeCoordinatorRef.current = new PatientProbeWakeCoordinator();
  }
  const [wakeNonce, setWakeNonce] = useState(0);
  const controllerRef = useRef<PatientAutopilotController | null>(null);
  const lifecycleRef = useRef<AutopilotPageLifecycle | null>(null);
  const serverContextRef = useRef<ServerContext | null>(null);
  const probeRetryAttempt = useRef(0);
  const probeRetryKey = useRef("");
  serverContextRef.current = server;
  const [localCapturePhase, setLocalCapturePhase] =
    useState<LocalAutopilotCapturePhase | null>(null);
  const capturedSessionRef = useRef<string | null>(null);
  capturedSessionRef.current = (input.sessionId as string | null) ?? null;

  // A capture that finished after the screen already moved on must neither
  // claim the new command nor clear it: a stale observer simply returns.
  // `cleared` is stricter still — it only wipes the exact identity that put the
  // phase there, so a dying old capture cannot blank a live new one.
  const observeCapturePhase = useCallback((event: LocalAutopilotCaptureEvent) => {
    if (event.sessionId !== capturedSessionRef.current) return;
    setLocalCapturePhase((current) => {
      if (event.phase === "cleared") {
        return sameCaptureIdentity(current, event) ? null : current;
      }
      if (!captureIdentityIsNewer(event, current)) return current;
      if (serverContextRef.current?.current?.command_key !== event.commandKey) return current;
      return event;
    });
  }, []);

  const reportAssetReadiness = useCallback((event: PatientAssetReadinessEvent) => {
    // A stale image callback can arrive after React has projected a newer
    // command. It must neither satisfy nor fail that newer command's gate.
    if (serverContextRef.current?.current?.command_key !== event.requestKey) return;
    assetGateRef.current?.report(event.requestKey, event.readiness);
    setAssetReadiness(event);
    if (event.readiness !== "failed") return;
    stopSpeaking();
    controllerRef.current?.stop();
    setMode("blocked");
    setBlockedCalm(false);
    setReason("题目图片未能安全显示，已停止语音和录音，请研究者处置");
  }, []);

  const releaseServerToLegacy = useCallback((
    context: ServerContext,
    resolvedKey: string,
  ): Promise<void> => {
    if (context.exitPromise) return context.exitPromise;
    stopSpeaking();
    setMode("probing");
    setReason("正在安全释放服务器控制");
    const active = context.controller;
    context.controller = null;
    if (controllerRef.current === active) controllerRef.current = null;
    const shutdown = active?.stopAndWait() ?? null;
    context.exitPromise = settleAutopilotServerExit({
      fence: context.fence,
      shutdown,
      ownerLease: context.ownerLease,
    }).then(() => {
      if (serverContextRef.current !== context) return;
      setServer(null);
      setResolvedProbeKey(resolvedKey);
      setReason(null);
      setMode("legacy");
    });
    return context.exitPromise;
  }, []);

  useEffect(() => {
    const update = () => setCapabilityRevision((value) => value + 1);
    window.addEventListener(DEVICE_CAPABILITY_UPDATED_EVENT, update);
    return () => window.removeEventListener(DEVICE_CAPABILITY_UPDATED_EVENT, update);
  }, []);

  useEffect(() => {
    assetGateRef.current?.reset("场次已切换");
    setAssetReadiness(null);
    setLocalCapturePhase(null);
    wakeCoordinatorRef.current?.reset();
  }, [input.sessionId]);

  // 服务器取得控制权后的同窗一次性唤醒(console 权威回执→本 hook):监听只锁存,
  // 严格拒绝旧场次/空场次/畸形与重复 token;真正的重新探测由下面的消费 effect
  // 按"已解析且仍是 legacy"一次性放行。
  useEffect(() => {
    const sessionId = input.sessionId;
    if (!sessionId) return;
    const onWake = (event: Event) => {
      const detail = (event as CustomEvent<unknown>).detail;
      if (wakeCoordinatorRef.current?.receive(detail, sessionId)) {
        setWakeNonce((value) => value + 1);
      }
    };
    window.addEventListener(PATIENT_AUTOPILOT_WAKE_EVENT, onWake);
    return () => window.removeEventListener(PATIENT_AUTOPILOT_WAKE_EVENT, onWake);
  }, [input.sessionId]);

  const credential = input.sessionId ? selectDeviceCredential(input.sessionId) : null;
  const capability = credential?.source === "active" ? credential.record : null;
  // Never copy the bearer capability into React state/dependency keys. Rotation
  // is represented by the in-memory update revision instead.
  const capabilityKey = capability
    ? `${capability.sessionId}\u0000${capability.expiresAt}\u0000${capabilityRevision}` : "";
  const probeKey = input.sessionId && capabilityKey
    ? `${input.sessionId}\u0000${capabilityKey}` : "";
  const probeAllowed = canProbePatientAutopilot({
    hasSession: input.sessionId !== null,
    hasExactCapability: capability !== null,
    sessionTerminal: input.sessionTerminal,
  });
  // Suppress the legacy subtree on the very first render that sees an exact
  // capability. Waiting for a passive effect would let legacy TTS/recorder
  // effects fire once before the P0a ownership probe finishes.
  const visibleMode = resolvePatientAutopilotVisibleMode({
    hasSession: input.sessionId !== null,
    hasExactCapability: capability !== null,
    probeKey,
    resolvedProbeKey,
    resolvedMode: mode,
  });

  useEffect(() => {
    const sessionId = input.sessionId;
    const selected = sessionId ? selectDeviceCredential(sessionId) : null;
    const activeCapability = selected?.source === "active" ? selected.record : null;
    if (!sessionId || input.sessionTerminal) {
      stopSpeaking();
      setMode("legacy");
      setResolvedProbeKey("");
      setServer(null);
      setReason(null);
      return;
    }
    if (!deviceCapabilityAllowsAutopilot(activeCapability, sessionId)) {
      stopSpeaking();
      setServer(null);
      setResolvedProbeKey("");
      setMode("probing");
      setReason("请研究者完成本场次设备配对");
      queueMicrotask(() => {
        const current = selectDeviceCredential(sessionId);
        if (current.source !== "active" || current.record?.sessionId !== sessionId) {
          window.dispatchEvent(new Event(DEVICE_PAIR_REQUIRED_EVENT));
        }
      });
      return;
    }
    if (probeRetryKey.current !== probeKey) {
      probeRetryKey.current = probeKey;
      probeRetryAttempt.current = 0;
    }
    let cancelled = false;
    let retryTimer: number | null = null;
    const ownerAbort = new AbortController();
    let ownerLease: AutopilotOwnerLease | null = null;
    let retainedContext: ServerContext | null = null;
    const releaseOwner = () => {
      const lease = ownerLease;
      if (!lease) return;
      ownerLease = null;
      lease.release();
      void lease.released;
    };
    setMode("probing");
    setResolvedProbeKey("");
    setServer(null);
    setReason(null);
    void (async () => {
      try {
        ownerLease = await acquireAutopilotOwnerLease(
          sessionId, undefined, ownerAbort.signal);
        if (cancelled) return;
        const delivery = await DurableAutopilotAckDelivery.create({
          capability: activeCapability,
          transport: autopilotHttpTransport,
        });
        const drained = await delivery.drainPending();
        const restored = await loadServerRuntime(sessionId, delivery, drained);
        if (cancelled) return;
        stopSpeaking();
        nextAutopilotOwnerGeneration += 1;
        const context: ServerContext = {
          delivery,
          current: restored.current,
          runtime: restored.runtime,
          ownerLease,
          ownerGeneration: nextAutopilotOwnerGeneration,
          controller: null,
          fence: new AutopilotExecutionFence(),
          drainedCommandKey: null,
          exitPromise: null,
        };
        retainedContext = context;
        probeRetryAttempt.current = 0;
        setServer(context);
        setMode("server");
        setResolvedProbeKey(probeKey);
      } catch (error) {
        if (cancelled) return;
        setServer(null);
        const failurePlan = planAutopilotProbeFailure(
          error, probeRetryAttempt.current);
        if (failurePlan.action === "stop-legacy") {
          probeRetryAttempt.current = 0;
          setMode("legacy");
          setReason(null);
          setResolvedProbeKey(probeKey);
        } else if (failurePlan.action === "retry") {
          stopSpeaking();
          setMode("probing");
          setResolvedProbeKey("");
          setReason("服务器状态暂时无法确认，人工流程继续锁定并自动重试");
          probeRetryAttempt.current += 1;
          retryTimer = window.setTimeout(
            () => setProbeEpoch((value) => value + 1), failurePlan.delayMs);
        } else {
          probeRetryAttempt.current = 0;
          stopSpeaking();
          setMode("blocked");
          setReason(failurePlan.retryExhausted
            ? "服务器状态连续无法确认，已停止自动重试，请研究者检查连接后重新配对"
            : blockedReason(error));
          setBlockedCalm(!failurePlan.retryExhausted && blockedIsCalm(error));
          setResolvedProbeKey(probeKey);
        }
      } finally {
        if (retainedContext === null) releaseOwner();
      }
    })();
    return () => {
      cancelled = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      ownerAbort.abort(new DOMException("自动流程页面已离开", "AbortError"));
      const context = retainedContext;
      const controller = context?.controller ?? null;
      if (context && controller) {
        context.controller = null;
        context.fence.registerControllerShutdown(
          lifecycleRef.current?.aborted
            ? controller.interruptRecordingForLifecycleAndWait()
            : controller.stopAndWait());
      }
      const idle = context?.fence.waitForActiveStart() ?? Promise.resolve();
      void idle.finally(releaseOwner);
    };
  }, [input.sessionId, input.sessionTerminal, capabilityKey, probeKey, probeEpoch]);

  // stop-legacy 闩(防 409 风暴)保持:被闩住后不轮询,直到明确的 serverOwned 唤醒
  // 或 capability/session epoch 改变。消费一个待命唤醒 = 恰好一次 probeEpoch+1,
  // 重新走上面同一个所有权探测 effect 进入既有 server runner,不建第二套执行器;
  // 探测在途时唤醒被持有不丢弃,server 已在场时标记完成、绝不重建 runner。
  useEffect(() => {
    const probeResolved = probeKey !== "" && probeKey === resolvedProbeKey;
    if (wakeCoordinatorRef.current?.consume({ mode: visibleMode, probeResolved })) {
      setProbeEpoch((value) => value + 1);
    }
  }, [wakeNonce, visibleMode, probeKey, resolvedProbeKey]);

  // 每个 owner generation 恰好装一次页面事件监听，owner 一换就按序摘掉。
  // 摘监听本身不是失败：普通的 session/gate/所有权变化走各自的 shutdown。
  useEffect(() => {
    if (!server) return;
    const lifecycle = new AutopilotPageLifecycle({
      ownerGeneration: server.ownerGeneration,
      host: browserAutopilotLifecycleHost(),
      shutdown: () => {
        stopSpeaking();
        // 同步物理关麦发生在这一步的最前面；返回的 Promise 只是等 ACK 收敛。
        void controllerRef.current?.interruptRecordingForLifecycleAndWait();
      },
    });
    lifecycle.install();
    lifecycleRef.current = lifecycle;
    return () => {
      lifecycle.uninstall();
      if (lifecycleRef.current === lifecycle) lifecycleRef.current = null;
    };
  }, [server]);

  // 只在 mount 上跑的 effect：它的 cleanup 与 StrictMode 探针当下无法区分，
  // 所以只登记一个 teardown 候选并排一个 microtask；紧接着的第二次 setup
  // 精确取消它。没有后继 setup 的那次才是真卸载，关麦上界一个 microtask。
  useEffect(() => {
    lifecycleRef.current?.cancelTeardown();
    return () => { lifecycleRef.current?.requestTeardown(); };
  }, []);

  const mediaAllowed = canRunPatientAutopilotMedia({
    serverOwned: visibleMode === "server",
    hasDurableDelivery: server !== null,
    activated: input.activated,
    ttsOn: input.ttsOn,
    connectionReady: input.connectionReady,
    sessionPaused: input.sessionPaused,
    sessionTerminal: input.sessionTerminal,
  });

  // Before user activation (or while explicitly gated), polling may update the
  // visible command but can never enter either media executor or emit an ACK.
  useEffect(() => {
    if (visibleMode !== "server" || !server || mediaAllowed || !probeAllowed
        || !shouldSchedulePassiveAutopilotPoll(server.runtime.phase)) return;
    let cancelled = false;
    let timer: number | null = null;
    let retryAttempt = 0;
    const poll = async () => {
      try {
        await server.fence.runPassive(async () => {
          if (cancelled) return;
          const restored = await loadServerRuntime(input.sessionId as string, server.delivery);
          if (!cancelled) setServer((before) => before?.delivery === server.delivery
            ? { ...before, current: restored.current, runtime: restored.runtime } : before);
        });
        retryAttempt = 0;
        if (!cancelled) setReason(null);
      } catch (error) {
        if (!cancelled) {
          const failurePlan = planAutopilotProbeFailure(error, retryAttempt);
          if (failurePlan.action === "stop-legacy") {
            await releaseServerToLegacy(server, probeKey);
          } else if (failurePlan.action === "retry") {
            stopSpeaking();
            setReason("服务器状态暂时无法确认，已停止媒体并自动重试");
            retryAttempt += 1;
            timer = window.setTimeout(poll, failurePlan.delayMs);
          } else {
            stopSpeaking();
            setMode("blocked");
            setReason(failurePlan.retryExhausted
              ? "服务器状态连续无法确认，已停止自动重试，请研究者检查连接后重新配对"
              : blockedReason(error));
            setBlockedCalm(!failurePlan.retryExhausted && blockedIsCalm(error));
          }
        }
        return;
      }
      if (!cancelled) timer = window.setTimeout(poll, 1_500);
    };
    timer = window.setTimeout(poll, 1_500);
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [visibleMode, server, mediaAllowed, input.sessionId, probeAllowed, probeKey,
    releaseServerToLegacy]);

  useEffect(() => {
    const context = serverContextRef.current;
    if (!mediaAllowed || !context || !input.sessionId
        || !shouldBootstrapAutopilotRunner(context.runtime.phase)) return;
    let cancelled = false;
    let timer: number | null = null;
    let controller: PatientAutopilotController | null = null;
    void (async () => {
      // Bootstrap itself is fenced as a passive delivery operation. If the
      // gate flips again while /next or capture recovery is in flight, owner
      // release and the next controller both wait for this exact operation.
      const restored = await context.fence.runPassive(async () => {
        if (cancelled) return null;
        return loadServerRuntime(input.sessionId as string, context.delivery);
      });
      if (cancelled || restored === null) return;
      setServer((before) => before?.delivery === context.delivery
        ? { ...before, current: restored.current, runtime: restored.runtime } : before);
      if (!canStartAutopilotRunner(restored.runtime)) return;
      controller = new PatientAutopilotController({
        sessionId: input.sessionId as string,
        transport: autopilotHttpTransport,
        speech: new BrowserAutopilotSpeechExecutor(
          input.sessionId as string, browserAutopilotSpeechPorts),
        recording: new BrowserAutopilotRecordingExecutor(input.sessionId as string, {
          ownerGeneration: context.ownerGeneration,
          observe: observeCapturePhase,
        }),
        ackDelivery: context.delivery,
        initialRuntime: restored.runtime,
        waitForPresentation: (command, signal) => (
          assetGateRef.current as PatientAssetMediaGate
        ).waitFor(command, signal),
        onState: (runtime) => {
          if (!cancelled) setServer((before) => before?.delivery === context.delivery
            ? { ...before, current: runtime.command, runtime } : before);
        },
      });
      context.controller = controller;
      controllerRef.current = controller;
      const tick = async () => {
        // pre-start 期间页面隐藏之后本窗口彻底停：重新可见不自动恢复、不自动重开麦。
        if (lifecycleRef.current?.aborted) return;
        const runtime = await (controller as PatientAutopilotController).pollOnce();
        if (cancelled || lifecycleRef.current?.aborted) return;
        if (runtime.phase === "paused" || runtime.phase === "scope_completed") return;
        // 仍然走 setTimeout 而不是 queueMicrotask：卸载/暂停的取消路径靠这个
        // timer 句柄，换成微任务就没法在同一个清理里撤掉。
        timer = window.setTimeout(tick, autopilotNextTickDelayMs(runtime));
      };
      void tick();
    })().catch((error: unknown) => {
      if (cancelled) return;
      const failurePlan = planAutopilotProbeFailure(
        error, probeRetryAttempt.current);
      if (failurePlan.action === "stop-legacy") {
        void releaseServerToLegacy(context, probeKey);
      } else if (failurePlan.action === "retry") {
        stopSpeaking();
        setMode("probing");
        setResolvedProbeKey("");
        setReason("正在重新核对服务器控制权");
        probeRetryAttempt.current += 1;
        timer = window.setTimeout(
          () => setProbeEpoch((value) => value + 1),
          failurePlan.delayMs,
        );
      } else {
        probeRetryAttempt.current = 0;
        stopSpeaking();
        setMode("blocked");
        setReason(failurePlan.retryExhausted
          ? "服务器状态连续无法确认，已停止自动重试，请研究者检查连接后重新配对"
          : blockedReason(error));
        setBlockedCalm(!failurePlan.retryExhausted && blockedIsCalm(error));
      }
    });
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
      const active = controller;
      if (!active) return;
      if (controllerRef.current === active) controllerRef.current = null;
      if (context.controller === active) context.controller = null;
      // pagehide 已经赢下的时候复用它那条 shutdown：再调一次 stopAndWait 会先把
      // controller 的 stopped 置成 true，本该发出的 record_failed 就被吞了。
      context.fence.registerControllerShutdown(
        lifecycleRef.current?.aborted
          ? active.interruptRecordingForLifecycleAndWait()
          : active.stopAndWait());
    };
  }, [mediaAllowed, server?.delivery, input.sessionId, probeKey, releaseServerToLegacy,
    observeCapturePhase]);

  // Releasing server ownership requires more than projecting "paused" onto the
  // screen.  Wait until the exact local controller has closed Audio/MediaRecorder,
  // finished any durable capture staging and released its device lease, then let
  // the paired device append one command-bound drain proof. The account takeover
  // endpoint remains locked until this proof exists.
  useEffect(() => {
    const context = serverContextRef.current;
    if (!input.sessionPaused || input.sessionTerminal || visibleMode !== "server" || !context
        || !input.sessionId) return;
    let cancelled = false;
    let retryTimer: number | null = null;
    let retryAttempt = 0;
    let requestAbort: AbortController | null = null;

    const report = async () => {
      let provenCommandKey: string | null = null;
      const active = context.controller;
      let shutdown: Promise<void> | null = null;
      if (active) {
        context.controller = null;
        if (controllerRef.current === active) controllerRef.current = null;
        shutdown = active.stopAndWait();
      }
      try {
        await acknowledgeDrainAfterShutdown({
          fence: context.fence,
          shutdown,
          acknowledge: async () => {
            if (cancelled) return;
            requestAbort = new AbortController();
            const target = await fetchExactAutopilotDrainTarget(
              input.sessionId as string,
              requestAbort.signal,
              browserAutopilotMediaDependencies,
            );
            if (context.drainedCommandKey === target.command_key) return;
            provenCommandKey = target.command_key;
            const receipt = await acknowledgeExactAutopilotDrain(
              input.sessionId as string,
              target.command_key,
              requestAbort.signal,
              browserAutopilotMediaDependencies,
            );
            assertExactDrainTransition(target, receipt);
          },
        });
        if (cancelled) return;
        if (provenCommandKey) context.drainedCommandKey = provenCommandKey;
        setServer((before) => before?.delivery === context.delivery
          ? { ...before } : before);
      } catch (error) {
        if (cancelled) return;
        const disposition = classifyAutopilotDrainFailure(error);
        if (disposition === "released") {
          // A concurrent, already-audited takeover won; the next ownership probe
          // will unmount this server runner. Never revive or re-report old media.
          await releaseServerToLegacy(context, probeKey);
          return;
        }
        if (disposition === "repair-credential") {
          stopSpeaking();
          setMode("probing");
          setReason("老人端设备凭据已失效，请研究者重新配对");
          window.dispatchEvent(new Event(DEVICE_PAIR_REQUIRED_EVENT));
          return;
        }
        if (disposition === "blocked") {
          stopSpeaking();
          setMode("blocked");
          setBlockedCalm(false);
          setReason("收麦证据与服务器状态不一致，已停止重试并等待研究者处置");
          return;
        }
        const delay = drainRetryDelayMs(retryAttempt);
        retryAttempt += 1;
        retryTimer = window.setTimeout(() => { void report(); }, delay);
      }
    };
    void report();
    return () => {
      cancelled = true;
      requestAbort?.abort(new DOMException("收麦回执已取消", "AbortError"));
      if (retryTimer !== null) window.clearTimeout(retryTimer);
    };
  }, [input.sessionPaused, input.sessionTerminal, input.sessionId, server?.delivery, visibleMode,
    probeKey, releaseServerToLegacy]);

  return {
    mode: visibleMode,
    runtime: server?.runtime ?? null,
    current: server?.current ?? null,
    reason: visibleMode === "server" && !input.ttsOn
      ? "自动流程需要显式开启语音后才能继续"
      : reason,
    blockedCalm,
    assetReadiness,
    localCapturePhase,
    reportAssetReadiness,
    stopMediaNow: () => controllerRef.current?.stop(),
    stopForPatientPauseNow: () => controllerRef.current?.stopForPatientPause(),
    stopRecordingNow: () => controllerRef.current?.stopRecordingNow(),
  };
}
