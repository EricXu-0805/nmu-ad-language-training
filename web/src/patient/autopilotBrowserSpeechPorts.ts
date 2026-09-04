import { fetchExactAutopilotTts } from "./autopilotMediaTransport.ts";
import { browserAutopilotMediaDependencies } from "./autopilotBrowserMediaDependencies.ts";
import type { AutopilotSpeechBrowserPorts } from "./autopilotSpeechExecutor.ts";
import {
  announceGestureNeeded, SILENT_WAV_DATA_URI, stopSpeaking, ttsEnabled,
} from "./tts.ts";

// 自动带练每条话术共用**同一个** <audio>,不再每句 new Audio()。
// 生产实证(2026-08-31 / 09-03 / 09-04,三台不同设备):第一句(离「点一下,开始」
// 只有一两秒)能放,老人答完之后的反馈句 play() 一律被浏览器拒掉(play_rejected),
// 自动流程当成设备故障安全暂停——「说完一张图就跳休息」。这类浏览器只认
// 「在用户手势里放过声的那个元素」,元素一换就要重新要手势。所以:元素只有一个,
// 在老人点屏那一下里先放一段静音把它解锁,之后每句都在它身上放。
let sharedAudio: HTMLAudioElement | null = null;

function sharedAutopilotAudio(): HTMLAudioElement {
  if (typeof Audio === "undefined") throw new Error("当前浏览器不支持可验证的音频播放");
  if (!sharedAudio) sharedAudio = new Audio();
  return sharedAudio;
}

/** 必须在用户手势(pointerdown/click 处理器)里同步调用。失败静默:解锁只是补强。 */
export function unlockAutopilotPlayback(): void {
  if (typeof Audio === "undefined") return;
  const audio = sharedAutopilotAudio();
  // 正在放真话术时绝不打断它。
  if (!audio.paused) return;
  audio.onplaying = null;
  audio.onended = null;
  audio.onerror = null;
  audio.src = SILENT_WAV_DATA_URI;
  void audio.play().catch(() => { /* 没解锁成,后面 play() 被拒时还有等手势那条路 */ });
}

/** Real browser bindings kept outside the deterministic speech state machine. */
export const browserAutopilotSpeechPorts: AutopilotSpeechBrowserPorts = {
  enabled: ttsEnabled,
  stopSpeaking,
  fetchTts: (sessionId, command, signal) => fetchExactAutopilotTts(
    sessionId, command, signal, browserAutopilotMediaDependencies),
  createAudio: sharedAutopilotAudio,
  createObjectUrl: (blob) => URL.createObjectURL(blob),
  revokeObjectUrl: (url) => URL.revokeObjectURL(url),
  now: () => performance.now(),
  playOnNextGesture: (audio, signal) => new Promise<void>((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return; }
    const onGesture = () => {
      cleanup();
      // play() 必须在手势处理器里**同步**发起——挪到 await 之后就不算手势了。
      resolve(audio.play());
    };
    const onAbort = () => { cleanup(); reject(signal.reason); };
    const cleanup = () => {
      document.removeEventListener("pointerdown", onGesture, true);
      signal.removeEventListener("abort", onAbort);
    };
    document.addEventListener("pointerdown", onGesture, { capture: true, once: true });
    signal.addEventListener("abort", onAbort, { once: true });
  }),
  announceGestureNeeded,
};
