import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { RUNTIME_ERROR_GRACE_MS, pollFailed, pollSucceeded } from "./runtimePollPolicy.ts";

test("距上次成功不足宽限窗的背景丢包不亮错;成功即重置锚点", () => {
  const health = pollSucceeded(10_000);
  assert.equal(pollFailed(health, 11_000, false), false);
  assert.equal(pollFailed(health, 10_000 + RUNTIME_ERROR_GRACE_MS - 1, false), false);
  // 成功一次锚点前移:此后的失败重新从新锚点起算。
  const refreshed = pollSucceeded(60_000);
  assert.equal(pollFailed(refreshed, 61_000, false), false);
});

test("距上次成功超过宽限窗即亮错——悬挂型断网(请求 12s 超时)首败立即呈现", () => {
  const health = pollSucceeded(0);
  // 请求悬挂 12 秒才失败:12s ≥ 4.5s 宽限,第一次失败就必须亮,不再多等一轮。
  assert.equal(pollFailed(health, 12_000, false), true);
  assert.equal(pollFailed(health, RUNTIME_ERROR_GRACE_MS, false), true);
  assert.equal(pollFailed(health, RUNTIME_ERROR_GRACE_MS - 1, false), false);
});

test("前台请求(首载/用户主动刷新)失败立即亮错,不吃宽限", () => {
  assert.equal(pollFailed(pollSucceeded(0), 5, true), true);
});

test("useSessionRuntime 真用了宽限决策;动作拒因走返回值,不写轮询错误槽", () => {
  const source = readFileSync(new URL("./useSessionRuntime.ts", import.meta.url), "utf8");
  // 背景轮询失败必须过 pollFailed 决策,不得直接 setError——一亮错消费方就会
  // 锁死人工面板并向老人端撤游标,单次丢包会掐断正在进行的自助录音。
  assert.match(source, /if \(pollFailed\(pollHealth\.current, Date\.now\(\), foreground\)\)/);
  // 锚点重置必须发生在「成功轮询」路径上(紧邻 setError(null) 的那处);
  // 只有换 session 那处重置时,一次历史失败会永久超窗、宽限完全失效。
  assert.match(source, /pollHealth\.current = pollSucceeded\(Date\.now\(\)\);\s*\n\s*setError\(null\)/);
  // 暂停/恢复的语义拒绝(409 等)必须以 { ok: false, message } 返回给调用方:
  // 轮询错误槽每一拍成功轮询都会清空,拒因写进去等于一闪即逝。
  assert.match(source, /ok: false, message: messageOf\(e\)/);
  assert.doesNotMatch(source, /catch \(e\) \{\s*setError\(/);
});

test("两个控制台把暂停·继续的拒因如实呈现(toast),不静默吞掉", () => {
  const training = readFileSync(
    new URL("../console/scoring/TrainingConsoleScreen.tsx", import.meta.url), "utf8");
  assert.match(training, /toast\(result\.message, "danger"\)/);
  assert.match(training, /暂停尚未生效/);
  const relationship = readFileSync(
    new URL("../console/relationship/RelationshipConsoleScreen.tsx", import.meta.url), "utf8");
  assert.match(relationship, /toast\(result\.message, "danger"\)/);
});

test("SessionControlBar 死胡同文案不得回归:被挡的「继续」必须给真话指引", () => {
  const bar = readFileSync(new URL("../console/SessionControlBar.tsx", import.meta.url), "utf8");
  assert.match(bar, /"暂不能在此继续"/);
  assert.match(bar, /resumeBlockedHint \?\?/);
  // 旧文案是谎话:没有任何在途确认,「等」不会有结果。字符串字面量不得回归。
  assert.doesNotMatch(bar, /等服务器确认后才能继续/);
  assert.doesNotMatch(bar, /\? "等待服务器确认"/);
  const training = readFileSync(
    new URL("../console/scoring/TrainingConsoleScreen.tsx", import.meta.url), "utf8");
  assert.match(training, /resumeBlockedHint=\{observerMode/);
});
