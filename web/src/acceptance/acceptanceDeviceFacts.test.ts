import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import {
  acquireAudioDeviceLease,
  type AudioDeviceLockCallback,
  type AudioDeviceLockManager,
} from "../audio/audioDeviceLease.ts";
import {
  DEVICE_LEASE_TIMEOUT_MS,
  bargeInEligibility,
  collectAcceptanceDeviceFacts,
  formatDeviceFactsLines,
  isDeviceFactsShape,
  type DeviceFactEnvironment,
  type DeviceFactPorts,
} from "./acceptanceDeviceFacts.ts";

// 这些值一旦出现在回执或纯文本里就是泄漏：设备号能跨站追踪这台平板，
// 蓝牙耳机的名称常常就是人名。
const SECRET_DEVICE_ID = "device-id-must-never-leak-0001";
const SECRET_GROUP_ID = "group-id-must-never-leak-0002";
const BLUETOOTH_LABEL = "张三的AirPods";

const TRACK_SETTINGS = {
  deviceId: SECRET_DEVICE_ID,
  groupId: SECRET_GROUP_ID,
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
  sampleRate: 48000,
  sampleSize: 16,
  channelCount: 1,
  latency: 0.01,
};

const BARGE_IN_REQUEST = {
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
  video: false,
};

interface FakeTrack {
  label: string;
  stopped: boolean;
  getSettings(): Record<string, unknown>;
  stop(): void;
}

interface FakeContext {
  state: string;
  closed: boolean;
}

interface TabletOptions {
  permissionBefore?: string;
  permissionAfter?: string;
  bargeInSettings?: Record<string, unknown>;
  bargeInTrackCount?: number;
  contextResume?: "runs" | "stays-suspended" | "hangs";
  getUserMediaError?: string;
}

/** 一台什么接口都有的「平板」；开过一次麦之后权限查询才变成 permissionAfter。 */
function tablet(options: TabletOptions = {}) {
  const log = {
    requests: [] as unknown[],
    tracks: [] as FakeTrack[],
    contexts: [] as FakeContext[],
    highEntropyHints: [] as string[][],
  };
  let opened = false;
  class FakeAudioContext {
    state = "suspended";
    closed = false;
    sampleRate = 48000;
    baseLatency = 0.01;
    outputLatency = 0.04;
    constructor() { log.contexts.push(this); }
    resume(): Promise<void> {
      if (options.contextResume === "hangs") return new Promise<void>(() => {});
      if (options.contextResume !== "stays-suspended") this.state = "running";
      return Promise.resolve();
    }
    close(): Promise<void> {
      this.closed = true;
      this.state = "closed";
      return Promise.resolve();
    }
  }
  const makeTrack = (settings: Record<string, unknown>): FakeTrack => ({
    label: BLUETOOTH_LABEL,
    stopped: false,
    getSettings: () => ({ ...settings }),
    stop() { this.stopped = true; },
  });
  const env = {
    navigator: {
      platform: "Linux armv81",
      maxTouchPoints: 10,
      language: "zh-CN",
      languages: ["zh-CN", "en-US"],
      hardwareConcurrency: 8,
      deviceMemory: 4,
      connection: { type: "wifi", effectiveType: "4g", downlink: 10, rtt: 50, saveData: false },
      userAgentData: {
        mobile: false,
        platform: "Android",
        brands: [{ brand: "Chromium", version: "128" }],
        async getHighEntropyValues(hints: string[]) {
          log.highEntropyHints.push(hints);
          return {
            model: "SM-X200",
            platformVersion: "14.0.0",
            fullVersionList: [{ brand: "Chromium", version: "128.0.6613.88" }],
          };
        },
      },
      permissions: {
        async query(descriptor: { name: string }) {
          assert.deepEqual(descriptor, { name: "microphone" });
          return { state: opened ? (options.permissionAfter ?? "granted") : (options.permissionBefore ?? "prompt") };
        },
      },
      mediaDevices: {
        getSupportedConstraints: () => ({
          deviceId: true, groupId: true,
          echoCancellation: true, noiseSuppression: true, autoGainControl: true,
          sampleRate: true, channelCount: true,
        }),
        async getUserMedia(constraints: { audio: unknown }) {
          log.requests.push(constraints);
          if (options.getUserMediaError) {
            throw Object.assign(new Error(`无法打开 ${BLUETOOTH_LABEL}`), { name: options.getUserMediaError });
          }
          opened = true;
          const bargeIn = typeof constraints.audio === "object";
          const settings = bargeIn ? { ...TRACK_SETTINGS, ...options.bargeInSettings } : TRACK_SETTINGS;
          const tracks = Array.from(
            { length: bargeIn ? (options.bargeInTrackCount ?? 1) : 1 }, () => makeTrack(settings));
          log.tracks.push(...tracks);
          return { getAudioTracks: () => tracks, getTracks: () => tracks };
        },
      },
    },
    screen: { width: 1340, height: 800, availWidth: 1340, availHeight: 752, orientation: { type: "landscape-primary" } },
    innerWidth: 1340,
    innerHeight: 694,
    devicePixelRatio: 2,
    matchMedia: (query: string) => ({ matches: query === "(display-mode: browser)" }),
    MediaRecorder: { isTypeSupported: (mime: string) => mime.startsWith("audio/webm") },
    AudioContext: FakeAudioContext,
    Intl: { DateTimeFormat: () => ({ resolvedOptions: () => ({ timeZone: "Asia/Shanghai" }) }) },
  } as unknown as DeviceFactEnvironment;
  return { env, log };
}

