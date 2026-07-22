import assert from "node:assert/strict";
import test from "node:test";

import { ensureExportIntent } from "./exportIntent.ts";

test("export retry reuses one stable high-entropy intent", () => {
  let calls = 0;
  const uuid = () => {
    calls += 1;
    return "01234567-89ab-4cde-8fab-0123456789ab";
  };
  const first = ensureExportIntent(null, "session-internal-id", uuid);
  const retry = ensureExportIntent(first, "session-internal-id", uuid);
  assert.equal(retry, first);
  assert.equal(calls, 1);
  assert.match(first.idempotencyKey, /^export-[0-9a-f-]{36}$/);
});

test("a different session receives a different export intent", () => {
  let seq = 0;
  const uuid = () => `${String(++seq).padStart(8, "0")}-89ab-4cde-8fab-0123456789ab`;
  const first = ensureExportIntent(null, "session-one", uuid);
  const second = ensureExportIntent(first, "session-two", uuid);
  assert.notEqual(second.idempotencyKey, first.idempotencyKey);
});
