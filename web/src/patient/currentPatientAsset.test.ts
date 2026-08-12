import assert from "node:assert/strict";
import test from "node:test";
import {
  loadCurrentPatientAsset,
  patientAssetRequirementReady,
  PatientAssetLoadError,
  type CurrentPatientAssetDependencies,
  type PatientAssetCredentialSelection,
} from "./currentPatientAsset.ts";

const CAPABILITY = "x".repeat(43);

function credential(sessionId = "S-ONE"): PatientAssetCredentialSelection {
  return {
    source: "active",
    headers: { "X-Device-Capability": CAPABILITY },
    record: {
      capability: CAPABILITY,
      sessionId,
      expiresAt: "2099-01-01T00:00:00Z",
    },
  };
}

function imageResponse(): Response {
  return new Response(new Blob([new Uint8Array([1, 2, 3, 4])], { type: "image/webp" }), {
    status: 200,
    headers: {
      "Content-Type": "image/webp",
      "Cache-Control": "private, no-store",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

function dependencies(overrides: Partial<CurrentPatientAssetDependencies> = {}): CurrentPatientAssetDependencies {
  return {
    fetchImpl: async () => imageResponse(),
    selectCredential: (sessionId) => credential(sessionId),
    handleAuthorizationFailure: () => false,
    createObjectUrl: () => "blob:patient-current-1",
    revokeObjectUrl: () => {},
    decodeImage: async () => ({ naturalWidth: 640, naturalHeight: 480 }),
    ...overrides,
  };
}

test("current asset request sends only the session current endpoint and exact device capability", async () => {
  let observedUrl = "";
  let observedInit: RequestInit | undefined;
  const asset = await loadCurrentPatientAsset("S-ONE", new AbortController().signal, dependencies({
    fetchImpl: async (url, init) => {
      observedUrl = url;
      observedInit = init;
      return imageResponse();
    },
  }));

  assert.equal(observedUrl, "/sessions/S-ONE/patient-asset/current");
  assert.equal(observedUrl.includes("wk2-99"), false);
  assert.equal(observedUrl.includes("image"), false);
  assert.equal(observedInit?.credentials, "omit");
  assert.equal(observedInit?.cache, "no-store");
  const observedHeaders = observedInit?.headers as Record<string, string> | undefined;
  assert.equal(observedHeaders?.["X-Device-Capability"], CAPABILITY);
  assert.equal(asset.naturalWidth, 640);
  asset.dispose();
});

test("a missing exact active-session capability fails before any network request", async () => {
  let fetches = 0;
  const noCredential: PatientAssetCredentialSelection = { source: null, headers: {}, record: null };
  await assert.rejects(
    loadCurrentPatientAsset("S-ONE", new AbortController().signal, dependencies({
      fetchImpl: async () => { fetches += 1; return imageResponse(); },
      selectCredential: () => noCredential,
    })),
    (error: unknown) => error instanceof PatientAssetLoadError
      && error.code === "authorization_required",
  );
  assert.equal(fetches, 0);
});

test("decode failure revokes the object URL and never returns a ready asset", async () => {
  const revoked: string[] = [];
  await assert.rejects(
    loadCurrentPatientAsset("S-ONE", new AbortController().signal, dependencies({
      createObjectUrl: () => "blob:decode-failure",
      revokeObjectUrl: (url) => { revoked.push(url); },
      decodeImage: async () => { throw new Error("bad bytes"); },
    })),
    (error: unknown) => error instanceof PatientAssetLoadError
      && error.code === "decode_failed",
  );
  assert.deepEqual(revoked, ["blob:decode-failure"]);
});

test("one finite deadline covers decode and revokes the object URL fail closed", async () => {
  const revoked: string[] = [];
  const response = imageResponse();
  Object.defineProperty(response, "blob", {
    value: async () => new Blob(
      [new Uint8Array([1, 2, 3, 4])],
      { type: "image/webp" },
    ),
  });
  await assert.rejects(
    loadCurrentPatientAsset("S-ONE", new AbortController().signal, dependencies({
      requestTimeoutMs: 10,
      // Keep the pre-decode path in the microtask queue. Native Response.blob()
      // may be delayed behind the 10 ms timer when the whole suite is busy,
      // which would test "timeout before URL creation" instead of decode cleanup.
      fetchImpl: async () => response,
      createObjectUrl: () => "blob:decode-timeout",
      revokeObjectUrl: (url) => { revoked.push(url); },
      decodeImage: async () => new Promise(() => {}),
    })),
    (error: unknown) => error instanceof PatientAssetLoadError
      && error.code === "not_available",
  );
  assert.deepEqual(revoked, ["blob:decode-timeout"]);
});

test("a switched item aborts and cleans the stale object URL while the replacement remains owned", async () => {
  let finishOld!: (value: { naturalWidth: number; naturalHeight: number }) => void;
  const oldDecode = new Promise<{ naturalWidth: number; naturalHeight: number }>((resolve) => {
    finishOld = resolve;
  });
  let markOldDecodeStarted!: () => void;
  const oldDecodeStarted = new Promise<void>((resolve) => { markOldDecodeStarted = resolve; });
  const revoked: string[] = [];
  let urlSeq = 0;
  const deps = dependencies({
    createObjectUrl: () => `blob:race-${++urlSeq}`,
    revokeObjectUrl: (url) => { revoked.push(url); },
    decodeImage: (url) => {
      if (url === "blob:race-1") {
        markOldDecodeStarted();
        return oldDecode;
      }
      return Promise.resolve({ naturalWidth: 320, naturalHeight: 240 });
    },
  });
  const oldController = new AbortController();
  const stale = loadCurrentPatientAsset("S-ONE", oldController.signal, deps);
  await oldDecodeStarted;
  const staleRejected = assert.rejects(stale, (error: unknown) => error instanceof DOMException
    && error.name === "AbortError");
  oldController.abort(new DOMException("切题", "AbortError"));
  const replacement = await loadCurrentPatientAsset("S-ONE", new AbortController().signal, deps);
  await staleRejected;
  finishOld({ naturalWidth: 640, naturalHeight: 480 });
  await Promise.resolve();

  assert.deepEqual(revoked, ["blob:race-1"]);
  replacement.dispose();
  replacement.dispose();
  assert.deepEqual(revoked, ["blob:race-1", "blob:race-2"]);
});

test("an explicit no-image contract is ready without a network state", () => {
  assert.equal(patientAssetRequirementReady(false, null, "no-image"), true);
  assert.equal(patientAssetRequirementReady(true, null, "itm-0001"), false);
  assert.equal(patientAssetRequirementReady(true, {
    requestKey: "itm-0002",
    readiness: "ready",
  }, "itm-0001"), false);
  assert.equal(patientAssetRequirementReady(true, {
    requestKey: "itm-0001",
    readiness: "ready",
  }, "itm-0001"), true);
});
