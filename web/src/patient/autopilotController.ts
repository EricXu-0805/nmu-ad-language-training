import type { DeviceCapabilityRecord } from "../security/deviceCapability.ts";
import {
  buildAutopilotAck,
  type AutopilotAck,
  type AutopilotErrorCode,
  type AutopilotMimeType,
  type AutopilotStopReason,
  type NextCommandProjection,
} from "./autopilotProtocol.ts";
import {
  autopilotRuntimeReducer,
  canOpenAutopilotMicrophone,
  canPlayAutopilotSpeech,
  restoreAutopilotRuntime,
  type AutopilotRuntimeState,
} from "./autopilotRuntime.ts";
import { autopilotMediaErrorCode } from "./autopilotMediaError.ts";

type TtsCommand = Extract<NextCommandProjection, { kind: "tts" }>;
type RecordCommand = Extract<NextCommandProjection, { kind: "record" }>;

export interface AutopilotTransport {
  next(sessionId: string): Promise<unknown | null>;
  ack(sessionId: string, commandKey: string, ack: AutopilotAck): Promise<unknown>;
}

export interface AutopilotSpeechPlayback {
  /** Resolves only after the browser/media element has really begun playback. */
  started: Promise<{ media_duration_ms?: number }>;
  /** Resolves only after the same playback reaches its real ended event. */
  ended: Promise<{ media_duration_ms?: number }>;
  /** Resolves only after playback/fetch teardown can no longer emit audio. */
  closed: Promise<void>;
  cancel(): void;
}

export interface AutopilotSpeechExecutor {
  start(command: TtsCommand): AutopilotSpeechPlayback;
}

export interface AutopilotRecordingCapture {
  /** Resolves only after MediaRecorder is actually recording. */
  started: Promise<{
    mime_type: AutopilotMimeType;
    sample_rate_hz?: number;
    channels?: 1 | 2;
  }>;
  /**
   * Resolves only after durable upload + audioSaved returned the exact receipt
   * tuple. A recorder adapter must never manufacture this tuple locally.
   */
  stopped: Promise<{
    stop_reason: AutopilotStopReason;
    raw_audio_id: string;
    receipt_server_seq: number;
    checksum: string;
    byte_count: number;
    duration_seconds: number;
  }>;
  /**
   * Resolves only after pending getUserMedia, MediaRecorder, persistence and the
   * device lease have all settled. A timeout is not physical microphone proof.
   */
  closed: Promise<void>;
  /** Optional patient "我说好了" boundary; it must still persist the same capture. */
  requestStop?(reason?: "user_done"): void;
  cancel(): void;
}

export interface AutopilotRecordingExecutor {
  start(command: RecordCommand): AutopilotRecordingCapture;
}

/** Production implementations durably stage the exact ACK before HTTP. */
export interface AutopilotAckDelivery {
  readonly initialDeviceEventSeq: number;
  send(
    command: NextCommandProjection,
    lastDeviceEventSeq: number,
    facts: Parameters<typeof buildAutopilotAck>[3],
  ): Promise<AutopilotAck>;
}

export interface PatientAutopilotControllerOptions {
  sessionId: string;
  transport: AutopilotTransport;
  speech: AutopilotSpeechExecutor;
  recording: AutopilotRecordingExecutor;
  ackDelivery?: AutopilotAckDelivery;
  initialCommand?: unknown | null;
  /**
   * Full restored runtime seed (e.g. from restoreAutopilotRuntimeAfterRefresh),
   * mutually exclusive with initialCommand. Rebuilding from just a command
   * loses phase/pause_reason — a paused runtime with a null command would
   * silently become waiting_command if only its command were passed through.
   * Its last_device_event_seq must match the controller's own seq origin.
   */
  initialRuntime?: AutopilotRuntimeState;
  initialDeviceEventSeq?: number;
  idempotencyKey?: (
    command: NextCommandProjection,
    ackType: AutopilotAck["ack_type"],
    deviceEventSeq: number,
  ) => string;
  onDeviceEventSeq?: (deviceEventSeq: number) => void;
  onState?: (state: AutopilotRuntimeState) => void;
  /** Resolves only after the exact command's current stimulus is decoded. */
  waitForPresentation?: (
    command: NextCommandProjection,
    signal: AbortSignal,
  ) => Promise<void>;
}

type MediaOutcome<T> = { ok: true; value: T } | { ok: false; error: unknown };

class MediaDeadlineError extends Error {
  constructor() {
    super("媒体命令超时");
    this.name = "MediaDeadlineError";
  }
}

