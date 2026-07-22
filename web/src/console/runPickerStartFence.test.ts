import assert from "node:assert/strict";
import test from "node:test";
import { createAsyncOperationFence, deliverIfCurrent } from "./runPickerStartFence.ts";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

test("an invalidated start request cannot drive the console after its screen unmounts", async () => {
  const fence = createAsyncOperationFence();
  const generation = fence.begin();
  const response = deferred<{ session_id: string }>();
  const resumed: string[] = [];

  const delivery = deliverIfCurrent(
    fence,
    generation,
    () => response.promise,
    (session) => resumed.push(session.session_id),
  );
  fence.invalidate();
  response.resolve({ session_id: "S-STALE" });

  assert.equal(await delivery, false);
  assert.deepEqual(resumed, []);
});

test("the current start request may deliver its validated session exactly once", async () => {
  const fence = createAsyncOperationFence();
  const generation = fence.begin();
  const resumed: string[] = [];
  assert.equal(await deliverIfCurrent(
    fence,
    generation,
    async () => ({ session_id: "S-CURRENT" }),
    (session) => resumed.push(session.session_id),
  ), true);
  assert.deepEqual(resumed, ["S-CURRENT"]);
});
