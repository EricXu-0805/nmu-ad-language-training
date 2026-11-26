import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError } from "../apiResponse.ts";
import { drainForeignOutboxEntries, type ForeignOutboxDrainDeps } from "./foreignOutboxDrain.ts";
import type { AudioOutboxEntry } from "./audioOutbox.ts";
import { sha256Blob } from "./audioUploadReceipt.ts";

const bytes = new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 1, 2, 3, 4]);
const blob = new Blob([bytes], { type: "audio/webm" });

function entry(overrides: Partial<AudioOutboxEntry> = {}): AudioOutboxEntry {
  return {
    schemaVersion: 1, rawAudioId: "raw-old-000001", sessionId: "S-OLD", turnKey: "itm-0001#1",
    containsDirectIdentifier: false, durationSeconds: 2.5, blobBytes: bytes.length, mimeType: "audio/webm",
    phase: "registered", createdAtMs: 1, updatedAtMs: 1, ...overrides,
  } as AudioOutboxEntry;
}

function conflict(): ApiError {
  return new ApiError(409, "设备配对场次不一致", { code: "device_session_mismatch", message: "x" }, "nested-detail");
}

function terminal410(saved: Record<string, unknown>): ApiError {
  return new ApiError(410, "audio_terminal_disposition", {
    code: "audio_terminal_disposition", schemaVersion: 1, action: "discard_local_copy", reason: "deleted",
    rawAudioId: saved.rawAudioId, sessionId: saved.sessionId, turnKey: saved.turnKey,
    byteCount: saved.byteCount, checksum: saved.checksum, containsDirectIdentifier: saved.containsDirectIdentifier,
  }, "nested-detail");
}

function deps(
  log: string[],
  behaviour: { upload?: "ok" | "409" | "401" | "404"; saved?: "ok" | "410" | "409" | "2xx-no-receipt"; savedGate?: Promise<void> },
): ForeignOutboxDrainDeps {
  return {
    readBlob: async () => blob,
    createAudio: async () => { log.push("createAudio"); return {}; },
    uploadAudioBlob: async (_rawAudioId, uploaded) => {
      log.push("upload");
      if (behaviour.upload === "409") throw conflict();
      if (behaviour.upload === "401") throw new ApiError(401, "device_pair_required");
      if (behaviour.upload === "404") throw new ApiError(404, "音频不存在");
      // 真服务器回执带的是它算出的校验值;这里照本机字节算,验证器才会认。
      return { raw_audio_id: "raw-old-000001", bytes: bytes.length, checksum: await sha256Blob(uploaded) };
    },
    putOutbox: async (e) => { log.push(`putOutbox:${e.phase}`); },
    putAudioSaved: async (saved) => {
      log.push("audioSaved");
      if (behaviour.savedGate) await behaviour.savedGate;
      if (behaviour.saved === "410") throw terminal410(saved as Record<string, unknown>);
      if (behaviour.saved === "409") throw conflict();
      if (behaviour.saved === "2xx-no-receipt") return { seq: 1 };
      return { seq: 1, audioReceipt: { rawAudioId: "raw-old-000001", serverSeq: 7, idempotent: false } };
    },
    completeOutbox: async () => { log.push("completeOutbox"); },
    discardTerminalOutbox: async () => { log.push("discard"); },
    reportLocalCopyDisposal: async () => { log.push("reportDisposal"); },
  };
}

test("a foreign entry refused on upload still probes audioSaved and is discarded on the exact 410", async () => {
  const log: string[] = [];
  const results = await drainForeignOutboxEntries([entry()], "S-NEW", deps(log, { upload: "409", saved: "410" }));
  assert.deepEqual(results.map((r) => r.outcome), ["terminal-discarded"]);
  assert.deepEqual(log, ["upload", "audioSaved", "discard", "reportDisposal"]);
});

test("a concealed foreign upload 404 reaches the exact server-governed 410 before local disposal", async () => {
  const log: string[] = [];
  const results = await drainForeignOutboxEntries(
    [entry()], "S-NEW", deps(log, { upload: "404", saved: "410" }));
  assert.deepEqual(results.map((r) => r.outcome), ["terminal-discarded"]);
  assert.deepEqual(log, ["upload", "audioSaved", "discard", "reportDisposal"]);
});

