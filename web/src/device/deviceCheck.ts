/**
 * 设备基础检查——只记录当前基础技术状况。它不能判定设备
 * 已可用于训练、正式研究或养老院。
 *
 * 审计 §11.5 的设备矩阵(目标手机/平板/麦克风/噪声/断网)只能在真机上验;
 * 这个模块的职责是让那次验收有东西可跑:研究者到现场打开 /device-check,
 * 几十秒拿到一份绿/黄/红清单和一段可复制进记录的文本。
 *
 * 判定逻辑与浏览器 API 完全解耦(全部经 DeviceCheckDependencies 注入),
 * 阈值都是工程启发而非标准——各机型的 AGC 会把"安静"抬到不同电平,
 * 所以每个阈值旁边写了它想抓住的具体故障,错了就照着改。
 */

export type CheckStatus = "pass" | "warn" | "fail";

export interface CheckResult {
  id: string;
  title: string;
  status: CheckStatus;
  detail: string;
}

export interface MicrophoneInfo {
  label: string;
  sampleRate: number | null;
  channelCount: number | null;
  echoCancellation: boolean | null;
  noiseSuppression: boolean | null;
  autoGainControl: boolean | null;
}

export interface NoiseSample {
  rmsDbfs: number;
  peakDbfs: number;
}

export interface DeviceCheckDependencies {
  secureContext: boolean;
  userAgent: string;
  screen: { width: number; height: number; pixelRatio: number };
  mediaDevicesAvailable: boolean;
  mediaRecorderAvailable: boolean;
  webLocksAvailable: boolean;
  indexedDbAvailable: boolean;
  broadcastChannelAvailable: boolean;
  blobUrlAvailable: boolean;
  currentBuildId: string;
  fetchDeployedBuildId(): Promise<string | null>;
  isTypeSupported(mime: string): boolean;
  /** getUserMedia + 读 track 设置;流由依赖方持有,供后续 sampleNoise 用。 */
  acquireMicrophone(): Promise<MicrophoneInfo>;
  /** 在已持有的麦克风流上采样 N 秒,返回 RMS/峰值(dBFS,≤0)。 */
  sampleNoise(seconds: number): Promise<NoiseSample>;
  releaseMicrophone(): void;
  audioContextSampleRate(): number | null;
  /** 必须在用户手势之后调;返回 resume 后的状态。 */
  resumeAudio(): Promise<"running" | "suspended" | "unavailable">;
  speechSynthesisVoices(): { total: number; chinese: number } | null;
  fetchImpl(input: string, init?: RequestInit): Promise<{
    ok: boolean; status: number; headers: { get(name: string): string | null };
  }>;
  /** 墙钟 epoch 毫秒(Date.now 口径)。时钟偏移要和服务器 Date 头同量纲——
   * 拿 performance.now() 来实现会把偏移算成整个 Unix 时间戳。 */
  now(): number;
  storageEstimate(): Promise<{ quotaBytes: number | null; usageBytes: number | null }>;
}

// 生产录音链的真实口径(audio/recorder.ts):首选 audio/webm,不支持则交给浏览器
// 默认容器。这里列出的候选只用于把"浏览器会给什么"写进报告。
const RECORDER_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/mp4",
];

export function checkRuntime(deps: DeviceCheckDependencies): CheckResult {
  const id = "runtime";
  const title = "运行环境";
  if (!deps.secureContext) {
    return { id, title, status: "fail",
      detail: "本页不是加密链接（HTTPS），浏览器会拒绝开麦克风。请用 https 开头的地址访问。" };
  }
  if (!deps.mediaDevicesAvailable) {
    return { id, title, status: "fail", detail: "这个浏览器不支持录音（缺 mediaDevices），请换新版 Chrome、Edge 或 Safari。" };
  }
  if (!deps.mediaRecorderAvailable) {
    return { id, title, status: "fail", detail: "这个浏览器不支持录音（缺 MediaRecorder），请换新版浏览器。" };
  }
  return { id, title, status: "pass", detail: "HTTPS + 录音 API 齐全" };
}

/**
 * 生产录音链不只需要开麦克风：Web Locks 防止多标签页同时持有
 * 媒体，IndexedDB 承载断网暂存与待上传回执，Blob URL 用于本地音频播放。
 * BroadcastChannel 只是同机快速唤醒提示，缺失时仍可靠 localStorage + 服务器轮询安全降级。
 */
