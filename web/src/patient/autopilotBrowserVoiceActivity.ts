import { createVoiceActivityDetector } from "./autopilotVoiceActivity.ts";

/**
 * 浏览器侧的采样器:在录音器**已经拿到**的那条 MediaStream 上挂一个 AnalyserNode,
 * 每 64 ms 读一帧时域 RMS 喂给纯判定;判到「说完」就回调一次并自拆。
 *
 * 绝不第二次 getUserMedia:流是 Recorder 的,这里只旁听。AudioContext 在拆除时
 * 一定 close——它自己不会让麦克风活着(track 由 Recorder 关),但留着就是一个
 * 挂着输入节点的音频图,下一次开麦前必须不存在。
 *
 * 任何一步失败(没有 AudioContext、自动播放策略把它挂在 suspended、analyser 读
 * 数抛错)都只是「听不见」:静默无为,按钮与作答窗口到点照旧收麦,行为与没有
 * VAD 时完全一样。这里没有任何路径会把错误抛给调用方。
 */

/** 采样间隔。AnalyserNode 一帧 2048 采样 ≈ 43 ms@48 kHz,64 ms 一读不会漏帧也不空转。 */
export const VAD_SAMPLE_INTERVAL_MS = 64;

export const VAD_ANALYSER_FFT_SIZE = 2048;

/** 采样器真正碰到的那几件浏览器对象,可注入以便在 Node 里证明拆除路径。 */
export interface VoiceActivitySamplerPorts {
  createContext(): AudioContext;
  setInterval(callback: () => void, intervalMs: number): number;
  clearInterval(handle: number): void;
  now(): number;
}

export const browserVoiceActivitySamplerPorts: VoiceActivitySamplerPorts = {
  createContext: () => new AudioContext(),
  setInterval: (callback, intervalMs) => window.setInterval(callback, intervalMs),
  clearInterval: (handle) => window.clearInterval(handle),
  now: () => performance.now(),
};

function timeDomainRms(samples: Float32Array): number {
  let sum = 0;
  for (let index = 0; index < samples.length; index += 1) {
    sum += samples[index] * samples[index];
  }
  return samples.length === 0 ? 0 : Math.sqrt(sum / samples.length);
}

/**
 * 开始旁听;返回拆除函数。拆除幂等,拆掉之后绝不再回调。
 *
 * `onTrailingSilence` 至多被调一次,而且一定在拆除之前——调用方拿到回调时
 * 采样器已经不存在了。
 */
export function observeTrailingSilence(
  stream: MediaStream,
  onTrailingSilence: () => void,
  ports: VoiceActivitySamplerPorts = browserVoiceActivitySamplerPorts,
): () => void {
  let context: AudioContext | null = null;
  let source: MediaStreamAudioSourceNode | null = null;
  let timer: number | null = null;
  let released = false;

  const release = (): void => {
    if (released) return;
    released = true;
    // 先撤定时器再拆图:任何一步抛错都不能拦住后面那步。
    const handle = timer;
    timer = null;
    if (handle !== null) {
      try { ports.clearInterval(handle); } catch { /* 定时器端口失效,图照拆 */ }
    }
    const node = source;
    source = null;
    if (node !== null) {
      try { node.disconnect(); } catch { /* 图已经没了 */ }
    }
    const graph = context;
    context = null;
    if (graph !== null) {
      try { void graph.close().catch(() => {}); } catch { /* close 同步抛错也算拆过了 */ }
    }
  };

  try {
    context = ports.createContext();
    source = context.createMediaStreamSource(stream);
    const analyser = context.createAnalyser();
    analyser.fftSize = VAD_ANALYSER_FFT_SIZE;
    source.connect(analyser);
    // 自动播放策略可能把新建的上下文挂在 suspended:试着唤醒,唤不醒就一直读到 0,
    // 判定永远停在 idle——正是「静默无为」。
    if (context.state === "suspended") {
      void context.resume().catch(() => {});
    }
    const detector = createVoiceActivityDetector();
    const frame = new Float32Array(analyser.fftSize);
    timer = ports.setInterval(() => {
      if (released) return;
      try {
        analyser.getFloatTimeDomainData(frame);
        if (detector.push(timeDomainRms(frame), ports.now()) !== "stopped") return;
      } catch {
        // 读数坏了就闭嘴:拆掉自己,余下的收麦路不受影响。
        release();
        return;
      }
      release();
      onTrailingSilence();
    }, VAD_SAMPLE_INTERVAL_MS);
  } catch {
    release();
  }
  return release;
}
