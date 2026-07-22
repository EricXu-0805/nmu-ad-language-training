import assert from "node:assert/strict";
import test from "node:test";

import {
  createSessionAbortIntent,
  SESSION_ABORT_REASONS,
  sessionAbortReasonLabel,
} from "./sessionAbort.ts";

test("abort reasons are a closed four-code protocol", () => {
  assert.deepEqual(SESSION_ABORT_REASONS.map(([code]) => code), [
    "participant_declined",
    "clinical_safety",
    "technical_failure",
    "researcher_decision",
  ]);
  assert.equal(sessionAbortReasonLabel("clinical_safety"), "临床安全原因");
  assert.equal(sessionAbortReasonLabel("free text"), "未知的受控原因");
});

test("an ambiguous abort retry retains the exact CAS and idempotency facts", () => {
  const intent = createSessionAbortIntent(
    "technical_failure",
    7,
    "abort-00000000-0000-4000-8000-000000000001",
  );
  const retry = intent;
  assert.equal(retry, intent);
  assert.deepEqual(retry, {
    reason_code: "technical_failure",
    expected_revision: 7,
    idempotency_key: "abort-00000000-0000-4000-8000-000000000001",
  });
  assert.throws(
    () => createSessionAbortIntent("technical_failure", -1, "abort-00000000-0000-4000-8000-000000000001"),
    /运行修订/,
  );
});
