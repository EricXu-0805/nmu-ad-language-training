// 老人端本地语音(小语开口):浏览器 speechSynthesis,声音来自操作系统本机语音包。
// ★零云端硬约束:只用 localService 中文语音;Chrome 的 "Google 普通话" 是联网合成(文本会出机器),
//   宁可静音也不用——找不到本机中文语音就什么都不读。
// ★只朗读屏上已显示的题库/脚本原文,不合成任何新话术(单一内容源不破)。
// 可靠性:Chrome 冷启动 getVoices() 为空(异步)、无用户激活时 speak 被静默拒(not-allowed)——
//   两种失败都把文本留在 pending,等 voiceschanged / 首次触屏后补读,而不是永久丢句。

let enabled = localStorage.getItem("nmu:tts") !== "off";
let pending: { text: string; tag: string } | null = null;

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

// 普通话优先:zh-CN/cmn > 其他 zh(macOS 常见 Meijia zh-TW / Sinji zh-HK 粤语排在婷婷前,
// 取"第一个 zh"可能给南京老人读粤语)。仍严格限 localService。
function pickLocalZhVoice(): SpeechSynthesisVoice | null {
  const norm = (l: string) => l.replace("_", "-").toLowerCase();
  const vs = window.speechSynthesis.getVoices().filter((v) => v.localService && (norm(v.lang).startsWith("zh") || norm(v.lang).startsWith("cmn")));
  const score = (v: SpeechSynthesisVoice) => {
    const l = norm(v.lang);
    return l.startsWith("zh-cn") || l.startsWith("cmn") ? 0 : l.startsWith("zh-tw") ? 1 : 2;
  };
  return vs.sort((a, b) => score(a) - score(b))[0] ?? null;
}

function utter(text: string, tag: string): void {
  const voice = pickLocalZhVoice();
  if (!voice) return; // 语音表未就绪或本机无中文语音:留在 pending 等补读,绝不落到联网语音
  const u = new SpeechSynthesisUtterance(text);
  u.voice = voice;
  u.lang = voice.lang;
  u.rate = 0.85;   // 老人节奏:略慢
  u.volume = 1;
  u.onstart = () => audit("start", tag, text);
  u.onend = () => { audit("end", tag, text); if (pending?.text === text) pending = null; };
  u.onerror = (e) => audit(`error:${e.error}`, tag, text); // not-allowed 等留 pending,触屏后补读
  window.speechSynthesis.speak(u);
}

// enqueue=false(默认):打断当前朗读换新话(换环节/换话术)。
// enqueue=true:排在当前朗读之后(线索——恢复/跳题场景问句与线索同帧到达,不许线索掐掉问句)。
export function speak(text: string, opts?: { tag?: string; enqueue?: boolean }): void {
  if (!enabled || !text || typeof window === "undefined" || !("speechSynthesis" in window)) return;
  const tag = opts?.tag ?? "";
  pending = { text, tag };
  if (!opts?.enqueue) window.speechSynthesis.cancel();
  utter(text, tag);
}

export function stopSpeaking(): void {
  pending = null;
  if (typeof window !== "undefined" && "speechSynthesis" in window) window.speechSynthesis.cancel();
}

// 补读:语音表就绪(voiceschanged)或获得用户激活(首次触屏)时,把最近一句没读出去的读了。
function retryPending(): void {
  if (!enabled || !pending) return;
  const s = window.speechSynthesis;
  if (s.speaking || s.pending) return; // 已在正常朗读,别重复
  utter(pending.text, pending.tag);
}

if (typeof window !== "undefined" && "speechSynthesis" in window) {
  window.speechSynthesis.getVoices(); // 触发 Chrome 异步加载
  window.speechSynthesis.onvoiceschanged = retryPending;
  document.addEventListener("pointerdown", () => retryPending());
}
