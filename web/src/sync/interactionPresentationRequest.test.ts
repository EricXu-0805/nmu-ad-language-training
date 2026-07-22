import assert from "node:assert/strict";
import test from "node:test";
import type { InteractionPresentationDraft } from "../api.ts";
import type { SessionRuntimeState } from "../types.ts";
import {
  buildExactInteractionPresentationRequest,
  exactRequestMatchesDraft,
  parseInteractionPresentationReceipt,
} from "./interactionPresentationRequest.ts";

const draft: InteractionPresentationDraft = {
  idempotency_key: "presentation:123e4567-e89b-42d3-a456-426614174000",
  interaction: {
    event_type: "cue_selected", item_id: "SE_锚", turn_seq: 1,
    prompt_level: 1, cue_type: "semantic",
  },
  cursor: {
    sessionId: "S-A", screen: "present", itemIdx: 0, turnIdx: 0,
    responseRole: "命名", cueLevel: 1, recording: "idle", selfStart: false,
  },
};

function runtime(overrides: Partial<SessionRuntimeState> = {}): SessionRuntimeState {
  return {
    sessionId: "S-A", status: "active", revision: 8,
    cursor: { sessionId: "S-A", itemIdx: 0, turnIdx: 0, wseq: 41 },
    rapportStep: null,
    ...overrides,
  };
}

test("the exact request uses the fresh queued runtime revision and cursor wseq", () => {
  const exact = buildExactInteractionPresentationRequest(draft, runtime(), "S-A");
  assert.equal(exact.cursor.expected_revision, 8);
  assert.equal(exact.cursor.wseq, 41);
  assert.equal(exact.idempotency_key, draft.idempotency_key);
  assert.equal(exactRequestMatchesDraft(exact, draft), true);
});

test("inactive, cross-session, missing-wseq, or moved snapshots fail closed", () => {
  assert.throws(() => buildExactInteractionPresentationRequest(
    draft, runtime({ status: "paused" }), "S-A"));
  assert.throws(() => buildExactInteractionPresentationRequest(
    draft, runtime({ sessionId: "S-B" }), "S-A"));
  assert.throws(() => buildExactInteractionPresentationRequest(
    draft, runtime({ cursor: { itemIdx: 0, turnIdx: 0 } }), "S-A"));
  assert.throws(() => buildExactInteractionPresentationRequest(
    draft, runtime({ cursor: { itemIdx: 1, turnIdx: 0, wseq: 42 } }), "S-A"));
});

test("a retry reuses the exact body and rejects same-key semantic mutation", () => {
  const exact = buildExactInteractionPresentationRequest(draft, runtime(), "S-A");
  assert.equal(exactRequestMatchesDraft(exact, draft), true);
  assert.equal(exactRequestMatchesDraft(exact, {
    ...draft,
    cursor: { ...draft.cursor, cueLevel: 2 },
  }), false);
});

function cueReceipt(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    interaction: {
      id: 17,
      session_id: "S-A",
      event_seq: 3,
      item_id: "SE_锚",
      turn_seq: 1,
      attempt_id: null,
      attempt_seq: null,
      event_type: "cue_selected",
      payload_json: '{"cue_type":"semantic","prompt_level":1}',
      created_at: "2026-07-20T03:33:19.984214",
      is_simulation: true,
    },
    cursor: {
      sessionId: "S-A",
      screen: "present",
      itemIdx: 0,
      turnIdx: 0,
      responseRole: "命名",
      cueLevel: 1,
      recording: "idle",
      selfStart: false,
      wseq: 42,
    },
    seq: 11,
    wseq: 42,
    runtimeRevision: 9,
    idempotent: false,
    ...overrides,
  };
}

test("an exact cue receipt proves the request, cursor, clocks, and idempotency fact", () => {
  const exact = buildExactInteractionPresentationRequest(draft, runtime(), "S-A");
  const parsed = parseInteractionPresentationReceipt(cueReceipt(), "S-A", exact);
  assert.equal(parsed.interaction.session_id, "S-A");
  assert.equal(parsed.cursor.wseq, 42);
  assert.equal(parsed.runtimeRevision, 9);
  assert.equal(parsed.idempotent, false);
  assert.equal(parseInteractionPresentationReceipt(
    cueReceipt({ idempotent: true }), "S-A", exact,
  ).idempotent, true);
});

