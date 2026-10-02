import assert from "node:assert/strict";
import test from "node:test";

import type { AutopilotVoiceSettings } from "./autopilotMediaTransport.ts";
import {
  AUTOPILOT_VOICE_SETTINGS_REFRESH_MS,
  AutopilotVoiceSettingsCache,
  type AutopilotVoiceSettingsPorts,
} from "./autopilotVoiceSettings.ts";

const SETTINGS: AutopilotVoiceSettings = {
  vad_enabled: true,
  vad_trailing_silence_ms: 3_000,
  vad_short_trailing_silence_ms: 4_500,
  barge_in_enabled: true,
};

interface PendingFetch {
  sessionId: string;
  signal: AbortSignal;
  resolve(value: AutopilotVoiceSettings): void;
  reject(error: unknown): void;
}

/** 每次读取都悬着,由测试决定什么时候、以什么结果落地;时钟手动推进。 */
function harness() {
  let nowMs = 50_000;
  const fetches: PendingFetch[] = [];
  const ports: AutopilotVoiceSettingsPorts = {
    fetch: (sessionId, signal) => new Promise<AutopilotVoiceSettings>((resolve, reject) => {
      fetches.push({ sessionId, signal, resolve, reject });
    }),
    now: () => nowMs,
  };
  return {
    ports,
    fetches,
    advance(ms: number) { nowMs += ms; },
  };
}

async function settle(): Promise<void> {
  for (let turn = 0; turn < 5; turn += 1) await Promise.resolve();
}

test("还没读到语音设置就不武装 VAD;读到 vad_enabled 才给出服务器那两个窗口", async () => {
  const run = harness();
  const cache = new AutopilotVoiceSettingsCache("S-VOICE", run.ports);
  assert.equal(cache.vadWindows(), null);
  // 刷新只发起、不等:同步返回,在途期间照样不武装。
  assert.equal(cache.refreshInBackground(), undefined);
  assert.equal(run.fetches.length, 1);
  assert.equal(run.fetches[0].sessionId, "S-VOICE");
  assert.equal(cache.vadWindows(), null);

  run.fetches[0].resolve({ ...SETTINGS, vad_trailing_silence_ms: 5_000, vad_short_trailing_silence_ms: 7_000 });
  await settle();
  assert.deepEqual(cache.vadWindows(), { trailingSilenceMs: 5_000, shortTrailingSilenceMs: 7_000 });
});

test("服务器关了 VAD:窗口为 null,与 9/21 之前一样只靠按钮和作答窗口", async () => {
  const run = harness();
  const cache = new AutopilotVoiceSettingsCache("S-VOICE", run.ports);
  cache.refreshInBackground();
  run.fetches[0].resolve({ ...SETTINGS, vad_enabled: false });
  await settle();
  assert.equal(cache.vadWindows(), null);
});

test("五分钟内最多读一次;在途时再叫不重复发", async () => {
  const run = harness();
  const cache = new AutopilotVoiceSettingsCache("S-VOICE", run.ports);
  cache.refreshInBackground();
  cache.refreshInBackground();
  assert.equal(run.fetches.length, 1);
  run.fetches[0].resolve(SETTINGS);
  await settle();

  run.advance(AUTOPILOT_VOICE_SETTINGS_REFRESH_MS - 1);
  cache.refreshInBackground();
  assert.equal(run.fetches.length, 1);
  run.advance(1);
  cache.refreshInBackground();
  assert.equal(run.fetches.length, 2);
  assert.equal(AUTOPILOT_VOICE_SETTINGS_REFRESH_MS, 300_000);
});

test("任何一次读取失败(超时、坏形状、网络)都把 VAD 关掉,直到下一次成功", async () => {
  const run = harness();
  const cache = new AutopilotVoiceSettingsCache("S-VOICE", run.ports);
  cache.refreshInBackground();
  run.fetches[0].resolve(SETTINGS);
  await settle();
  assert.notEqual(cache.vadWindows(), null);

  run.advance(AUTOPILOT_VOICE_SETTINGS_REFRESH_MS);
  cache.refreshInBackground();
  // 在途期间沿用上一次成功的设置。
  assert.notEqual(cache.vadWindows(), null);
  run.fetches[1].reject(new Error("语音设置响应不符合封闭契约"));
  await settle();
  assert.equal(cache.vadWindows(), null);

  run.advance(AUTOPILOT_VOICE_SETTINGS_REFRESH_MS);
  cache.refreshInBackground();
  run.fetches[2].resolve(SETTINGS);
  await settle();
  assert.deepEqual(cache.vadWindows(), { trailingSilenceMs: 3_000, shortTrailingSilenceMs: 4_500 });
});

