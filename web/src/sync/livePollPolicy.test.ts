import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { livePollingHasActiveCapability } from "./livePollPolicy.ts";

const record = {
  capability: "x".repeat(43),
  sessionId: "S-ONE",
  expiresAt: "2099-01-01T00:00:00Z",
};

test("live-state polling starts only after an active patient capability exists", () => {
  assert.equal(livePollingHasActiveCapability({ source: "active", record }), true);
  assert.equal(livePollingHasActiveCapability({ source: null, record: null }), false);
  assert.equal(livePollingHasActiveCapability({ source: "recovery", record }), false);
  assert.equal(livePollingHasActiveCapability({ source: "active", record: null }), false);
});

test("the live hook checks pairing before allocating or sending a request", () => {
  const source = readFileSync(new URL("./useLiveCursor.ts", import.meta.url), "utf8");
  const poll = source.slice(
    source.indexOf("const poll ="),
    source.indexOf("// No authority request is sent before pairing."),
  );
  const gate = poll.indexOf("if (!livePollingHasActiveCapability(credential))");
  const request = poll.indexOf("const controller = new AbortController()");
  assert.ok(gate >= 0);
  assert.ok(request > gate);
  assert.doesNotMatch(source, /headerless probe/);
});
