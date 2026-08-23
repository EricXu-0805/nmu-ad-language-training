import assert from "node:assert/strict";
import test from "node:test";
import type { AudioAsset, ItemEvent, Session, TurnEvent } from "../types";
import {
  emptySessionJournal,
  journalStorageKey,
  loadSessionJournal,
  mergeServerJournal,
  parseStoredSessionJournal,
  type ServerSessionJournal,
  type SessionJournal,
} from "./useSessionJournal.ts";

class MemoryStorage {
  readonly values = new Map<string, string>();
  readonly removed: string[] = [];

  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  setItem(key: string, value: string): void { this.values.set(key, value); }
  removeItem(key: string): void { this.removed.push(key); this.values.delete(key); }
}

function persistedJournal(sessionId = "S-A"): SessionJournal {
  return {
    schemaVersion: 2,
    sessionId,
    itemEvents: {
      ITEM: {
        itemEventId: 11,
        taskType: "单要素",
        imageId: null,
        provenance: { source: "server_committed", sessionId, itemId: "ITEM" },
      },
    },
    turns: {
      "ITEM#1": {
        turnId: 21,
        responseRole: "命名",
        asrSaved: true,
        confirmed: true,
        aiJudged: true,
        locked: true,
        rawAudioId: "aud-committed",
        provenance: {
          source: "server_committed",
          sessionId,
          turnKey: "ITEM#1",
          rawAudioId: "aud-committed",
        },
      },
    },
    audios: {
      "aud-committed": {
        turnKey: "ITEM#1",
        containsDirectIdentifier: false,
        isReliabilitySample: false,
        lastStatus: "recorded",
        durationSeconds: 1.5,
        provenance: {
          source: "server_committed",
          sessionId,
          turnKey: "ITEM#1",
          rawAudioId: "aud-committed",
        },
      },
      "aud-pending": {
        turnKey: "ITEM#2",
        containsDirectIdentifier: false,
        isReliabilitySample: false,
        lastStatus: "recorded",
        durationSeconds: 2,
        provenance: {
          source: "local_pending_audio",
          sessionId,
          turnKey: "ITEM#2",
          rawAudioId: "aud-pending",
        },
      },
    },
    cueLevels: { "ITEM#1": 1 },
    cueProvenance: {
      "ITEM#1": { source: "server_committed", sessionId, turnKey: "ITEM#1" },
    },
    cursor: { itemIdx: 1, turnIdx: 0 },
  };
}

function item(sessionId = "S-A"): ItemEvent {
  return {
    id: 11,
    session_id: sessionId,
    item_id: "ITEM",
    image_id: null,
    task_type: "单要素",
    item_set_type: "训练集",
  };
}

function turn(overrides: Partial<TurnEvent> = {}): TurnEvent {
  return {
    id: 21,
    item_event_id: 11,
    turn_seq: 1,
    response_role: "命名",
    raw_audio_id: null,
    confirmed_response_text: null,
    confirmation_revision: 0,
    prompt_level: 0,
    ai_answer_type: null,
    ai_score: null,
    ai_needs_review: null,
    judge_portrait_used: false,
    score_locked: false,
    ...overrides,
  };
}

function audio(rawAudioId: string, status: AudioAsset["status"] = "recorded"): AudioAsset {
  return {
    raw_audio_id: rawAudioId,
    turn_key: "ITEM#1",
    session_id: "S-A",
    is_simulation: true,
    audio_format: "audio/webm",
    status,
    is_reliability_sample: false,
    withdrawn: false,
    contains_direct_identifier: false,
  };
}

function remote(overrides: Partial<ServerSessionJournal> = {}): ServerSessionJournal {
  return {
    session: { session_id: "S-A" } as Session,
    items: [item()],
    turns: [turn()],
    audios: [],
    interactions: [],
    attempts: [],
    audio_receipts: [],
    ...overrides,
  };
}

test("A-key/B-payload is rejected, current A key is deleted, and recovery starts empty", () => {
  const storage = new MemoryStorage();
  storage.setItem(journalStorageKey("S-A"), JSON.stringify(persistedJournal("S-B")));
  storage.setItem(journalStorageKey("S-B"), "keep-other-session");

  const result = loadSessionJournal("S-A", storage);

  assert.deepEqual(result, emptySessionJournal("S-A"));
  assert.deepEqual(storage.removed, [journalStorageKey("S-A")]);
  assert.equal(storage.getItem(journalStorageKey("S-B")), "keep-other-session");
});

test("unknown fields and incomplete nested provenance fail closed", () => {
  const unknown = structuredClone(persistedJournal()) as SessionJournal & { surprise?: boolean };
  unknown.surprise = true;
  assert.equal(parseStoredSessionJournal(JSON.stringify(unknown), "S-A"), null);

  const incomplete = structuredClone(persistedJournal()) as unknown as {
    turns: Record<string, Record<string, unknown>>;
  };
  delete incomplete.turns["ITEM#1"].provenance;
  const storage = new MemoryStorage();
  storage.setItem(journalStorageKey("S-A"), JSON.stringify(incomplete));
  assert.deepEqual(loadSessionJournal("S-A", storage), emptySessionJournal("S-A"));
  assert.deepEqual(storage.removed, [journalStorageKey("S-A")]);
});

