import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { liveWriteRetryDecision, rejectedLiveWriteMessage } from "./liveWriteRetryPolicy.ts";

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

test("a refused handshake keeps its reason over the follow-on mismatch refusals", () => {
  const full = "两个床位都在使用中，请等其中一场中止或最终完成后再开（暂停不会腾出床位）";
  const mismatch = "live payload 场次与当前操作端场次不一致";
  assert.equal(rejectedLiveWriteMessage("session", full, null), full);
  assert.equal(rejectedLiveWriteMessage("cursor", mismatch, full), full);
  assert.equal(rejectedLiveWriteMessage("rapportStep", mismatch, full), full);
  assert.equal(rejectedLiveWriteMessage("cursor", mismatch, null), mismatch);
});

test("the hook remembers a refused handshake until a later write lands", () => {
  const source = readFileSync(new URL("./useCursorWriter.ts", import.meta.url), "utf8");
  const refused = source.slice(
    source.indexOf("if (error instanceof ApiError && error.status === 409)"),
    source.indexOf("failedWrite.current = { kind, payload };"),
  );
  assert.match(refused, /if \(kind === "session"\) handshakeRefusal\.current = message;/);
  assert.match(refused, /setSyncError\(rejectedLiveWriteMessage\(kind, message, handshakeRefusal\.current\)\)/);
  const denied = source.slice(
    source.indexOf("failedWrite.current = { kind, payload };") - 400,
    source.indexOf("failedWrite.current = { kind, payload };"),
  );
  assert.match(denied, /kind === "session" && error instanceof ApiError && \(error\.status === 403 \|\| error\.status === 404\)/);
  assert.match(denied, /handshakeRefusal\.current = errorMessage\(error\);/);
  const landed = source.slice(
    source.indexOf("onCommitted?.(serverWseq);"),
    source.indexOf("return true;"),
  );
  assert.match(landed, /handshakeRefusal\.current = null;/);
});