function observeMedia<T>(promise: Promise<T>, timeoutMs: number): Promise<MediaOutcome<T>> {
  // Attach both continuations immediately. Some browser media APIs can emit an
  // ended/error event before the started ACK round-trip completes; that must not
  // become an unhandled rejection while we wait for the server revision.
  return new Promise((resolve) => {
    const timeout = globalThis.setTimeout(
      () => resolve({ ok: false, error: new MediaDeadlineError() }), timeoutMs);
    promise.then(
      (value) => {
        globalThis.clearTimeout(timeout);
        resolve({ ok: true, value });
      },
      (error: unknown) => {
        globalThis.clearTimeout(timeout);
        resolve({ ok: false, error });
      },
    );
  });
}

function defaultIdempotencyKey(
  _command: NextCommandProjection,
  ackType: AutopilotAck["ack_type"],
  deviceEventSeq: number,
): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (!uuid) throw new Error("当前浏览器不能安全生成自动驾驶回执标识");
  return `ack:${ackType}:${deviceEventSeq}:${uuid}`;
}

/** Only the exact, unexpired active-session capability may activate polling. */
export function deviceCapabilityAllowsAutopilot(
  capability: DeviceCapabilityRecord | null,
  sessionId: string,
  nowMs = Date.now(),
): capability is DeviceCapabilityRecord {
  return capability !== null
    && capability.sessionId === sessionId
    && Number.isFinite(Date.parse(capability.expiresAt))
    && Date.parse(capability.expiresAt) > nowMs;
}

/**
 * One paired-device media runner. It deliberately has no built-in microphone
 * implementation: the production adapter must first reuse the existing
 * IndexedDB/outbox/receipt chain. Tests inject inert media doubles.
 */
export class PatientAutopilotController {
  private stateValue: AutopilotRuntimeState;
  private readonly sessionId: string;
  private readonly transport: AutopilotTransport;
  private readonly speech: AutopilotSpeechExecutor;
  private readonly recording: AutopilotRecordingExecutor;
  private readonly ackDelivery?: AutopilotAckDelivery;
  private readonly makeIdempotencyKey: NonNullable<PatientAutopilotControllerOptions["idempotencyKey"]>;
  private readonly onDeviceEventSeq?: (deviceEventSeq: number) => void;
  private readonly onState?: (state: AutopilotRuntimeState) => void;
  private readonly waitForPresentation?: NonNullable<
    PatientAutopilotControllerOptions["waitForPresentation"]
  >;
  private readonly presentationAbort = new AbortController();
  private inFlight: Promise<AutopilotRuntimeState> | null = null;
  private activeMedia: { cancel(): void; closed: Promise<void> } | null = null;
  private stopped = false;

  constructor(options: PatientAutopilotControllerOptions) {
    this.sessionId = options.sessionId;
    this.transport = options.transport;
    this.speech = options.speech;
    this.recording = options.recording;
    this.ackDelivery = options.ackDelivery;
    this.makeIdempotencyKey = options.idempotencyKey ?? defaultIdempotencyKey;
    this.onDeviceEventSeq = options.onDeviceEventSeq;
    this.onState = options.onState;
    this.waitForPresentation = options.waitForPresentation;
    const initialSeq = options.ackDelivery?.initialDeviceEventSeq
      ?? options.initialDeviceEventSeq ?? 0;
    if (options.ackDelivery && options.initialDeviceEventSeq !== undefined
        && options.initialDeviceEventSeq !== initialSeq) {
      throw new Error("ACK delivery 与控制器序号起点不一致");
    }
    if (options.initialRuntime && options.initialCommand !== undefined) {
      throw new Error("initialRuntime 与 initialCommand 不得同时提供");
    }
    if (options.initialRuntime) {
      if (options.initialRuntime.last_device_event_seq !== initialSeq) {
        throw new Error("initialRuntime 与控制器序号起点不一致");
      }
      this.stateValue = options.initialRuntime;
    } else {
      this.stateValue = restoreAutopilotRuntime(
        options.initialCommand ?? null,
        initialSeq,
      );
    }
    this.emitState();
  }

  get state(): AutopilotRuntimeState { return this.stateValue; }

  stop(): void {
    this.stopped = true;
    this.presentationAbort.abort(new DOMException("自动驾驶媒体已停止", "AbortError"));
    this.activeMedia?.cancel();
    this.activeMedia = null;
    if (this.stateValue.phase !== "paused"
        && this.stateValue.phase !== "scope_completed") {
      this.stateValue = autopilotRuntimeReducer(this.stateValue, {
        type: "technical_failure",
      });
    }
    this.emitState();
  }

