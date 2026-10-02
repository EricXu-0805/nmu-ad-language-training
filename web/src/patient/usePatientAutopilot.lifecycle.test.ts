import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  captureIdentityIsNewer,
  retainBargeInObservation,
  sameCaptureIdentity,
  type LocalAutopilotCaptureEvent,
  type LocalAutopilotCapturePhase,
} from "./autopilotCapturePresentation.ts";
import type { BargeInObservation } from "./autopilotBrowserBargeIn.ts";
import type { NextCommandProjection } from "./autopilotProtocol.ts";

const SOURCE = readFileSync(
  new URL("./usePatientAutopilot.ts", import.meta.url), "utf8");

const SESSION = "S-HOOK-LIFECYCLE";
const COMMAND = "cmd-record-hook-0001";
const IDENTITY = {
  sessionId: SESSION,
  commandKey: COMMAND,
  ownerGeneration: 2,
  captureGeneration: 1,
};

/**
 * 复刻 hook 里 `observeCapturePhase` 的那条 reducer。
 *
 * 它和生产共用同两个判据函数（`sameCaptureIdentity` / `captureIdentityIsNewer`），
 * 所以这里测的是真实的过滤规则，不是另写一套。hook 本身要在真实 React 里跑才
 * 能测到 effect 时序，那部分由下面的窄 wiring anchor 承担。
 */
function reduceCapturePhase(
  current: LocalAutopilotCapturePhase | null,
  event: LocalAutopilotCaptureEvent,
  currentSessionId: string,
  onScreenCommandKey: string | null,
): LocalAutopilotCapturePhase | null {
  if (event.sessionId !== currentSessionId) return current;
  if (event.phase === "cleared") {
    return sameCaptureIdentity(current, event) ? null : current;
  }
  if (!captureIdentityIsNewer(event, current)) return current;
  if (onScreenCommandKey !== event.commandKey) return current;
  return event;
}

test("listening 事件驱动当前命令的屏显", () => {
  const listening: LocalAutopilotCapturePhase = { ...IDENTITY, phase: "listening" };
  assert.deepEqual(
    reduceCapturePhase(null, listening, SESSION, COMMAND), listening);
});

test("旧场次的事件一律不进状态", () => {
  const stale: LocalAutopilotCaptureEvent = {
    ...IDENTITY, sessionId: "S-OLD", phase: "listening",
  };
  assert.equal(reduceCapturePhase(null, stale, SESSION, COMMAND), null);
});

test("屏幕已经翻到下一题时，迟到的 listening 既不抢也不清", () => {
  const listening: LocalAutopilotCapturePhase = { ...IDENTITY, phase: "listening" };
  assert.equal(
    reduceCapturePhase(null, listening, SESSION, "cmd-record-hook-0002"), null);
});

test("同一次捕获的 listening → persisting 正常推进", () => {
  const listening: LocalAutopilotCapturePhase = { ...IDENTITY, phase: "listening" };
  const persisting: LocalAutopilotCapturePhase = {
    ...IDENTITY, phase: "persisting", stopReason: "user_done",
  };
  const afterListening = reduceCapturePhase(null, listening, SESSION, COMMAND);
  assert.deepEqual(
    reduceCapturePhase(afterListening, persisting, SESSION, COMMAND), persisting);
});

test("只有身份全等的 clear 才清得掉；旧 owner 或旧 capture 的 clear 无效", () => {
  const listening: LocalAutopilotCapturePhase = {
    ...IDENTITY, ownerGeneration: 3, captureGeneration: 5, phase: "listening",
  };
  const live = reduceCapturePhase(null, listening, SESSION, COMMAND);

  for (const drift of [
    { ownerGeneration: 2 },
    { captureGeneration: 4 },
    { commandKey: "cmd-record-hook-0002" },
  ]) {
    assert.deepEqual(
      reduceCapturePhase(live, { ...listening, ...drift, phase: "cleared" },
        SESSION, COMMAND),
      live,
      "旧身份的 clear 不得清掉活着的 listening",
    );
  }

  assert.equal(
    reduceCapturePhase(live, { ...listening, phase: "cleared" }, SESSION, COMMAND),
    null,
  );
});

