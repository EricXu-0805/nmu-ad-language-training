import assert from "node:assert/strict";
import test from "node:test";
import { requireServerWseq } from "./liveWriteAck.ts";

test("only a finite non-negative server wseq authorizes a live broadcast", () => {
  assert.equal(requireServerWseq(42.9), 42);
  for (const invalid of [undefined, null, -1, Number.NaN, Number.POSITIVE_INFINITY, "42"]) {
    assert.throws(() => requireServerWseq(invalid), /服务器未返回/);
  }
});
