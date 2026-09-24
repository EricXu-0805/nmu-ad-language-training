import assert from "node:assert/strict";
import test from "node:test";
import type { DeviceCredentialSelection } from "../api.ts";
import { ApiError } from "../apiResponse.ts";
import { AutopilotMediaError } from "./autopilotMediaError.ts";
import {
  acknowledgeExactAutopilotDrain,
  authorizeExactAutopilotRecording,
  authorizeExactAutopilotBargeIn,
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

test("voice monitor authorization binds the started prompt and refuses malformed or stale authority", async () => {
  const command = { ...pendingTts(), state: "started" as const, command_revision: 1 };
  const receipt = { allowed: true, barge_in_authorized: true, runtime_status: "active", is_simulation: false };
  let calls = 0;
  const { deps } = dependencies(async (input, init) => {
    calls += 1;
    assert.equal(String(input), "/sessions/S%2FONE/autopilot/commands/cmd-question%3A0001/barge-in-authorization");
    assert.deepEqual(JSON.parse(String(init?.body)), {
      command_revision: 1, control_generation: 1, runner_generation: 1,
    });
    assert.equal(init?.credentials, "omit");
    return Response.json(receipt);
  });
  await authorizeExactAutopilotBargeIn("S/ONE", command, new AbortController().signal, deps);
  await assert.rejects(authorizeExactAutopilotBargeIn("S/ONE", pendingTts(), new AbortController().signal, deps));
  assert.equal(calls, 1);
  for (const response of [ { ...receipt, barge_in_authorized: false },
    { ...receipt, allowed: "true" }, { ...receipt, recording_authorized: true },
    { ...receipt, runtime_status: "paused" }, { ...receipt, is_simulation: null } ]) {
    await assert.rejects(authorizeExactAutopilotBargeIn("S/ONE", command,
      new AbortController().signal, dependencies(async () => Response.json(response)).deps));
  }
  let active = credential;
  const switched = dependencies(async () => {
    active = { ...credential, record: { ...credential.record!, capability: "y".repeat(43) } };
    return Response.json(receipt);
  }).deps;
  switched.selectCredential = () => active;
  await assert.rejects(authorizeExactAutopilotBargeIn("S/ONE", command,
    new AbortController().signal, switched), /凭据已变化/);
  const timed = dependencies(() => new Promise<Response>(() => {})).deps;
  timed.requestTimeoutMs = 5;
  await assert.rejects(authorizeExactAutopilotBargeIn("S/ONE", command,
    new AbortController().signal, timed), /超时/);
});

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
      recording_authorized: true,
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

  // 真实研究场次:is_simulation:false + 显式肯定授权照样放行。
  const research = dependencies(async () => new Response(JSON.stringify({
    allowed: true,
    recording_authorized: true,
    runtime_status: "active",
    is_simulation: false,
  }), { status: 200 }));
  assert.deepEqual(
    await authorizeExactAutopilotRecording(
      "S/ONE", "cmd-record-0001", new AbortController().signal, research.deps),
    { allowed: true, runtime_status: "active", is_simulation: false },
  );

  const extra = dependencies(async () => new Response(JSON.stringify({
    allowed: true,
    recording_authorized: true,
    runtime_status: "active",
    is_simulation: true,
    command_key: "leak",
  }), { status: 200 }));
  await assert.rejects(() => authorizeExactAutopilotRecording(
    "S/ONE", "cmd-record-0001", new AbortController().signal, extra.deps), /未证明/);

  // 旧后端三键形状（没有 recording_authorized 肯定授权）必须拒绝:
  // 新前端配旧后端绝不静默开麦。
  const legacyShape = dependencies(async () => new Response(JSON.stringify({
    allowed: true,
    runtime_status: "active",
    is_simulation: true,
  }), { status: 200 }));
  await assert.rejects(() => authorizeExactAutopilotRecording(
    "S/ONE", "cmd-record-0001", new AbortController().signal, legacyShape.deps), /未证明/);

  for (const spoiledAuthorization of [false, "true", 1, null]) {
    const spoiled = dependencies(async () => new Response(JSON.stringify({
      allowed: true,
      recording_authorized: spoiledAuthorization,
      runtime_status: "active",
      is_simulation: true,
    }), { status: 200 }));
    await assert.rejects(() => authorizeExactAutopilotRecording(
      "S/ONE", "cmd-record-0001", new AbortController().signal, spoiled.deps), /未证明/);
  }

  // is_simulation 必须是严格布尔——字符串/缺失/null 全拒。
  for (const spoiledSimulation of ["false", 0, null]) {
    const spoiled = dependencies(async () => new Response(JSON.stringify({
      allowed: true,
      recording_authorized: true,
      runtime_status: "active",
      is_simulation: spoiledSimulation,
    }), { status: 200 }));
    await assert.rejects(() => authorizeExactAutopilotRecording(
      "S/ONE", "cmd-record-0001", new AbortController().signal, spoiled.deps), /未证明/);
  }
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
    (error: unknown) => {
      // TTS 侧现在带 failure_stage 包装;原 ApiError 语义原封进 cause,不丢细节。
      if (!(error instanceof AutopilotMediaError)) return false;
      if (error.errorCode !== "audio_playback_failed"
        || error.failureStage !== "fetch_failed") return false;
      const cause = error.cause;
      return cause instanceof ApiError
        && cause.status === 409
        && cause.detailEnvelope === "nested-detail"
        && (cause.detailData as { code?: string }).code === "autopilot_command_not_current";
    },
  );
  assert.deepEqual(authFailures, [409]);
});

