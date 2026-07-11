// 老人端本地语音(小语开口),两级链路、零云端:
// ① 本地神经 TTS(后端 /tts/speak,piper onnx,CPU 实时,自然人声)——同源请求,文本不出机器;
// ② 回退:浏览器 speechSynthesis,只用 localService 中文语音(Chrome "Google 普通话" 是联网合成,
//    文本会出机器——宁可静音也不用;找不到本机中文语音就什么都不读)。
// ★只朗读屏上已显示的题库/脚本原文,不合成任何新话术(单一内容源不破)。
//
// 播放核心是显式状态机,防三类并发事故(复审确证过的真事故):
// - busy 在取队即置位(fetch 在途也算忙)——否则同帧"问句+线索"两次 speak 并发抢同一 audio 元素;
// - gen 世代计数,打断即递增——迟到的旧 fetch/旧 play 回调看到世代不符就自弃,
//   绝不把已被换掉的旧句用系统语音"还魂"叠在新句上;
// - 播放被拒(无用户激活)把句子退回队首——触屏补读按原顺序读"问句→线索",不丢主句。

let enabled = localStorage.getItem("nmu:tts") !== "off";
let pending: { text: string; tag: string } | null = null;
let lastText: { text: string; tag: string } | null = null; // 最近一次屏显文本(试听重读用,读的仍是屏上原文)

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
type Line = { text: string; tag: string };
let neural: "unknown" | "on" | "off" = "unknown"; // 后端引擎可用性,204 一次探明整会话回退
const audioEl = typeof Audio !== "undefined" ? new Audio() : null;
let queue: Line[] = [];
let busy = false;   // 取队即置位:fetch/play 在途都算忙,同帧第二句只能排队
let gen = 0;        // 世代:打断/停止即 +1,旧世代的一切异步续体自弃
let curUrl: string | null = null; // 正在播的 blobURL:打断路径也要回收,不靠 onended

function interrupt(): void {
  gen += 1;
  busy = false;
  if (audioEl && !audioEl.paused) audioEl.pause();
  if (curUrl) { URL.revokeObjectURL(curUrl); curUrl = null; }
  if (typeof window !== "undefined" && "speechSynthesis" in window) window.speechSynthesis.cancel();
}

function driveQueue(): void {
  if (busy) return;
  const next = queue.shift();
  if (!next) return;
  busy = true;
  void playItem(next, gen);
}

async function playItem(item: Line, g: number): Promise<void> {
  // ① 神经路径
  if (audioEl && neural !== "off") {
    let url: string | null = null;
    try {
      const res = await fetch(`/tts/speak?text=${encodeURIComponent(item.text)}`);
      if (g !== gen) return;                       // 已被打断:新句自会驱动,旧续体退场
      if (res.status === 204) { neural = "off"; }
      else if (res.ok) {
        neural = "on";
        url = URL.createObjectURL(await res.blob());
        if (g !== gen) { URL.revokeObjectURL(url); return; }
        const u = url;
        audioEl.src = u;
        curUrl = u;
        audioEl.onended = () => {
          URL.revokeObjectURL(u);
          if (curUrl === u) curUrl = null;
          if (g !== gen) return;
          audit("end", item.tag, item.text);
          if (pending?.text === item.text) pending = null;
          busy = false;
          driveQueue();
        };
        audioEl.onerror = () => {
          URL.revokeObjectURL(u);
          if (curUrl === u) curUrl = null;
          if (g !== gen) return;
          audit("error:audio", item.tag, item.text);
          busy = false;
          driveQueue();
        };
        await audioEl.play();
        audit("start@piper", item.tag, item.text);
        return;                                     // 播放中,后续由 onended 续驱
      }
      // !res.ok(非 204):这一句落到 ② 回退
    } catch (e) {
      if (url) { URL.revokeObjectURL(url); if (curUrl === url) curUrl = null; }
      if (g !== gen) return;
      if (e instanceof DOMException && e.name === "NotAllowedError") {
        // 无用户激活:句子退回队首,触屏后按原顺序补读(问句仍在线索前面)
        audit("error:not-allowed", item.tag, item.text);
        queue.unshift(item);
        busy = false;
        return;
      }
      if (e instanceof DOMException && e.name === "AbortError") {
        // 被打断(pause/换 src):新句已接管,旧句静默退场,绝不回退系统语音还魂。
        // 同世代 Abort 理论不可达,防御性放掉忙位以免队列停摆。
        audit("error:aborted", item.tag, item.text);
        if (g === gen) { busy = false; driveQueue(); }
        return;
      }
      // 网络/后端异常:这一句落到 ② 回退
    }
  }
  if (g !== gen) return;
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
  if (neural === "on") return "小语·本地神经(华言)";
  if (typeof window === "undefined" || !("speechSynthesis" in window)) return null;
  return pickLocalZhVoice()?.name ?? null;
}

