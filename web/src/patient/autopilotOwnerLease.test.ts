import assert from "node:assert/strict";
import test from "node:test";
import {
  acquireAutopilotOwnerLease,
  AutopilotOwnerLeaseUnavailableError,
  type AutopilotOwnerLockCallback,
  type AutopilotOwnerLockManager,
} from "./autopilotOwnerLease.ts";

class SerialLockManager implements AutopilotOwnerLockManager {
  #tails = new Map<string, Promise<unknown>>();

  request(
    name: string,
    _options: { mode: "exclusive"; signal?: AbortSignal },
    callback: AutopilotOwnerLockCallback,
  ): Promise<unknown> {
    const before = this.#tails.get(name) ?? Promise.resolve();
    const next = before.then(() => callback({}));
    this.#tails.set(name, next.catch(() => undefined));
    return next;
  }
}

test("one session has exactly one origin-wide patient runner", async () => {
  const manager = new SerialLockManager();
  const first = await acquireAutopilotOwnerLease("S-ONE", manager);
  let secondAcquired = false;
  const secondPromise = acquireAutopilotOwnerLease("S-ONE", manager).then((lease) => {
    secondAcquired = true;
    return lease;
  });

  await Promise.resolve();
  assert.equal(secondAcquired, false);
  first.release();
  await first.released;
  const second = await secondPromise;
  assert.equal(secondAcquired, true);
  second.release();
  await second.released;
});

test("different sessions do not block one another", async () => {
  const manager = new SerialLockManager();
  const first = await acquireAutopilotOwnerLease("S-ONE", manager);
  const second = await acquireAutopilotOwnerLease("S-TWO", manager);
  first.release();
  second.release();
  await Promise.all([first.released, second.released]);
});

test("missing Web Locks support fails closed before server probing", async () => {
  await assert.rejects(
    acquireAutopilotOwnerLease("S-ONE", null),
    (error: unknown) => error instanceof AutopilotOwnerLeaseUnavailableError,
  );
});
