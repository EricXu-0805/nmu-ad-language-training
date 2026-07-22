import assert from "node:assert/strict";
import test from "node:test";
import type { NextCommandProjection } from "./autopilotProtocol.ts";
import { PatientAssetGateError, PatientAssetMediaGate } from "./patientAssetMediaGate.ts";

function command(commandKey: string): NextCommandProjection {
  return {
    schema_version: 1,
    command_key: commandKey,
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
    payload: {
      speech_key: "question.current",
      speech_text: "请看这张图片。",
      purpose: "question",
    },
  };
}

test("the exact command remains blocked until its decoded image reports ready", async () => {
  const gate = new PatientAssetMediaGate();
  let settled = false;
  const waiting = gate.waitFor(
    command("cmd-current-0001"),
    new AbortController().signal,
  ).then(() => { settled = true; });
  await Promise.resolve();
  assert.equal(settled, false);
  gate.report("cmd-current-0001", "loading");
  await Promise.resolve();
  assert.equal(settled, false);
  gate.report("cmd-current-0001", "ready");
  await waiting;
  assert.equal(settled, true);
});

test("decode failure and command switching reject waiters fail closed", async () => {
  const gate = new PatientAssetMediaGate();
  const failed = gate.waitFor(command("cmd-current-0001"), new AbortController().signal);
  gate.report("cmd-current-0001", "failed");
  await assert.rejects(failed, PatientAssetGateError);

  const stale = gate.waitFor(command("cmd-current-0001"), new AbortController().signal);
  gate.report("cmd-current-0002", "loading");
  await assert.rejects(stale, PatientAssetGateError);
  const current = gate.waitFor(command("cmd-current-0002"), new AbortController().signal);
  gate.report("cmd-current-0002", "ready");
  await current;
});

test("controller shutdown aborts a pending image waiter", async () => {
  const gate = new PatientAssetMediaGate();
  const controller = new AbortController();
  const waiting = gate.waitFor(command("cmd-current-0001"), controller.signal);
  controller.abort(new DOMException("媒体停止", "AbortError"));
  await assert.rejects(waiting, (error: unknown) => error instanceof DOMException
    && error.name === "AbortError");
});