export function checkBrowserSafetyFoundation(deps: DeviceCheckDependencies): CheckResult {
  const id = "browser-safety";
  const title = "浏览器安全能力";
  const missing = [
    [deps.webLocksAvailable, "Web Locks"],
    [deps.indexedDbAvailable, "IndexedDB"],
    [deps.blobUrlAvailable, "Blob URL"],
  ].filter(([available]) => !available).map(([, name]) => name);
  if (missing.length > 0) {
    return {
      id,
      title,
      status: "fail",
      detail: `缺少 ${missing.join("、")}；无法保证多窗口互斥、断网暂存和安全停止，请换新版浏览器。`,
    };
  }
  if (!deps.broadcastChannelAvailable) {
    return {
      id,
      title,
      status: "warn",
      detail: "BroadcastChannel 不可用；将使用 localStorage + 服务器轮询安全降级，现场需验证多窗口停止。",
    };
  }
  return { id, title, status: "pass", detail: "多窗口互斥、本地暂存、快速唤醒与音频播放 API 齐全" };
}

export async function checkBuildBinding(deps: DeviceCheckDependencies): Promise<CheckResult> {
  const id = "build-binding";
  const title = "页面版本";
  const current = deps.currentBuildId.trim();
  if (!/^\d{10,}$/.test(current)) {
    return { id, title, status: "fail", detail: "本页缺少可核对的版本号，不能生成通过记录。" };
  }
  let deployed: string | null = null;
  try { deployed = (await deps.fetchDeployedBuildId())?.trim() ?? null; }
  catch { /* 统一落成 fail */ }
  if (deployed === null || !/^\d{10,}$/.test(deployed)) {
    return { id, title, status: "fail", detail: "服务器没有返回有效版本号；本页可能不是正式发布版本。" };
  }
  if (deployed !== current) {
    return { id, title, status: "fail", detail: `本页版本 ${current} 与服务器 ${deployed} 不一致，请刷新后重查。` };
  }
  return { id, title, status: "pass", detail: current };
}

export function mapMicrophoneError(error: unknown): string {
  const name = (error as { name?: string } | null)?.name ?? "";
  switch (name) {
    case "NotAllowedError":
      return "麦克风权限被拒。到浏览器设置里允许本站使用麦克风，然后重新检查。";
    case "NotFoundError":
      return "找不到麦克风设备。确认设备有麦克风且未被系统禁用。";
    case "NotReadableError":
      return "麦克风被其它应用占用（微信通话、录音应用等），关掉它们再试。";
    default:
      return `开麦克风失败：${String((error as Error | null)?.message ?? error)}`;
  }
}

/** 采样中途失败(权限被收回/设备被占用等)也要给中文说明,不能直出英文异常名。 */
export function describeSampleFailure(error: unknown): string {
  const name = (error as { name?: string } | null)?.name ?? "";
  if (name === "NotAllowedError" || name === "NotFoundError" || name === "NotReadableError") {
    return `采样失败：${mapMicrophoneError(error)}`;
  }
  return `采样失败（原始错误：${String((error as Error | null)?.message ?? error)}）`;
}

export function checkRecorderCodec(deps: DeviceCheckDependencies): CheckResult {
  const id = "codec";
  const title = "录音编码";
  const supported = RECORDER_CANDIDATES.filter((mime) => {
    try { return deps.isTypeSupported(mime); } catch { return false; }
  });
  if (supported.some((mime) => mime.startsWith("audio/webm"))) {
    return { id, title, status: "pass", detail: `与正式录音格式一致（audio/webm）。支持：${supported.join("、")}` };
  }
  if (supported.length > 0) {
    return { id, title, status: "warn",
      detail: `不支持 audio/webm，录音会用浏览器默认格式（${supported.join("、")}）。` +
              "必须在这台设备上做完录音、回放和上传的真机验收。" };
  }
  return { id, title, status: "fail", detail: "常见音频格式一个都不支持，录音大概率不可用。" };
}