  /**
   * Stop media synchronously, then wait until the serialized runner has left
   * every ACK/media continuation. The cross-tab owner lease is released only
   * after this promise settles, so the next tab cannot overlap speech or ACKs.
   */
  async stopAndWait(): Promise<void> {
    const running = this.inFlight;
    const media = this.activeMedia;
    this.stop();
    const pending: Promise<unknown>[] = [];
    if (running) pending.push(running);
    if (media) pending.push(media.closed);
    if (pending.length > 0) await Promise.allSettled(pending);
  }

  stopRecordingNow(): void {
    if (this.stateValue.phase !== "recording") return;
    const capture = this.activeMedia as AutopilotRecordingCapture | null;
    capture?.requestStop?.("user_done");
  }

  private emitState(): void { this.onState?.(this.stateValue); }

  /** Serialize timer/visibility/manual refresh calls into one media owner. */
  pollOnce(): Promise<AutopilotRuntimeState> {
    if (this.inFlight) return this.inFlight;
    this.inFlight = this.runOnce().finally(() => { this.inFlight = null; });
    return this.inFlight;
  }

  private async runOnce(): Promise<AutopilotRuntimeState> {
    if (this.stopped || this.stateValue.phase === "paused"
        || this.stateValue.phase === "scope_completed") return this.stateValue;
    try {
      await this.refreshCommand();
      // refreshCommand mutates the property through a method; widen the prior
      // control-flow narrowing before examining the newly reduced state.
      if ((this.stateValue as AutopilotRuntimeState).phase === "paused") return this.stateValue;
      const canPlay = canPlayAutopilotSpeech(this.stateValue);
      const canRecord = canOpenAutopilotMicrophone(this.stateValue);
      if ((canPlay || canRecord) && this.waitForPresentation) {
        const command = this.stateValue.command;
        if (!command) throw new Error("媒体命令缺少当前题目投影");
        await this.waitForPresentation(command, this.presentationAbort.signal);
        if (this.stopped) return this.stateValue;
        if (this.stateValue.command?.command_key !== command.command_key) {
          throw new Error("图片门禁与当前媒体命令不一致");
        }
      }
      if (canPlay) await this.runSpeech();
      else if (canRecord) await this.runRecording();
      return this.stateValue;
    } catch {
      this.activeMedia?.cancel();
      this.activeMedia = null;
      // Preserve the reducer's more specific fail-closed reason (protocol,
      // TTS, recording). Only an unclassified transport/runtime exception is
      // promoted to technical_failure.
      const caughtPhase = (this.stateValue as AutopilotRuntimeState).phase;
      if (caughtPhase !== "paused" && caughtPhase !== "scope_completed") {
        this.stateValue = autopilotRuntimeReducer(this.stateValue, {
          type: "technical_failure",
        });
      }
      this.emitState();
      return this.stateValue;
    }
  }

  private async refreshCommand(): Promise<void> {
    if (this.stopped) return;
    const current = await this.transport.next(this.sessionId);
    this.stateValue = autopilotRuntimeReducer(this.stateValue, {
      type: "server_command",
      command: current,
    });
    this.emitState();
  }

  private currentCommand<K extends NextCommandProjection["kind"]>(
    kind: K,
  ): Extract<NextCommandProjection, { kind: K }> {
    const command = this.stateValue.command;
    if (!command || command.kind !== kind) throw new Error("当前媒体命令已失效");
    return command as Extract<NextCommandProjection, { kind: K }>;
  }

  private async sendAck(command: NextCommandProjection, facts: Parameters<typeof buildAutopilotAck>[3]): Promise<void> {
    if (this.stopped) throw new Error("自动驾驶已停止");
    const nextSeq = this.stateValue.last_device_event_seq + 1;
    const ack = this.ackDelivery
      ? await this.ackDelivery.send(command, this.stateValue.last_device_event_seq, facts)
      : buildAutopilotAck(
        command,
        this.stateValue.last_device_event_seq,
        this.makeIdempotencyKey(command, facts.ack_type, nextSeq),
        facts,
      );
    // Local state advances only after the server has durably accepted the ACK.
    if (!this.ackDelivery) await this.transport.ack(this.sessionId, command.command_key, ack);
    this.stateValue = autopilotRuntimeReducer(this.stateValue, {
      type: "device_ack",
      ack,
    });
    const expectedFailurePause = facts.ack_type === "tts_failed"
      ? this.stateValue.pause_reason === "tts_failed"
      : facts.ack_type === "record_failed"
        ? this.stateValue.pause_reason === "record_failed"
        : false;
    if (this.stateValue.phase === "paused" && !expectedFailurePause) {
      throw new Error("自动驾驶回执未通过本地状态校验");
    }
    this.onDeviceEventSeq?.(this.stateValue.last_device_event_seq);
    this.emitState();
  }