class GrantingLockManager implements AudioDeviceLockManager {
  held = false;

  async request(
    _name: string,
    _options: { mode: "exclusive"; signal?: AbortSignal },
    callback: AudioDeviceLockCallback,
  ): Promise<void> {
    this.held = true;
    try { await callback({}); } finally { this.held = false; }
  }
}

/** 老人端占着锁不放：只会在请求被撤销时拒绝。 */
class BusyLockManager implements AudioDeviceLockManager {
  request(
    _name: string,
    options: { mode: "exclusive"; signal?: AbortSignal },
  ): Promise<unknown> {
    return new Promise((_resolve, reject) => {
      options.signal?.addEventListener("abort", () => reject(options.signal?.reason), { once: true });
    });
  }
}

function ports(manager: AudioDeviceLockManager | null): DeviceFactPorts {
  return {
    acquireLease: (signal) => acquireAudioDeviceLease(manager, signal),
    setTimeout: (callback, ms) => setTimeout(callback, ms),
    clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
    now: () => Date.parse("2026-10-02T06:00:00.000Z"),
  };
}

const BARGE_IN_SOURCE = readFileSync(
  new URL("../patient/autopilotBrowserBargeIn.ts", import.meta.url), "utf8");
const RECORDER_SOURCE = readFileSync(new URL("../audio/recorder.ts", import.meta.url), "utf8");

test("拿到锁：录音与提前回答检测两种设置各开一次麦，读完立刻关麦、关 AudioContext、还锁", async () => {
  const { env, log } = tablet();
  const manager = new GrantingLockManager();
  const facts = await collectAcceptanceDeviceFacts(env, ports(manager));

  assert.equal(facts.collectedAtIso, "2026-10-02T06:00:00.000Z");
  assert.equal(facts.microphone.status, "collected");
  assert.deepEqual(log.requests, [{ audio: true }, BARGE_IN_REQUEST]);
  assert.equal(log.tracks.length, 2);
  assert.ok(log.tracks.every((track) => track.stopped), "每条音轨都要关掉");
  assert.ok(log.contexts.length >= 1);
  assert.ok(log.contexts.every((context) => context.closed), "每个 AudioContext 都要关掉");
  assert.equal(manager.held, false, "采集结束时设备锁必须已经还回去");

  assert.deepEqual(facts.microphone.recorder, {
    audioTrackCount: 1,
    settings: {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      sampleRate: 48000, sampleSize: 16, channelCount: 1, latency: 0.01,
    },
    errorName: null,
  });
  assert.equal(facts.microphonePermission, "prompt");
  assert.equal(facts.microphone.permissionAfterOpen, "granted");
  assert.equal(facts.microphone.bargeInAudioContextState, "running");
  assert.equal(facts.microphone.bargeInEligible, true);
});

