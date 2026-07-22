import { api } from "../api.ts";
import { acquireAudioDeviceLease, type AudioDeviceLease } from "../audio/audioDeviceLease.ts";
import {
  advanceAudioOutbox,
  attachAudioCaptureReceipt,
  attachAutopilotStopReason,
  createAudioOutboxEntry,
  type AudioOutboxEntry,
} from "../audio/audioOutbox.ts";
import { blobStore } from "../audio/blobStore.ts";
import {
  sha256Blob,
  validateAudioCaptureReceiptAck,
  validateAudioUploadReceipt,
} from "../audio/audioUploadReceipt.ts";
import { Recorder } from "../audio/recorder.ts";
import {
  AUTOPILOT_MIME_TYPES,
  type AutopilotMimeType,
  type AutopilotStopReason,
  type NextCommandProjection,
} from "./autopilotProtocol.ts";
import type {
  AutopilotRecordingCapture,
  AutopilotRecordingExecutor,
} from "./autopilotController.ts";
import { AutopilotMediaError } from "./autopilotMediaError.ts";
import { authorizesMicrophoneStart } from "./recordingAuthorization.ts";
import { authorizeExactAutopilotRecording } from "./autopilotMediaTransport.ts";
import { browserAutopilotMediaDependencies } from "./autopilotBrowserMediaDependencies.ts";

type RecordCommand = Extract<NextCommandProjection, { kind: "record" }>;
type RecordStoppedFacts = Awaited<AutopilotRecordingCapture["stopped"]>;

function exactAutopilotMime(value: string | null): AutopilotMimeType {
  const normalized = (value ?? "").toLowerCase().replace(/\s+/g, "");
  const match = AUTOPILOT_MIME_TYPES.find((candidate) => candidate === normalized);
  if (!match) throw new Error("录音编码不在服务器自动驾驶白名单");
  return match;
}

function assertEntryMatchesCommand(entry: AudioOutboxEntry, command: RecordCommand): void {
  if (entry.rawAudioId !== command.payload.raw_audio_id
      || entry.sessionId.length === 0
      || entry.turnKey !== command.payload.turn_ref
      || entry.containsDirectIdentifier !== command.payload.contains_direct_identifier
      || entry.autopilotStopReason === undefined) {
    throw new Error("本机录音 outbox 与服务器当前录音命令不一致");
  }
}

