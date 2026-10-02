/**
 * 「采集设备参数」：把平板上浏览器自己说得出的参数记进验收回执，不做任何判定。
 *
 * 设备型号与资产编号仍要人工填：iPad 没有任何接口报型号（iPadOS Safari 默认报 Mac 的 UA，
 * 只有 maxTouchPoints 露馅），精简过的安卓 UA 只写「Android 10; K」，userAgentData 只有
 * Chromium 有。这里读到的 model 只是线索。
 *
 * 开麦那部分先拿与老人端共用的音频设备锁，约 1.5 秒拿不到就不开麦：老人端正在用麦克风时，
 * 验收页绝不能把它抢过来。开麦只读设置，读完立刻关，不录音。
 *
 * 一律只取白名单字段：不带 deviceId、groupId，也不带设备名称——蓝牙耳机的名称常常就是人名。
 * 任何一个接口缺失或抛错，都只把那一项记成 null，不让整次采集失败。
 */
import { acquireAudioDeviceLease, type AudioDeviceLease } from "../audio/audioDeviceLease.ts";
import { releaseMediaStreamTracks } from "../audio/recorder.ts";

export const DEVICE_LEASE_TIMEOUT_MS = 1_500;
// resume() 在浏览器不准出声时可能一直挂着；等这么久之后照实读 state。
const CONTEXT_RESUME_TIMEOUT_MS = 1_000;

// 与 audio/recorder.ts 开麦用的约束一致。
const RECORDER_CONSTRAINTS: MediaStreamConstraints = { audio: true };
// 与 patient/autopilotBrowserBargeIn.ts 开麦用的约束一致；测试从那份源码里把两边对上。
const BARGE_IN_CONSTRAINTS: MediaStreamConstraints = {
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
  video: false,
};
// recorder.ts 首选 audio/webm，不支持时交给浏览器默认容器（Safari 给的是 audio/mp4）。
const RECORDER_MIME_TYPES = ["audio/webm", "audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"];
const HIGH_ENTROPY_HINTS = ["model", "platformVersion", "fullVersionList"];
const DISPLAY_MODES = ["fullscreen", "standalone", "minimal-ui", "browser"] as const;
const TEXT_LIMIT = 300;

export type MicrophonePermissionFact = "granted" | "denied" | "prompt";
export type MicrophoneProbeStatus = "collected" | "lease-busy" | "lease-unavailable" | "no-media-devices";
const PROBE_STATUSES: readonly string[] = ["collected", "lease-busy", "lease-unavailable", "no-media-devices"];

export interface BrandVersionFact {
  brand: string;
  version: string;
}

export interface TrackSettingsFact {
  /** 新版 Chromium 可能报字符串模式（如 "all"）；提前回答检测只认 true。 */
  echoCancellation: boolean | string | null;
  noiseSuppression: boolean | null;
  autoGainControl: boolean | null;
  sampleRate: number | null;
  sampleSize: number | null;
  channelCount: number | null;
  latency: number | null;
}

export interface MicrophoneOpenFact {
  audioTrackCount: number | null;
  settings: TrackSettingsFact | null;
  /** 打不开时只记错误名（如 NotAllowedError）：错误消息里可能带设备名称。 */
  errorName: string | null;
}

export interface MicrophoneProbeFact {
  status: MicrophoneProbeStatus;
  /** 按录音的约束 { audio: true } 开一次麦读到的设置。 */
  recorder: MicrophoneOpenFact | null;
  /** 按提前回答检测的约束开一次麦读到的设置。 */
  bargeIn: MicrophoneOpenFact | null;
  bargeInAudioContextState: string | null;
  /** 开过麦之后的权限：老人端录过一次音之后，提前回答检测查到的就是它。 */
  permissionAfterOpen: MicrophonePermissionFact | null;
  /** 与提前回答检测开检测前的三道检查同口径；没开成麦（拿不到锁等）为 null。 */
  bargeInEligible: boolean | null;
}

