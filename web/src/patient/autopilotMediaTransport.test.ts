import assert from "node:assert/strict";
import test from "node:test";
import type { DeviceCredentialSelection } from "../api.ts";
import { ApiError } from "../apiResponse.ts";
import {
  acknowledgeExactAutopilotDrain,
  authorizeExactAutopilotRecording,
  fetchExactAutopilotDrainTarget,
  fetchExactAutopilotTts,
  type AutopilotMediaTransportDependencies,
} from "./autopilotMediaTransport.ts";
import type { NextCommandProjection } from "./autopilotProtocol.ts";

type TtsCommand = Extract<NextCommandProjection, { kind: "tts" }>;

const credential: DeviceCredentialSelection = {
  source: "active",
  record: {
    capability: "x".repeat(43),
    sessionId: "S/ONE",
    expiresAt: "2026-07-19T12:00:00Z",
  },
  headers: { "X-Device-Capability": "x".repeat(43) },
};

function pendingTts(commandKey = "cmd-question:0001"): TtsCommand {
  return {
    schema_version: 1,
    command_key: commandKey,
    command_seq: 1,
    kind: "tts",
    state: "pending",
    command_revision: 0,
    control_generation: 1,
    runner_generation: 1,
    item_ref: "itm-0001",
    turn_seq: 1,
    attempt_seq: 1,
    prompt_level: 0,
    payload: {
      speech_key: "wk2.01.question",
      speech_text: "请说出图片中的物品。",
      purpose: "question",
    },
  };
}

function dependencies(fetchImpl: AutopilotMediaTransportDependencies["fetchImpl"]): {
  deps: AutopilotMediaTransportDependencies;
  authFailures: number[];
} {
  const authFailures: number[] = [];
  return {
    authFailures,
    deps: {
      fetchImpl,
      selectCredential: () => credential,
      handleAuthorizationFailure: (status) => {
        authFailures.push(status);
        return true;
      },
      csrf: () => ({ "X-CSRF-Token": "csrf-proof" }),
      nextCommand: async () => pendingTts(),
    },
  };
}

test("exact TTS sends only the encoded command URL and device proofs", async () => {
  let observed: { url: string; init: RequestInit } | null = null;
  const { deps } = dependencies(async (input, init = {}) => {
    observed = { url: String(input), init };
    return new Response(new Blob(["RIFFvoice"], { type: "audio/wav" }), {
      status: 200,
      headers: { "Content-Type": "audio/wav" },
    });
  });
  const controller = new AbortController();
  const blob = await fetchExactAutopilotTts(
    "S/ONE", pendingTts(), controller.signal, deps);
  assert.equal(blob?.type, "audio/wav");
  assert.ok(observed);
  assert.equal(observed.url,
    "/sessions/S%2FONE/autopilot/commands/cmd-question%3A0001/tts");
  assert.equal(observed.init.method, "POST");
  assert.equal(observed.init.credentials, "omit");
  assert.equal(observed.init.cache, "no-store");
  assert.equal(Object.hasOwn(observed.init, "body"), false);
  assert.deepEqual(observed.init.headers, {
    "X-Device-Capability": "x".repeat(43),
    "X-CSRF-Token": "csrf-proof",
  });
});

