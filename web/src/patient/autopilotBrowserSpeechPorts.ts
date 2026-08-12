import { fetchExactAutopilotTts } from "./autopilotMediaTransport.ts";
import { browserAutopilotMediaDependencies } from "./autopilotBrowserMediaDependencies.ts";
import type { AutopilotSpeechBrowserPorts } from "./autopilotSpeechExecutor.ts";
import { stopSpeaking, ttsEnabled } from "./tts.ts";

/** Real browser bindings kept outside the deterministic speech state machine. */
export const browserAutopilotSpeechPorts: AutopilotSpeechBrowserPorts = {
  enabled: ttsEnabled,
  stopSpeaking,
  fetchTts: (sessionId, command, signal) => fetchExactAutopilotTts(
    sessionId, command, signal, browserAutopilotMediaDependencies),
  createAudio: () => {
    if (typeof Audio === "undefined") throw new Error("当前浏览器不支持可验证的音频播放");
    return new Audio();
  },
  createObjectUrl: (blob) => URL.createObjectURL(blob),
  revokeObjectUrl: (url) => URL.revokeObjectURL(url),
  now: () => performance.now(),
};
