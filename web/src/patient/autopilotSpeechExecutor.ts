import type {
  AutopilotSpeechExecutor,
  AutopilotSpeechPlayback,
} from "./autopilotController.ts";
import { AutopilotMediaError } from "./autopilotMediaError.ts";
import type { NextCommandProjection } from "./autopilotProtocol.ts";

import type { BargeInHandle, BargeInObservation } from "./autopilotBrowserBargeIn.ts";

type TtsCommand = Extract<NextCommandProjection, { kind: "tts" }>;

export interface AutopilotSpeechBrowserPorts {
  enabled(): boolean;
  stopSpeaking(): void;
  fetchTts(sessionId: string, command: TtsCommand, signal: AbortSignal): Promise<Blob | null>;
  createAudio(): HTMLAudioElement;
  createObjectUrl(blob: Blob): string;
  revokeObjectUrl(url: string): void;
  now(): number;
  startBargeIn?(input: {
    sessionId: string; command: TtsCommand; audio: HTMLAudioElement;
    onConfirmed(): boolean; onPlaybackFailed(error: unknown): void;
    observe?(observation: BargeInObservation): void;
  }): BargeInHandle;
  observeBargeIn?(observation: BargeInObservation): void;
  /**
   * play() 被浏览器以「无用户手势」拒绝时的第二条路:等下一次点屏,在那个手势
   * 处理器里**同步**再 play() 一次并把它的 promise 交回来。signal 中止即拒绝。
   */
  playOnNextGesture?(audio: HTMLAudioElement, signal: AbortSignal): Promise<void>;
  /** 让老人端把「点一下，接着听」亮出来/收起来。 */
  announceGestureNeeded?(needed: boolean): void;
}

