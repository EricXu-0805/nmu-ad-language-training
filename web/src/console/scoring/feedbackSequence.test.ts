import assert from "node:assert/strict";
import test from "node:test";
import { nextFeedbackSequence, observeFeedbackSequence } from "./feedbackSequence.ts";

test("feedback sequence remains monotonic across component remount-style calls", () => {
  const beforeUnmount = nextFeedbackSequence();
  const afterRemount = nextFeedbackSequence();
  assert.ok(afterRemount > beforeUnmount);
});

test("a restored cursor advances the module-level feedback sequence floor", () => {
  const restored = Date.now() + 1_000_000;
  observeFeedbackSequence(restored);
  assert.ok(nextFeedbackSequence() > restored);
});
