import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// P0-3 / P1-7 / P1-9 回归:老人端不在线时录音入口要拦住并说人话;
// 「正在显示」只许在线时断言;录音过程与看门狗都有可见计时。
const source = readFileSync(new URL("./TrainingConsoleScreen.tsx", import.meta.url), "utf8");

test("P0-3:老人端不在线时,示意录音按钮禁用且给可见短句原因(不是 title)", () => {
  assert.match(source, /disabled=\{work\.locked \|\| patientLinkDown \|\| patientScreenPaused\}/);
  assert.match(source, /老人端未连接——请先在老人端完成配对/);
  // armRecording 里对人工路径再拦一次(键盘/竞态下按钮禁用可能被绕过)。
  assert.match(source, /if \(!autoPilotRef\.current && \(patientLinkDown \|\| patientScreenPaused\)\) \{/);
  const cuePanel = source.slice(source.indexOf("function CueButtons"));
  assert.doesNotMatch(cuePanel, /title=\{text == null/);
});

test("D5②:老人端还停在暂停画面时,录音入口禁用并给可见原因——不许对暂停屏开麦", () => {
  // presence 报在线且 screen=paused(患者暂停闩/暂停画面未解除)即为门。
  assert.match(source, /const patientScreenPaused = presence\.state === "online" && presence\.screen === "paused";/);
  assert.match(source, /老人端还停在暂停画面/);
});

test("P0-3/P1-7:「老人端正在显示」只在 presence 在线时断言;离线改说已发送未送达", () => {
  assert.match(source, /patientOnline\s*\?\s*`老人端正在显示：\$\{current\}`/);
  assert.match(source, /已发送，但老人端此刻不在线，连接后会显示/);
});

test("D4②:老人端在暂停画面时「正在显示」必须如实说暂停,不许断言线索已上屏", () => {
  assert.match(source, /patientScreenPaused\s*\?\s*`老人端正在显示暂停画面/);
});

test("训练屏有常驻的老人端连接状态点(在线/断开/未连接)", () => {
  assert.match(source, /老人端在线/);
  assert.match(source, /老人端已断开/);
  assert.match(source, /老人端未连接/);
  // presence 由 ConsoleShell 传入,本屏不自己再开一个 console-state 轮询。
  assert.doesNotMatch(source, /usePatientPresence\(/);
});

test("P1-9:armed 期间显示 mm:ss 计时;停止后看门狗剩余秒数可见", () => {
  assert.match(source, /录音中 \{formatRecordingClock\(Date\.now\(\) - recArmedAt\)\}/);
  assert.match(source, /正在等老人端回传录音…还剩 \{watchdogSecondsLeft\(watchdogUntil, Date\.now\(\)\)\} 秒/);
  // 计时器与 armed 态同生共死。
  assert.match(source, /if \(recState !== "armed"\) setRecArmedAt\(null\);/);
});

test("P0-4 UI:收到录音后显示条数与最新时长,并保留可试听回放条", () => {
  assert.match(source, /已收到录音 \{Math\.max\(1, Object\.values\(journal\.audios\)/);
  assert.match(source, /AuthenticatedAudio rawAudioId=\{pendingAudio\[turnK\]\.rawAudioId\}/);
});