test("不开麦也读得到的参数：平台、触控点、userAgentData、屏幕、语言、网络、约束支持、录音格式、音频输出", async () => {
  const { env, log } = tablet();
  const facts = await collectAcceptanceDeviceFacts(env, ports(new GrantingLockManager()));
  assert.equal(facts.platform, "Linux armv81");
  assert.equal(facts.maxTouchPoints, 10);
  assert.deepEqual(log.highEntropyHints, [["model", "platformVersion", "fullVersionList"]]);
  assert.deepEqual(facts.userAgentData, {
    mobile: false,
    platform: "Android",
    brands: [{ brand: "Chromium", version: "128" }],
    model: "SM-X200",
    platformVersion: "14.0.0",
    fullVersionList: [{ brand: "Chromium", version: "128.0.6613.88" }],
  });
  assert.deepEqual(facts.screen, {
    width: 1340, height: 800, availWidth: 1340, availHeight: 752, devicePixelRatio: 2,
    viewportWidth: 1340, viewportHeight: 694, orientation: "landscape-primary", displayMode: "browser",
  });
  assert.equal(facts.language, "zh-CN");
  assert.deepEqual(facts.languages, ["zh-CN", "en-US"]);
  assert.equal(facts.timeZone, "Asia/Shanghai");
  assert.equal(facts.hardwareConcurrency, 8);
  assert.equal(facts.deviceMemoryGb, 4);
  assert.deepEqual(facts.connection, {
    type: "wifi", effectiveType: "4g", downlinkMbps: 10, rttMs: 50, saveData: false,
  });
  assert.deepEqual(facts.supportedConstraints, {
    echoCancellation: true, noiseSuppression: true, autoGainControl: true, sampleRate: true, channelCount: true,
  });
  assert.deepEqual(facts.mediaRecorderTypes, {
    "audio/webm": true, "audio/webm;codecs=opus": true, "audio/mp4": false, "audio/ogg;codecs=opus": false,
  });
  assert.deepEqual(facts.audioContext, { sampleRate: 48000, baseLatency: 0.01, outputLatency: 0.04 });
});

test("回执里绝不出现 deviceId、groupId 或设备名称，纯文本摘要也一样", async () => {
  const { env } = tablet();
  const facts = await collectAcceptanceDeviceFacts(env, ports(new GrantingLockManager()));
  const outputs = [JSON.stringify(facts), formatDeviceFactsLines(facts).join("\n")];
  for (const output of outputs) {
    for (const forbidden of [SECRET_DEVICE_ID, SECRET_GROUP_ID, BLUETOOTH_LABEL, "deviceId", "groupId", "label"]) {
      assert.ok(!output.includes(forbidden), `输出里出现了 ${forbidden}`);
    }
  }
});

test("开麦失败只记错误名：错误消息里可能带设备名称，不进回执", async () => {
  const { env } = tablet({ getUserMediaError: "NotAllowedError", permissionAfter: "denied" });
  const facts = await collectAcceptanceDeviceFacts(env, ports(new GrantingLockManager()));
  assert.equal(facts.microphone.status, "collected");
  assert.deepEqual(facts.microphone.recorder, { audioTrackCount: null, settings: null, errorName: "NotAllowedError" });
  assert.deepEqual(facts.microphone.bargeIn, { audioTrackCount: null, settings: null, errorName: "NotAllowedError" });
  assert.equal(facts.microphone.bargeInEligible, false);
  assert.ok(!JSON.stringify(facts).includes(BLUETOOTH_LABEL));
});

test("老人端占着设备锁：约 1.5 秒内拿不到就不开麦，其余参数照记", async () => {
  assert.equal(DEVICE_LEASE_TIMEOUT_MS, 1_500);
  const { env, log } = tablet();
  const facts = await collectAcceptanceDeviceFacts(env, ports(new BusyLockManager()), { leaseMs: 20 });
  assert.equal(facts.microphone.status, "lease-busy");
  assert.deepEqual(log.requests, [], "拿不到锁就一次 getUserMedia 都不许调");
  assert.equal(facts.microphone.recorder, null);
  assert.equal(facts.microphone.bargeIn, null);
  assert.equal(facts.microphone.permissionAfterOpen, null);
  assert.equal(facts.microphone.bargeInEligible, null, "没测成就是未知，不是不满足");
  assert.equal(facts.maxTouchPoints, 10);
  assert.equal(facts.audioContext?.sampleRate, 48000);
  assert.match(formatDeviceFactsLines(facts).join("\n"), /老人端或别的页面正在用麦克风/);
});

test("浏览器没有 Web Locks：生产录音链同样会拒绝开麦，这里也不开", async () => {
  const { env, log } = tablet();
  const facts = await collectAcceptanceDeviceFacts(env, ports(null));
  assert.equal(facts.microphone.status, "lease-unavailable");
  assert.deepEqual(log.requests, []);
  assert.equal(facts.microphone.bargeInEligible, null);
});

