import assert from "node:assert/strict";
import test from "node:test";
import { AutopilotExecutionFence } from "./autopilotExecutionFence.ts";
import { settleAutopilotServerExit } from "./autopilotServerExitCoordinator.ts";

function deferred(): { promise: Promise<void>; resolve(): void } {
  let resolvePromise!: () => void;
  const promise = new Promise<void>((resolve) => { resolvePromise = resolve; });
  return { promise, resolve: resolvePromise };
}

test("manual plane waits for controller shutdown and actual owner-lock release", async () => {
  const shutdown = deferred();
  const released = deferred();
  const events: string[] = [];
  const operation = settleAutopilotServerExit({
    fence: new AutopilotExecutionFence(),
    shutdown: shutdown.promise.then(() => { events.push("controller-closed"); }),
    ownerLease: {
      release: () => { events.push("owner-release-requested"); },
      released: released.promise.then(() => { events.push("owner-released"); }),
    },
  }).then(() => { events.push("legacy-may-mount"); });

  await Promise.resolve();
  assert.deepEqual(events, []);
  shutdown.resolve();
  for (let turn = 0; turn < 8 && events.length < 2; turn += 1) {
    await Promise.resolve();
  }
  assert.deepEqual(events, ["controller-closed", "owner-release-requested"]);
  released.resolve();
  await operation;
  assert.deepEqual(events, [
    "controller-closed", "owner-release-requested", "owner-released", "legacy-may-mount",
  ]);
});