function utterItem(item: Line, g: number): void {
  if (typeof window === "undefined" || !("speechSynthesis" in window)) { busy = false; return; }
  const voice = pickLocalZhVoice();
  if (!voice) {
    // 语音表未就绪/本机无中文语音:退回队首等 voiceschanged/触屏补读,绝不落到联网语音
    queue.unshift(item);
    busy = false;
    return;
  }
  const u = new SpeechSynthesisUtterance(item.text);
  u.voice = voice;
  u.lang = voice.lang;
  u.rate = 0.85;   // 老人节奏:略慢(神经引擎在服务端用 length_scale 达成同一意图)
  u.volume = 1;
  u.onstart = () => audit(`start@${voice.name}`, item.tag, item.text);
  u.onend = () => {
    if (g !== gen) return;
    audit("end", item.tag, item.text);
    if (pending?.text === item.text) pending = null;
    busy = false;
    driveQueue();
  };
  u.onerror = (e) => {
    audit(`error:${e.error}`, item.tag, item.text);
    if (g !== gen) return;
    if (e.error === "not-allowed") { queue.unshift(item); busy = false; return; } // 触屏后补读
    busy = false;
    driveQueue();                                   // 其他错误:跳过这句,队列不停摆
  };
  window.speechSynthesis.speak(u);
}

// ---------------- 对外 API ----------------
// enqueue=false(默认):打断当前朗读换新话(换环节/换话术)。
// enqueue=true:排在当前朗读之后(线索——恢复/跳题场景问句与线索同帧到达,不许线索掐掉问句)。
export function speak(text: string, opts?: { tag?: string; enqueue?: boolean }): void {
  if (typeof window === "undefined" || !text) return;
  const tag = opts?.tag ?? "";
  lastText = { text, tag };
  if (!enabled) return;
  pending = { text, tag };
  if (!opts?.enqueue) {
    interrupt();
    queue = [{ text, tag }];
  } else {
    queue.push({ text, tag });
  }
  driveQueue(); // busy(含 fetch 在途)时这里是空操作,由在途句的终态回调续驱
}

// 试听:重读当前屏显文本(开关打开那一下既给了用户激活,也当场验证音色/音量)。
// 无屏显历史时读"您好"——老人端待机屏上的原文。
export function speakSample(): void {
  const t = lastText ?? { text: "您好", tag: "sample" };
  speak(t.text, { tag: t.tag });
}

export function stopSpeaking(): void {
  pending = null;
  queue = [];
  interrupt();
}

// 补读:语音表就绪(voiceschanged)或获得用户激活(首次触屏)时,把没读出去的接着读。
// 队列有货(被拒句已退回队首)优先驱队列;队列空才用 pending 单槽兜底。
function retryPending(): void {
  if (!enabled || busy) return;
  if ("speechSynthesis" in window && (window.speechSynthesis.speaking || window.speechSynthesis.pending)) return;
  if (queue.length === 0) {
    if (!pending) return;
    queue.push({ text: pending.text, tag: pending.tag });
  }
  driveQueue();
}

if (typeof window !== "undefined" && "speechSynthesis" in window) {
  window.speechSynthesis.getVoices(); // 触发 Chrome 异步加载
  window.speechSynthesis.onvoiceschanged = retryPending;
  document.addEventListener("pointerdown", () => retryPending());
}