test("a concealed upload 404 may complete only after a valid server capture receipt", async () => {
  const log: string[] = [];
  const results = await drainForeignOutboxEntries(
    [entry()], "S-NEW", deps(log, { upload: "404" }));
  assert.deepEqual(results.map((r) => r.outcome), ["server-saved"]);
  assert.deepEqual(log, ["upload", "audioSaved", "completeOutbox"]);
});

test("404 is only a metadata probe: refused, malformed or unreceipted outcomes preserve the local recording", async () => {
  const responses = [
    ...[401, 403, 404, 409, 422, 429, 500].map((status) => ({
      name: `http-${status}`,
      saved: async (_saved: object) => { throw new ApiError(status, "synthetic refusal"); },
    })),
    {
      name: "network-error",
      saved: async (_saved: object) => { throw new TypeError("synthetic offline"); },
    },
    {
      name: "mismatched-410",
      saved: async (saved: object) => { throw terminal410({ ...saved, rawAudioId: "another-recording" }); },
    },
    {
      name: "unstructured-410",
      saved: async (_saved: object) => { throw new ApiError(410, "gone"); },
    },
    { name: "2xx-without-receipt", saved: async (_saved: object) => ({ seq: 1 }) },
  ];
  for (const response of responses) {
    const log: string[] = [];
    const d = deps(log, { upload: "404" });
    d.putAudioSaved = async (saved) => { log.push("audioSaved"); return response.saved(saved); };
    const results = await drainForeignOutboxEntries([entry()], "S-NEW", d);
    assert.equal(results[0].outcome, "kept", response.name);
    assert.deepEqual(log, ["upload", "audioSaved"], response.name);
  }
});

test("the upload recovery probe does not expand to auth, validation, rate limit, server or network errors", async () => {
  for (const error of [
    ...[401, 403, 422, 429, 500, 503].map((status) => new ApiError(status, "synthetic refusal")),
    new TypeError("synthetic offline"),
  ]) {
    const log: string[] = [];
    const d = deps(log, {});
    d.uploadAudioBlob = async () => { log.push("upload"); throw error; };
    const results = await drainForeignOutboxEntries([entry()], "S-NEW", d);
    assert.deepEqual(results.map((r) => [r.outcome, r.reason]), [["kept", "upload-failed"]]);
    assert.deepEqual(log, ["upload"]);
  }
});

test("a registration 404 still preserves captured bytes without a metadata probe", async () => {
  const log: string[] = [];
  const d = deps(log, {});
  d.createAudio = async () => { log.push("createAudio"); throw new ApiError(404, "not found"); };
  const results = await drainForeignOutboxEntries([entry({ phase: "captured" })], "S-NEW", d);
  assert.deepEqual(results.map((r) => [r.outcome, r.reason]), [["kept", "register-failed"]]);
  assert.deepEqual(log, ["createAudio"]);
});

test("entries of the current session are never touched here", async () => {
  const log: string[] = [];
  const results = await drainForeignOutboxEntries([entry({ sessionId: "S-NEW" })], "S-NEW", deps(log, {}));
  assert.deepEqual(results, []);
  assert.deepEqual(log, []);
});

test("an upload that is neither accepted nor a conflict keeps the entry untouched", async () => {
  const log: string[] = [];
  const results = await drainForeignOutboxEntries([entry()], "S-NEW", deps(log, { upload: "401" }));
  assert.deepEqual(results.map((r) => [r.outcome, r.reason]), [["kept", "upload-failed"]]);
  assert.deepEqual(log, ["upload"]);
});

test("a plain 409 on audioSaved (no 410 contract) keeps the entry", async () => {
  const log: string[] = [];
  const results = await drainForeignOutboxEntries([entry()], "S-NEW", deps(log, { upload: "409", saved: "409" }));
  assert.equal(results[0].outcome, "kept");
  assert.ok(!log.includes("discard"));
});

test("a registered entry the server still accepts is uploaded, receipted and completed locally", async () => {
  const log: string[] = [];
  const results = await drainForeignOutboxEntries([entry()], "S-NEW", deps(log, {}));
  assert.deepEqual(results.map((r) => r.outcome), ["server-saved"]);
  assert.deepEqual(log, ["upload", "putOutbox:uploaded", "audioSaved", "completeOutbox"]);
});

test("a captured entry is registered first, then follows the same chain", async () => {
  const log: string[] = [];
  const results = await drainForeignOutboxEntries([entry({ phase: "captured" })], "S-NEW", deps(log, {}));
  assert.deepEqual(results.map((r) => r.outcome), ["server-saved"]);
  assert.deepEqual(log, ["createAudio", "putOutbox:registered", "upload", "putOutbox:uploaded", "audioSaved", "completeOutbox"]);
});

