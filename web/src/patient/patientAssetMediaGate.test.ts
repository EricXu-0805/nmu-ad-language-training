import assert from "node:assert/strict";
import test from "node:test";
import type { NextCommandProjection } from "./autopilotProtocol.ts";
import {
  PatientAssetGateError, PatientAssetMediaGate, patientStimulusKey,
} from "./patientAssetMediaGate.ts";

function command(
  commandKey: string,
  position: { item_ref?: string; turn_seq?: number } = {},
): NextCommandProjection {
  return {
    schema_version: 1,
    command_key: commandKey,
    command_seq: 1,
    kind: "tts",
    state: "pending",
    command_revision: 0,
    control_generation: 1,
    runner_generation: 1,
    item_ref: position.item_ref ?? "itm-0001",
    turn_seq: position.turn_seq ?? 1,
    attempt_seq: 1,
    prompt_level: 0,
    payload: {
      speech_key: "question.current",
      speech_text: "请看这张图片。",
      purpose: "question",
    },
  };
}

/** 提问 TTS 之后的录音命令：换了 command_key，题与轮都没换。 */
function recordAfter(question: NextCommandProjection): NextCommandProjection {
  return {
    schema_version: 1,
    command_key: `${question.command_key}-record`,
    command_seq: question.command_seq + 1,
    kind: "record",
    state: "pending",
    command_revision: 0,
    control_generation: question.control_generation,
    runner_generation: question.runner_generation,
    item_ref: question.item_ref,
    turn_seq: question.turn_seq,
    attempt_seq: question.attempt_seq,
    prompt_level: question.prompt_level,
    payload: {
      raw_audio_id: "raw-gate-0001",
      turn_ref: `${question.item_ref}#${question.turn_seq}`,
      max_duration_seconds: 15,
      contains_direct_identifier: false,
      presentation_speech_key: "question.current",
      presentation_speech_text: "请看这张图片。",
      presentation_purpose: "question",
    },
  };
}

const KEY_0001 = patientStimulusKey(command("cmd-current-0001"));

test("the exact stimulus remains blocked until its decoded image reports ready", async () => {
  const gate = new PatientAssetMediaGate();
  let settled = false;
  const waiting = gate.waitFor(
    command("cmd-current-0001"),
    new AbortController().signal,
  ).then(() => { settled = true; });
  await Promise.resolve();
  assert.equal(settled, false);
  gate.report(KEY_0001, "loading");
  await Promise.resolve();
  assert.equal(settled, false);
  gate.report(KEY_0001, "ready");
  await waiting;
  assert.equal(settled, true);
});

test("decode failure and item switching reject waiters fail closed", async () => {
  const gate = new PatientAssetMediaGate();
  const failed = gate.waitFor(command("cmd-current-0001"), new AbortController().signal);
  gate.report(KEY_0001, "failed");
  await assert.rejects(failed, PatientAssetGateError);

  const stale = gate.waitFor(command("cmd-current-0001"), new AbortController().signal);
  const nextItem = command("cmd-current-0002", { item_ref: "itm-0002" });
  gate.report(patientStimulusKey(nextItem), "loading");
  await assert.rejects(stale, PatientAssetGateError);
  const current = gate.waitFor(nextItem, new AbortController().signal);
  gate.report(patientStimulusKey(nextItem), "ready");
  await current;
});

test("同一题同一轮的录音命令直接沿用提问已经就绪的题图：不重置门禁、不重下同一张图", async () => {
  // 2026-09-17 养老院实测:提问播完→开麦 1.6 s 里有一段是录音命令(新 command_key)
  // 把门禁重置成 loading、ImagePane 把同一张图再下载解码一遍。
  const question = command("cmd-current-0001");
  const record = recordAfter(question);
  assert.equal(patientStimulusKey(record), patientStimulusKey(question));
  // ImagePane 的 requestKey 就是这个键：键不变，effect 不重跑，图不重下。
  assert.notEqual(record.command_key, question.command_key);

  const gate = new PatientAssetMediaGate();
  await (async () => {
    const waiting = gate.waitFor(question, new AbortController().signal);
    gate.report(patientStimulusKey(question), "ready");
    await waiting;
  })();
  // 录音命令的等待当场就绪，一次 loading 都不经过。
  let settled = false;
  const recordWait = gate.waitFor(record, new AbortController().signal)
    .then(() => { settled = true; });
  await Promise.resolve();
  assert.equal(settled, true);
  await recordWait;
});

test("键只认投影自带的位置事实：换题或换轮都是新键，绝不跨题复用字节", () => {
  const question = command("cmd-current-0001");
  assert.notEqual(
    patientStimulusKey(command("cmd-current-0002", { item_ref: "itm-0002" })),
    patientStimulusKey(question));
  assert.notEqual(
    patientStimulusKey(command("cmd-current-0003", { turn_seq: 2 })),
    patientStimulusKey(question));
  // item_ref 与 turn_seq 之间有分隔符：拼接不会把 "itm-1" + 11 与 "itm-11" + 1 混成一个键。
  assert.notEqual(
    patientStimulusKey({ item_ref: "itm-1", turn_seq: 11 }),
    patientStimulusKey({ item_ref: "itm-11", turn_seq: 1 }));
});

test("controller shutdown aborts a pending image waiter", async () => {
  const gate = new PatientAssetMediaGate();
  const controller = new AbortController();
  const waiting = gate.waitFor(command("cmd-current-0001"), controller.signal);
  controller.abort(new DOMException("媒体停止", "AbortError"));
  await assert.rejects(waiting, (error: unknown) => error instanceof DOMException
    && error.name === "AbortError");
});
