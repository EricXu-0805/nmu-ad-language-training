// 老人端语音(小语开口),两级链路:
// ① 神经 TTS(后端 /tts/speak,同源)——后端按配置走云端(白名单闭集,只合成题库/脚本/
//    固定话术,患者字段永不出网)或本地 piper,失败自动降级;前端只看响应,不感知引擎。
// ② 回退:浏览器 speechSynthesis,只用 localService 中文语音(Chrome "Google 普通话" 是联网合成,
//    文本会出机器——宁可静音也不用;找不到本机中文语音就什么都不读)。
// ★只朗读屏上已显示的题库/脚本原文,不合成任何新话术(单一内容源不破)。
//
// 播放核心是显式状态机,防三类并发事故(复审确证过的真事故):
// - busy 在取队即置位(fetch 在途也算忙)——否则同帧"问句+线索"两次 speak 并发抢同一 audio 元素;
// - gen 世代计数,打断即递增——迟到的旧 fetch/旧 play 回调看到世代不符就自弃,
//   绝不把已被换掉的旧句用系统语音"还魂"叠在新句上;
// - 播放被拒(无用户激活)把句子退回队首——触屏补读按原顺序读"问句→线索",不丢主句。

import { handleDeviceAuthorizationFailure, selectDeviceCredential } from "../api";
import { csrfHeader } from "../security/csrf";
import { ttsEngineLabel } from "./ttsEngineLabel";
import {
  EMPTY_TTS_CONTEXT_STATE,
  rememberTtsLine,
  replayLineForContext,
  transitionTtsContext,
  type TtsContextState,
  type TtsPlaybackContextKey,
  type TtsReplayLine,
} from "./ttsContext";
import { mustDiscardSynthesizedSpeech } from "./ttsResponsePolicy";

let enabled = localStorage.getItem("nmu:tts") !== "off";
let pending: TtsReplayLine | null = null;
// 试听重读不是全局历史；它只属于当前场次+控制面+题位上下文。
let contextState: TtsContextState = EMPTY_TTS_CONTEXT_STATE;

export function ttsEnabled(): boolean { return enabled; }
export function setTtsEnabled(on: boolean): void {
  enabled = on;
  localStorage.setItem("nmu:tts", on ? "on" : "off");
  if (!on) stopSpeaking();
}

// 朗读审计(本地环形日志,最近 200 条):记录小语何时说了什么,
// 供分析侧从研究音频里剔除机器人语音段(问句尾音会进热麦,这是唯一的机器可剔依据)。
function audit(ev: string, tag: string, text: string): void {
  try {
    const k = "nmu:tts:log";
    const arr = JSON.parse(localStorage.getItem(k) ?? "[]") as unknown[];
    arr.push({ t: Date.now(), ev, tag, text });
    localStorage.setItem(k, JSON.stringify(arr.slice(-200)));
  } catch { /* 审计失败不影响朗读 */ }
}

// ---------------- 播放状态机 ----------------
type Line = TtsReplayLine & { fetchPath?: string };
let neural: "unknown" | "on" | "off" = "unknown"; // 后端引擎可用性;只有"引擎未接"(X-Tts-Engine=null-0)的 204 才一次锁定整场回退
let activeEngineTag: string | null = null; // 只记录实际返回可播放字节的服务器签发音源
let neuralFails = 0;           // 连续网络/后端失败计数:偶发抖动单句回退,连败才整场降级
const NEURAL_FAIL_LATCH = 3;   // (一次瞬断就把 neural 钉死,会让无本机中文语音的设备整场静音)
const audioEl = typeof Audio !== "undefined" ? new Audio() : null;
let queue: Line[] = [];
let busy = false;   // 取队即置位:fetch/play 在途都算忙,同帧第二句只能排队
let gen = 0;        // 世代:打断/停止即 +1,旧世代的一切异步续体自弃
let curUrl: string | null = null; // 正在播的 blobURL:打断路径也要回收,不靠 onended

// 「这一句的播放已经收口」——播完、失败或被打断。第1周多轮对话据此决定什么时候
// 才放行麦克风:开麦时机不能开环估时(云合成冷启没有上界),抢话时机器人自己的
// 声音会被录进老人的答句、被云 ASR 当成老人说的话写进研究账本。
export type SpeechOutcome = "played" | "failed" | "interrupted";
const settleListeners = new Set<(tag: string, outcome: SpeechOutcome) => void>();