export interface AcceptanceDeviceFacts {
  collectedAtIso: string;
  platform: string | null;
  maxTouchPoints: number | null;
  userAgentData: {
    mobile: boolean | null;
    platform: string | null;
    brands: BrandVersionFact[] | null;
    model: string | null;
    platformVersion: string | null;
    fullVersionList: BrandVersionFact[] | null;
  } | null;
  screen: {
    width: number | null;
    height: number | null;
    availWidth: number | null;
    availHeight: number | null;
    devicePixelRatio: number | null;
    viewportWidth: number | null;
    viewportHeight: number | null;
    orientation: string | null;
    displayMode: (typeof DISPLAY_MODES)[number] | null;
  };
  language: string | null;
  languages: string[] | null;
  timeZone: string | null;
  hardwareConcurrency: number | null;
  deviceMemoryGb: number | null;
  connection: {
    type: string | null;
    effectiveType: string | null;
    downlinkMbps: number | null;
    rttMs: number | null;
    saveData: boolean | null;
  } | null;
  /** 浏览器声称能请求哪些约束；只说明能请求，不说明真的开着（那要看 microphone）。 */
  supportedConstraints: {
    echoCancellation: boolean;
    noiseSuppression: boolean;
    autoGainControl: boolean;
    sampleRate: boolean;
    channelCount: boolean;
  } | null;
  microphonePermission: MicrophonePermissionFact | null;
  mediaRecorderTypes: Record<string, boolean> | null;
  audioContext: { sampleRate: number | null; baseLatency: number | null; outputLatency: number | null } | null;
  microphone: MicrophoneProbeFact;
}

interface AudioContextLike {
  readonly state: string;
  readonly sampleRate?: number;
  readonly baseLatency?: number;
  readonly outputLatency?: number;
  resume(): Promise<void>;
  close(): Promise<void>;
}

/** window 里本页要读的那一小部分。测试传假对象：缺哪一项，就是浏览器没有哪一项。 */
export interface DeviceFactEnvironment {
  navigator?: {
    platform?: string;
    maxTouchPoints?: number;
    language?: string;
    languages?: readonly string[];
    hardwareConcurrency?: number;
    deviceMemory?: number;
    connection?: { type?: string; effectiveType?: string; downlink?: number; rtt?: number; saveData?: boolean };
    userAgentData?: {
      mobile?: boolean;
      platform?: string;
      brands?: readonly unknown[];
      getHighEntropyValues?(hints: string[]): Promise<unknown>;
    };
    permissions?: { query(descriptor: PermissionDescriptor): Promise<{ state: string }> };
    mediaDevices?: {
      getSupportedConstraints?(): object;
      getUserMedia?(constraints: MediaStreamConstraints): Promise<MediaStream>;
    };
  };
  screen?: { width?: number; height?: number; availWidth?: number; availHeight?: number; orientation?: { type?: string } };
  innerWidth?: number;
  innerHeight?: number;
  devicePixelRatio?: number;
  matchMedia?(query: string): { matches: boolean };
  MediaRecorder?: { isTypeSupported?(mimeType: string): boolean };
  AudioContext?: new () => AudioContextLike;
  webkitAudioContext?: new () => AudioContextLike;
  Intl?: { DateTimeFormat(): { resolvedOptions(): { timeZone?: string } } };
}
type NavigatorFacts = DeviceFactEnvironment["navigator"];
type MediaDevicesFacts = NonNullable<NonNullable<NavigatorFacts>["mediaDevices"]>;