async function finishExactCapture(
  command: RecordCommand,
  entryValue: AudioOutboxEntry,
  blob: Blob,
): Promise<{ entry: AudioOutboxEntry; facts: RecordStoppedFacts }> {
  let entry = entryValue;
  assertEntryMatchesCommand(entry, command);
  if (blob.size !== entry.blobBytes || (blob.type || "audio/webm") !== entry.mimeType) {
    throw new Error("本机录音字节与 outbox 元数据不一致");
  }
  const checksum = await sha256Blob(blob);

  if (entry.phase === "captured") {
    // POST is an idempotent confirmation of the server-preallocated row. It
    // carries the command-issued raw ID unchanged and never generates a second
    // client ID. A conflicting registration remains a hard failure.
    const registration = await api.createAudio({
      raw_audio_id: command.payload.raw_audio_id,
      session_id: entry.sessionId,
      turn_key: command.payload.turn_ref,
      contains_direct_identifier: command.payload.contains_direct_identifier,
    });
    if (registration.raw_audio_id !== command.payload.raw_audio_id
        || registration.registered !== true) {
      throw new Error("服务器预分配录音登记收据不一致");
    }
    entry = advanceAudioOutbox(entry, "registered");
    await blobStore.putOutbox(entry);
  }

  if (entry.phase !== "uploaded") {
    // The exact PUT response proves that the same preallocated row accepted
    // these bytes.
    const uploadReceipt = await api.uploadAudioBlob(
      command.payload.raw_audio_id,
      blob,
      entry.sessionId,
    );
    validateAudioUploadReceipt(uploadReceipt, {
      rawAudioId: entry.rawAudioId,
      bytes: blob.size,
      checksum,
    });
    entry = advanceAudioOutbox(entry, "uploaded", { checksum });
    await blobStore.putOutbox(entry);
  } else if (entry.checksum !== checksum) {
    throw new Error("已上传录音的本机 checksum 与字节不一致");
  }

  if (entry.captureReceiptServerSeq === undefined) {
    const response = await api.putAudioSaved({
      rawAudioId: entry.rawAudioId,
      durationSeconds: entry.durationSeconds,
      byteCount: entry.blobBytes,
      checksum,
      turnKey: entry.turnKey,
      sessionId: entry.sessionId,
      containsDirectIdentifier: entry.containsDirectIdentifier,
    });
    const receipt = validateAudioCaptureReceiptAck(response, entry.rawAudioId);
    entry = attachAudioCaptureReceipt(entry, receipt.serverSeq);
    await blobStore.putOutbox(entry);
  }

  const receiptServerSeq = entry.captureReceiptServerSeq;
  const stopReason = entry.autopilotStopReason;
  if (receiptServerSeq === undefined || stopReason === undefined || entry.checksum === undefined) {
    throw new Error("录音缺少服务器 audioSaved 采集收据");
  }
  return {
    entry,
    facts: {
      stop_reason: stopReason,
      raw_audio_id: entry.rawAudioId,
      receipt_server_seq: receiptServerSeq,
      checksum: entry.checksum,
      byte_count: entry.blobBytes,
      duration_seconds: entry.durationSeconds,
    },
  };
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
} {
  let resolvePromise!: (value: T) => void;
  let rejectPromise!: (error: unknown) => void;
  const promise = new Promise<T>((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  return { promise, resolve: resolvePromise, reject: rejectPromise };
}

class BrowserAutopilotCapture implements AutopilotRecordingCapture {
  readonly started: AutopilotRecordingCapture["started"];
  readonly stopped: AutopilotRecordingCapture["stopped"];
  readonly closed: Promise<void>;
  private readonly startedDeferred = deferred<Awaited<AutopilotRecordingCapture["started"]>>();
  private readonly stoppedDeferred = deferred<RecordStoppedFacts>();
  private readonly closedDeferred = deferred<void>();
  private readonly abortController = new AbortController();
  private readonly recorder = new Recorder();
  private lease: AudioDeviceLease | null = null;
  private maxTimer: number | null = null;
  private requestedStop: AutopilotStopReason | null = null;
  private stopResolve: ((reason: AutopilotStopReason) => void) | null = null;
  private didStart = false;
  private settled = false;
  private readonly sessionId: string;
  private readonly command: RecordCommand;

  constructor(sessionId: string, command: RecordCommand) {
    this.sessionId = sessionId;
    this.command = command;
    this.started = this.startedDeferred.promise;
    this.stopped = this.stoppedDeferred.promise;
    this.closed = this.closedDeferred.promise;
    void this.run();
  }

  requestStop(reason: "user_done" = "user_done"): void {
    this.signalStop(reason);
  }

  cancel(): void {
    if (!this.didStart) {
      this.abortController.abort(new DOMException("录音命令已取消", "AbortError"));
      this.recorder.cancelPendingStart();
      this.recorder.discardActive();
      return;
    }
    // Once a real microphone start has occurred, cancellation still closes and
    // stages the captured bytes. It never leaves a hot mic or invents stopped.
    if (this.requestedStop !== null && (this.recorder.active || this.recorder.stopping)) {
      // A stop/onstop that exceeded the controller deadline must be physically
      // torn down as well; Recorder rejects the pending stop so the Web Lock is
      // released in this capture's finally path.
      this.recorder.discardActive();
    } else {
      this.signalStop("stream_end");
    }
  }

  private signalStop(reason: AutopilotStopReason): void {
    if (this.requestedStop) return;
    this.requestedStop = reason;
    this.stopResolve?.(reason);
  }

  private waitForStop(): Promise<AutopilotStopReason> {
    if (this.requestedStop) return Promise.resolve(this.requestedStop);
    return new Promise((resolve) => { this.stopResolve = resolve; });
  }

  private async run(): Promise<void> {
    let phase: "starting" | "recording" | "persistence" = "starting";
    try {
      this.lease = await acquireAudioDeviceLease(undefined, this.abortController.signal);
      const snapshot = await blobStore.recoverySnapshot();
      if (snapshot.invalidBlobKeyCount > 0 || snapshot.legacyOrphans.length > 0
          || snapshot.entries.length > 0) {
        throw new Error("本机存在待恢复录音，禁止覆盖开新麦克风");
      }
      const authorization = await authorizeExactAutopilotRecording(
        this.sessionId,
        this.command.command_key,
        this.abortController.signal,
        browserAutopilotMediaDependencies,
      );
      if (!authorizesMicrophoneStart(authorization)) {
        throw new Error("当前场次未授权开启麦克风");
      }
      const started = await this.recorder.start();
      if (!started || this.abortController.signal.aborted) {
        this.recorder.discardActive();
        throw new Error("麦克风未真实进入 recording 状态");
      }
      // Permission prompts can outlive the command that authorized opening.
      // Revalidate after the stream really exists; a pause/takeover during the
      // prompt closes tracks before record_started is exposed or ACKed.
      const postPermissionAuthorization = await authorizeExactAutopilotRecording(
        this.sessionId,
        this.command.command_key,
        this.abortController.signal,
        browserAutopilotMediaDependencies,
      );
      if (!authorizesMicrophoneStart(postPermissionAuthorization)
          || this.abortController.signal.aborted) {
        this.recorder.discardActive();
        throw new Error("麦克风权限返回后自动驾驶命令已失效");
      }
      const mimeType = exactAutopilotMime(this.recorder.mimeType);
      this.didStart = true;
      phase = "recording";
      this.startedDeferred.resolve({ mime_type: mimeType });
      this.maxTimer = window.setTimeout(
        () => this.signalStop("max_duration"),
        this.command.payload.max_duration_seconds * 1_000,
      );

      const stopReason = await this.waitForStop();
      if (this.maxTimer !== null) window.clearTimeout(this.maxTimer);
      this.maxTimer = null;
      const recording = await this.recorder.stop();
      if (recording.blob.size <= 0) throw new Error("录音没有产生可持久化字节");
      phase = "persistence";
      let entry = createAudioOutboxEntry({
        rawAudioId: this.command.payload.raw_audio_id,
        sessionId: this.sessionId,
        turnKey: this.command.payload.turn_ref,
        containsDirectIdentifier: this.command.payload.contains_direct_identifier,
        durationSeconds: recording.durationSeconds,
        blob: recording.blob,
      });
      entry = attachAutopilotStopReason(entry, stopReason);
      await blobStore.stageOutbox(entry, recording.blob);
      const completed = await finishExactCapture(this.command, entry, recording.blob);
      this.settled = true;
      this.stoppedDeferred.resolve(completed.facts);
    } catch (error) {
      const mapped = error instanceof AutopilotMediaError ? error
        : phase === "starting"
          ? new AutopilotMediaError(
            error instanceof DOMException
              && (error.name === "NotAllowedError" || error.name === "SecurityError")
              ? "microphone_denied"
              : error instanceof DOMException && error.name === "NotFoundError"
                ? "microphone_unavailable" : "recording_start_failed",
            "麦克风未能安全启动",
            { cause: error },
          )
          : phase === "persistence"
            ? new AutopilotMediaError(
              "recording_upload_failed",
              "录音上传或采集收据未完成",
              { cause: error },
            )
            : new AutopilotMediaError(
              "recording_runtime_failed",
              "录音过程未正常结束",
              { cause: error },
            );
      if (!this.didStart) this.startedDeferred.reject(mapped);
      this.stoppedDeferred.reject(mapped);
      if (this.recorder.active) this.recorder.discardActive();
    } finally {
      if (this.maxTimer !== null) window.clearTimeout(this.maxTimer);
      this.maxTimer = null;
      this.recorder.dispose();
      const lease = this.lease;
      this.lease = null;
      if (lease) {
        lease.release();
        await lease.released;
      }
      if (!this.settled && this.didStart && this.requestedStop === null) {
        // Defensive only; all real starts should either persist or reject stopped.
        this.signalStop("stream_end");
      }
      this.closedDeferred.resolve(undefined);
    }
  }
}

export class BrowserAutopilotRecordingExecutor implements AutopilotRecordingExecutor {
  private readonly sessionId: string;

  constructor(sessionId: string) { this.sessionId = sessionId; }

  start(command: RecordCommand): AutopilotRecordingCapture {
    if (command.state !== "pending") throw new Error("仅 pending 录音命令可开麦");
    return new BrowserAutopilotCapture(this.sessionId, command);
  }
}

/**
 * Refresh recovery for a physically stopped capture. It can finish upload and
 * audioSaved only when the server's exact started command matches the durable
 * outbox; otherwise it refuses to reopen or synthesize a recording.
 */
export async function recoverAutopilotRecording(
  sessionId: string,
  command: RecordCommand,
): Promise<RecordStoppedFacts | null> {
  if (command.state !== "started") return null;
  const lease = await acquireAudioDeviceLease();
  try {
    const snapshot = await blobStore.recoverySnapshot();
    if (snapshot.invalidBlobKeyCount > 0 || snapshot.legacyOrphans.length > 0) {
      throw new Error("本机录音恢复存储状态非法");
    }
    if (snapshot.entries.length === 0) return null;
    if (snapshot.entries.length !== 1) {
      throw new Error("自动驾驶恢复期存在多份待处置录音");
    }
    const entry = snapshot.entries[0];
    assertEntryMatchesCommand(entry, command);
    if (entry.sessionId !== sessionId) throw new Error("恢复录音属于其他场次");
    const blob = await blobStore.get(entry.rawAudioId);
    if (!blob) throw new Error("恢复录音缺少本机原始字节");
    return (await finishExactCapture(command, entry, blob)).facts;
  } finally {
    lease.release();
    await lease.released;
  }
}