export function onSpeechSettled(
  listener: (tag: string, outcome: SpeechOutcome) => void,
): () => void {
  settleListeners.add(listener);
  return () => { settleListeners.delete(listener); };
}

function notifySettled(tag: string, outcome: SpeechOutcome): void {
  for (const listener of [...settleListeners]) {
    try { listener(tag, outcome); } catch { /* 订阅方自己的错不影响播放链 */ }
  }
}

function interrupt(): void {
  const wasBusy = busy;
  const interruptedTag = pending?.tag ?? "";
  gen += 1;
  busy = false;
  ttsGesture(false);   // 被打断/换话的句子不再等手势
  if (audioEl && !audioEl.paused) audioEl.pause();
  if (curUrl) { URL.revokeObjectURL(curUrl); curUrl = null; }
  if (typeof window !== "undefined" && "speechSynthesis" in window) window.speechSynthesis.cancel();
  if (wasBusy) notifySettled(interruptedTag, "interrupted");
}

function driveQueue(): void {
  if (busy) return;
  const next = queue.shift();
  if (!next) return;
  busy = true;
  void playItem(next, gen);
}

async function playItem(item: Line, g: number): Promise<void> {
  if (item.contextKey !== contextState.activeContextKey) return;
  // ① 神经路径
  if (audioEl && neural !== "off") {
    let url: string | null = null;
    try {
      const credential = selectDeviceCredential();
      // fetchPath=按行取音(第1周回应句):文本只在服务端持久行里,请求不携带正文。
      const res = item.fetchPath
        ? await fetch(item.fetchPath, {
          method: "POST",
          credentials: "omit",
          cache: "no-store",
          headers: { ...credential.headers, ...csrfHeader("POST") },
        })
        : await fetch("/tts/speak", {
          method: "POST",
          credentials: "omit",
          cache: "no-store",
          headers: {
            "Content-Type": "application/json",
            ...credential.headers,
            ...csrfHeader("POST"),
          },
          body: JSON.stringify({ text: item.text }),
        });
      if (g !== gen || item.contextKey !== contextState.activeContextKey) return;
      // 已被打断/撤销上下文:新句自会驱动,旧续体退场
      const engineTag = res.headers.get("X-Tts-Engine") ?? "";
      if (!res.ok) {
        const errorText = await res.text();
        if (mustDiscardSynthesizedSpeech(res.status, errorText)) {
          // The request was authorized when synthesis began but lost its exact
          // bedside authority before bytes returned.  Neither those bytes nor
          // browser speech synthesis may resurrect the old sentence.
          stopSpeaking();
          return;
        }
        if (handleDeviceAuthorizationFailure(res.status, errorText, credential)) {
          // Auth/session loss is not an engine outage: never resurrect an old-session
          // sentence through system-speech fallback while re-pairing.
          stopSpeaking();
          return;
        }
      }
      if (res.status === 204) {
        // 云接入后 204 有两种成因:引擎未接(null-0,稳定态)才一次锁死整场;
        // 云端瞬态失败(header 带引擎名)按连败计数——云 5 秒后恢复不该整场哑掉。
        if (engineTag === "null-0" || ++neuralFails >= NEURAL_FAIL_LATCH) {
          neural = "off";
          activeEngineTag = null;
        }
      }
      else if (res.ok) {
        const blob = await res.blob();
        if (g !== gen || item.contextKey !== contextState.activeContextKey) return;
        url = URL.createObjectURL(blob);
        neural = "on";
        neuralFails = 0;
        activeEngineTag = engineTag || null;
        const u = url;
        audioEl.src = u;
        curUrl = u;
        audioEl.onended = () => {
          URL.revokeObjectURL(u);
          if (curUrl === u) curUrl = null;
          if (g !== gen || item.contextKey !== contextState.activeContextKey) return;
          audit("end", item.tag, item.text);
          if (pending?.text === item.text && pending.contextKey === item.contextKey) pending = null;
          busy = false;
          notifySettled(item.tag, "played");
          driveQueue();
        };
        audioEl.onerror = () => {
          URL.revokeObjectURL(u);
          if (curUrl === u) curUrl = null;
          if (g !== gen || item.contextKey !== contextState.activeContextKey) return;
          audit("error:audio", item.tag, item.text);
          // 浏览器无法解码/播放本地 WAV 时，本会话直接降级到系统语音。
          // 若仍保留 neural=on，voiceschanged/pointerdown 的补读会不断重取同一句，
          // 造成老人端无声且后端被 /tts/speak 高频请求淹没。
          neural = "off";
          activeEngineTag = null;
          utterItem(item, g);
        };
        await audioEl.play();
        audit(`start@${engineTag || "neural"}`, item.tag, item.text);
        ttsGesture(false);
        return;                                     // 播放中,后续由 onended 续驱
      }
      else if (++neuralFails >= NEURAL_FAIL_LATCH) {   // 连败:整场降级,本句落回退
        neural = "off";
        activeEngineTag = null;
      }
      // !res.ok(非 204):这一句落到 ② 回退
    } catch (e) {
      if (url) { URL.revokeObjectURL(url); if (curUrl === url) curUrl = null; }
      if (g !== gen || item.contextKey !== contextState.activeContextKey) return;
      if (e instanceof DOMException && e.name === "NotAllowedError") {
        // 无用户激活:句子退回队首,触屏后按原顺序补读(问句仍在线索前面)。
        // 同时把「点一下，接着听」亮出来——否则老人对着一句没放出来的话干等,
        // 第1周的开麦闭环也要等到 12 秒超时才放行麦克风。
        audit("error:not-allowed", item.tag, item.text);
        queue.unshift(item);
        busy = false;
        ttsGesture(true);
        return;
      }
      if (e instanceof DOMException && e.name === "AbortError") {
        // 被打断(pause/换 src):新句已接管,旧句静默退场,绝不回退系统语音还魂。
        // 同世代 Abort 理论不可达,防御性放掉忙位以免队列停摆。
        audit("error:aborted", item.tag, item.text);
        if (g === gen) { busy = false; driveQueue(); }
        return;
      }
      // 网络/后端异常:单句回退系统语音;只有连续多次失败才整场停用神经路径
      // (防补读风暴),一次瞬断不钉死——成功一次即清零复活。
      if (++neuralFails >= NEURAL_FAIL_LATCH) {
        neural = "off";
        activeEngineTag = null;
      }
      // 这一句落到 ② 回退
    }
  }
  if (g !== gen || item.contextKey !== contextState.activeContextKey) return;
  // ② 系统语音回退
  utterItem(item, g);
}