export interface DeviceFactPorts {
  acquireLease(signal: AbortSignal): Promise<AudioDeviceLease>;
  setTimeout(callback: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
  now(): number;
}

export const browserDeviceFactPorts: DeviceFactPorts = {
  acquireLease: (signal) => acquireAudioDeviceLease(undefined, signal),
  setTimeout: (callback, ms) => window.setTimeout(callback, ms),
  clearTimeout: (handle) => window.clearTimeout(handle as number),
  now: () => Date.now(),
};

function read<T>(get: () => T): T | undefined {
  try { return get(); } catch { return undefined; }
}

function text(value: unknown): string | null {
  return typeof value === "string" ? value.slice(0, TEXT_LIMIT) : null;
}

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function flag(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function brandList(value: unknown): BrandVersionFact[] | null {
  if (!Array.isArray(value)) return null;
  return value.slice(0, 12).flatMap((entry: unknown) => {
    if (!isRecord(entry)) return [];
    const brand = text(entry.brand);
    const version = text(entry.version);
    return brand === null || version === null ? [] : [{ brand, version }];
  });
}

async function queryMicrophonePermission(nav: NavigatorFacts): Promise<MicrophonePermissionFact | null> {
  try {
    const status = await nav?.permissions?.query({ name: "microphone" as PermissionName });
    const state = status?.state;
    return state === "granted" || state === "denied" || state === "prompt" ? state : null;
  } catch {
    return null;
  }
}

async function readUserAgentData(nav: NavigatorFacts): Promise<AcceptanceDeviceFacts["userAgentData"]> {
  const data = read(() => nav?.userAgentData);
  if (typeof data !== "object" || data === null) return null;
  let high: unknown;
  try {
    high = await data.getHighEntropyValues?.([...HIGH_ENTROPY_HINTS]);
  } catch {
    high = undefined;            // 被拒时低熵值照记
  }
  const extra = isRecord(high) ? high : {};
  return {
    mobile: flag(read(() => data.mobile)),
    platform: text(read(() => data.platform)),
    brands: brandList(read(() => data.brands)),
    model: text(extra.model),
    platformVersion: text(extra.platformVersion),
    fullVersionList: brandList(extra.fullVersionList),
  };
}

function readScreen(env: DeviceFactEnvironment): AcceptanceDeviceFacts["screen"] {
  const screen = read(() => env.screen);
  return {
    width: finite(read(() => screen?.width)),
    height: finite(read(() => screen?.height)),
    availWidth: finite(read(() => screen?.availWidth)),
    availHeight: finite(read(() => screen?.availHeight)),
    devicePixelRatio: finite(read(() => env.devicePixelRatio)),
    viewportWidth: finite(read(() => env.innerWidth)),
    viewportHeight: finite(read(() => env.innerHeight)),
    orientation: text(read(() => screen?.orientation?.type)),
    displayMode: DISPLAY_MODES.find(
      (mode) => read(() => env.matchMedia?.(`(display-mode: ${mode})`).matches) === true) ?? null,
  };
}

function readLanguages(nav: NavigatorFacts): string[] | null {
  const list = read(() => nav?.languages);
  if (!Array.isArray(list)) return null;
  return list.slice(0, 10).flatMap((entry: unknown) => {
    const value = text(entry);
    return value === null ? [] : [value];
  });
}

function readConnection(nav: NavigatorFacts): AcceptanceDeviceFacts["connection"] {
  const connection = read(() => nav?.connection);
  if (typeof connection !== "object" || connection === null) return null;
  return {
    type: text(read(() => connection.type)),
    effectiveType: text(read(() => connection.effectiveType)),
    downlinkMbps: finite(read(() => connection.downlink)),
    rttMs: finite(read(() => connection.rtt)),
    saveData: flag(read(() => connection.saveData)),
  };
}

function readSupportedConstraints(nav: NavigatorFacts): AcceptanceDeviceFacts["supportedConstraints"] {
  const supported = read(() => nav?.mediaDevices?.getSupportedConstraints?.());
  if (!isRecord(supported)) return null;
  return {
    echoCancellation: supported.echoCancellation === true,
    noiseSuppression: supported.noiseSuppression === true,
    autoGainControl: supported.autoGainControl === true,
    sampleRate: supported.sampleRate === true,
    channelCount: supported.channelCount === true,
  };
}

function readRecorderTypes(env: DeviceFactEnvironment): Record<string, boolean> | null {
  const recorder = read(() => env.MediaRecorder);
  if (typeof read(() => recorder?.isTypeSupported) !== "function") return null;
  return Object.fromEntries(RECORDER_MIME_TYPES.map(
    (mime) => [mime, read(() => recorder?.isTypeSupported?.(mime)) === true]));
}

function openAudioContext(env: DeviceFactEnvironment): AudioContextLike | null {
  const Context = read(() => env.AudioContext ?? env.webkitAudioContext);
  if (typeof Context !== "function") return null;
  return read(() => new Context()) ?? null;
}

function closeAudioContext(context: AudioContextLike): void {
  try { void context.close().catch(() => {}); } catch { /* 已经关了 */ }
}

function readOutputAudio(env: DeviceFactEnvironment): AcceptanceDeviceFacts["audioContext"] {
  const context = openAudioContext(env);
  if (context === null) return null;
  try {
    return {
      sampleRate: finite(read(() => context.sampleRate)),
      baseLatency: finite(read(() => context.baseLatency)),
      outputLatency: finite(read(() => context.outputLatency)),
    };
  } finally {
    closeAudioContext(context);
  }
}

function pickTrackSettings(raw: Record<string, unknown>): TrackSettingsFact {
  // getSettings() 还会带 deviceId、groupId：逐项白名单，绝不整份展开。
  const echo = raw.echoCancellation;
  return {
    echoCancellation: typeof echo === "string" ? text(echo) : flag(echo),
    noiseSuppression: flag(raw.noiseSuppression),
    autoGainControl: flag(raw.autoGainControl),
    sampleRate: finite(raw.sampleRate),
    sampleSize: finite(raw.sampleSize),
    channelCount: finite(raw.channelCount),
    latency: finite(raw.latency),
  };
}

function describeStream(stream: MediaStream): MicrophoneOpenFact {
  const tracks = read(() => stream.getAudioTracks());
  if (!Array.isArray(tracks)) return { audioTrackCount: null, settings: null, errorName: null };
  const raw: unknown = tracks.length > 0 ? read(() => tracks[0]!.getSettings()) : undefined;
  return {
    audioTrackCount: tracks.length,
    settings: isRecord(raw) ? pickTrackSettings(raw) : null,
    errorName: null,
  };
}

function openFailure(error: unknown): MicrophoneOpenFact {
  return {
    audioTrackCount: null,
    settings: null,
    errorName: text(read(() => (error as { name?: unknown } | null)?.name)) ?? "Error",
  };
}

async function settleWithin(promise: Promise<unknown> | undefined, ports: DeviceFactPorts, ms: number): Promise<void> {
  let timer: unknown;
  try {
    await Promise.race([
      Promise.resolve(promise).catch(() => undefined),
      new Promise<void>((resolve) => { timer = ports.setTimeout(resolve, ms); }),
    ]);
  } finally {
    ports.clearTimeout(timer);
  }
}

async function openRecorderMicrophone(mediaDevices: MediaDevicesFacts): Promise<MicrophoneOpenFact> {
  let stream: MediaStream;
  try {
    stream = await mediaDevices.getUserMedia!(RECORDER_CONSTRAINTS);
  } catch (error) {
    return openFailure(error);
  }
  try {
    return describeStream(stream);
  } finally {
    releaseMediaStreamTracks(stream);
  }
}

async function openBargeInMicrophone(
  env: DeviceFactEnvironment, mediaDevices: MediaDevicesFacts, ports: DeviceFactPorts, resumeMs: number,
): Promise<{ open: MicrophoneOpenFact; contextState: string | null }> {
  let stream: MediaStream;
  try {
    stream = await mediaDevices.getUserMedia!(BARGE_IN_CONSTRAINTS);
  } catch (error) {
    return { open: openFailure(error), contextState: null };
  }
  try {
    const open = describeStream(stream);
    // 与提前回答检测同一顺序：先拿到流，再建 AudioContext，挂起就 resume 一次。
    const context = openAudioContext(env);
    if (context === null) return { open, contextState: null };
    try {
      if (read(() => context.state) === "suspended") {
        await settleWithin(read(() => context.resume()), ports, resumeMs);
      }
      return { open, contextState: text(read(() => context.state)) };
    } finally {
      closeAudioContext(context);
    }
  } finally {
    releaseMediaStreamTracks(stream);
  }
}

async function acquireLeaseWithin(
  ports: DeviceFactPorts, ms: number,
): Promise<AudioDeviceLease | "lease-busy" | "lease-unavailable"> {
  const controller = new AbortController();
  const timer = ports.setTimeout(
    () => controller.abort(new DOMException("等音频设备锁超时", "TimeoutError")), ms);
  try {
    return await ports.acquireLease(controller.signal);
  } catch {
    // 超时撤销 = 有人占着锁；没到时间就被拒 = 浏览器没有 Web Locks，正式录音同样会被拒。
    return controller.signal.aborted ? "lease-busy" : "lease-unavailable";
  } finally {
    ports.clearTimeout(timer);
  }
}

function notProbed(status: MicrophoneProbeStatus): MicrophoneProbeFact {
  return {
    status, recorder: null, bargeIn: null,
    bargeInAudioContextState: null, permissionAfterOpen: null, bargeInEligible: null,
  };
}

/**
 * 与 patient/autopilotBrowserBargeIn.ts 开检测前的三道检查一一对应：麦克风权限已授予
 * （查不到也算没授予）、恰好一条音轨且 getSettings().echoCancellation === true、
 * AudioContext 进了 running。少一道，那台平板就不会开提前回答检测。
 */
export function bargeInEligibility(
  permission: MicrophonePermissionFact | null, open: MicrophoneOpenFact, contextState: string | null,
): boolean {
  return permission === "granted"
    && open.audioTrackCount === 1
    && open.settings?.echoCancellation === true
    && contextState === "running";
}

async function probeMicrophone(
  env: DeviceFactEnvironment, nav: NavigatorFacts, ports: DeviceFactPorts, leaseMs: number, resumeMs: number,
): Promise<MicrophoneProbeFact> {
  const mediaDevices = read(() => nav?.mediaDevices);
  if (!mediaDevices || typeof read(() => mediaDevices.getUserMedia) !== "function") {
    return notProbed("no-media-devices");
  }
  const lease = await acquireLeaseWithin(ports, leaseMs);
  if (lease === "lease-busy" || lease === "lease-unavailable") return notProbed(lease);
  try {
    const recorder = await openRecorderMicrophone(mediaDevices);
    const bargeIn = await openBargeInMicrophone(env, mediaDevices, ports, resumeMs);
    const permissionAfterOpen = await queryMicrophonePermission(nav);
    return {
      status: "collected",
      recorder,
      bargeIn: bargeIn.open,
      bargeInAudioContextState: bargeIn.contextState,
      permissionAfterOpen,
      bargeInEligible: bargeInEligibility(permissionAfterOpen, bargeIn.open, bargeIn.contextState),
    };
  } finally {
    // 等浏览器真把锁放掉再返回：采集一结束，老人端就能立刻拿到麦克风。
    lease.release();
    await lease.released;
  }
}

export async function collectAcceptanceDeviceFacts(
  env: DeviceFactEnvironment,
  ports: DeviceFactPorts = browserDeviceFactPorts,
  timeouts: { leaseMs?: number; resumeMs?: number } = {},
): Promise<AcceptanceDeviceFacts> {
  const collectedAtIso = new Date(ports.now()).toISOString();
  const nav = read(() => env.navigator);
  const [userAgentData, microphonePermission] = await Promise.all([
    readUserAgentData(nav), queryMicrophonePermission(nav),
  ]);
  const audioContext = readOutputAudio(env);
  const microphone = await probeMicrophone(
    env, nav, ports,
    timeouts.leaseMs ?? DEVICE_LEASE_TIMEOUT_MS, timeouts.resumeMs ?? CONTEXT_RESUME_TIMEOUT_MS);
  return {
    collectedAtIso,
    platform: text(read(() => nav?.platform)),
    maxTouchPoints: finite(read(() => nav?.maxTouchPoints)),
    userAgentData,
    screen: readScreen(env),
    language: text(read(() => nav?.language)),
    languages: readLanguages(nav),
    timeZone: text(read(() => env.Intl?.DateTimeFormat().resolvedOptions().timeZone)),
    hardwareConcurrency: finite(read(() => nav?.hardwareConcurrency)),
    deviceMemoryGb: finite(read(() => nav?.deviceMemory)),
    connection: readConnection(nav),
    supportedConstraints: readSupportedConstraints(nav),
    microphonePermission,
    mediaRecorderTypes: readRecorderTypes(env),
    audioContext,
    microphone,
  };
}

/** 本机存储或冻结字节里读回来的设备参数，至少要有页面会往下读的那几层。 */
export function isDeviceFactsShape(value: unknown): value is AcceptanceDeviceFacts {
  return isRecord(value)
    && typeof value.collectedAtIso === "string"
    && isRecord(value.screen)
    && isRecord(value.microphone)
    && PROBE_STATUSES.includes(value.microphone.status as string);
}

const UNKNOWN = "未知";

function show(value: unknown): string {
  if (typeof value === "number") return String(value);
  if (typeof value !== "string") return UNKNOWN;
  return value === "" ? "（空）" : value;
}

function yesNo(value: unknown): string {
  return value === true ? "是" : value === false ? "否" : UNKNOWN;
}

function onOff(value: unknown): string {
  return value === true ? "开" : value === false ? "关" : show(value);
}

function seconds(value: unknown): string {
  return typeof value === "number" ? `${value.toFixed(3)} s` : UNKNOWN;
}

const PERMISSION_TEXT: Record<MicrophonePermissionFact, string> = {
  granted: "已允许",
  denied: "已拒绝",
  prompt: "还没允许过（会弹窗问）",
};

function permissionText(value: unknown): string {
  return typeof value === "string" && Object.hasOwn(PERMISSION_TEXT, value)
    ? PERMISSION_TEXT[value as MicrophonePermissionFact]
    : "查不到（浏览器不支持查询）";
}

function openText(open: MicrophoneOpenFact | null | undefined): string {
  if (!open) return "没开";
  if (open.errorName) return `打不开（${open.errorName}）`;
  const settings = open.settings;
  return `音轨 ${show(open.audioTrackCount)} 条，回声消除 ${onOff(settings?.echoCancellation)}，`
    + `降噪 ${onOff(settings?.noiseSuppression)}，自动增益 ${onOff(settings?.autoGainControl)}，`
    + `${show(settings?.sampleRate)} Hz，${show(settings?.channelCount)} 声道`;
}

const PROBE_SKIPPED_TEXT: Record<Exclude<MicrophoneProbeStatus, "collected">, string> = {
  "lease-busy": "没开麦——老人端或别的页面正在用麦克风（没拿到设备锁）；结束或暂停老人端练习后重新采集",
  "lease-unavailable": "没开麦——浏览器没有跨页面录音锁（Web Locks），正式录音在这台设备上同样会被拒绝",
  "no-media-devices": "没开麦——浏览器不支持打开麦克风（没有 getUserMedia）",
};

/** 回执纯文本和页面上共用的摘要；完整数据只在 JSON 里。 */
export function formatDeviceFactsLines(facts: AcceptanceDeviceFacts | null): string[] {
  if (facts === null) return ["设备参数：未采集（没点「采集设备参数」）"];
  const agent = facts.userAgentData;
  const screen = facts.screen;
  const connection = facts.connection;
  const supported = facts.supportedConstraints;
  const output = facts.audioContext;
  const recorderTypes = facts.mediaRecorderTypes;
  const microphone = facts.microphone;
  const lines = [
    `设备参数：自动采集于 ${facts.collectedAtIso}（型号、资产编号以人工填写为准）`,
    `  平台 ${show(facts.platform)}，触控点 ${show(facts.maxTouchPoints)}；`
      + (agent ? `浏览器自报型号 ${show(agent.model)}，系统版本 ${show(agent.platformVersion)}`
        : "浏览器不提供 userAgentData"),
    `  屏幕 ${show(screen.width)}×${show(screen.height)} @${show(screen.devicePixelRatio)}x，`
      + `视口 ${show(screen.viewportWidth)}×${show(screen.viewportHeight)}，`
      + `方向 ${show(screen.orientation)}，显示模式 ${show(screen.displayMode)}`,
    `  语言 ${show(facts.language)}，时区 ${show(facts.timeZone)}，CPU ${show(facts.hardwareConcurrency)} 核，`
      + `内存约 ${show(facts.deviceMemoryGb)} GB，网络 `
      + (connection ? `${show(connection.type)} / ${show(connection.effectiveType)}` : "查不到"),
    `  麦克风权限 ${permissionText(facts.microphonePermission)}；浏览器能请求的录音约束：`
      + (supported ? `回声消除 ${yesNo(supported.echoCancellation)}，降噪 ${yesNo(supported.noiseSuppression)}，`
        + `自动增益 ${yesNo(supported.autoGainControl)}` : "查不到"),
    "  录音格式："
      + (recorderTypes ? Object.entries(recorderTypes)
        .map(([mime, supportedType]) => `${mime} ${supportedType === true ? "支持" : "不支持"}`).join("，")
        : "查不到（没有 MediaRecorder）"),
    "  音频输出："
      + (output ? `采样率 ${show(output.sampleRate)} Hz，基础延迟 ${seconds(output.baseLatency)}，`
        + `输出延迟 ${seconds(output.outputLatency)}` : "查不到（没有 AudioContext）"),
  ];
  if (microphone.status !== "collected") {
    return [...lines, `  麦克风实测：${PROBE_SKIPPED_TEXT[microphone.status]}`];
  }
  return [
    ...lines,
    `  麦克风实测（录音用的设置）：${openText(microphone.recorder)}`,
    `  麦克风实测（提前回答检测用的设置）：${openText(microphone.bargeIn)}；`
      + `AudioContext ${show(microphone.bargeInAudioContextState)}；开麦后权限 ${permissionText(microphone.permissionAfterOpen)}`,
    `  提前回答检测条件（只看这台平板，服务器开关另算）：${microphone.bargeInEligible ? "满足" : "不满足"}`,
  ];
}
