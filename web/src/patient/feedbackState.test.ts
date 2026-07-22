import assert from "node:assert/strict";
import test from "node:test";
import { advanceFeedbackPlayback, initialFeedbackPlaybackState } from "./feedbackState.ts";

test("does not replay a feedback sequence restored by the first connected snapshot", () => {
  const disconnected = advanceFeedbackPlayback(initialFeedbackPlaybackState(), false, 7);
  assert.equal(disconnected.shouldSpeak, false);

  const connected = advanceFeedbackPlayback(disconnected.state, true, 7);
  assert.equal(connected.shouldSpeak, false);
  assert.equal(connected.state.lastSeq, 7);
});

test("speaks the first feedback created after a connected baseline without feedback", () => {
  const baseline = advanceFeedbackPlayback(initialFeedbackPlaybackState(), true, undefined);
  const firstNew = advanceFeedbackPlayback(baseline.state, true, 1);

  assert.equal(firstNew.shouldSpeak, true);
  assert.equal(firstNew.state.lastSeq, 1);
});

test("deduplicates a delivered feedback sequence and speaks the next one", () => {
  const baseline = advanceFeedbackPlayback(initialFeedbackPlaybackState(), true, undefined);
  const first = advanceFeedbackPlayback(baseline.state, true, 1);
  const duplicate = advanceFeedbackPlayback(first.state, true, 1);
  const second = advanceFeedbackPlayback(duplicate.state, true, 2);

  assert.equal(duplicate.shouldSpeak, false);
  assert.equal(second.shouldSpeak, true);
});