// ---------------- ② 回退:浏览器系统语音 ----------------
// 选声:严格限 localService;普通话(zh-CN/cmn)优先于 zh-TW/zh-HK(防给南京老人读粤语);
// 同为 zh-CN 再按质量排——macOS 把 Eddy/Flo/Grandma 等"玩具音色"也标成 zh-CN 本机语音,
// 枚举序还常排在婷婷前,不降权就会选中怪声(真机已踩中,被当成"不是普通话")。
const NOVELTY_VOICES = new Set([
  "Albert", "Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos", "Eddy", "Flo", "Fred",
  "Good News", "Grandma", "Grandpa", "Jester", "Junior", "Kathy", "Organ", "Ralph", "Reed",
  "Rocko", "Sandy", "Shelley", "Superstar", "Trinoids", "Whisper", "Wobble", "Zarvox",
]);
// 已知的高质量中文音色(macOS 婷婷;Windows 晓晓/慧慧/康康/瑶瑶),命中即置顶
const PREFERRED_VOICES = ["婷婷", "Tingting", "Ting-Ting", "晓晓", "Xiaoxiao", "慧慧", "Huihui", "瑶瑶", "Yaoyao", "康康", "Kangkang"];

function pickLocalZhVoice(): SpeechSynthesisVoice | null {
  const norm = (l: string) => l.replace("_", "-").toLowerCase();
  const pinned = localStorage.getItem("nmu:tts:voice"); // 研究者可钉死某音色(设备本地)
  const vs = window.speechSynthesis.getVoices().filter((v) => v.localService && (norm(v.lang).startsWith("zh") || norm(v.lang).startsWith("cmn")));
  const score = (v: SpeechSynthesisVoice) => {
    if (pinned && v.name === pinned) return -1;
    const l = norm(v.lang);
    const langBase = l.startsWith("zh-cn") || l.startsWith("cmn") ? 0 : l.startsWith("zh-tw") ? 40 : 60;
    const base = v.name.split(" (")[0].trim();
    const nameAdj = PREFERRED_VOICES.some((p) => v.name.includes(p)) ? 0 : NOVELTY_VOICES.has(base) ? 20 : 10;
    // 同名高级/增强变体(研究者在系统设置下载后出现)音质远好于紧凑版:同层再压一档,自动优先
    const qualityAdj = /高级|增强|Enhanced|Premium|Superior/i.test(v.name) ? -5 : 0;
    return langBase + nameAdj + qualityAdj;
  };
  return [...vs].sort((a, b) => score(a) - score(b))[0] ?? null;
}