  private async mediaFailure(
    kind: "tts" | "record",
    errorCode: AutopilotErrorCode,
  ): Promise<void> {
    const command = this.currentCommand(kind);
    await this.sendAck(command, {
      ack_type: kind === "tts" ? "tts_failed" : "record_failed",
      error_code: errorCode,
    });
  }

  private async runSpeech(): Promise<void> {
    const initial = this.currentCommand("tts");
    let playback: AutopilotSpeechPlayback;
    try { playback = this.speech.start(initial); }
    catch {
      await this.mediaFailure("tts", "audio_playback_failed");
      return;
    }
    this.activeMedia = playback;
    const startedOutcome = observeMedia(playback.started, 15_000);
    const endedOutcome = observeMedia(playback.ended, 120_000);
    try {
      const observedStart = await startedOutcome;
      if (!observedStart.ok) {
        playback.cancel();
        await playback.closed;
        await this.mediaFailure("tts", observedStart.error instanceof MediaDeadlineError
          ? "device_command_timeout" : "audio_playback_failed");
        return;
      }
      await this.sendAck(initial, { ack_type: "tts_started", ...observedStart.value });

      // The terminal ACK must echo the server's started revision, never the old
      // pending revision cached before playback began.
      await this.refreshCommand();
      const startedCommand = this.currentCommand("tts");
      if (startedCommand.state !== "started") throw new Error("TTS started revision 未确认");

      const observedEnd = await endedOutcome;
      if (!observedEnd.ok) {
        playback.cancel();
        await playback.closed;
        await this.mediaFailure("tts", observedEnd.error instanceof MediaDeadlineError
          ? "device_command_timeout" : "audio_playback_failed");
        return;
      }
      await playback.closed;
      await this.sendAck(startedCommand, {
        ack_type: "tts_ended",
        media_ended: true,
        ...observedEnd.value,
      });
      await this.refreshCommand();
    } finally {
      await playback.closed;
      if (this.activeMedia === playback) this.activeMedia = null;
    }
  }

  private async runRecording(): Promise<void> {
    // This method is the sole microphone admission point. The reducer guard in
    // runOnce proves an exact, pending server record command exists first.
    const initial = this.currentCommand("record");
    let capture: AutopilotRecordingCapture;
    try { capture = this.recording.start(initial); }
    catch (error) {
      await this.mediaFailure("record", autopilotMediaErrorCode(error, "recording_start_failed"));
      return;
    }
    this.activeMedia = capture;
    const startedOutcome = observeMedia(capture.started, 20_000);
    const stoppedOutcome = observeMedia(
      capture.stopped,
      (initial.payload.max_duration_seconds * 1_000) + 90_000,
    );
    try {
      const observedStart = await startedOutcome;
      if (!observedStart.ok) {
        capture.cancel();
        await capture.closed;
        await this.mediaFailure("record", observedStart.error instanceof MediaDeadlineError
          ? "device_command_timeout"
          : autopilotMediaErrorCode(observedStart.error, "recording_start_failed"));
        return;
      }
      await this.sendAck(initial, { ack_type: "record_started", ...observedStart.value });
      await this.refreshCommand();
      const startedCommand = this.currentCommand("record");
      if (startedCommand.state !== "started") throw new Error("录音 started revision 未确认");

      const observedStop = await stoppedOutcome;
      if (!observedStop.ok) {
        capture.cancel();
        await capture.closed;
        await this.mediaFailure("record", observedStop.error instanceof MediaDeadlineError
          ? "device_command_timeout"
          : autopilotMediaErrorCode(observedStop.error, "recording_runtime_failed"));
        return;
      }
      await capture.closed;
      await this.sendAck(startedCommand, { ack_type: "record_stopped", ...observedStop.value });
      await this.refreshCommand();
    } finally {
      await capture.closed;
      if (this.activeMedia === capture) this.activeMedia = null;
    }
  }
}
