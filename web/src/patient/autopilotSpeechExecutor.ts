import type {
  AutopilotSpeechExecutor,
  AutopilotSpeechPlayback,
} from "./autopilotController.ts";
import type { NextCommandProjection } from "./autopilotProtocol.ts";
import { fetchExactAutopilotTts } from "./autopilotMediaTransport.ts";
import { browserAutopilotMediaDependencies } from "./autopilotBrowserMediaDependencies.ts";
import { stopSpeaking, ttsEnabled } from "./tts.ts";

type TtsCommand = Extract<NextCommandProjection, { kind: "tts" }>;

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

  constructor(sessionId: string, command: TtsCommand) {
    this.sessionId = sessionId;
    this.command = command;
    if (typeof Audio === "undefined") throw new Error("当前浏览器不支持可验证的音频播放");
    this.audio = new Audio();
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
    URL.revokeObjectURL(this.objectUrl);
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
      if (!ttsEnabled()) throw new Error("自动驾驶语音门禁未显式开启");
      stopSpeaking();
      const blob = await fetchExactAutopilotTts(
        this.sessionId,
        this.command,
        this.abortController.signal,
        browserAutopilotMediaDependencies,
      );
      if (blob === null) throw new Error("TTS 服务当前未产生音频");
      if (this.terminal) return;
      this.objectUrl = URL.createObjectURL(blob);
      this.audio.src = this.objectUrl;
      this.audio.onplaying = () => {
        if (this.terminal || this.startAtMs !== null) return;
        this.startAtMs = performance.now();
        const duration = Number.isFinite(this.audio.duration) && this.audio.duration >= 0
          ? Math.round(this.audio.duration * 1_000) : undefined;
        this.startedDeferred.resolve(duration === undefined ? {} : { media_duration_ms: duration });
      };
      this.audio.onended = () => {
        if (this.terminal || this.startAtMs === null) return;
        const elapsed = Math.max(0, Math.round(performance.now() - this.startAtMs));
        this.terminal = true;
        this.cleanupUrl();
        this.endedDeferred.resolve({ media_duration_ms: elapsed });
        this.closedDeferred.resolve(undefined);
      };
      this.audio.onerror = () => this.fail(new Error("TTS 音频解码或播放失败"));
      await this.audio.play();
    } catch (error) {
      this.fail(error);
    }
  }
}

export class BrowserAutopilotSpeechExecutor implements AutopilotSpeechExecutor {
  private readonly sessionId: string;

  constructor(sessionId: string) { this.sessionId = sessionId; }

  start(command: TtsCommand): AutopilotSpeechPlayback {
    if (command.state !== "pending") throw new Error("仅 pending TTS 命令可播放");
    return new BrowserAutopilotSpeechPlayback(this.sessionId, command);
  }
}
