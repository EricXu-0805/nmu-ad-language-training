import type {
  AutopilotSpeechExecutor,
  AutopilotSpeechPlayback,
} from "./autopilotController.ts";
import { AutopilotMediaError } from "./autopilotMediaError.ts";
import type { NextCommandProjection } from "./autopilotProtocol.ts";

type TtsCommand = Extract<NextCommandProjection, { kind: "tts" }>;

export interface AutopilotSpeechBrowserPorts {
  enabled(): boolean;
  stopSpeaking(): void;
  fetchTts(sessionId: string, command: TtsCommand, signal: AbortSignal): Promise<Blob | null>;
  createAudio(): HTMLAudioElement;
  createObjectUrl(blob: Blob): string;
  revokeObjectUrl(url: string): void;
  now(): number;
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
  private readonly endedDeferred = deferred<{ media_duration_ms?: number }>();
  private readonly closedDeferred = deferred<void>();
  private readonly abortController = new AbortController();
  private readonly audio: HTMLAudioElement;
  private objectUrl: string | null = null;
  private startAtMs: number | null = null;
  private terminal = false;
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
    this.closedDeferred.resolve(undefined);
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
    this.closedDeferred.resolve(undefined);
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
        this.closedDeferred.resolve(undefined);
      };
      this.audio.onerror = () => this.fail(new AutopilotMediaError(
        "audio_playback_failed", "TTS 音频解码或播放失败",
        { failureStage: "media_decode_error" }));
      try {
        await this.audio.play();
      } catch (error) {
        // NotAllowedError / NotSupportedError 等 play() 拒绝在这里统一定阶段。
        throw new AutopilotMediaError(
          "audio_playback_failed", "浏览器拒绝开始播放合成语音",
          { cause: error, failureStage: "play_rejected" });
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
