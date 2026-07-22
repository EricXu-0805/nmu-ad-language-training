import assert from "node:assert/strict";
import test from "node:test";
import {
  acknowledgeDrainAfterShutdown,
  assertExactDrainTransition,
} from "./autopilotDrainCoordinator.ts";
import { AutopilotExecutionFence } from "./autopilotExecutionFence.ts";

function deferred(): { promise: Promise<void>; resolve(): void } {
  let resolvePromise!: () => void;
  const promise = new Promise<void>((resolve) => { resolvePromise = resolve; });
  return { promise, resolve: resolvePromise };
}

test("device drain proof is appended only after the exact media teardown settles", async () => {
  const shutdown = deferred();
  const events: string[] = [];
  const operation = acknowledgeDrainAfterShutdown({
    fence: new AutopilotExecutionFence(),
    shutdown: shutdown.promise.then(() => { events.push("physical-media-idle"); }),
    acknowledge: async () => { events.push("server-drain-proof"); },
  });
  await Promise.resolve();
  assert.deepEqual(events, []);
  shutdown.resolve();
  await operation;
  assert.deepEqual(events, ["physical-media-idle", "server-drain-proof"]);
});

test("an already idle controller can append its proof without inventing teardown", async () => {
  const events: string[] = [];
  await acknowledgeDrainAfterShutdown({
    fence: new AutopilotExecutionFence(),
    shutdown: null,
    acknowledge: async () => { events.push("server-drain-proof"); },
  });
  assert.deepEqual(events, ["server-drain-proof"]);
});

test("drain receipt is revision-bound to either one mutation or an exact replay", () => {
  assert.doesNotThrow(() => assertExactDrainTransition(
    { state_revision: 2 }, { replayed: false, state_revision: 3 }));
  assert.doesNotThrow(() => assertExactDrainTransition(
    { state_revision: 3 }, { replayed: true, state_revision: 3 }));
  assert.throws(() => assertExactDrainTransition(
    { state_revision: 2 }, { replayed: true, state_revision: 3 }), /状态转移/);
  assert.throws(() => assertExactDrainTransition(
    { state_revision: 2 }, { replayed: false, state_revision: 4 }), /状态转移/);
});