// ---------------- TTS 合成 POST 的有界重试：每次尝试各自 4 s，最多再试两次 ----------------

function audioResponse(): Response {
  return new Response(new Blob(["RIFFvoice"], { type: "audio/wav" }), {
    status: 200,
    headers: { "Content-Type": "audio/wav" },
  });
}

for (const transient of [
  { name: "网络 TypeError", fail: () => { throw new TypeError("Failed to fetch"); } },
  { name: "503", fail: () => new Response("upstream down", { status: 503 }) },
  { name: "502", fail: () => new Response("", { status: 502 }) },
]) {
  test(`TTS 合成 POST 瞬时失败(${transient.name})两次后第三次成功：同一 URL、同一凭据，revalidate 照旧两次`, async () => {
    const urls: string[] = [];
    let calls = 0;
    const { deps } = dependencies(async (input) => {
      calls += 1;
      urls.push(String(input));
      if (calls <= 2) return transient.fail();
      return audioResponse();
    });
    deps.ttsRetryDelaysMs = [0, 0];
    let authorityReads = 0;
    deps.nextCommand = async () => { authorityReads += 1; return pendingTts(); };

    const blob = await fetchExactAutopilotTts(
      "S/ONE", pendingTts(), new AbortController().signal, deps);

    assert.equal(blob?.type, "audio/wav");
    assert.equal(calls, 3);
    assert.equal(new Set(urls).size, 1);
    assert.equal(authorityReads, 2);
  });
}

test("TTS 合成 POST 三次都瞬时失败：抛 fetch_failed，cause 是最后那个错误，不再有第四次", async () => {
  let calls = 0;
  const { deps } = dependencies(async () => {
    calls += 1;
    return new Response("", { status: 500 + calls });
  });
  deps.ttsRetryDelaysMs = [0, 0];
  await assert.rejects(
    () => fetchExactAutopilotTts("S/ONE", pendingTts(), new AbortController().signal, deps),
    (error: unknown) => error instanceof AutopilotMediaError
      && error.failureStage === "fetch_failed"
      && error.cause instanceof ApiError && error.cause.status === 503,
  );
  assert.equal(calls, 3);
});

for (const permanent of [
  { name: "401", status: 401 },
  { name: "409", status: 409 },
  { name: "422", status: 422 },
  { name: "404", status: 404 },
]) {
  test(`TTS 合成 POST ${permanent.name} 一次都不重试`, async () => {
    let calls = 0;
    const { deps } = dependencies(async () => {
      calls += 1;
      return new Response(JSON.stringify({ detail: { code: "x", message: "y" } }),
        { status: permanent.status, headers: { "Content-Type": "application/json" } });
    });
    deps.ttsRetryDelaysMs = [0, 0];
    await assert.rejects(
      () => fetchExactAutopilotTts("S/ONE", pendingTts(), new AbortController().signal, deps),
      (error: unknown) => error instanceof AutopilotMediaError
        && error.cause instanceof ApiError && error.cause.status === permanent.status,
    );
    assert.equal(calls, 1);
  });
}

