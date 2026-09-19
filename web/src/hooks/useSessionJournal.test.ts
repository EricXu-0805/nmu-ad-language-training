import assert from "node:assert/strict";
import test from "node:test";
import type { AttemptEvent, AudioAsset, ItemEvent, Session, TurnEvent } from "../types";
import {
  emptySessionJournal,
  journalStorageKey,
  loadSessionJournal,
  mergeServerJournal,
  parseJournalAttempts,
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

test("attempts 投影只保留面板字段,回答原文不落 SessionJournal;畸形行整体拒收", () => {
  const row: AttemptEvent = {
    id: 501,
    session_id: "S-A",
    item_id: "SE_螺母",
    turn_seq: 1,
    response_role: "命名",
    attempt_seq: 2,
    raw_audio_id: "aud-501",
    prompt_level: 1,
    cue_type: "prompt_level_1",
    duration_seconds: 2.4,
    asr_text: "刘世茂",
    asr_confidence: 0.61,
    operational_answer_type: "错误",
    operational_score: 0,
    operational_needs_review: true,
    judge_mode: "rule",
    judge_portrait_used: false,
    processing_status: "completed",
    error_code: null,
    created_at: "2026-09-17T02:00:00Z",
    processed_at: "2026-09-17T02:00:03Z",
    is_simulation: false,
  };
  assert.deepEqual(parseJournalAttempts([row], "S-A"), [{
    attemptId: 501,
    itemId: "SE_螺母",
    turnSeq: 1,
    attemptSeq: 2,
    promptLevel: 1,
    asrText: "刘世茂",
    answerType: "错误",
    score: 0,
    needsReview: true,
    processingStatus: "completed",
    errorCode: null,
    createdAt: "2026-09-17T02:00:00Z",
  }]);
  assert.deepEqual(parseJournalAttempts(undefined, "S-A"), []);
  // 未转写的行:可空字段落 null,不编值。
  assert.deepEqual(parseJournalAttempts([{
    ...row, id: 502, attempt_seq: 3, processing_status: "received",
    asr_text: undefined, operational_answer_type: undefined, operational_score: undefined,
    operational_needs_review: undefined, error_code: undefined,
  }], "S-A")[0], {
    attemptId: 502, itemId: "SE_螺母", turnSeq: 1, attemptSeq: 3, promptLevel: 1,
    asrText: null, answerType: null, score: null, needsReview: null,
    processingStatus: "received", errorCode: null, createdAt: "2026-09-17T02:00:00Z",
  });
  assert.throws(() => parseJournalAttempts([{ ...row, session_id: "S-B" }], "S-A"), /attempt/);
  assert.throws(() => parseJournalAttempts([row, row], "S-A"), /attempt/);
  assert.throws(() => parseJournalAttempts([{ ...row, processing_status: "done" as never }], "S-A"), /attempt/);
  assert.throws(() => parseJournalAttempts([{ ...row, attempt_seq: 0 }], "S-A"), /attempt/);
  assert.throws(() => parseJournalAttempts([{ ...row, operational_score: Number.NaN }], "S-A"), /attempt/);
  // mergeServerJournal 的持久化结构里没有 attempts 字段(回答原文不进 localStorage)。
  const merged = mergeServerJournal(persistedJournal(), remote({ attempts: [row] }));
  assert.equal("attempts" in merged, false);
});
