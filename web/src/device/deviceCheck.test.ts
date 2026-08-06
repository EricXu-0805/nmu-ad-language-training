import assert from "node:assert/strict";
import test from "node:test";
import {
  checkAudioOutput,
  checkFallbackVoice,
  checkMicrophone,
  checkRecorderCodec,
  checkRuntime,
  classifyNetwork,
  classifyNoiseFloor,
  classifySampleRate,
  classifyScreen,
  classifySpeechLevel,
  classifyStorage,
  formatReport,
  mapMicrophoneError,
  runAutoChecks,
  summarize,
  type CheckResult,
  type DeviceCheckDependencies,
} from "./deviceCheck.ts";

function deps(overrides: Partial<DeviceCheckDependencies> = {}): DeviceCheckDependencies {
  return {
    secureContext: true,
    userAgent: "TestUA/1.0",
    screen: { width: 1280, height: 800, pixelRatio: 2 },
    mediaDevicesAvailable: true,
    mediaRecorderAvailable: true,
    isTypeSupported: (mime) => mime.startsWith("audio/webm"),
    acquireMicrophone: async () => ({
      label: "内置麦克风", sampleRate: 48000, channelCount: 1,
      echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    }),
    sampleNoise: async () => ({ rmsDbfs: -55, peakDbfs: -40 }),
    releaseMicrophone: () => {},
    audioContextSampleRate: () => 48000,
    resumeAudio: async () => "running",
    speechSynthesisVoices: () => ({ total: 12, chinese: 3 }),
    fetchImpl: async () => ({
      ok: true, status: 200,
      headers: { get: (name: string) => (name === "date" ? new Date().toUTCString() : null) },
    }),
    now: () => Date.now(),
    storageEstimate: async () => ({ quotaBytes: 2_000_000_000, usageBytes: 100_000_000 }),
    ...overrides,
  };
}

// ---------------- 运行环境 ----------------

test("insecure context fails before anything touches the microphone", () => {
  const result = checkRuntime(deps({ secureContext: false }));
  assert.equal(result.status, "fail");
  assert.match(result.detail, /HTTPS/);
});

test("missing MediaRecorder is fail, complete runtime is pass", () => {
  assert.equal(checkRuntime(deps({ mediaRecorderAvailable: false })).status, "fail");
  assert.equal(checkRuntime(deps()).status, "pass");
});

// ---------------- 麦克风 ----------------

test("permission denial maps to an actionable message", () => {
  assert.match(mapMicrophoneError({ name: "NotAllowedError" }), /权限被拒/);
  assert.match(mapMicrophoneError({ name: "NotFoundError" }), /找不到麦克风/);
  assert.match(mapMicrophoneError({ name: "NotReadableError" }), /占用/);
  assert.match(mapMicrophoneError(new Error("weird")), /weird/);
});

test("microphone check reports the track processing switches", async () => {
  const outcome = await checkMicrophone(deps());
  assert.equal(outcome.result.status, "pass");
  assert.match(outcome.result.detail, /回声消除开/);
  assert.match(outcome.result.detail, /自动增益开/);
  assert.equal(outcome.info?.sampleRate, 48000);
});

test("microphone failure carries the mapped reason and no info", async () => {
  const outcome = await checkMicrophone(deps({
    acquireMicrophone: async () => { throw { name: "NotAllowedError" }; },
  }));
  assert.equal(outcome.result.status, "fail");
  assert.equal(outcome.info, null);
});

// ---------------- 编码 / 采样率 ----------------

test("webm support matches the production recorder and passes", () => {
  assert.equal(checkRecorderCodec(deps()).status, "pass");
});

test("mp4-only browsers get a warn with the fallback container named", () => {
  const result = checkRecorderCodec(deps({ isTypeSupported: (m) => m === "audio/mp4" }));
  assert.equal(result.status, "warn");
  assert.match(result.detail, /audio\/mp4/);
});

test("no supported container at all is fail, and a throwing probe counts as unsupported", () => {
  assert.equal(checkRecorderCodec(deps({ isTypeSupported: () => false })).status, "fail");
  assert.equal(checkRecorderCodec(deps({
    isTypeSupported: () => { throw new Error("boom"); },
  })).status, "fail");
});

test("sample rate thresholds: <16k fail, <32k warn, else pass, null warn", () => {
  assert.equal(classifySampleRate(8000).status, "fail");
  assert.equal(classifySampleRate(16000).status, "warn");
  assert.equal(classifySampleRate(44100).status, "pass");
  assert.equal(classifySampleRate(null).status, "warn");
});

// ---------------- 电平 ----------------

test("near-digital-silence is a dead microphone, not a quiet room", () => {
  const result = classifyNoiseFloor({ rmsDbfs: -90, peakDbfs: -85 });
  assert.equal(result.status, "fail");
  assert.match(result.detail, /静音或损坏/);
});

test("noise floor thresholds: quiet pass, some noise warn, loud fail", () => {
  assert.equal(classifyNoiseFloor({ rmsDbfs: -55, peakDbfs: -40 }).status, "pass");
  assert.equal(classifyNoiseFloor({ rmsDbfs: -40, peakDbfs: -30 }).status, "warn");
  assert.equal(classifyNoiseFloor({ rmsDbfs: -25, peakDbfs: -15 }).status, "fail");
});

test("speech level: clear pass, weak warn with elder note, absent fail", () => {
  assert.equal(classifySpeechLevel({ rmsDbfs: -30, peakDbfs: -20 }).status, "pass");
  const weak = classifySpeechLevel({ rmsDbfs: -50, peakDbfs: -42 });
  assert.equal(weak.status, "warn");
  assert.match(weak.detail, /老人/);
  assert.equal(classifySpeechLevel({ rmsDbfs: -70, peakDbfs: -60 }).status, "fail");
});