export function currentVoiceName(): string | null {
  if (neural === "on") return ttsEngineLabel(activeEngineTag) ?? "小语·服务端语音";
  if (typeof window === "undefined" || !("speechSynthesis" in window)) return null;
  return pickLocalZhVoice()?.name ?? null;
}

function utterItem(item: Line, g: number): void {
  if (item.contextKey !== contextState.activeContextKey) { busy = false; return; }
  if (typeof window === "undefined" || !("speechSynthesis" in window)) {
    busy = false;
    notifySettled(item.tag, "failed");   // 这台设备根本不会出声,等它没有意义
    return;
  }
  const voice = pickLocalZhVoice();
  if (!voice) {
    // 语音表未就绪/本机无中文语音:退回队首等 voiceschanged/触屏补读,绝不落到联网语音。
    // 但要立刻收口:等待方(第1周多轮的开麦闸)不能为一句可能永远不会响的话
    // 一直挂着麦克风——屏上的字已经在了,流程该往下走。
    queue.unshift(item);
    busy = false;
    notifySettled(item.tag, "failed");
    return;
  }
  const u = new SpeechSynthesisUtterance(item.text);
  u.voice = voice;
  u.lang = voice.lang;
  u.rate = 0.85;   // 老人节奏:略慢(神经引擎在服务端用 length_scale 达成同一意图)
  u.volume = 1;
  u.onstart = () => audit(`start@${voice.name}`, item.tag, item.text);
  u.onend = () => {
    if (g !== gen || item.contextKey !== contextState.activeContextKey) return;
    audit("end", item.tag, item.text);
    if (pending?.text === item.text && pending.contextKey === item.contextKey) pending = null;
    busy = false;
    notifySettled(item.tag, "played");
    driveQueue();
  };
  u.onerror = (e) => {
    audit(`error:${e.error}`, item.tag, item.text);
    if (g !== gen || item.contextKey !== contextState.activeContextKey) return;
    if (e.error === "not-allowed") { queue.unshift(item); busy = false; return; } // 触屏后补读
    busy = false;
    notifySettled(item.tag, "failed");
    driveQueue();                                   // 其他错误:跳过这句,队列不停摆
  };
  window.speechSynthesis.speak(u);
}

// ---------------- 对外 API ----------------
// enqueue=false(默认):打断当前朗读换新话(换环节/换话术)。
// enqueue=true:排在当前朗读之后(线索——恢复/跳题场景问句与线索同帧到达,不许线索掐掉问句)。
export function speak(text: string, opts: {
  contextKey: TtsPlaybackContextKey;
  tag?: string;
  enqueue?: boolean;
  /** 按行取音端点(第1周回应句)。text 仅作屏显/回退朗读,不进任何合成请求。 */
  fetchPath?: string;
}): void {
  if (typeof window === "undefined" || !text) return;
  const tag = opts?.tag ?? "";
  const contextKey = opts.contextKey;
  if (!contextKey || contextState.activeContextKey !== contextKey) return;
  const line: Line = { text, tag, contextKey, fetchPath: opts?.fetchPath };
  contextState = rememberTtsLine(contextState, line);
  if (!enabled) return;
  // 轮询、StrictMode 或同状态重挂载都不应把同一句重复塞进播放队列。
  // tag 绑定具体题目/环节，因此只去重“同内容且同环节”的在途请求。
  if (
    pending?.text === text
    && pending.tag === tag
    && pending.contextKey === contextKey
    && (busy || queue.some((queued) => queued.text === text
      && queued.tag === tag && queued.contextKey === contextKey))
  ) return;
  pending = line;
  if (!opts?.enqueue) {
    interrupt();
    queue = [line];
  } else {
    queue.push(line);
  }
  driveQueue(); // busy(含 fetch 在途)时这里是空操作,由在途句的终态回调续驱
}