test("top-level receipt fields and server clocks are exact and fail closed", () => {
  const exact = buildExactInteractionPresentationRequest(draft, runtime(), "S-A");
  const malformed = [
    { ...cueReceipt(), extra: "leak" },
    { ...cueReceipt(), seq: 0 },
    { ...cueReceipt(), seq: 11.5 },
    { ...cueReceipt(), wseq: 41, cursor: { ...(cueReceipt().cursor as object), wseq: 41 } },
    { ...cueReceipt(), wseq: 42.5 },
    { ...cueReceipt(), runtimeRevision: 8 },
    { ...cueReceipt(), runtimeRevision: 10 },
    { ...cueReceipt(), idempotent: "false" },
  ];
  const { idempotent: _missing, ...missing } = cueReceipt();
  malformed.push(missing);
  for (const value of malformed) {
    assert.throws(
      () => parseInteractionPresentationReceipt(value, "S-A", exact),
      /与本次请求不一致/,
    );
  }
});

test("interaction evidence must be one exact request-bound ledger record", () => {
  const exact = buildExactInteractionPresentationRequest(draft, runtime(), "S-A");
  const base = cueReceipt();
  const interaction = base.interaction as Record<string, unknown>;
  const malformed = [
    { ...interaction, secret: "answer" },
    { ...interaction, session_id: "S-B" },
    { ...interaction, item_id: "SE_别题" },
    { ...interaction, turn_seq: 2 },
    { ...interaction, attempt_id: 17, attempt_seq: 1 },
    { ...interaction, event_type: "feedback_selected" },
    { ...interaction, payload_json: '{"prompt_level":1,"cue_type":"semantic"}' },
    { ...interaction, payload_json: '{"cue_type":"semantic","prompt_level":1,"text":"leak"}' },
    { ...interaction, created_at: "2026-02-30T03:33:19" },
    { ...interaction, is_simulation: 1 },
  ];
  for (const value of malformed) {
    assert.throws(() => parseInteractionPresentationReceipt(
      cueReceipt({ interaction: value }), "S-A", exact,
    ));
  }
});

test("cursor response must exactly echo the request projection with only new server clocks", () => {
  const exact = buildExactInteractionPresentationRequest(draft, runtime(), "S-A");
  const base = cueReceipt();
  const cursor = base.cursor as Record<string, unknown>;
  const malformed = [
    { ...cursor, extra: "unknown" },
    { ...cursor, sessionId: "S-B" },
    { ...cursor, cueLevel: 2 },
    { ...cursor, wseq: 43 },
    { ...cursor, expected_revision: 8 },
    { ...cursor, fbSeq: 42 },
  ];
  for (const value of malformed) {
    assert.throws(() => parseInteractionPresentationReceipt(
      cueReceipt({ cursor: value }), "S-A", exact,
    ));
  }
});

test("feedback receipt binds attempt evidence and the server-issued feedback sequence", () => {
  const feedbackDraft: InteractionPresentationDraft = {
    idempotency_key: "presentation:223e4567-e89b-42d3-a456-426614174000",
    interaction: {
      event_type: "feedback_selected",
      item_id: "SE_锚",
      turn_seq: 1,
      attempt_id: 23,
      feedback_key: "self",
    },
    cursor: {
      sessionId: "S-A",
      screen: "present",
      itemIdx: 0,
      turnIdx: 0,
      responseRole: "命名",
      cueLevel: 0,
      recording: "idle",
      selfStart: false,
      fbKey: "self",
      fbItemId: "SE_锚",
    },
  };
  const exact = buildExactInteractionPresentationRequest(feedbackDraft, runtime(), "S-A");
  const receipt = cueReceipt({
    interaction: {
      ...(cueReceipt().interaction as object),
      attempt_id: 23,
      attempt_seq: 2,
      event_type: "feedback_selected",
      payload_json: '{"feedback_key":"self"}',
    },
    cursor: {
      ...feedbackDraft.cursor,
      wseq: 42,
      fbSeq: 42,
    },
  });
  assert.equal(parseInteractionPresentationReceipt(
    receipt, "S-A", exact,
  ).cursor.fbSeq, 42);
  assert.throws(() => parseInteractionPresentationReceipt({
    ...receipt,
    cursor: { ...(receipt.cursor as object), fbSeq: 43 },
  }, "S-A", exact));
  assert.throws(() => parseInteractionPresentationReceipt({
    ...receipt,
    interaction: { ...(receipt.interaction as object), attempt_seq: null },
  }, "S-A", exact));
});
