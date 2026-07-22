import assert from "node:assert/strict";
import test from "node:test";

import type { TechnicalPauseDraft } from "../api.ts";
import type { SessionRuntimeState } from "../types.ts";
import {
  buildExactTechnicalPauseRequest,
  exactTechnicalPauseRequestMatchesDraft,
  reconcilePendingTechnicalPauseForTakeover,
  technicalPauseDispositionIsUncertain,
} from "./technicalPauseRequest.ts";

const draft: TechnicalPauseDraft = {
  idempotency_key: "technical-pause:123e4567-e89b-42d3-a456-426614174000",
  error_code: "client_microphone",
  attempt_id: 17,
};

function runtime(overrides: Partial<SessionRuntimeState> = {}): SessionRuntimeState {
  return {
    sessionId: "S-A",
    status: "active",
    revision: 9,
    cursor: {
      sessionId: "S-A",
      itemIdx: 2,
      turnIdx: 1,
      screen: "present",
      recording: "idle",
      wseq: 41,
    },
    rapportStep: null,
    ...overrides,
  };
}

test("technical pause request binds one high-entropy key to exact runtime/live CAS", () => {
  const exact = buildExactTechnicalPauseRequest(draft, runtime(), "S-A");
  assert.equal(exact.expected_revision, 9);
  assert.equal(exact.expected_live_wseq, 41);
  assert.equal(exactTechnicalPauseRequestMatchesDraft(exact, draft), true);
  assert.equal(exactTechnicalPauseRequestMatchesDraft(exact, {
    ...draft,
    error_code: "client_audio",
  }), false);
});

test("technical pause request fails closed on inactive, foreign, or incomplete snapshots", () => {
  assert.throws(
    () => buildExactTechnicalPauseRequest(draft, runtime({ status: "paused" }), "S-A"),
    /拒绝构造新技术暂停/,
  );
  assert.throws(
    () => buildExactTechnicalPauseRequest(draft, runtime({ sessionId: "S-B" }), "S-A"),
    /不属于当前场次/,
  );
  assert.throws(
    () => buildExactTechnicalPauseRequest(draft, runtime({ cursor: null }), "S-A"),
    /revision\/wseq/,
  );
});

test("only ambiguous transport/server outcomes retain the exact body for replay", () => {
  assert.equal(technicalPauseDispositionIsUncertain({ status: 0 }), true);
  assert.equal(technicalPauseDispositionIsUncertain({ status: 408 }), true);
  assert.equal(technicalPauseDispositionIsUncertain({ status: 503 }), true);
  assert.equal(technicalPauseDispositionIsUncertain({ status: 409 }), false);
  assert.equal(technicalPauseDispositionIsUncertain({ status: 422 }), false);
});

test("lost double ACK is reconciled before takeover so a later failure gets a new key", async () => {
  const exact = buildExactTechnicalPauseRequest(draft, runtime(), "S-A");
  const replayed: typeof exact[] = [];
  const reconciled = await reconcilePendingTechnicalPauseForTakeover(
    "paused",
    exact,
    async (request) => { replayed.push(request); },
  );
  assert.deepEqual(replayed, [exact]);
  assert.equal(reconciled.ready, true);
  assert.equal(reconciled.pending, null);

  const second = buildExactTechnicalPauseRequest({
    ...draft,
    idempotency_key: "technical-pause:223e4567-e89b-42d3-a456-426614174999",
    error_code: "client_audio",
  }, runtime({ revision: 11, cursor: { ...runtime().cursor!, wseq: 52 } }), "S-A");
  assert.notEqual(second.idempotency_key, exact.idempotency_key);
});

test("takeover clears only a proven replay or superseded receipt", async () => {
  const exact = buildExactTechnicalPauseRequest(draft, runtime(), "S-A");
  const superseded = await reconcilePendingTechnicalPauseForTakeover(
    "paused",
    exact,
    async () => { throw { detailData: { code: "technical_pause_replay_superseded" } }; },
  );
  assert.equal(superseded.ready, true);
  assert.equal(superseded.pending, null);

  const unproven = new Error("network still unknown");
  const blocked = await reconcilePendingTechnicalPauseForTakeover(
    "paused", exact, async () => { throw unproven; },
  );
  assert.equal(blocked.ready, false);
  assert.equal(blocked.pending, exact);
  assert.equal(blocked.error, unproven);
});