test("新一代 capture 已经在屏上时，上一代的迟到事件不得覆盖", () => {
  const fresh: LocalAutopilotCapturePhase = {
    ...IDENTITY, captureGeneration: 6, phase: "listening",
  };
  const live = reduceCapturePhase(null, fresh, SESSION, COMMAND);
  const late: LocalAutopilotCaptureEvent = {
    ...IDENTITY, captureGeneration: 5, phase: "persisting", stopReason: "max_duration",
  };

  assert.deepEqual(reduceCapturePhase(live, late, SESSION, COMMAND), live);
});

// ---------------- 窄 wiring anchor ----------------
//
// 行为证据在 autopilotPageLifecycle.test.ts（真实 setup→cleanup→setup、真卸载、
// 三个同步事件）。这里只钉住"hook 真的接的是那个生产 coordinator，而且接法没
// 退化"，不承担主要证据。真实浏览器 StrictMode journey 仍列为未验证项。

test("hook 装的是生产 coordinator，并且每个 owner generation 装一次、按序摘掉", () => {
  assert.match(SOURCE, /new AutopilotPageLifecycle\(/);
  assert.match(SOURCE, /browserAutopilotLifecycleHost\(\)/);
  assert.match(SOURCE, /ownerGeneration: server\.ownerGeneration/);
  assert.match(SOURCE, /lifecycle\.install\(\)/);
  assert.match(SOURCE, /lifecycle\.uninstall\(\)/);
});

test("mount-only effect 只登记候选与取消候选，不在 cleanup 当下判定卸载", () => {
  const mountOnly = SOURCE.slice(SOURCE.indexOf("lifecycleRef.current?.cancelTeardown()"));
  assert.match(mountOnly, /cancelTeardown\(\)/);
  assert.match(mountOnly, /requestTeardown\(\)/);
  // cleanup 里绝不能直接关麦或直接停控制器。
  const cleanupLine = mountOnly.slice(0, mountOnly.indexOf("}, []);"));
  assert.doesNotMatch(cleanupLine, /stopAndWait/);
  assert.doesNotMatch(cleanupLine, /interruptRecordingForLifecycleAndWait/);
});

test("生命周期赢下之后，hook 清理复用同一条 shutdown，不再走 stopAndWait 吞 ACK", () => {
  assert.match(SOURCE, /lifecycleRef\.current\?\.aborted\s*\n?\s*\?\s*\w+\.interruptRecordingForLifecycleAndWait\(\)/);
  // 反向：普通清理仍然走原来的 stopAndWait，不得被误报成 device_runtime_failed。
  assert.match(SOURCE, /: \w+\.stopAndWait\(\)\)/);
});

test("pre-start 被生命周期中断之后本窗口停止 tick，重新可见不自动恢复", () => {
  assert.match(SOURCE, /if \(lifecycleRef\.current\?\.aborted\) return;/);
  assert.match(SOURCE, /if \(cancelled \|\| lifecycleRef\.current\?\.aborted\) return;/);
});

test("录音执行器拿到的是本次 owner 代际，屏显事件才可能被正确过滤", () => {
  assert.match(SOURCE, /ownerGeneration: context\.ownerGeneration/);
  assert.match(SOURCE, /observe: observeCapturePhase/);
});

const INTERRUPTED: BargeInObservation = {
  sessionId: SESSION, commandKey: "cmd-question-hook-0001", itemRef: "itm-0001",
  turnSeq: 1, attemptSeq: 1, controlGeneration: 3, runnerGeneration: 7,
  phase: "interrupted",
};