export function classifySampleRate(rate: number | null): CheckResult {
  const id = "sample-rate";
  const title = "采样率";
  if (rate === null) {
    return { id, title, status: "warn", detail: "浏览器不报告采样率；以录音回放的听感为准。" };
  }
  if (rate < 16000) {
    return { id, title, status: "fail",
      detail: `${rate} Hz，低于语音识别要求的 16 kHz，识别质量会明显变差。` };
  }
  if (rate < 32000) {
    return { id, title, status: "warn", detail: `${rate} Hz，偏低（常见设备是 44.1/48 kHz），录音识别效果要在真机上验过。` };
  }
  return { id, title, status: "pass", detail: `${rate} Hz` };
}

/**
 * 安静段判定。想抓的故障,按阈值从下往上:
 *   峰值 < -70 dBFS —— 采到的几乎是数字静音:麦克风坏了/被系统静音/权限给了假流。
 *   RMS  > -30 dBFS —— 环境明显嘈杂(空调机房/临街开窗),会污染转写。
 *   RMS  > -45 dBFS —— 有些底噪,提醒关注但不拦。
 */
export function classifyNoiseFloor(sample: NoiseSample): CheckResult {
  const id = "noise-floor";
  const title = "环境噪声";
  const rms = Math.round(sample.rmsDbfs);
  if (sample.peakDbfs < -70) {
    return { id, title, status: "fail",
      detail: `电平几乎为零（峰值 ${Math.round(sample.peakDbfs)} dBFS）——` +
              "麦克风可能被静音或损坏。检查系统音量面板里的输入设备。" };
  }
  if (sample.rmsDbfs > -30) {
    return { id, title, status: "fail", detail: `环境明显嘈杂（RMS ${rms} dBFS），换安静房间。` };
  }
  if (sample.rmsDbfs > -45) {
    return { id, title, status: "warn", detail: `有些底噪（RMS ${rms} dBFS），尽量关空调、关窗。` };
  }
  return { id, title, status: "pass", detail: `安静（RMS ${rms} dBFS）` };
}

/** 说话段判定:请研究者正常说一句话,峰值要能盖过 -35 dBFS,否则等于收不到人声。 */
export function classifySpeechLevel(sample: NoiseSample): CheckResult {
  const id = "speech-level";
  const title = "人声拾取";
  const peak = Math.round(sample.peakDbfs);
  if (sample.peakDbfs > -35) {
    return { id, title, status: "pass", detail: `人声清晰（峰值 ${peak} dBFS）` };
  }
  if (sample.peakDbfs > -50) {
    return { id, title, status: "warn",
      detail: `人声偏弱（峰值 ${peak} dBFS）。老人声音更小——让设备离受试者近一些。` };
  }
  return { id, title, status: "fail",
    detail: `几乎没收到人声（峰值 ${peak} dBFS）。确认对着正确的麦克风说话。` };
}

export function classifyNetwork(status: number | null, latencyMs: number | null,
                                clockSkewSeconds: number | null): CheckResult {
  const id = "network";
  const title = "服务器连通";
  if (status === null || latencyMs === null) {
    return { id, title, status: "fail", detail: "连不上服务器。检查网络，然后看页面顶部的联网状态。" };
  }
  if (status !== 200) {
    return { id, title, status: "fail", detail: `服务器返回 ${status}，平台可能不可用。` };
  }
  const skewNote = clockSkewSeconds !== null && Math.abs(clockSkewSeconds) > 120
    ? `；设备时钟与服务器差 ${Math.round(Math.abs(clockSkewSeconds))} 秒，记录一律以服务器时间为准`
    : "";
  if (latencyMs > 1500) {
    return { id, title, status: "warn", detail: `能连上但偏慢（${Math.round(latencyMs)} ms）${skewNote}` };
  }
  return { id, title, status: "pass", detail: `${Math.round(latencyMs)} ms${skewNote}` };
}

export function classifyStorage(quotaBytes: number | null, usageBytes: number | null): CheckResult {
  const id = "storage";
  const title = "本地存储";
  if (quotaBytes === null) {
    return { id, title, status: "warn", detail: "浏览器不报告存储空间；录音暂存空间未知。" };
  }
  const freeMb = Math.floor((quotaBytes - (usageBytes ?? 0)) / (1024 * 1024));
  if (freeMb < 50) {
    return { id, title, status: "fail",
      detail: `可用仅 ${freeMb} MB——断网时录音暂存会很快写满。清理浏览器存储。` };
  }
  if (freeMb < 200) {
    return { id, title, status: "warn", detail: `可用 ${freeMb} MB，偏紧；留意时间长的场次。` };
  }
  return { id, title, status: "pass", detail: `可用约 ${freeMb} MB` };
}