test("cross-session turn/audio provenance rejects the complete local snapshot", () => {
  const foreignTurn = structuredClone(persistedJournal()) as SessionJournal;
  foreignTurn.turns["ITEM#1"].provenance.sessionId = "S-B";
  assert.equal(parseStoredSessionJournal(JSON.stringify(foreignTurn), "S-A"), null);

  const foreignAudio = structuredClone(persistedJournal()) as SessionJournal;
  foreignAudio.audios["aud-pending"].provenance.sessionId = "S-B";
  assert.equal(parseStoredSessionJournal(JSON.stringify(foreignAudio), "S-A"), null);
});

test("server turn flags replace forged local locked/confirmed claims instead of OR-promoting them", () => {
  const local = persistedJournal();
  const merged = mergeServerJournal(local, remote());

  assert.equal(merged.turns["ITEM#1"].locked, false);
  assert.equal(merged.turns["ITEM#1"].confirmed, false);
  assert.equal(merged.turns["ITEM#1"].aiJudged, false);
  assert.equal(merged.turns["ITEM#1"].rawAudioId, undefined);
  assert.equal(merged.turns["ITEM#1"].provenance.rawAudioId, undefined);
});

test("server confirmation revision replaces local CAS claims and stored invalid revisions fail closed", () => {
  const local = persistedJournal();
  local.turns["ITEM#1"].confirmationRevision = 99;
  const merged = mergeServerJournal(local, remote({
    turns: [turn({ confirmed_response_text: "服务端真值", confirmation_revision: 2 })],
  }));
  assert.equal(merged.turns["ITEM#1"].confirmationRevision, 2);
  assert.equal(merged.turns["ITEM#1"].confirmedText, "服务端真值");

  const invalid = structuredClone(persistedJournal()) as SessionJournal;
  invalid.turns["ITEM#1"].confirmationRevision = -1;
  assert.equal(parseStoredSessionJournal(JSON.stringify(invalid), "S-A"), null);

  assert.throws(() => mergeServerJournal(local, remote({
    turns: [{ ...turn(), confirmation_revision: undefined } as unknown as TurnEvent],
  })), /confirmation_revision/);
});

test("foreign/vanished committed audio is dropped while strictly-proven pending local audio survives", () => {
  const merged = mergeServerJournal(persistedJournal(), remote());

  assert.equal(merged.audios["aud-committed"], undefined);
  assert.equal(merged.audios["aud-pending"].provenance.source, "local_pending_audio");
  assert.equal(merged.audios["aud-pending"].turnKey, "ITEM#2");
});

test("a server audio and status override a same-id local pending reference", () => {
  const local = persistedJournal();
  local.audios["aud-committed"].provenance.source = "local_pending_audio";
  const serverTurn = turn({ raw_audio_id: "aud-committed" });
  const merged = mergeServerJournal(local, remote({
    turns: [serverTurn],
    audios: [audio("aud-committed", "checksum_verified")],
  }));

  assert.equal(merged.audios["aud-committed"].lastStatus, "checksum_verified");
  assert.equal(merged.audios["aud-committed"].provenance.source, "server_committed");
  assert.equal(merged.turns["ITEM#1"].rawAudioId, "aud-committed");
});

test("pending audio already named by a server receipt is not retained as unuploaded", () => {
  const merged = mergeServerJournal(persistedJournal(), remote({
    audio_receipts: [{
      server_seq: 1,
      raw_audio_id: "aud-pending",
      session_id: "S-A",
      turn_key: "ITEM#2",
      received_at: "2026-07-19T00:00:00Z",
      duration_seconds: 2,
      byte_count: 12,
      checksum: "abc",
      data_classification: "simulation",
      is_simulation: true,
      contains_direct_identifier: false,
    }],
  }));

  assert.equal(merged.audios["aud-pending"], undefined);
});

test("附带小修:无 turn 的服务端音频从采集回执取真实时长——自动带练录音不再显示 0.0 秒", () => {
  const serverAudio = audio("aud-ap", "checksum_verified");
  const merged = mergeServerJournal(persistedJournal(), remote({
    audios: [serverAudio],
    audio_receipts: [{
      server_seq: 7,
      raw_audio_id: "aud-ap",
      session_id: "S-A",
      turn_key: "ITEM#1",
      received_at: "2026-08-22T00:00:00Z",
      duration_seconds: 14.2,
      byte_count: 999,
      checksum: "def",
      data_classification: "simulation",
      is_simulation: true,
      contains_direct_identifier: false,
    }],
  }));
  assert.equal(merged.audios["aud-ap"].durationSeconds, 14.2);
  // 有 turn 时仍以 turn 记录为准。
  const withTurn = mergeServerJournal(persistedJournal(), remote({
    turns: [turn({ raw_audio_id: "aud-ap", duration_seconds: 3.5 })],
    audios: [serverAudio],
    audio_receipts: [{
      server_seq: 8,
      raw_audio_id: "aud-ap",
      session_id: "S-A",
      turn_key: "ITEM#1",
      received_at: "2026-08-22T00:00:00Z",
      duration_seconds: 14.2,
      byte_count: 999,
      checksum: "def",
      data_classification: "simulation",
      is_simulation: true,
      contains_direct_identifier: false,
    }],
  }));
  assert.equal(withTurn.audios["aud-ap"].durationSeconds, 3.5);
  // 什么都没有:保持 undefined(上层不许再显示成 0.0 秒)。
  const bare = mergeServerJournal(persistedJournal(), remote({ audios: [serverAudio] }));
  assert.equal(bare.audios["aud-ap"].durationSeconds, undefined);
});