// ---------------- 网络 / 存储 / 屏幕 / 声音 ----------------

test("network: unreachable fail, non-200 fail, slow warn, fast pass", () => {
  assert.equal(classifyNetwork(null, null, null).status, "fail");
  assert.equal(classifyNetwork(503, 80, 0).status, "fail");
  assert.equal(classifyNetwork(200, 2400, 0).status, "warn");
  assert.equal(classifyNetwork(200, 90, 0).status, "pass");
});

test("clock skew beyond two minutes is surfaced but never blocks", () => {
  const result = classifyNetwork(200, 90, 400);
  assert.equal(result.status, "pass");
  assert.match(result.detail, /服务器时间为准/);
  assert.doesNotMatch(classifyNetwork(200, 90, 30).detail, /时钟/);
});

test("storage: <50MB free fail, <200MB warn, plenty pass, unknown warn", () => {
  const mb = 1024 * 1024;
  assert.equal(classifyStorage(100 * mb, 60 * mb).status, "fail");
  assert.equal(classifyStorage(300 * mb, 150 * mb).status, "warn");
  assert.equal(classifyStorage(2000 * mb, 100 * mb).status, "pass");
  assert.equal(classifyStorage(null, null).status, "warn");
});

test("a phone-sized screen warns but never fails", () => {
  assert.equal(classifyScreen({ width: 390, height: 844, pixelRatio: 3 }).status, "warn");
  assert.equal(classifyScreen({ width: 1280, height: 800, pixelRatio: 2 }).status, "pass");
});

test("suspended audio context after the gesture is a hard fail", () => {
  assert.equal(checkAudioOutput("running").status, "pass");
  assert.equal(checkAudioOutput("suspended").status, "fail");
  assert.equal(checkAudioOutput("unavailable").status, "fail");
});

test("fallback voice: chinese present pass, absent warn, none warn — never fail", () => {
  assert.equal(checkFallbackVoice({ total: 12, chinese: 3 }).status, "pass");
  assert.equal(checkFallbackVoice({ total: 5, chinese: 0 }).status, "warn");
  assert.equal(checkFallbackVoice(null).status, "warn");
});

// ---------------- 汇总 / 报告 ----------------

function mk(status: CheckResult["status"]): CheckResult {
  return { id: "x", title: "X", status, detail: "d" };
}

test("verdict: any fail dominates, then any warn, else usable", () => {
  assert.equal(summarize([mk("pass"), mk("fail"), mk("warn")]), "not-usable");
  assert.equal(summarize([mk("pass"), mk("warn")]), "usable-with-warnings");
  assert.equal(summarize([mk("pass"), mk("pass")]), "usable");
});

test("the report is one conclusion per line and carries the user agent", () => {
  const report = formatReport("TestUA/1.0", [
    { id: "a", title: "麦克风", status: "pass", detail: "内置" },
    { id: "b", title: "网络", status: "fail", detail: "连不上" },
  ], "not-usable", "2026-08-06T21:00:00+08:00");
  const lines = report.split("\n");
  assert.match(lines[0], /不可用/);
  assert.equal(lines[1], "UA: TestUA/1.0");
  assert.equal(lines[2], "✓ 麦克风: 内置");
  assert.equal(lines[3], "✗ 网络: 连不上");
});

// ---------------- 编排 ----------------

test("auto checks run in a stable order and stream results as they land", async () => {
  const seen: string[] = [];
  const run = await runAutoChecks(deps(), (r) => seen.push(r.id));
  assert.deepEqual(seen, ["runtime", "microphone", "codec", "sample-rate",
    "audio-output", "fallback-voice", "network", "storage", "screen"]);
  assert.equal(run.results.length, 9);
  assert.ok(run.results.every((r) => r.status === "pass"), JSON.stringify(run.results));
  assert.equal(run.micInfo?.label, "内置麦克风");
});

test("a failed runtime short-circuits the microphone attempt", async () => {
  let touched = false;
  const run = await runAutoChecks(deps({
    secureContext: false,
    acquireMicrophone: async () => { touched = true; throw new Error("must not"); },
  }), () => {});
  assert.equal(touched, false);
  const mic = run.results.find((r) => r.id === "microphone");
  assert.equal(mic?.status, "fail");
  assert.match(mic!.detail, /未尝试/);
});

test("a throwing dependency becomes that check's fail, not a crash", async () => {
  const run = await runAutoChecks(deps({
    storageEstimate: async () => { throw new Error("quota exploded"); },
  }), () => {});
  const storage = run.results.find((r) => r.id === "storage");
  assert.equal(storage?.status, "fail");
  assert.match(storage!.detail, /quota exploded/);
  assert.equal(run.results.length, 9, "其余检查照常跑完");
});

test("sample rate falls back to the audio context when the track hides it", async () => {
  const run = await runAutoChecks(deps({
    acquireMicrophone: async () => ({
      label: "x", sampleRate: null, channelCount: null,
      echoCancellation: null, noiseSuppression: null, autoGainControl: null,
    }),
    audioContextSampleRate: () => 44100,
  }), () => {});
  const rate = run.results.find((r) => r.id === "sample-rate");
  assert.equal(rate?.status, "pass");
  assert.match(rate!.detail, /44100/);
});

test("an unreachable server lands as a network fail with guidance", async () => {
  const run = await runAutoChecks(deps({
    fetchImpl: async () => { throw new TypeError("Failed to fetch"); },
  }), () => {});
  const network = run.results.find((r) => r.id === "network");
  assert.equal(network?.status, "fail");
  assert.match(network!.detail, /连不上服务器/);
});