test("TTS 204(本轮无音频)不是失败：交回 null，一次都不重试", async () => {
  let calls = 0;
  const { deps } = dependencies(async () => {
    calls += 1;
    return new Response(null, { status: 204 });
  });
  deps.ttsRetryDelaysMs = [0, 0];
  assert.equal(
    await fetchExactAutopilotTts("S/ONE", pendingTts(), new AbortController().signal, deps),
    null);
  assert.equal(calls, 1);
});

test("每次尝试各自的期限：悬挂的合成 POST 到点按 408 中止并重试，第二次成功", async () => {
  const signals: AbortSignal[] = [];
  let calls = 0;
  const { deps } = dependencies((_input, init = {}) => {
    calls += 1;
    signals.push(init.signal as AbortSignal);
    if (calls === 1) return new Promise<Response>(() => undefined);
    return Promise.resolve(audioResponse());
  });
  deps.ttsAttemptTimeoutMs = 5;
  deps.ttsRetryDelaysMs = [0, 0];

  const blob = await fetchExactAutopilotTts(
    "S/ONE", pendingTts(), new AbortController().signal, deps);

  assert.equal(blob?.type, "audio/wav");
  assert.equal(calls, 2);
  // 第一条请求确实被自己的期限物理中止，不是悬着不管。
  assert.equal(signals[0]?.aborted, true);
  assert.ok(signals[0]?.reason instanceof ApiError && signals[0].reason.status === 408);
  assert.equal(signals[1]?.aborted, false);
});

test("冷缓存合成慢但会成功：第一次期限足够长,一次 POST 就拿到音频,不掐、不重发第二次合成", async () => {
  // 复核 2026-09-19:每次一律 4 s 会把 >4 s 的云合成掐成 tts_failed,且每掐一次服务端又起
  // 一次合成(没有 in-flight 去重)。第一次给长期限,短期限只留给之后的连接级重试。
  let calls = 0;
  const { deps } = dependencies(async () => {
    calls += 1;
    await new Promise((resolve) => setTimeout(resolve, 30));
    return audioResponse();
  });
  deps.ttsAttemptTimeoutsMs = [200, 5];
  deps.ttsRetryDelaysMs = [0, 0];

  const blob = await fetchExactAutopilotTts(
    "S/ONE", pendingTts(), new AbortController().signal, deps);
  assert.equal(blob?.type, "audio/wav");
  assert.equal(calls, 1);
});

test("生产期限:首次 10 s、之后 3.5 s,两次退避 0.5/0.75 s,最坏 18.25 s 在 20 s 起播期限内", async () => {
  const source = await import("node:fs").then((fs) => fs.readFileSync(
    new URL("./autopilotMediaTransport.ts", import.meta.url), "utf8"));
  assert.match(source, /TTS_ATTEMPT_TIMEOUTS_MS: readonly number\[\] = \[10_000, 3_500\];/);
  assert.match(source, /TTS_RETRY_DELAYS_MS: readonly number\[\] = \[500, 750\];/);
});