export function classifyScreen(screen: { width: number; height: number; pixelRatio: number }): CheckResult {
  const id = "screen";
  const title = "屏幕显示";
  const shorter = Math.min(screen.width, screen.height);
  const spec = `${screen.width}×${screen.height} @${screen.pixelRatio}x`;
  if (shorter < 500) {
    // 屏幕尺寸是现场体验门禁，这里只提示而不代替人工验收。
    return { id, title, status: "warn", detail: `${spec}——屏幕偏小（像手机），题图和大字排版会挤，建议用平板。` };
  }
  return { id, title, status: "pass", detail: spec };
}

export function checkAudioOutput(state: "running" | "suspended" | "unavailable"): CheckResult {
  const id = "audio-output";
  const title = "音频播放";
  if (state === "running") {
    return { id, title, status: "pass", detail: "声音通路已打开（稍后会放一声提示音，请人工确认听到）" };
  }
  if (state === "suspended") {
    return { id, title, status: "fail",
      detail: "浏览器暂时不允许本页出声，语音播报会没声。请重新点「开始检查」再试。" };
  }
  return { id, title, status: "fail", detail: "这个浏览器不支持网页音频，语音播报不可用。" };
}

export function checkFallbackVoice(voices: { total: number; chinese: number } | null): CheckResult {
  const id = "fallback-voice";
  const title = "兜底语音";
  // 这只是 generic/manual 页面可能用到的系统语音提示；
  // 服务器托管的自动流程不使用浏览器语音回退。
  if (voices === null || voices.total === 0) {
    return { id, title, status: "warn", detail: "浏览器没有系统语音；自动训练不受影响，但手动操作页面不能用系统语音提示。" };
  }
  if (voices.chinese === 0) {
    return { id, title, status: "warn",
      detail: `系统语音 ${voices.total} 个但没有中文；手动操作页面的中文提示不可依赖。` };
  }
  return { id, title, status: "pass", detail: `中文系统语音 ${voices.chinese} 个` };
}

export interface MicrophoneOutcome {
  result: CheckResult;
  info: MicrophoneInfo | null;
}

export async function checkMicrophone(deps: DeviceCheckDependencies): Promise<MicrophoneOutcome> {
  const id = "microphone";
  const title = "麦克风";
  try {
    const info = await deps.acquireMicrophone();
    const parts = [info.label || "默认设备"];
    if (info.echoCancellation !== null) parts.push(`回声消除${info.echoCancellation ? "开" : "关"}`);
    if (info.noiseSuppression !== null) parts.push(`降噪${info.noiseSuppression ? "开" : "关"}`);
    if (info.autoGainControl !== null) parts.push(`自动增益${info.autoGainControl ? "开" : "关"}`);
    return { result: { id, title, status: "pass", detail: parts.join(" · ") }, info };
  } catch (error) {
    return { result: { id, title, status: "fail", detail: mapMicrophoneError(error) }, info: null };
  }
}

export type Verdict = "basic-pass" | "basic-pass-with-warnings" | "basic-fail" | "incomplete";

export const REQUIRED_CHECK_IDS = Object.freeze([
  "runtime", "build-binding", "browser-safety", "microphone", "codec", "sample-rate",
  "audio-output", "fallback-voice", "network", "storage", "screen",
  "noise-floor", "speech-level", "speaker",
] as const);

export function summarize(results: CheckResult[]): Verdict {
  const ids = results.map((result) => result.id);
  const unique = new Set(ids);
  if (unique.size !== ids.length
      || REQUIRED_CHECK_IDS.some((id) => !unique.has(id))
      || ids.some((id) => !(REQUIRED_CHECK_IDS as readonly string[]).includes(id))) {
    return "incomplete";
  }
  if (results.some((r) => r.status === "fail")) return "basic-fail";
  if (results.some((r) => r.status === "warn")) return "basic-pass-with-warnings";
  return "basic-pass";
}

