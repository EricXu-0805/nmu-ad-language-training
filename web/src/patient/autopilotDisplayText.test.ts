import assert from "node:assert/strict";
import test from "node:test";
import type { NextCommandProjection } from "./autopilotProtocol.ts";
import { resolveExactAutopilotDisplayText } from "./autopilotDisplayText.ts";

function tts(purpose: "question" | "cue" | "feedback" | "tell_answer" = "question"):
NextCommandProjection {
  const promptLevel = purpose === "cue" ? 1 : purpose === "tell_answer" ? 3 : 0;
  return {
    schema_version: 1, command_key: "cmd-display-tts-0001", command_seq: 7,
    kind: "tts", state: "pending", command_revision: 0,
    control_generation: 2, runner_generation: 4,
    item_ref: "itm-0001", turn_seq: 1,
    attempt_seq: 1, prompt_level: promptLevel,
    payload: { speech_key: "display.exact", speech_text: "这是服务器签发的当前问题。", purpose },
  };
}

function record(
  promptLevel: 0 | 1 | 2 | 3 = 0,
  presentationText = "这是服务器签发的当前问题。",
): NextCommandProjection {
  const presentationPurpose = promptLevel === 0 ? "question" as const : "cue" as const;
  return {
    schema_version: 1, command_key: "cmd-display-rec-0002", command_seq: 8,
    kind: "record", state: "pending", command_revision: 0,
    control_generation: 2, runner_generation: 4,
    item_ref: "itm-0001", turn_seq: 1,
    attempt_seq: 1, prompt_level: promptLevel,
    payload: {
      raw_audio_id: "raw-display-0001", turn_ref: "itm-0001#1",
      max_duration_seconds: 12, contains_direct_identifier: false,
      presentation_speech_key: `display.${presentationPurpose}`,
      presentation_speech_text: presentationText,
      presentation_purpose: presentationPurpose,
    },
  };
}

test("the exact server question remains visible throughout its following record command", () => {
  const question = resolveExactAutopilotDisplayText(null, tts());
  const duringRecord = resolveExactAutopilotDisplayText(question, record());
  assert.equal(duringRecord?.text, "这是服务器签发的当前问题。");
});

test("an exact cue remains visible and refresh-during-record uses the record projection itself", () => {
  const cue = resolveExactAutopilotDisplayText(null, tts("cue"));
  assert.equal(resolveExactAutopilotDisplayText(cue, record(1))?.text, cue?.text);
  assert.equal(resolveExactAutopilotDisplayText(
    null, record(1, "刷新后仍由服务器提供的提示。"),
  )?.text, "刷新后仍由服务器提供的提示。");
});

test("a record from another sequence or context cannot leak the previous prompt", () => {
  const question = resolveExactAutopilotDisplayText(null, tts());
  const other = record(0, "另一个服务器签发的问题。");
  assert.equal(resolveExactAutopilotDisplayText(question, {
    ...other, command_seq: 9, runner_generation: 5,
  })?.text, "另一个服务器签发的问题。");
});

test("a transient null command preserves the exact prompt until the next signed command", () => {
  const question = resolveExactAutopilotDisplayText(null, tts());
  assert.equal(resolveExactAutopilotDisplayText(question, null), question);
});
