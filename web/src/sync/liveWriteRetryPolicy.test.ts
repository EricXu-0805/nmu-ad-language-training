import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { liveWriteRetryDecision } from "./liveWriteRetryPolicy.ts";

test("ambiguous session handshakes are never replayed over transient bedside facts", () => {
  assert.equal(liveWriteRetryDecision("session"), "restart_context");
});

test("only the exact failed positional write is eligible for in-context retry", () => {
  assert.equal(liveWriteRetryDecision("cursor"), "retry_exact");
  assert.equal(liveWriteRetryDecision("rapportStep"), "retry_exact");
});

test("the hook retries one recorded failure and never replays lastSession", () => {
  const source = readFileSync(new URL("./useCursorWriter.ts", import.meta.url), "utf8");
  const body = source.slice(
    source.indexOf("const retrySync = useCallback"),
    source.indexOf("const resetSession"),
  );
  assert.match(body, /const failure = failedWrite\.current/);
  assert.match(body, /enqueue\(failure\.kind, failure\.payload\)/);
  assert.doesNotMatch(body, /lastSession|enqueue\("session"/);
});
