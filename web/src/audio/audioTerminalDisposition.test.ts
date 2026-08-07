import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, decodeJsonApiResponse } from "../apiResponse.ts";
import { advanceAudioOutbox, createAudioOutboxEntry } from "./audioOutbox.ts";
import {
  buildAudioSavedPayload,
  finalizeAudioSavedRequest,
  parseAudioTerminalDisposition,
} from "./audioTerminalDisposition.ts";
import { sha256Blob } from "./audioUploadReceipt.ts";

const blob = new Blob(["elder-voice"], { type: "audio/webm" });

async function fixture() {
  const checksum = await sha256Blob(blob);
  const captured = createAudioOutboxEntry({
    rawAudioId: "aud-terminal-1",
    sessionId: "S-1",
    turnKey: "SE_锚#1",
    containsDirectIdentifier: true,
    durationSeconds: 2.5,
    blob,
    nowMs: 100,
  });
  const entry = advanceAudioOutbox(
    advanceAudioOutbox(captured, "registered", { nowMs: 200 }),
    "uploaded",
    { checksum, nowMs: 300 },
  );
  const saved = buildAudioSavedPayload(entry, checksum);
  const detail = {
    code: "audio_terminal_disposition",
    schemaVersion: 1,
    action: "discard_local_copy",
    reason: "withdrawn",
    rawAudioId: saved.rawAudioId,
    sessionId: saved.sessionId,
    turnKey: saved.turnKey,
    byteCount: saved.byteCount,
    checksum: saved.checksum,
    containsDirectIdentifier: saved.containsDirectIdentifier,
  } as const;
  return { entry, saved, detail };
}

function responseError(status: number, body: unknown): ApiError {
  try {
    decodeJsonApiResponse({
      status,
      ok: false,
      text: JSON.stringify(body),
    });
  } catch (error) {
    if (error instanceof ApiError) return error;
    throw error;
  }
  throw new Error("expected ApiError");
}

function terminalError(status: number, detail: unknown): ApiError {
  return responseError(status, { detail });
}

test("only an exact HTTP 410 ApiError bound to current bytes is accepted", async () => {
  const { entry, saved, detail } = await fixture();
  assert.deepEqual(
    await parseAudioTerminalDisposition(terminalError(410, detail), { entry, blob, saved }),
    detail,
  );
  assert.equal(
    await parseAudioTerminalDisposition(terminalError(409, detail), { entry, blob, saved }),
    null,
  );
  assert.equal(
    await parseAudioTerminalDisposition(terminalError(200, detail), { entry, blob, saved }),
    null,
  );
  assert.equal(
    await parseAudioTerminalDisposition(terminalError(404, detail), { entry, blob, saved }),
    null,
  );
  assert.equal(
    await parseAudioTerminalDisposition(new ApiError(410, "proxy error", "<html>gone</html>"), { entry, blob, saved }),
    null,
  );
  assert.equal(
    await parseAudioTerminalDisposition(responseError(410, detail), { entry, blob, saved }),
    null,
  );
  assert.equal(
    await parseAudioTerminalDisposition(responseError(410, { detail, requestId: "trace" }), { entry, blob, saved }),
    null,
  );
  assert.equal(
    await parseAudioTerminalDisposition(Object.assign(new Error("terminal"), { detailData: detail }), { entry, blob, saved }),
    null,
  );
});

test("terminal parser rejects every schema, binding, and live-byte ambiguity", async () => {
  const { entry, saved, detail } = await fixture();
  const reject = async (changed: unknown, overrides: Partial<{ entry: typeof entry; blob: Blob; saved: typeof saved }> = {}) => {
    assert.equal(await parseAudioTerminalDisposition(
      terminalError(410, changed),
      { entry: overrides.entry ?? entry, blob: overrides.blob ?? blob, saved: overrides.saved ?? saved },
    ), null);
  };

  await reject({ ...detail, extra: true });
  await reject({ ...detail, checksum: detail.checksum.toUpperCase() });
  await reject({ ...detail, rawAudioId: "aud-other" });
  await reject({ ...detail, sessionId: "S-2" });
  await reject({ ...detail, turnKey: "OTHER" });
  await reject({ ...detail, byteCount: detail.byteCount + 1 });
  await reject({ ...detail, containsDirectIdentifier: false });
  await reject({ ...detail, reason: "completed" });
  await reject(detail, { blob: new Blob(["other-voice"], { type: "audio/webm" }) });
  await reject(detail, { saved: { ...saved, checksum: "A".repeat(64) } });
  await reject(detail, { saved: { ...saved, unexpected: true } as typeof saved });
});