test("exact recording authorization sends no client facts and parses a closed receipt", async () => {
  let observed: RequestInit | null = null;
  const { deps } = dependencies(async (_input, init = {}) => {
    observed = init;
    return new Response(JSON.stringify({
      allowed: true,
      runtime_status: "active",
      is_simulation: true,
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  const result = await authorizeExactAutopilotRecording(
    "S/ONE", "cmd-record-0001", new AbortController().signal, deps);
  assert.deepEqual(result, {
    allowed: true,
    runtime_status: "active",
    is_simulation: true,
  });
  assert.ok(observed);
  assert.equal(Object.hasOwn(observed, "body"), false);

  const extra = dependencies(async () => new Response(JSON.stringify({
    allowed: true,
    runtime_status: "active",
    is_simulation: true,
    command_key: "leak",
  }), { status: 200 }));
  await assert.rejects(() => authorizeExactAutopilotRecording(
    "S/ONE", "cmd-record-0001", new AbortController().signal, extra.deps), /未证明/);
  const notSimulation = dependencies(async () => new Response(JSON.stringify({
    allowed: true,
    runtime_status: "active",
    is_simulation: false,
  }), { status: 200 }));
  await assert.rejects(() => authorizeExactAutopilotRecording(
    "S/ONE", "cmd-record-0001", new AbortController().signal, notSimulation.deps), /未证明/);
});

test("drain ACK is exact, bodyless, and waits for the caller's physical-stop boundary", async () => {
  let observed: { url: string; init: RequestInit } | null = null;
  const { deps } = dependencies(async (input, init = {}) => {
    observed = { url: String(input), init };
    return new Response(JSON.stringify({ replayed: false, state_revision: 3 }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  const receipt = await acknowledgeExactAutopilotDrain(
    "S/ONE", "cmd record/1", new AbortController().signal, deps);
  assert.deepEqual(receipt, { replayed: false, state_revision: 3 });
  assert.ok(observed);
  assert.equal(observed.url,
    "/sessions/S%2FONE/autopilot/commands/cmd%20record%2F1/drain-ack");
  assert.equal(observed.init.method, "POST");
  assert.equal(Object.hasOwn(observed.init, "body"), false);
  assert.equal((observed.init.headers as Record<string, string>)["Content-Type"], undefined);

  const invalid = dependencies(async () => new Response(JSON.stringify({
    replayed: false,
    state_revision: 3,
    client_selected: true,
  }), { status: 200, headers: { "Content-Type": "application/json" } }));
  await assert.rejects(() => acknowledgeExactAutopilotDrain(
    "S/ONE", "cmd-record-0001", new AbortController().signal, invalid.deps),
  /封闭契约/);
});

test("paused refresh recovers only the exact opaque drain target", async () => {
  let observed: { url: string; init: RequestInit } | null = null;
  const { deps } = dependencies(async (input, init = {}) => {
    observed = { url: String(input), init };
    return new Response(JSON.stringify({
      command_key: "cmd-drain-target-0001",
      state_revision: 2,
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  const target = await fetchExactAutopilotDrainTarget(
    "S/ONE", new AbortController().signal, deps);
  assert.deepEqual(target, {
    command_key: "cmd-drain-target-0001",
    state_revision: 2,
  });
  assert.ok(observed);
  assert.equal(observed.url, "/sessions/S%2FONE/autopilot/drain-target");
  assert.equal(observed.init.method, "GET");
  assert.equal(Object.hasOwn(observed.init, "body"), false);
  assert.deepEqual(observed.init.headers, {
    "X-Device-Capability": "x".repeat(43),
  });

  const extra = dependencies(async () => new Response(JSON.stringify({
    command_key: "cmd-drain-target-0001",
    state_revision: 2,
    item_id: "must-not-leak",
  }), { status: 200 }));
  await assert.rejects(() => fetchExactAutopilotDrainTarget(
    "S/ONE", new AbortController().signal, extra.deps), /封闭契约/);
});

test("a never-returning drain request reaches a finite fail-closed deadline", async () => {
  const { deps } = dependencies(() => new Promise<Response>(() => undefined));
  deps.requestTimeoutMs = 5;
  await assert.rejects(
    () => fetchExactAutopilotDrainTarget(
      "S/ONE", new AbortController().signal, deps),
    (error: unknown) => error instanceof ApiError && error.status === 408,
  );
});

test("exact media errors preserve canonical API details and run auth-loss handling", async () => {
  const { deps, authFailures } = dependencies(async () => new Response(JSON.stringify({
    detail: { code: "autopilot_command_not_current", message: "stale" },
  }), { status: 409, headers: { "Content-Type": "application/json" } }));
  await assert.rejects(
    () => fetchExactAutopilotTts(
      "S/ONE", pendingTts("cmd-stale-0001"), new AbortController().signal, deps),
    (error: unknown) => error instanceof ApiError
      && error.status === 409
      && error.detailEnvelope === "nested-detail"
      && (error.detailData as { code?: string }).code === "autopilot_command_not_current",
  );
  assert.deepEqual(authFailures, [409]);
});

test("TTS bytes are discarded when server authority changes during synthesis", async () => {
  const { deps } = dependencies(async () => new Response(
    new Blob(["RIFFvoice"], { type: "audio/wav" }), { status: 200 }));
  deps.nextCommand = async () => null;
  await assert.rejects(
    () => fetchExactAutopilotTts(
      "S/ONE", pendingTts(), new AbortController().signal, deps),
    /服务器运行时或命令世代已变化/,
  );
});

test("TTS authority is re-proved after blob materialization and before bytes reach play", async () => {
  const { deps } = dependencies(async () => new Response(
    new Blob(["RIFFvoice"], { type: "audio/wav" }), { status: 200 }));
  let authorityReads = 0;
  deps.nextCommand = async () => {
    authorityReads += 1;
    return authorityReads === 1
      ? pendingTts()
      : { ...pendingTts(), runner_generation: 2 };
  };

  await assert.rejects(
    () => fetchExactAutopilotTts(
      "S/ONE", pendingTts(), new AbortController().signal, deps),
    /服务器运行时或命令世代已变化/,
  );
  assert.equal(authorityReads, 2);
});

test("capability rotation during final authority read discards synthesized bytes", async () => {
  const { deps } = dependencies(async () => new Response(
    new Blob(["RIFFvoice"], { type: "audio/wav" }), { status: 200 }));
  const replacement: DeviceCredentialSelection = {
    source: "active",
    record: {
      capability: "y".repeat(43),
      sessionId: "S/ONE",
      expiresAt: "2026-07-19T12:00:00Z",
    },
    headers: { "X-Device-Capability": "y".repeat(43) },
  };
  let active = credential;
  let authorityReads = 0;
  deps.selectCredential = () => active;
  deps.nextCommand = async () => {
    authorityReads += 1;
    if (authorityReads === 2) active = replacement;
    return pendingTts();
  };

  await assert.rejects(
    () => fetchExactAutopilotTts(
      "S/ONE", pendingTts(), new AbortController().signal, deps),
    /设备凭据已变化/,
  );
  assert.equal(authorityReads, 2);
});

test("missing exact active capability fails before any network request", async () => {
  let requests = 0;
  const { deps } = dependencies(async () => {
    requests += 1;
    return new Response(null, { status: 204 });
  });
  deps.selectCredential = () => ({ headers: {}, source: null, record: null });
  await assert.rejects(
    () => fetchExactAutopilotTts(
      "S/ONE", pendingTts("cmd-tts-0001"), new AbortController().signal, deps),
    (error: unknown) => error instanceof ApiError && error.status === 401,
  );
  assert.equal(requests, 0);
});