test("a 2xx audioSaved without the server ledger receipt never deletes the local copy", async () => {
  const log: string[] = [];
  const results = await drainForeignOutboxEntries([entry()], "S-NEW", deps(log, { saved: "2xx-no-receipt" }));
  assert.equal(results[0].outcome, "kept");
  assert.ok(!log.includes("completeOutbox"));
  assert.ok(!log.includes("discard"));
});

test("a foreign entry refused with 409 is not retried again within the same current session", async () => {
  // 2026-11-26 生产:平板上 4 段别场录音每次开麦前都被重传、每段 POST /audio 409 + audioSaved 409,
  // 每轮开录从 ~1.7 s 拖到 ~5 s。同一场里拒过的不再重试;换场或刷新页面后照常重试,本机副本一直保留。
  const log: string[] = [];
  const refused = new Map<string, string>();
  const d = deps(log, { saved: "409" });
  d.createAudio = async () => { log.push("createAudio"); throw conflict(); };
  const captured = entry({ phase: "captured" });

  const first = await drainForeignOutboxEntries([captured], "S-NEW", d, refused);
  assert.deepEqual(first.map((r) => [r.outcome, r.reason]), [["kept", "http-409"]]);
  assert.deepEqual(log, ["createAudio", "audioSaved"]);

  const second = await drainForeignOutboxEntries([captured], "S-NEW", d, refused);
  assert.deepEqual(second.map((r) => [r.outcome, r.reason]), [["kept", "refused-this-session"]]);
  assert.deepEqual(log, ["createAudio", "audioSaved"], "same session: no new requests");

  const nextSession = await drainForeignOutboxEntries([captured], "S-NEXT", d, refused);
  assert.deepEqual(nextSession.map((r) => [r.outcome, r.reason]), [["kept", "http-409"]]);
  assert.deepEqual(log, ["createAudio", "audioSaved", "createAudio", "audioSaved"], "a new session retries");
  assert.ok(!log.includes("discard") && !log.includes("completeOutbox"), "the local copy is never removed");
});

test("transient failures are retried on the next record command of the same session", async () => {
  const log: string[] = [];
  const refused = new Map<string, string>();
  const d = deps(log, {});
  d.uploadAudioBlob = async () => { log.push("upload"); throw new TypeError("synthetic offline"); };
  await drainForeignOutboxEntries([entry()], "S-NEW", d, refused);
  await drainForeignOutboxEntries([entry()], "S-NEW", d, refused);
  assert.deepEqual(log, ["upload", "upload"]);
  assert.equal(refused.size, 0);
});

test("a refusal memo for one entry does not skip a different foreign entry", async () => {
  const log: string[] = [];
  const refused = new Map<string, string>([["raw-old-000001", "S-NEW"]]);
  const d = deps(log, {});
  d.uploadAudioBlob = async (rawAudioId, uploaded) => {
    log.push("upload");
    return { raw_audio_id: rawAudioId, bytes: bytes.length, checksum: await sha256Blob(uploaded) };
  };
  d.putAudioSaved = async (saved) => {
    log.push("audioSaved");
    const rawAudioId = (saved as { rawAudioId: string }).rawAudioId;
    return { seq: 1, audioReceipt: { rawAudioId, serverSeq: 8, idempotent: false } };
  };
  const results = await drainForeignOutboxEntries(
    [entry(), entry({ rawAudioId: "raw-old-000002" })], "S-NEW", d, refused);
  assert.deepEqual(results.map((r) => [r.rawAudioId, r.outcome]), [
    ["raw-old-000001", "kept"], ["raw-old-000002", "server-saved"]]);
  assert.equal(log.filter((step) => step === "upload").length, 1);
});

test("the same entry is drained once at a time: a second caller joins the in-flight drain", async () => {
  const log: string[] = [];
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const d = deps(log, { savedGate: gate });
  const first = drainForeignOutboxEntries([entry()], "S-NEW", d);
  const second = drainForeignOutboxEntries([entry()], "S-NEW", d);
  release();
  const [a, b] = await Promise.all([first, second]);
  assert.deepEqual(a, b);
  assert.equal(log.filter((step) => step === "upload").length, 1);
  assert.equal(log.filter((step) => step === "audioSaved").length, 1);
});