test("父 signal 在退避等待中中止：不再发第二次，原样抛 AbortError", async () => {
  let calls = 0;
  const { deps } = dependencies(async () => {
    calls += 1;
    throw new TypeError("Failed to fetch");
  });
  deps.ttsRetryDelaysMs = [50, 50];
  const controller = new AbortController();
  const pending = fetchExactAutopilotTts("S/ONE", pendingTts(), controller.signal, deps);
  await new Promise((resolve) => setTimeout(resolve, 5));
  controller.abort(new DOMException("语音播放已取消", "AbortError"));
  await assert.rejects(pending, (error: unknown) => error instanceof DOMException
    && error.name === "AbortError");
  await new Promise((resolve) => setTimeout(resolve, 70));
  assert.equal(calls, 1);
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

test("TTS 响应头已到但音频下载断流时重取同一命令,完整字节仍须两次授权复核", async () => {
  let requests = 0;
  let authorityReads = 0;
  const { deps } = dependencies(async () => {
    requests += 1;
    const response = new Response(new Blob(["RIFFvoice"], { type: "audio/wav" }));
    if (requests === 1) response.blob = async () => { throw new TypeError("terminated"); };
    return response;
  });
  deps.ttsRetryDelaysMs = [0, 0];
  deps.nextCommand = async () => { authorityReads += 1; return pendingTts(); };
  const blob = await fetchExactAutopilotTts(
    "S/ONE", pendingTts(), new AbortController().signal, deps);
  assert.equal(await blob?.text(), "RIFFvoice");
  assert.equal(requests, 2);
  assert.equal(authorityReads, 3);
});

test("音频响应体下载也受单次期限约束,迟到的第一份字节不会继续复核或交给播放", async () => {
  let requests = 0;
  let authorityReads = 0;
  let firstSignal: AbortSignal | undefined;
  let resolveLateBody!: (blob: Blob) => void;
  const { deps } = dependencies(async (_input, init = {}) => {
    requests += 1;
    const response = new Response(new Blob(["RIFFretry"], { type: "audio/wav" }));
    if (requests === 1) {
      firstSignal = init.signal as AbortSignal;
      response.blob = () => new Promise<Blob>((resolve) => { resolveLateBody = resolve; });
    }
    return response;
  });
  deps.ttsAttemptTimeoutMs = 10;
  deps.ttsRetryDelaysMs = [0, 0];
  deps.nextCommand = async () => { authorityReads += 1; return pendingTts(); };
  // 此计时器只让旧实现可确定地退出,修正实现应在它之前重试成功。
  const late = setTimeout(() => resolveLateBody(new Blob(["RIFFlate"], { type: "audio/wav" })), 80);
  try {
    const blob = await fetchExactAutopilotTts(
      "S/ONE", pendingTts(), new AbortController().signal, deps);
    assert.equal(await blob?.text(), "RIFFretry");
    assert.equal(requests, 2);
    assert.equal(firstSignal?.aborted, true);
    resolveLateBody(new Blob(["RIFFlate"], { type: "audio/wav" }));
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    assert.equal(authorityReads, 3);
  } finally {
    clearTimeout(late);
  }
});

test("TTS 播放前授权复核遇到一次 503 可恢复,新一轮仍须复核同一条命令", async () => {
  let requests = 0;
  let authorityReads = 0;
  const { deps } = dependencies(async () => {
    requests += 1;
    return new Response(new Blob(["RIFFvoice"], { type: "audio/wav" }));
  });
  deps.ttsRetryDelaysMs = [0, 0];
  deps.nextCommand = async () => {
    authorityReads += 1;
    if (authorityReads === 1) throw new ApiError(503, "暂时不可用");
    return pendingTts();
  };
  const blob = await fetchExactAutopilotTts(
    "S/ONE", pendingTts(), new AbortController().signal, deps);
  assert.equal(blob?.type, "audio/wav");
  assert.equal(requests, 2);
  assert.equal(authorityReads, 3);
});

test("断流重试期间命令代际变化时立即拒绝,不使用先前授权或再发第三次请求", async () => {
  let requests = 0;
  let authorityReads = 0;
  const { deps } = dependencies(async () => {
    requests += 1;
    const response = new Response(new Blob(["RIFFvoice"], { type: "audio/wav" }));
    if (requests === 1) response.blob = async () => { throw new TypeError("terminated"); };
    return response;
  });
  deps.ttsRetryDelaysMs = [0, 0];
  deps.nextCommand = async () => {
    authorityReads += 1;
    return authorityReads === 1 ? pendingTts() : { ...pendingTts(), runner_generation: 2 };
  };
  await assert.rejects(() => fetchExactAutopilotTts(
    "S/ONE", pendingTts(), new AbortController().signal, deps),
  (error: unknown) => error instanceof AutopilotMediaError && error.failureStage === "authority_changed");
  assert.equal(requests, 2);
  assert.equal(authorityReads, 2);
});

test("TTS 内容无效仍是 blob_invalid,不会因纳入获取重试范围而重试", async () => {
  let requests = 0;
  const { deps } = dependencies(async () => {
    requests += 1;
    return new Response(new Blob(["error page"], { type: "text/html" }));
  });
  deps.ttsRetryDelaysMs = [0, 0];
  await assert.rejects(() => fetchExactAutopilotTts(
    "S/ONE", pendingTts(), new AbortController().signal, deps),
  (error: unknown) => error instanceof AutopilotMediaError && error.failureStage === "blob_invalid");
  assert.equal(requests, 1);
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
    (error: unknown) => error instanceof AutopilotMediaError
      && error.errorCode === "audio_playback_failed"
      && error.failureStage === "credential_rotated"
      && error.cause instanceof ApiError && error.cause.status === 401,
  );
  assert.equal(requests, 0);
});