function isGestureRequired(error: unknown): boolean {
  return error instanceof DOMException && error.name === "NotAllowedError";
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

class BrowserAutopilotSpeechPlayback implements AutopilotSpeechPlayback {
  readonly started: AutopilotSpeechPlayback["started"];
  readonly ended: AutopilotSpeechPlayback["ended"];
  readonly closed: Promise<void>;
  private readonly startedDeferred = deferred<{ media_duration_ms?: number }>();
  private readonly endedDeferred = deferred<Awaited<AutopilotSpeechPlayback["ended"]>>();
  private readonly closedDeferred = deferred<void>();
  private readonly abortController = new AbortController();
  private readonly audio: HTMLAudioElement;
  private objectUrl: string | null = null;
  private startAtMs: number | null = null;
  private terminal = false;
  private bargeIn: BargeInHandle | null = null;
  private bargeInArmed = false;
  private readonly sessionId: string;
  private readonly command: TtsCommand;
  private readonly ports: AutopilotSpeechBrowserPorts;

  constructor(
    sessionId: string,
    command: TtsCommand,
    ports: AutopilotSpeechBrowserPorts,
  ) {
    this.sessionId = sessionId;
    this.command = command;
    this.ports = ports;
    this.audio = ports.createAudio();
    this.started = this.startedDeferred.promise;
    this.ended = this.endedDeferred.promise;
    this.closed = this.closedDeferred.promise;
    void this.run();
  }

  cancel(): void {
    if (this.terminal) return;
    this.terminal = true;
    this.abortController.abort(new DOMException("语音播放已取消", "AbortError"));
    this.audio.pause();
    this.cleanupUrl();
    const error = new DOMException("语音播放已取消", "AbortError");
    this.startedDeferred.reject(error);
    this.endedDeferred.reject(error);
    this.finishClose();
  }

  armBargeIn(command: TtsCommand): void {
    if (this.terminal || this.bargeInArmed || !this.ports.startBargeIn
        || this.startAtMs === null || command.state !== "started"
        || command.command_key !== this.command.command_key
        || command.control_generation !== this.command.control_generation
        || command.runner_generation !== this.command.runner_generation
        || (command.payload.purpose !== "question" && command.payload.purpose !== "cue")) return;
    this.bargeInArmed = true;
    try {
      this.bargeIn = this.ports.startBargeIn({
        sessionId: this.sessionId, command, audio: this.audio,
        onConfirmed: () => this.interruptForAnswer("voice_activity"),
        onPlaybackFailed: (error) => this.fail(new AutopilotMediaError(
          "audio_playback_failed", "提前回答检测后无法继续朗读",
          { cause: error, failureStage: "play_rejected" })),
        observe: this.ports.observeBargeIn,
      });
    } catch { /* Optional enhancement: the explicit answer button still works. */ }
  }

  private finishClose(): void {
    const monitor = this.bargeIn;
    this.bargeIn = null;
    if (monitor) {
      monitor.close();
      void monitor.closed.then(() => this.closedDeferred.resolve(undefined));
    } else this.closedDeferred.resolve(undefined);
  }

  answerNow(): boolean { return this.interruptForAnswer("answer_now"); }

  private interruptForAnswer(reason: "answer_now" | "voice_activity"): boolean {
    if (this.terminal || this.audio.ended === true || this.startAtMs === null
        || !Number.isFinite(this.audio.currentTime) || this.audio.currentTime < 0
        || (this.command.payload.purpose !== "question" && this.command.payload.purpose !== "cue")) return false;
    // Settle only after the actual element is paused. This is an interruption,
    // never a fabricated ended event, and it does not open any microphone.
    try { this.audio.pause(); } catch { return false; }
    if (!this.audio.paused) return false;
    this.terminal = true;
    const elapsed = Math.round(this.audio.currentTime * 1_000);
    this.cleanupUrl();
    this.endedDeferred.resolve({ interrupted: true, media_duration_ms: elapsed,
      ...(reason === "voice_activity" ? { interrupt_reason: reason } : {}) });
    this.finishClose();
    return true;
  }

  private cleanupUrl(): void {
    if (!this.objectUrl) return;
    this.ports.revokeObjectUrl(this.objectUrl);
    this.objectUrl = null;
  }

  private fail(error: unknown): void {
    if (this.terminal) return;
    this.terminal = true;
    this.audio.pause();
    this.cleanupUrl();
    this.startedDeferred.reject(error);
    this.endedDeferred.reject(error);
    this.finishClose();
  }

  private async run(): Promise<void> {
    try {
      if (!this.ports.enabled()) {
        throw new AutopilotMediaError(
          "audio_playback_failed", "自动驾驶语音门禁未显式开启",
          { failureStage: "executor_start_failed" });
      }
      this.ports.stopSpeaking();
      const blob = await this.ports.fetchTts(
        this.sessionId, this.command, this.abortController.signal);
      if (blob === null) {
        throw new AutopilotMediaError(
          "audio_playback_failed", "TTS 服务当前未产生音频",
          { failureStage: "fetch_failed" });
      }
      if (this.terminal) return;
      this.objectUrl = this.ports.createObjectUrl(blob);
      this.audio.src = this.objectUrl;
      this.audio.onplaying = () => {
        if (this.terminal || this.startAtMs !== null) return;
        this.startAtMs = this.ports.now();
        const duration = Number.isFinite(this.audio.duration) && this.audio.duration >= 0
          ? Math.round(this.audio.duration * 1_000) : undefined;
        this.startedDeferred.resolve(duration === undefined ? {} : { media_duration_ms: duration });
      };
      this.audio.onended = () => {
        if (this.terminal || this.startAtMs === null) return;
        const elapsed = Math.max(0, Math.round(this.ports.now() - this.startAtMs));
        this.terminal = true;
        this.cleanupUrl();
        this.endedDeferred.resolve({ media_duration_ms: elapsed });
        this.finishClose();
      };
      this.audio.onerror = () => this.fail(new AutopilotMediaError(
        "audio_playback_failed", "TTS 音频解码或播放失败",
        { failureStage: "media_decode_error" }));
      try {
        await this.audio.play();
      } catch (error) {
        // NotAllowedError / NotSupportedError 等 play() 拒绝在这里统一定阶段。
        if (this.terminal || !isGestureRequired(error) || !this.ports.playOnNextGesture) {
          throw new AutopilotMediaError(
            "audio_playback_failed", "浏览器拒绝开始播放合成语音",
            { cause: error, failureStage: "play_rejected" });
        }
        // 浏览器要手势才肯出声(生产三台设备实证):这不是设备坏了,把整场
        // 安全暂停等于让老人为浏览器策略背锅。亮出「点一下，接着听」,在那一下
        // 里重放;取消/超时(控制器 20 秒起播期限)照旧走 AbortError 收口。
        this.ports.announceGestureNeeded?.(true);
        try {
          await this.ports.playOnNextGesture(this.audio, this.abortController.signal);
        } catch (retryError) {
          if (this.terminal) return;
          throw new AutopilotMediaError(
            "audio_playback_failed", "浏览器拒绝开始播放合成语音（点屏后仍被拒）",
            { cause: retryError, failureStage: "play_rejected" });
        } finally {
          this.ports.announceGestureNeeded?.(false);
        }
      }
    } catch (error) {
      this.fail(error);
    }
  }
}

export class BrowserAutopilotSpeechExecutor implements AutopilotSpeechExecutor {
  private readonly sessionId: string;
  private readonly ports: AutopilotSpeechBrowserPorts;

  constructor(
    sessionId: string,
    ports: AutopilotSpeechBrowserPorts,
  ) {
    this.sessionId = sessionId;
    this.ports = ports;
  }

  start(command: TtsCommand): AutopilotSpeechPlayback {
    if (command.state !== "pending") {
      throw new AutopilotMediaError(
        "audio_playback_failed", "仅 pending TTS 命令可播放",
        { failureStage: "executor_start_failed" });
    }
    return new BrowserAutopilotSpeechPlayback(this.sessionId, command, this.ports);
  }
}