export function classifySpeakerConfirmation(beepPlayed: boolean, heard: boolean): CheckResult {
  if (beepPlayed && heard) {
    return { id: "speaker", title: "扬声器", status: "pass", detail: "人工确认听到了提示音" };
  }
  return {
    id: "speaker",
    title: "扬声器",
    status: "fail",
    detail: beepPlayed
      ? "没听到提示音——检查系统音量和静音开关，语音播报会没声。"
      : "浏览器没有成功播放提示音，不能人工点选为通过。",
  };
}

/** 可复制进设备矩阵记录的纯文本报告。一行一个结论,机器和人都好读。 */
export function formatReport(userAgent: string, results: CheckResult[],
                             verdict: Verdict, finishedAtIso: string): string {
  const label = {
    "basic-pass": "基础检查通过，仍需真机验收",
    "basic-pass-with-warnings": "基础检查有警告，不得直接使用",
    "basic-fail": "基础检查不通过",
    incomplete: "检查未完成，不得使用",
  }[verdict];
  const mark = { pass: "✓", warn: "△", fail: "✗" };
  const lines = [
    `设备基础检查 ${finishedAtIso} → ${label}`,
    `UA: ${userAgent}`,
    ...results.map((r) => `${mark[r.status]} ${r.title}: ${r.detail}`),
  ];
  return lines.join("\n");
}

export interface AutoCheckRun {
  results: CheckResult[];
  micInfo: MicrophoneInfo | null;
}

/**
 * 自动段编排:除了两段需要现场配合的电平采样(安静/说话)和人工确认的提示音,
 * 其余检查一次跑完。任何一步自身抛错都落成该项 fail,绝不让整页白屏;
 * 麦克风流的释放由调用方在整个流程(含噪声采样)结束后负责。
 */
export async function runAutoChecks(
  deps: DeviceCheckDependencies,
  onResult: (result: CheckResult) => void,
): Promise<AutoCheckRun> {
  const results: CheckResult[] = [];
  const push = (result: CheckResult) => { results.push(result); onResult(result); };

  const guarded = async (id: string, title: string, run: () => Promise<CheckResult> | CheckResult) => {
    try {
      push(await run());
    } catch (error) {
      push({ id, title, status: "fail", detail: `检查自身出错：${String(error)}` });
    }
  };

  await guarded("runtime", "运行环境", () => checkRuntime(deps));
  await guarded("build-binding", "页面版本", () => checkBuildBinding(deps));
  await guarded("browser-safety", "浏览器安全能力", () => checkBrowserSafetyFoundation(deps));

  let micInfo: MicrophoneInfo | null = null;
  if (results.slice(0, 3).every((result) => result.status !== "fail")) {
    await guarded("microphone", "麦克风", async () => {
      const outcome = await checkMicrophone(deps);
      micInfo = outcome.info;
      return outcome.result;
    });
  } else {
    push({ id: "microphone", title: "麦克风", status: "fail", detail: "运行环境不满足，未尝试开麦。" });
  }

  await guarded("codec", "录音编码", () => checkRecorderCodec(deps));
  await guarded("sample-rate", "采样率", () => {
    const info = micInfo as MicrophoneInfo | null;
    return classifySampleRate(info?.sampleRate ?? deps.audioContextSampleRate());
  });
  await guarded("audio-output", "音频播放", async () => checkAudioOutput(await deps.resumeAudio()));
  await guarded("fallback-voice", "兜底语音", () => checkFallbackVoice(deps.speechSynthesisVoices()));
  await guarded("network", "服务器连通", async () => {
    const started = deps.now();
    try {
      const response = await deps.fetchImpl("/health", { cache: "no-store" });
      const latency = deps.now() - started;
      const dateHeader = response.headers.get("date");
      // Date 头精度是整秒,往返再各吃掉一截;偏移小于几秒都当作零看待。
      const skew = dateHeader
        ? (new Date(dateHeader).getTime() - (started + latency / 2)) / 1000
        : null;
      return classifyNetwork(response.status, latency, skew);
    } catch {
      return classifyNetwork(null, null, null);
    }
  });
  await guarded("storage", "本地存储", async () => {
    const estimate = await deps.storageEstimate();
    return classifyStorage(estimate.quotaBytes, estimate.usageBytes);
  });
  await guarded("screen", "屏幕显示", () => classifyScreen(deps.screen));

  return { results, micInfo };
}
