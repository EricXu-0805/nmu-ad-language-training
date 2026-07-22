import assert from "node:assert/strict";
import test from "node:test";
import { AutopilotExecutionFence } from "./autopilotExecutionFence.ts";

function deferred(): { promise: Promise<void>; resolve(): void } {
  let resolvePromise!: () => void;
  const promise = new Promise<void>((resolve) => { resolvePromise = resolve; });
  return { promise, resolve: resolvePromise };
}

test("rapid media gate off/on waits for teardown and passive recovery", async () => {
  const fence = new AutopilotExecutionFence();
  const shutdown = deferred();
  const passive = deferred();
  const passiveStarted = deferred();
  const events: string[] = [];
  fence.registerControllerShutdown(shutdown.promise.then(() => { events.push("old-stopped"); }));
  const passiveRun = fence.runPassive(async () => {
    events.push("passive-start");
    passiveStarted.resolve();
    await passive.promise;
    events.push("passive-end");
  });
  const activeStart = fence.waitForActiveStart().then(() => { events.push("new-start"); });

  await Promise.resolve();
  assert.deepEqual(events, []);
  shutdown.resolve();
  await passiveStarted.promise;
  assert.deepEqual(events, ["old-stopped", "passive-start"]);
  passive.resolve();
  await Promise.all([passiveRun, activeStart]);
  assert.deepEqual(events, ["old-stopped", "passive-start", "passive-end", "new-start"]);
});

test("a shutdown registered during passive work still fences the next controller", async () => {
  const fence = new AutopilotExecutionFence();
  const passive = deferred();
  const lateShutdown = deferred();
  const events: string[] = [];
  const passiveRun = fence.runPassive(async () => {
    events.push("passive-start");
    await passive.promise;
    fence.registerControllerShutdown(lateShutdown.promise.then(() => {
      events.push("late-stopped");
    }));
    events.push("passive-end");
  });
  const activeStart = fence.waitForActiveStart().then(() => { events.push("new-start"); });
  passive.resolve();
  await passiveRun;
  await Promise.resolve();
  assert.deepEqual(events, ["passive-start", "passive-end"]);
  lateShutdown.resolve();
  await activeStart;
  assert.deepEqual(events, ["passive-start", "passive-end", "late-stopped", "new-start"]);
});
