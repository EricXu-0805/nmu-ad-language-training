import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import type { AutopilotTerminalFailureLatch } from "./autopilotAckOutbox.ts";
import {
  canStartAutopilotRunner,
  isAutopilotRuntimeTerminal,
  shouldBootstrapAutopilotRunner,
  shouldSchedulePassiveAutopilotPoll,
} from "./autopilotHookRuntimeGate.ts";
import {
  restoreAutopilotRuntimeAfterRefresh,
  type AutopilotRecoveryDependencies,
} from "./autopilotRecordingRecovery.ts";
import type { AutopilotAck, NextCommandProjection } from "./autopilotProtocol.ts";
import type { AutopilotRuntimeState } from "./autopilotRuntime.ts";

test("isAutopilotRuntimeTerminal 只认 paused 与 scope_completed", () => {
  assert.equal(isAutopilotRuntimeTerminal("paused"), true);
  assert.equal(isAutopilotRuntimeTerminal("scope_completed"), true);
  for (const phase of [
    "waiting_command", "tts_ready", "tts_playing", "waiting_server_after_tts",
    "record_ready", "recording", "waiting_server_after_record",
  ] as const) {
    assert.equal(isAutopilotRuntimeTerminal(phase), false, phase);
  }
});

test("shouldBootstrapAutopilotRunner / shouldSchedulePassiveAutopilotPoll 在终态时一致地拒绝", () => {
  assert.equal(shouldBootstrapAutopilotRunner("paused"), false);
  assert.equal(shouldBootstrapAutopilotRunner("scope_completed"), false);
  assert.equal(shouldBootstrapAutopilotRunner("waiting_command"), true);
  assert.equal(shouldSchedulePassiveAutopilotPoll("paused"), false);
  assert.equal(shouldSchedulePassiveAutopilotPoll("record_ready"), true);
});

test("canStartAutopilotRunner：即使 command 是 null，paused 的 runtime 也不得起 controller", () => {
  const pausedWithoutCommand: AutopilotRuntimeState = {
    phase: "paused", command: null, last_device_event_seq: 3, last_ack: null,
    pause_reason: "technical_failure",
  };
  assert.equal(canStartAutopilotRunner(pausedWithoutCommand), false);
  const waiting: AutopilotRuntimeState = {
    phase: "waiting_command", command: null, last_device_event_seq: 3, last_ack: null, pause_reason: null,
  };
  assert.equal(canStartAutopilotRunner(waiting), true);
});

// ---------------- 同一次 Hook 生命周期：owner load → 第二阶段重取 → controller 闸 ----------------

function pendingTts(): NextCommandProjection {
  return {
    schema_version: 1,
    command_key: "cmd-gate-question-0001",
    command_seq: 1,
    kind: "tts",
    state: "pending",
    command_revision: 0,
    control_generation: 1,
    runner_generation: 1,
    item_ref: "itm-0001",
    turn_seq: 1,
    attempt_seq: 1,
    prompt_level: 0,
    payload: { speech_key: "wk2.01.question", speech_text: "问题", purpose: "question" },
  };
}

function latchFor(command: NextCommandProjection): AutopilotTerminalFailureLatch {
  return {
    commandKey: command.command_key,
    command,
    ack: {
      idempotency_key: "ack.tts_failed.5.gate",
      ack_type: "tts_failed",
      error_code: "audio_playback_failed",
      control_generation: command.control_generation,
      runner_generation: command.runner_generation,
      command_revision: command.command_revision,
      device_event_seq: 5,
    } as unknown as AutopilotAck,
  };
}

function noOutboxDeps(): AutopilotRecoveryDependencies {
  return {
    acquireLease: async () => {
      throw new Error("不应触发麦克风租约：这次 latch 场景不是录音命令");
    },
    recoverySnapshot: async () => ({ entries: [], invalidBlobKeyCount: 0, legacyOrphans: [] }),
    readBlob: async () => null,
    finish: async () => { throw new Error("不应触发音频收尾"); },
  };
}

test("同一次挂载：owner load 落 paused 之后，第二阶段无 drained 的重取仍然是 paused，且两个闸都拒绝继续", async () => {
  const command = pendingTts();
  const latch = latchFor(command);
  const delivery = { initialDeviceEventSeq: 5, terminalFailureLatch: latch, send: async () => { throw new Error("不应再次发送"); } };

  // 第一阶段：owner probe 的 drainPending 已经把这次失败记进 latch（模拟已经
  // 发生过一次 drainPending → deliver → 本地内存 latch 生效)，owner load 本身
  // 用 drained 恢复出 paused seed。
  const seed = await restoreAutopilotRuntimeAfterRefresh({
    sessionId: "S-GATE", next: async () => null, delivery, deps: noOutboxDeps(),
    drained: { command, ack: latch.ack },
  });
  assert.equal(seed.phase, "paused");
  assert.equal(shouldBootstrapAutopilotRunner(seed.phase), false);

  // 第二阶段：mediaAllowed=false 的被动轮询、或 mediaAllowed=true 的 bootstrap，
  // 都会不带 drained 再调一次同一个恢复函数。即使真的被调用，latch 仍然存在
  // （同一个 delivery 实例），reconcile 必须继续给出 paused，不是 waiting_command。
  const secondStage = await restoreAutopilotRuntimeAfterRefresh({
    sessionId: "S-GATE", next: async () => null, delivery, deps: noOutboxDeps(),
  });
  assert.equal(secondStage.phase, "paused");
  assert.equal(secondStage.pause_reason, "tts_failed");
  assert.equal(shouldSchedulePassiveAutopilotPoll(secondStage.phase), false);
  assert.equal(shouldBootstrapAutopilotRunner(secondStage.phase), false);
  assert.equal(canStartAutopilotRunner(secondStage), false);
});

// ---------------- 源码接线守卫：闸函数必须真的被 usePatientAutopilot 调用 ----------------

test("源码接线守卫：usePatientAutopilot 确实调用了 bootstrap/should-poll/should-start-controller 三个闸", () => {
  const hookPath = join(dirname(fileURLToPath(import.meta.url)), "usePatientAutopilot.ts");
  const source = readFileSync(hookPath, "utf8");
  assert.match(source, /shouldBootstrapAutopilotRunner\(/);
  assert.match(source, /shouldSchedulePassiveAutopilotPoll\(/);
  assert.match(source, /canStartAutopilotRunner\(/);
  // 闸必须挡在 controller 构造之前，不能只是名义上出现在文件里。
  const bootstrapGateIndex = source.indexOf("shouldBootstrapAutopilotRunner(");
  const controllerConstructIndex = source.indexOf("new PatientAutopilotController(");
  assert.ok(bootstrapGateIndex >= 0 && controllerConstructIndex > bootstrapGateIndex);
  const startGateIndex = source.indexOf("canStartAutopilotRunner(");
  assert.ok(startGateIndex >= 0 && controllerConstructIndex > startGateIndex);
  // 必须传完整 initialRuntime（restored.runtime），不能只传 initialCommand/
  // restored.current——否则一个 paused-with-null-command 的 seed 会被控制器
  // 自己的 restoreAutopilotRuntime(null) 重建成 waiting_command。
  assert.match(source, /initialRuntime:\s*restored\.runtime/);
  assert.doesNotMatch(source, /initialCommand:\s*restored\.current/);
});