test("什么接口都没有的浏览器：每一项都是 null，不抛错", async () => {
  const facts = await collectAcceptanceDeviceFacts({}, ports(new GrantingLockManager()));
  assert.deepEqual(facts, {
    collectedAtIso: "2026-10-02T06:00:00.000Z",
    platform: null,
    maxTouchPoints: null,
    userAgentData: null,
    screen: {
      width: null, height: null, availWidth: null, availHeight: null, devicePixelRatio: null,
      viewportWidth: null, viewportHeight: null, orientation: null, displayMode: null,
    },
    language: null,
    languages: null,
    timeZone: null,
    hardwareConcurrency: null,
    deviceMemoryGb: null,
    connection: null,
    supportedConstraints: null,
    microphonePermission: null,
    mediaRecorderTypes: null,
    audioContext: null,
    microphone: {
      status: "no-media-devices", recorder: null, bargeIn: null,
      bargeInAudioContextState: null, permissionAfterOpen: null, bargeInEligible: null,
    },
  });
  assert.ok(formatDeviceFactsLines(facts).length > 0);
});

test("接口在但一碰就抛或被拒：只把那一项记成 null", async () => {
  const explode = () => { throw new Error("boom"); };
  const env = {
    navigator: {
      get platform(): string { throw new Error("boom"); },
      userAgentData: { brands: [], mobile: true, platform: "Android", getHighEntropyValues: async () => explode() },
      permissions: { query: async () => explode() },
      mediaDevices: { getSupportedConstraints: explode },
    },
    get screen(): unknown { throw new Error("boom"); },
    matchMedia: explode,
    MediaRecorder: { isTypeSupported: explode },
    AudioContext: class { constructor() { throw new Error("boom"); } },
    Intl: { DateTimeFormat: explode },
  } as unknown as DeviceFactEnvironment;
  const facts = await collectAcceptanceDeviceFacts(env, ports(new GrantingLockManager()));
  assert.equal(facts.platform, null);
  assert.deepEqual(facts.userAgentData, {
    mobile: true, platform: "Android", brands: [], model: null, platformVersion: null, fullVersionList: null,
  });
  assert.equal(facts.microphonePermission, null);
  assert.equal(facts.supportedConstraints, null);
  assert.equal(facts.screen.width, null);
  assert.equal(facts.screen.displayMode, null);
  assert.deepEqual(facts.mediaRecorderTypes, {
    "audio/webm": false, "audio/webm;codecs=opus": false, "audio/mp4": false, "audio/ogg;codecs=opus": false,
  });
  assert.equal(facts.audioContext, null);
  assert.equal(facts.timeZone, null);
  assert.equal(facts.microphone.status, "no-media-devices");
});

test("提前回答检测条件与 autopilotBrowserBargeIn.ts 的三道检查同口径", async () => {
  const cases: [string, TabletOptions, boolean][] = [
    ["三道都过", {}, true],
    ["开过麦权限仍不是 granted", { permissionAfter: "prompt" }, false],
    ["两条音轨", { bargeInTrackCount: 2 }, false],
    ["回声消除没开", { bargeInSettings: { echoCancellation: false } }, false],
    ["回声消除报的是字符串模式", { bargeInSettings: { echoCancellation: "all" } }, false],
    ["AudioContext resume 后仍挂起", { contextResume: "stays-suspended" }, false],
  ];
  for (const [name, options, expected] of cases) {
    const { env } = tablet(options);
    const facts = await collectAcceptanceDeviceFacts(env, ports(new GrantingLockManager()));
    assert.equal(facts.microphone.bargeInEligible, expected, name);
  }
  // 浏览器查不到权限：提前回答检测按「没授予」处理，这里同样不满足。
  const { env } = tablet();
  (env.navigator as { permissions?: unknown }).permissions = undefined;
  const noQuery = await collectAcceptanceDeviceFacts(env, ports(new GrantingLockManager()));
  assert.equal(noQuery.microphonePermission, null);
  assert.equal(noQuery.microphone.bargeInEligible, false);
  // 字符串模式照实记下来，不硬转成 true。
  const { env: stringMode } = tablet({ bargeInSettings: { echoCancellation: "all" } });
  const recorded = await collectAcceptanceDeviceFacts(stringMode, ports(new GrantingLockManager()));
  assert.equal(recorded.microphone.bargeIn?.settings?.echoCancellation, "all");

  const settings = {
    echoCancellation: true, noiseSuppression: null, autoGainControl: null,
    sampleRate: null, sampleSize: null, channelCount: null, latency: null,
  };
  const open = { audioTrackCount: 1, settings, errorName: null };
  assert.equal(bargeInEligibility("granted", open, "running"), true);
  assert.equal(bargeInEligibility(null, open, "running"), false);
  assert.equal(bargeInEligibility("granted", { ...open, settings: null }, "running"), false);
  assert.equal(bargeInEligibility("granted", open, null), false);
});