test("读取端口同步抛错:算一次失败,不外抛,五分钟后照常再试", async () => {
  const run = harness();
  let calls = 0;
  const cache = new AutopilotVoiceSettingsCache("S-VOICE", {
    ...run.ports,
    fetch: () => { calls += 1; throw new Error("没有可用的设备凭据"); },
  });
  assert.doesNotThrow(() => cache.refreshInBackground());
  await settle();
  assert.equal(cache.vadWindows(), null);
  run.advance(AUTOPILOT_VOICE_SETTINGS_REFRESH_MS);
  cache.refreshInBackground();
  assert.equal(calls, 2);
});

test("hasSettings:只有缓存里有一份成功读到的设置才为真(服务器关了 VAD 也算读到)", async () => {
  const run = harness();
  const cache = new AutopilotVoiceSettingsCache("S-VOICE", run.ports);
  assert.equal(cache.hasSettings(), false);
  cache.refreshInBackground();
  assert.equal(cache.hasSettings(), false);
  run.fetches[0].resolve({ ...SETTINGS, vad_enabled: false });
  await settle();
  assert.equal(cache.hasSettings(), true);
  assert.equal(cache.vadWindows(), null);

  run.advance(AUTOPILOT_VOICE_SETTINGS_REFRESH_MS);
  cache.refreshInBackground();
  run.fetches[1].reject(new Error("404"));
  await settle();
  assert.equal(cache.hasSettings(), false);
  cache.dispose();
  assert.equal(cache.hasSettings(), false);
});

test("acceptsAckFact:只有最近一次成功读面声明过的事实名才为真", async () => {
  const run = harness();
  const cache = new AutopilotVoiceSettingsCache("S-VOICE", run.ports);
  // 还没读过、读取在途:一律不发,宁可这一句缺这两个键。
  assert.equal(cache.acceptsAckFact("barge_in_armed"), false);
  cache.refreshInBackground();
  assert.equal(cache.acceptsAckFact("barge_in_armed"), false);
  run.fetches[0].resolve({ ...SETTINGS, ack_facts: ["barge_in_armed", "barge_in_listened_ms"] });
  await settle();
  assert.equal(cache.acceptsAckFact("barge_in_armed"), true);
  assert.equal(cache.acceptsAckFact("barge_in_listened_ms"), true);

  // 读失败:不知道现在的服务端认不认,当不认。
  run.advance(AUTOPILOT_VOICE_SETTINGS_REFRESH_MS);
  cache.refreshInBackground();
  run.fetches[1].reject(new Error("语音设置读取超时"));
  await settle();
  assert.equal(cache.acceptsAckFact("barge_in_armed"), false);

  // 四键 = 不认这些键的服务端(含回滚后的 af59c28):设置照用,事实一个都不认。
  run.advance(AUTOPILOT_VOICE_SETTINGS_REFRESH_MS);
  cache.refreshInBackground();
  run.fetches[2].resolve(SETTINGS);
  await settle();
  assert.equal(cache.hasSettings(), true);
  assert.equal(cache.acceptsAckFact("barge_in_armed"), false);
  assert.equal(cache.acceptsAckFact("barge_in_listened_ms"), false);

  // 逐个名字认:只声明了 armed 就只报 armed。
  run.advance(AUTOPILOT_VOICE_SETTINGS_REFRESH_MS);
  cache.refreshInBackground();
  run.fetches[3].resolve({ ...SETTINGS, ack_facts: ["barge_in_armed"] });
  await settle();
  assert.equal(cache.acceptsAckFact("barge_in_armed"), true);
  assert.equal(cache.acceptsAckFact("barge_in_listened_ms"), false);
  cache.dispose();
  assert.equal(cache.acceptsAckFact("barge_in_armed"), false);
});

test("dispose:中止在途读取,迟到的结果不再生效,之后也不再刷新", async () => {
  const run = harness();
  const cache = new AutopilotVoiceSettingsCache("S-VOICE", run.ports);
  cache.refreshInBackground();
  const pending = run.fetches[0];
  cache.dispose();
  assert.equal(pending.signal.aborted, true);
  pending.resolve(SETTINGS);
  await settle();
  assert.equal(cache.vadWindows(), null);
  run.advance(AUTOPILOT_VOICE_SETTINGS_REFRESH_MS * 2);
  cache.refreshInBackground();
  assert.equal(run.fetches.length, 1);
});
