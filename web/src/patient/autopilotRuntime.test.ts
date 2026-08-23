import assert from "node:assert/strict";
import test from "node:test";
import { commandSupersedesTerminalLatch } from "./autopilotRuntime.ts";
import type { NextCommandProjection } from "./autopilotProtocol.ts";

function command(overrides: Partial<NextCommandProjection & {
  command_key: string; command_seq: number; control_generation: number; runner_generation: number;
  command_revision: number;
}> = {}): NextCommandProjection {
  return {
    schema_version: 1,
    command_key: "cmd-runtime-0001",
    command_seq: 3,
    kind: "tts",
    state: "pending",
    command_revision: 0,
    control_generation: 2,
    runner_generation: 2,
    item_ref: "itm-0001",
    turn_seq: 1,
    attempt_seq: 1,
    prompt_level: 0,
    payload: { speech_key: "wk2.01.question", speech_text: "问题", purpose: "question" },
    ...overrides,
  } as NextCommandProjection;
}

test("同时 key 不同、seq 严格更大、两代都严格更大 才判定为新权威", () => {
  const latched = command();
  const candidate = command({
    command_key: "cmd-runtime-0002", command_seq: 4, control_generation: 3, runner_generation: 3,
  });
  assert.equal(commandSupersedesTerminalLatch(latched, candidate), true);
});

test("command_key 相同不算新权威，即便其余字段都前进", () => {
  const latched = command();
  const candidate = command({ command_seq: 4, control_generation: 3, runner_generation: 3 });
  assert.equal(commandSupersedesTerminalLatch(latched, candidate), false);
});

test("command_seq 未严格前进（相等或回退）不算新权威", () => {
  const latched = command();
  const same = command({ command_key: "cmd-runtime-0002", control_generation: 3, runner_generation: 3 });
  const regressed = command({
    command_key: "cmd-runtime-0002", command_seq: 2, control_generation: 3, runner_generation: 3,
  });
  assert.equal(commandSupersedesTerminalLatch(latched, same), false);
  assert.equal(commandSupersedesTerminalLatch(latched, regressed), false);
});

test("两代必须都严格前进；只前进一代不算新权威", () => {
  const latched = command();
  const onlyControl = command({
    command_key: "cmd-runtime-0002", command_seq: 4, control_generation: 3, runner_generation: 2,
  });
  const onlyRunner = command({
    command_key: "cmd-runtime-0002", command_seq: 4, control_generation: 2, runner_generation: 3,
  });
  assert.equal(commandSupersedesTerminalLatch(latched, onlyControl), false);
  assert.equal(commandSupersedesTerminalLatch(latched, onlyRunner), false);
});

test("command_revision 不参与判据：仅 revision 不同、其余同 key 时仍不构成新权威", () => {
  const latched = command();
  const candidate = command({ command_revision: 9 });
  assert.equal(commandSupersedesTerminalLatch(latched, candidate), false);
});

test("external_runtime_released 不覆盖已有暂停与完成判定", async () => {
  const { autopilotRuntimeReducer } = await import("./autopilotRuntime.ts");
  const paused = {
    phase: "paused", command: null, last_device_event_seq: 2, last_ack: null,
    pause_reason: "technical_failure",
  } as const;
  assert.equal(autopilotRuntimeReducer(paused, { type: "external_runtime_released" }), paused);
  const completed = {
    phase: "scope_completed", command: null, last_device_event_seq: 6, last_ack: null,
    pause_reason: null,
  } as const;
  assert.equal(autopilotRuntimeReducer(completed, { type: "external_runtime_released" }), completed);
});

test("交互静默推进:收麦后服务器直接下发下一题问句,跨题 record→tts 是合法接续", async () => {
  const { autopilotRuntimeReducer } = await import("./autopilotRuntime.ts");
  const record = command({
    kind: "record",
    command_key: "cmd-runtime-rec-0001",
    command_seq: 4,
    payload: {
      raw_audio_id: "raw-runtime-0001",
      turn_ref: "itm-0001#1",
      max_duration_seconds: 15,
      contains_direct_identifier: false,
      presentation_speech_key: "wk2.01.question",
      presentation_speech_text: "问题",
      presentation_purpose: "question",
    },
  } as Partial<NextCommandProjection>);
  const waiting = {
    phase: "waiting_server_after_record",
    command: record,
    last_device_event_seq: 5,
    last_ack: null,
    pause_reason: null,
  } as const;
  const nextQuestion = command({
    command_key: "cmd-runtime-next-q-0001",
    command_seq: 5,
    item_ref: "itm-0002",
    turn_seq: 1,
  });
  const reduced = autopilotRuntimeReducer(
    waiting, { type: "server_command", command: nextQuestion });
  assert.equal(reduced.phase, "tts_ready");
  assert.equal(reduced.command?.command_key, "cmd-runtime-next-q-0001");
  assert.equal(reduced.pause_reason, null);
});