test("terminal finalization discards without audioSaved broadcast or progress side effects", async () => {
  const { entry, saved, detail } = await fixture();
  const calls: string[] = [];
  const outcome = await finalizeAudioSavedRequest(
    { entry, blob, saved, broadcastSaved: true },
    {
      putAudioSaved: async () => { calls.push("put"); throw terminalError(410, detail); },
      completeServerSaved: async () => { calls.push("complete"); },
      discardTerminalOutbox: async () => { calls.push("discard"); },
      broadcastAudioSaved: () => { calls.push("broadcast"); },
    },
  );

  assert.equal(outcome.kind, "terminal-discarded");
  assert.deepEqual(calls, ["put", "discard"]);
});

test("terminal disposal reports exactly once after local discard, echoing the disposition", async () => {
  const { entry, saved, detail } = await fixture();
  const calls: string[] = [];
  const reported: unknown[] = [];
  const outcome = await finalizeAudioSavedRequest(
    { entry, blob, saved, broadcastSaved: true },
    {
      putAudioSaved: async () => { calls.push("put"); throw terminalError(410, detail); },
      completeServerSaved: async () => { calls.push("complete"); },
      discardTerminalOutbox: async () => { calls.push("discard"); },
      broadcastAudioSaved: () => { calls.push("broadcast"); },
      reportLocalCopyDisposal: async (disposition) => {
        calls.push("report");
        reported.push(disposition);
      },
    },
  );

  assert.equal(outcome.kind, "terminal-discarded");
  // 删除先于上报;绝不广播、绝不 complete。
  assert.deepEqual(calls, ["put", "discard", "report"]);
  assert.equal(reported.length, 1);
  assert.deepEqual(reported[0], outcome.kind === "terminal-discarded" ? outcome.disposition : null);
});

test("disposal report failure is swallowed: local deletion is the only fact that must hold", async () => {
  const { entry, saved, detail } = await fixture();
  const calls: string[] = [];
  const outcome = await finalizeAudioSavedRequest(
    { entry, blob, saved, broadcastSaved: true },
    {
      putAudioSaved: async () => { calls.push("put"); throw terminalError(410, detail); },
      completeServerSaved: async () => { calls.push("complete"); },
      discardTerminalOutbox: async () => { calls.push("discard"); },
      broadcastAudioSaved: () => { calls.push("broadcast"); },
      reportLocalCopyDisposal: async () => {
        calls.push("report");
        throw new Error("network down");
      },
    },
  );

  assert.equal(outcome.kind, "terminal-discarded");
  assert.deepEqual(calls, ["put", "discard", "report"]);
});

test("server-saved path never emits a disposal report", async () => {
  const { entry, saved } = await fixture();
  const calls: string[] = [];
  const outcome = await finalizeAudioSavedRequest(
    { entry, blob, saved, broadcastSaved: false },
    {
      putAudioSaved: async () => ({ audioReceipt: { serverSeq: 1, rawAudioId: saved.rawAudioId, idempotent: true } }),
      completeServerSaved: async () => { calls.push("complete"); },
      discardTerminalOutbox: async () => { calls.push("discard"); },
      broadcastAudioSaved: () => { calls.push("broadcast"); },
      reportLocalCopyDisposal: async () => { calls.push("report"); },
    },
  );

  assert.equal(outcome.kind, "server-saved");
  assert.deepEqual(calls, ["complete"]);
});

test("foreign recovery can clear a real receipt without broadcasting into current UI", async () => {
  const { entry, saved } = await fixture();
  const calls: string[] = [];
  const outcome = await finalizeAudioSavedRequest(
    { entry, blob, saved, broadcastSaved: false },
    {
      putAudioSaved: async () => ({ audioReceipt: { serverSeq: 1, rawAudioId: saved.rawAudioId, idempotent: true } }),
      completeServerSaved: async () => { calls.push("complete"); },
      discardTerminalOutbox: async () => { calls.push("discard"); },
      broadcastAudioSaved: () => { calls.push("broadcast"); },
    },
  );

  assert.equal(outcome.kind, "server-saved");
  assert.deepEqual(calls, ["complete"]);
});

test("a generation fence is re-evaluated after the receipt before broadcasting", async () => {
  const { entry, saved } = await fixture();
  const calls: string[] = [];
  let generationIsCurrent = true;
  const outcome = await finalizeAudioSavedRequest(
    { entry, blob, saved, broadcastSaved: () => generationIsCurrent },
    {
      putAudioSaved: async () => {
        calls.push("put");
        generationIsCurrent = false;
        return { audioReceipt: { serverSeq: 1, rawAudioId: saved.rawAudioId, idempotent: true } };
      },
      completeServerSaved: async () => { calls.push("complete"); },
      discardTerminalOutbox: async () => { calls.push("discard"); },
      broadcastAudioSaved: () => { calls.push("broadcast"); },
    },
  );
  assert.equal(outcome.kind, "server-saved");
  assert.deepEqual(calls, ["put", "complete"]);
});