test("resume 一直挂着也不让采集卡住：限时之后照实记下 state", async () => {
  const { env, log } = tablet({ contextResume: "hangs" });
  const manager = new GrantingLockManager();
  const facts = await collectAcceptanceDeviceFacts(env, ports(manager), { resumeMs: 20 });
  assert.equal(facts.microphone.bargeInAudioContextState, "suspended");
  assert.equal(facts.microphone.bargeInEligible, false);
  assert.ok(log.tracks.every((track) => track.stopped));
  assert.equal(manager.held, false);
});

test("源码钉住：两种开麦约束与提前回答检测的三道检查没有走样", async () => {
  const { env, log } = tablet();
  await collectAcceptanceDeviceFacts(env, ports(new GrantingLockManager()));
  const [recorderAsked, bargeInAsked] = log.requests;

  assert.match(RECORDER_SOURCE, /getUserMedia\(\s*\{\s*audio:\s*true\s*\}\s*\)/);
  assert.deepEqual(recorderAsked, { audio: true });

  const literal = BARGE_IN_SOURCE.match(
    /getUserMedia\(\s*\{\s*audio:\s*\{([^}]*)\}\s*,\s*video:\s*(true|false)\s*,?\s*\}\s*\)/);
  assert.ok(literal, "autopilotBrowserBargeIn.ts 里找不到提前回答检测的开麦约束");
  const audio = Object.fromEntries(literal[1]!.split(",").map((part) => part.trim()).filter(Boolean)
    .map((pair) => {
      const [key, value] = pair.split(":").map((side) => side.trim());
      return [key!, JSON.parse(value!) as unknown];
    }));
  assert.deepEqual(bargeInAsked, { audio, video: JSON.parse(literal[2]!) as unknown });

  assert.match(BARGE_IN_SOURCE, /\.state\s*===\s*"granted"/);
  assert.match(BARGE_IN_SOURCE,
    /tracks\.length\s*!==\s*1\s*\|\|\s*tracks\[0\]\.getSettings\(\)\.echoCancellation\s*!==\s*true/);
  assert.match(BARGE_IN_SOURCE, /if\s*\(context\.state\s*===\s*"suspended"\)\s*await context\.resume\(\)/);
  assert.match(BARGE_IN_SOURCE, /context\.state\s*!==\s*"running"/);
});

test("纯文本摘要：说清采集时间、型号以人工为准、检测条件只看平板", async () => {
  assert.deepEqual(formatDeviceFactsLines(null), ["设备参数：未采集（没点「采集设备参数」）"]);
  const { env } = tablet();
  const facts = await collectAcceptanceDeviceFacts(env, ports(new GrantingLockManager()));
  const text = formatDeviceFactsLines(facts).join("\n");
  assert.match(text, /自动采集于 2026-10-02T06:00:00\.000Z/);
  assert.match(text, /型号、资产编号以人工填写为准/);
  assert.match(text, /触控点 10/);
  assert.match(text, /浏览器自报型号 SM-X200/);
  assert.match(text, /麦克风实测（录音用的设置）：音轨 1 条，回声消除 开/);
  assert.match(text, /提前回答检测条件（只看这台平板，服务器开关另算）：满足/);
});

test("存回本机再读：设备参数结构要过关，乱数据不认", async () => {
  const { env } = tablet();
  const facts = await collectAcceptanceDeviceFacts(env, ports(new GrantingLockManager()));
  assert.equal(isDeviceFactsShape(JSON.parse(JSON.stringify(facts))), true);
  for (const bad of [null, "x", [], {}, { ...facts, collectedAtIso: 1 }, { ...facts, screen: null },
    { ...facts, microphone: { ...facts.microphone, status: "hacked" } }]) {
    assert.equal(isDeviceFactsShape(bad), false, JSON.stringify(bad)?.slice(0, 60));
  }
});