function speech(commandKey: string, purpose: "question" | "feedback"): NextCommandProjection {
  return {
    schema_version: 1, command_key: commandKey, command_seq: 3, kind: "tts", state: "pending",
    command_revision: 0, control_generation: 3, runner_generation: 7, item_ref: "itm-0001",
    turn_seq: 1, attempt_seq: 1, prompt_level: 0,
    payload: { speech_key: `wk2.01.${purpose}`, speech_text: "说得很好。", purpose },
  };
}

function record(): NextCommandProjection {
  return {
    schema_version: 1, command_key: "cmd-record-hook-0002", command_seq: 2, kind: "record",
    state: "pending", command_revision: 0, control_generation: 3, runner_generation: 7,
    item_ref: "itm-0001", turn_seq: 1, attempt_seq: 1, prompt_level: 0,
    payload: {
      raw_audio_id: "raw-hook-0001", turn_ref: "itm-0001#1", max_duration_seconds: 15,
      contains_direct_identifier: false, presentation_speech_key: "wk2.01.question",
      presentation_speech_text: "请说出图片中的物品。", presentation_purpose: "question",
    },
  };
}

test("开口打断的观察留到它那条录音为止:换成别的朗读命令(反馈、重播)就清掉", () => {
  // 被打断的那一句本身、紧接着的录音、命令暂时为空:都还要提醒从头说。
  assert.equal(retainBargeInObservation(INTERRUPTED, speech(INTERRUPTED.commandKey, "question")),
    INTERRUPTED);
  assert.equal(retainBargeInObservation(INTERRUPTED, record()), INTERRUPTED);
  assert.equal(retainBargeInObservation(INTERRUPTED, null), INTERRUPTED);
  // 反馈句、重播的提问:这次打断已经翻篇。
  assert.equal(retainBargeInObservation(INTERRUPTED, speech("cmd-feedback-hook-0003", "feedback")), null);
  assert.equal(retainBargeInObservation(INTERRUPTED, speech("cmd-question-hook-0004", "question")), null);
  // 其他相位的观察(检测中、不可用)由各自的 monitor 自己收尾,不在这里改。
  const monitoring = { ...INTERRUPTED, phase: "monitoring" as const };
  assert.equal(retainBargeInObservation(monitoring, speech("cmd-feedback-hook-0003", "feedback")), monitoring);
  assert.equal(retainBargeInObservation(null, record()), null);
});

test("runner 每次状态推进都用同一个规则收拢打断观察", () => {
  assert.match(SOURCE, /setBargeIn\(\(before\) => retainBargeInObservation\(before, runtime\.command\)\)/);
});

test("语音设置按场次缓存;开麦只读缓存,刷新在 runner 每一拍后台发起、从不 await", () => {
  // 场次一换就换一份缓存,上一场的设置不进这一场。
  assert.match(SOURCE, /voiceSettings\?\.sessionId !== sessionId/);
  assert.match(SOURCE, /new AutopilotVoiceSettingsCache\(\s*sessionId,/);
  assert.match(SOURCE, /fetchAutopilotVoiceSettings\(/);
  // 执行器拿到的是读缓存的函数,不是一次网络请求。
  assert.match(SOURCE, /voiceActivityWindows: \(\) => voiceSettings\.vadWindows\(\)/);
  // tick 里先顺手发起刷新、再进 pollOnce;刷新的返回值不进任何 await。
  const tick = SOURCE.slice(SOURCE.indexOf("const tick = async () => {"));
  const refreshAt = tick.indexOf("voiceSettings.refreshInBackground();");
  const pollAt = tick.indexOf(".pollOnce()");
  assert.ok(refreshAt > 0 && pollAt > refreshAt, "刷新必须在 pollOnce 之前发起");
  assert.doesNotMatch(SOURCE, /await\s+voiceSettings/);
  assert.doesNotMatch(SOURCE, /await\s+[\w.]*refreshInBackground/);
});
