/**
 * DeviceCheckDependencies 的真浏览器实现。判定逻辑一行都不在这里——
 * 这里只负责把 Web API 的毛边(异步 voices、AGC、autoplay 手势窗口)收干净。
 */
import type { DeviceCheckDependencies, MicrophoneInfo, NoiseSample } from "./deviceCheck.ts";

export interface BrowserDeviceCheckDependencies extends DeviceCheckDependencies {
  /** 绑定当前页面生命周期；可用于 React 开发模式的 effect 重放。 */
  activate(): void;
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
  let disposed = false;
  let microphoneGeneration = 0;

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

  const ensureContext = (): AudioContext | null => {
    if (disposed) return null;
    if (context !== null) return context;
    const Ctor = window.AudioContext
      ?? (window as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return null;
    context = new Ctor();
    return context;
  };

  return {
    activate() {
      disposed = false;
      readVoices();
      try {
        window.speechSynthesis?.addEventListener("voiceschanged", readVoices);
      } catch { /* 没有 speechSynthesis */ }
    },
    secureContext: window.isSecureContext,
    userAgent: navigator.userAgent,
    screen: {
      width: window.innerWidth,
      height: window.innerHeight,
      pixelRatio: window.devicePixelRatio || 1,
    },
    mediaDevicesAvailable: typeof navigator.mediaDevices?.getUserMedia === "function",
    mediaRecorderAvailable: typeof window.MediaRecorder === "function",
    webLocksAvailable: typeof navigator.locks?.request === "function",
    indexedDbAvailable: typeof window.indexedDB?.open === "function",
    broadcastChannelAvailable: typeof window.BroadcastChannel === "function",
    blobUrlAvailable: typeof URL.createObjectURL === "function" && typeof URL.revokeObjectURL === "function",
    currentBuildId: __BUILD_ID__,
    async fetchDeployedBuildId(): Promise<string | null> {
      const response = await fetch("/build-id.txt", {
        cache: "no-store",
        credentials: "same-origin",
      });
      return response.ok ? response.text() : null;
    },

    isTypeSupported: (mime) => window.MediaRecorder?.isTypeSupported?.(mime) ?? false,

    async acquireMicrophone(): Promise<MicrophoneInfo> {
      if (disposed) throw new DOMException("设备检查已结束", "AbortError");
      const generation = ++microphoneGeneration;
      stream?.getTracks().forEach((track) => track.stop());
      stream = null;
      const acquired = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (disposed || generation !== microphoneGeneration) {
        acquired.getTracks().forEach((track) => track.stop());
        throw new DOMException("设备检查已结束", "AbortError");
      }
      stream = acquired;
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
      microphoneGeneration += 1;
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
    // 必须是墙钟:时钟偏移拿它和服务器 Date 头相减。performance.now() 是页面
    // 启动起的毫秒,真机上会把偏移报成整个 Unix 时间戳(≈56 年)。
    now: () => Date.now(),

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

    async indexedDbRoundTrip(kind) {
      // 独立的一次性库：探针残留绝不能混进 nmu-audio，被恢复快照当成历史孤儿而停麦。
      const name = `nmu-probe-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`;
      const written = new Uint8Array([0x1a, 0x45, 0xdf, 0xa3]);
      const value = kind === "blob" ? new Blob([written], { type: "audio/webm" }) : written.slice().buffer;
      const db = await new Promise<IDBDatabase>((resolve, reject) => {
        const request = window.indexedDB.open(name, 1);
        request.onupgradeneeded = () => { request.result.createObjectStore("probe"); };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
      const step = (mode: IDBTransactionMode, run: (store: IDBObjectStore) => IDBRequest) =>
        new Promise<unknown>((resolve, reject) => {
          const transaction = db.transaction("probe", mode);
          const request = run(transaction.objectStore("probe"));
          transaction.oncomplete = () => resolve(request.result);
          transaction.onabort = () => reject(transaction.error ?? new Error("探针事务中止"));
        });
      try {
        await step("readwrite", (store) => store.put(value, "scratch"));
        const stored = await step("readonly", (store) => store.get("scratch"));
        const read = stored instanceof Blob ? await stored.arrayBuffer() : stored;
        if (!(read instanceof ArrayBuffer) || (kind === "blob") !== (stored instanceof Blob)
            || new Uint8Array(read).join() !== written.join()) {
          throw new Error("暂存读回内容与写入不一致");
        }
        await step("readwrite", (store) => store.delete("scratch"));
      } finally {
        db.close();
        window.indexedDB.deleteDatabase(name);
      }
    },

    dispose() {
      disposed = true;
      microphoneGeneration += 1;
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
