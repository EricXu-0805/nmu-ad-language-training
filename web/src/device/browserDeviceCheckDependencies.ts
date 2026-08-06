/**
 * DeviceCheckDependencies 的真浏览器实现。判定逻辑一行都不在这里——
 * 这里只负责把 Web API 的毛边(异步 voices、AGC、autoplay 手势窗口)收干净。
 */
import type { DeviceCheckDependencies, MicrophoneInfo, NoiseSample } from "./deviceCheck.ts";

export interface BrowserDeviceCheckDependencies extends DeviceCheckDependencies {
  /**
   * 必须在用户手势回调里**同步**调用:Safari 只允许手势栈内 resume AudioContext,
   * 等 getUserMedia 的 await 回来激活窗口就关了。
   */
  armAudioSync(): void;
  /** 放一声提示音(660Hz 半秒),供人工确认扬声器;通路不可用时返回 false。 */
  playBeep(): boolean;
  dispose(): void;
}

export function createBrowserDeviceCheckDependencies(): BrowserDeviceCheckDependencies {
  let stream: MediaStream | null = null;
  let context: AudioContext | null = null;

  // Chrome 的 getVoices() 首次调用常是空的,列表经 voiceschanged 异步到——
  // 页面打开就预热,等研究者点"开始"时缓存基本已就位。
  let voices: { total: number; chinese: number } | null = null;
  const readVoices = () => {
    try {
      const list = window.speechSynthesis?.getVoices() ?? [];
      voices = {
        total: list.length,
        chinese: list.filter((v) => v.lang.toLowerCase().startsWith("zh")).length,
      };
    } catch {
      voices = null;
    }
  };
  readVoices();
  try {
    window.speechSynthesis?.addEventListener("voiceschanged", readVoices);
  } catch { /* 没有 speechSynthesis */ }

  const ensureContext = (): AudioContext | null => {
    if (context !== null) return context;
    const Ctor = window.AudioContext
      ?? (window as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return null;
    context = new Ctor();
    return context;
  };

  return {
    secureContext: window.isSecureContext,
    userAgent: navigator.userAgent,
    screen: {
      width: window.innerWidth,
      height: window.innerHeight,
      pixelRatio: window.devicePixelRatio || 1,
    },
    mediaDevicesAvailable: typeof navigator.mediaDevices?.getUserMedia === "function",
    mediaRecorderAvailable: typeof window.MediaRecorder === "function",

    isTypeSupported: (mime) => window.MediaRecorder?.isTypeSupported?.(mime) ?? false,

    async acquireMicrophone(): Promise<MicrophoneInfo> {
      stream?.getTracks().forEach((track) => track.stop());
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const track = stream.getAudioTracks()[0];
      const settings: MediaTrackSettings = track?.getSettings?.() ?? {};
      return {
        label: track?.label ?? "",
        sampleRate: typeof settings.sampleRate === "number" ? settings.sampleRate : null,
        channelCount: typeof settings.channelCount === "number" ? settings.channelCount : null,
        echoCancellation: typeof settings.echoCancellation === "boolean" ? settings.echoCancellation : null,
        noiseSuppression: typeof settings.noiseSuppression === "boolean" ? settings.noiseSuppression : null,
        autoGainControl: typeof settings.autoGainControl === "boolean" ? settings.autoGainControl : null,
      };
    },

    async sampleNoise(seconds: number): Promise<NoiseSample> {
      const ctx = ensureContext();
      if (ctx === null || stream === null) throw new Error("音频上下文或麦克风流不可用");
      const source = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 2048;
      source.connect(analyser);
      const buffer = new Float32Array(analyser.fftSize);
      let sumSquares = 0;
      let frames = 0;
      let peak = 0;
      try {
        await new Promise<void>((resolve) => {
          const started = performance.now();
          const timer = window.setInterval(() => {
            analyser.getFloatTimeDomainData(buffer);
            let frameSum = 0;
            for (const value of buffer) {
              frameSum += value * value;
              const magnitude = Math.abs(value);
              if (magnitude > peak) peak = magnitude;
            }
            sumSquares += frameSum / buffer.length;
            frames += 1;
            if (performance.now() - started >= seconds * 1000) {
              clearInterval(timer);
              resolve();
            }
          }, 60);
        });
      } finally {
        source.disconnect();
      }
      const rms = frames > 0 ? Math.sqrt(sumSquares / frames) : 0;
      const toDbfs = (value: number) => (value > 0 ? 20 * Math.log10(value) : -100);
      return { rmsDbfs: toDbfs(rms), peakDbfs: toDbfs(peak) };
    },

    releaseMicrophone() {
      stream?.getTracks().forEach((track) => track.stop());
      stream = null;
    },

    audioContextSampleRate: () => ensureContext()?.sampleRate ?? null,

    armAudioSync() {
      const ctx = ensureContext();
      // 不 await:只要 resume 发生在手势栈内,promise 什么时候落定都行。
      void ctx?.resume().catch(() => { /* resumeAudio 会如实报告 state */ });
    },

    async resumeAudio(): Promise<"running" | "suspended" | "unavailable"> {
      const ctx = ensureContext();
      if (ctx === null) return "unavailable";
      try {
        await ctx.resume();
      } catch { /* state 说话 */ }
      return ctx.state === "running" ? "running" : "suspended";
    },

    playBeep(): boolean {
      const ctx = ensureContext();
      if (ctx === null || ctx.state !== "running") return false;
      const oscillator = ctx.createOscillator();
      const gain = ctx.createGain();
      oscillator.type = "sine";
      oscillator.frequency.value = 660;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.3, ctx.currentTime + 0.05);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.5);
      oscillator.connect(gain).connect(ctx.destination);
      oscillator.start();
      oscillator.stop(ctx.currentTime + 0.55);
      return true;
    },

    speechSynthesisVoices: () => voices,

    fetchImpl: (input, init) => fetch(input, init),
    now: () => performance.now(),

    async storageEstimate() {
      try {
        const estimate = await navigator.storage?.estimate?.();
        return {
          quotaBytes: typeof estimate?.quota === "number" ? estimate.quota : null,
          usageBytes: typeof estimate?.usage === "number" ? estimate.usage : null,
        };
      } catch {
        return { quotaBytes: null, usageBytes: null };
      }
    },

    dispose() {
      stream?.getTracks().forEach((track) => track.stop());
      stream = null;
      try {
        window.speechSynthesis?.removeEventListener("voiceschanged", readVoices);
      } catch { /* 没有 speechSynthesis */ }
      void context?.close().catch(() => { /* 已关 */ });
      context = null;
    },
  };
}