// 试听:只重读这一个床旁上下文已屏显的原文。新受试者/新题未就绪时
// 保持静音，不用上一位话术，也不用无来源的通用句替代。
export function speakSample(contextKey: TtsPlaybackContextKey | null): void {
  const line = replayLineForContext(contextState, contextKey);
  if (!line) return;
  // 回放必须原样带上取音通道:LLM 现编句丢了 fetchPath 会被当自由文本
  // POST 进 /tts/speak——白名单拒它,老人听到的就换成另一副本地嗓子。
  speak(line.text, {
    contextKey: line.contextKey, tag: line.tag,
    fetchPath: (line as { fetchPath?: string }).fetchPath,
  });
}

/**
 * Bind the only legacy TTS context allowed to speak.  Any transition (including
 * active -> null -> same key across pause/resume) revokes queued bytes and replay
 * memory before the next paint.
 */
export function setTtsContext(contextKey: TtsPlaybackContextKey | null): void {
  const transition = transitionTtsContext(contextState, contextKey);
  contextState = transition.state;
  if (!transition.changed) return;
  pending = null;
  queue = [];
  interrupt();
}

export function clearTtsContext(): void {
  setTtsContext(null);
}

export function stopSpeaking(): void {
  pending = null;
  queue = [];
  interrupt();
}

/** 老人端「点一下，接着听」覆层据此出现/消失(detail: { needed: boolean })。 */
export const PLAYBACK_NEEDS_GESTURE_EVENT = "nmu:playback-needs-gesture";

export function announceGestureNeeded(needed: boolean): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(PLAYBACK_NEEDS_GESTURE_EVENT, { detail: { needed } }));
}

// 本模块自己亮过的覆层才由本模块收:interrupt() 每次换话都会跑,若无条件广播 false,
// 会把自动带练执行器正在等手势的覆层一并收掉(两条播放链共用同一个事件)。
let ttsWaitingForGesture = false;
function ttsGesture(needed: boolean): void {
  if (ttsWaitingForGesture === needed) return;
  ttsWaitingForGesture = needed;
  announceGestureNeeded(needed);
}

// 8 kHz 单声道 8 个采样的静音 WAV(60 字节)。只用来在用户手势里「解锁」播放元素:
// 有些浏览器只认在手势里放过声的那个元素,之后同一元素上的 play() 才不要手势。
export const SILENT_WAV_DATA_URI =
  "data:audio/wav;base64,UklGRjQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YRAAAAAAAAAAAAAAAAAAAAAAAAAA";

/** 必须在用户手势处理器里同步调用;正在出声时不动它。失败静默(解锁只是补强)。 */
export function unlockTtsPlayback(): void {
  if (!audioEl || busy || !audioEl.paused) return;
  audioEl.onended = null;
  audioEl.onerror = null;
  audioEl.src = SILENT_WAV_DATA_URI;
  void audioEl.play().catch(() => { /* 没解锁成,pointerdown 补读那条路还在 */ });
}

// 补读:语音表就绪(voiceschanged)或获得用户激活(首次触屏)时,把没读出去的接着读。
// 队列有货(被拒句已退回队首)优先驱队列;队列空才用 pending 单槽兜底。
function retryPending(): void {
  if (!enabled || busy) return;
  if ("speechSynthesis" in window && (window.speechSynthesis.speaking || window.speechSynthesis.pending)) return;
  if (queue.length === 0) {
    if (!pending) return;
    if (pending.contextKey !== contextState.activeContextKey) {
      pending = null;
      return;
    }
    queue.push(pending);
  }
  driveQueue();
}

if (typeof window !== "undefined" && "speechSynthesis" in window) {
  window.speechSynthesis.getVoices(); // 触发 Chrome 异步加载
  window.speechSynthesis.onvoiceschanged = retryPending;
  document.addEventListener("pointerdown", () => retryPending());
}
