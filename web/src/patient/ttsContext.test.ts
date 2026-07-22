import assert from "node:assert/strict";
import test from "node:test";
import {
  EMPTY_TTS_CONTEXT_STATE,
  rememberTtsLine,
  replayLineForContext,
  transitionTtsContext,
} from "./ttsContext.ts";

const A = "session:S-A|plane:legacy|item:0|turn:0";
const B = "session:S-B|plane:legacy|item:0|turn:0";

test("patient or control-context switch cannot replay the preceding patient's line", () => {
  let state = transitionTtsContext(EMPTY_TTS_CONTEXT_STATE, A).state;
  state = rememberTtsLine(state, { text: "上一位的问题", tag: "itm-a", contextKey: A });
  assert.equal(replayLineForContext(state, A)?.text, "上一位的问题");

  state = transitionTtsContext(state, B).state;
  assert.equal(replayLineForContext(state, B), null);
  assert.equal(replayLineForContext(state, A), null);
});

test("pause clears replay memory and resume waits for newly painted current text", () => {
  let state = transitionTtsContext(EMPTY_TTS_CONTEXT_STATE, A).state;
  state = rememberTtsLine(state, { text: "暂停前的话术", tag: "itm-a", contextKey: A });

  state = transitionTtsContext(state, null).state;
  assert.equal(replayLineForContext(state, A), null);
  state = transitionTtsContext(state, A).state;
  assert.equal(replayLineForContext(state, A), null);

  state = rememberTtsLine(state, { text: "恢复后的当前话术", tag: "itm-a", contextKey: A });
  assert.equal(replayLineForContext(state, A)?.text, "恢复后的当前话术");
});

test("same active bedside context keeps a legitimate toggle-off/on replay", () => {
  let state = transitionTtsContext(EMPTY_TTS_CONTEXT_STATE, A).state;
  state = rememberTtsLine(state, { text: "当前屏幕原文", tag: "itm-a", contextKey: A });

  const unchanged = transitionTtsContext(state, A);
  assert.equal(unchanged.changed, false);
  assert.equal(replayLineForContext(unchanged.state, A)?.text, "当前屏幕原文");
});
