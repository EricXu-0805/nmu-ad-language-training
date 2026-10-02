import type { AutopilotVoiceSettings } from "./autopilotMediaTransport.ts";
import type { VoiceActivityWindows } from "./autopilotVoiceActivity.ts";

/**
 * 平板侧的服务器语音开关缓存(按场次)。
 *
 * 开麦那一刻只读缓存,绝不在提问→开麦的路上等网络:读面在后台发起,五分钟最多
 * 一次。还没成功读到过、最近一次读取失败(超时、坏形状、网络)、或服务器关了
 * VAD,这次采集都不武装静音自动收麦——只剩按钮与作答窗口到点,即 2026-09-21
 * 之前的行为。提前回答检测不看这里:服务器关掉它时授权路由回 409,平板走既有的
 * 「检测不可用」那条路。
 */
export const AUTOPILOT_VOICE_SETTINGS_REFRESH_MS = 5 * 60_000;

export interface AutopilotVoiceSettingsPorts {
  fetch(sessionId: string, signal: AbortSignal): Promise<AutopilotVoiceSettings>;
  now(): number;
}

export class AutopilotVoiceSettingsCache {
  readonly sessionId: string;
  private readonly ports: AutopilotVoiceSettingsPorts;
  private latest: AutopilotVoiceSettings | null = null;
  private lastAttemptAtMs: number | null = null;
  private inFlight: AbortController | null = null;
  private disposed = false;

  constructor(sessionId: string, ports: AutopilotVoiceSettingsPorts) {
    this.sessionId = sessionId;
    this.ports = ports;
  }

  /** 只发起、不等;离上次发起不足五分钟或已有一次在途就什么都不做。永不抛。 */
  refreshInBackground(): void {
    if (this.disposed || this.inFlight !== null) return;
    const now = this.ports.now();
    if (this.lastAttemptAtMs !== null
        && now - this.lastAttemptAtMs < AUTOPILOT_VOICE_SETTINGS_REFRESH_MS) return;
    this.lastAttemptAtMs = now;
    const abort = new AbortController();
    this.inFlight = abort;
    let operation: Promise<AutopilotVoiceSettings>;
    try {
      operation = this.ports.fetch(this.sessionId, abort.signal);
    } catch (error) {
      operation = Promise.reject(error);
    }
    operation.then(
      (settings) => { if (this.inFlight === abort) this.latest = settings; },
      () => { if (this.inFlight === abort) this.latest = null; },
    ).finally(() => {
      if (this.inFlight === abort) this.inFlight = null;
    });
  }

  /** 这次采集用的两个尾静默窗口;null = 不武装。 */
  vadWindows(): VoiceActivityWindows | null {
    const settings = this.latest;
    if (settings === null || !settings.vad_enabled) return null;
    return {
      trailingSilenceMs: settings.vad_trailing_silence_ms,
      shortTrailingSilenceMs: settings.vad_short_trailing_silence_ms,
    };
  }

  dispose(): void {
    this.disposed = true;
    this.latest = null;
    const abort = this.inFlight;
    this.inFlight = null;
    abort?.abort(new DOMException("语音设置读取已取消", "AbortError"));
  }
}
