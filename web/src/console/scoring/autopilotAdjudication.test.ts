import assert from "node:assert/strict";
import test from "node:test";
import { ApiError } from "../../apiResponse.ts";
import type { AutopilotStatusReceipt } from "../../autopilot/startControl.ts";
import { submitReviewedAdjudication } from "./autopilotAdjudication.ts";

const reviewed: AutopilotStatusReceipt = {
  scopeKey: "p0a_sim_first_single_v1", mode: "autonomous", status: "paused",
  stateRevision: 7, serverOwned: true, takeoverReady: true, commandKind: "record",
  positionItemId: "DE_刀子+西瓜", positionTurnSeq: 1, lastErrorCode: null,
};

test("submit exactly the paused state reviewed in the dialog", async () => {
  const receipts: AutopilotStatusReceipt[] = [];
  const advanced = { ...reviewed, stateRevision: 8, status: "waiting_tts" as const };
  const revisions: number[] = [];
  const result = await submitReviewedAdjudication({
    reviewed, readStatus: async () => ({ ...reviewed }),
    write: async revision => { revisions.push(revision); return advanced; },
    acceptReceipt: receipt => receipts.push(receipt),
  });
  assert.equal(result, "accepted");
  assert.deepEqual(revisions, [7]);
  assert.deepEqual(receipts, [reviewed, advanced]);
});

test("an open dialog cannot decide a different item, turn, or later answer", async () => {
  for (const patch of [
    { positionItemId: "SE_茶杯" }, { positionTurnSeq: 2 }, { stateRevision: 9 },
    { takeoverReady: false }, { positionItemId: null, positionTurnSeq: null },
    { status: "waiting_recording" as const },
  ]) {
    let writes = 0;
    const latest = { ...reviewed, ...patch };
    let displayed: AutopilotStatusReceipt | null = null;
    assert.equal(await submitReviewedAdjudication({
      reviewed, readStatus: async () => latest,
      write: async () => { writes += 1; return latest; },
      acceptReceipt: receipt => { displayed = receipt; },
    }), "changed");
    assert.equal(writes, 0);
    assert.deepEqual(displayed, latest);
  }
});

test("a concurrent revision conflict refreshes without resubmitting the old decision", async () => {
  let reads = 0;
  let writes = 0;
  const next = { ...reviewed, stateRevision: 12, positionTurnSeq: 2 };
  const receipts: AutopilotStatusReceipt[] = [];
  assert.equal(await submitReviewedAdjudication({
    reviewed, readStatus: async () => ++reads === 1 ? reviewed : next,
    write: async () => {
      writes += 1;
      throw new ApiError(409, "changed", { code: "autopilot_revision_conflict" }, "nested-detail");
    },
    acceptReceipt: receipt => receipts.push(receipt),
  }), "changed");
  assert.equal(writes, 1);
  assert.deepEqual(receipts, [reviewed, next]);
});

test("a lost write response remains uncertain and does not create another decision", async () => {
  let writes = 0;
  await assert.rejects(submitReviewedAdjudication({
    reviewed, readStatus: async () => reviewed,
    write: async () => { writes += 1; throw new Error("network lost"); },
    acceptReceipt: () => {},
  }), /network lost/);
  assert.equal(writes, 1);
});
