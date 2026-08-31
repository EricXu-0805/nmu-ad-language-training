import type { DeviceCredentialSelection } from "../api.ts";
import { ApiError, decodeJsonApiResponse } from "../apiResponse.ts";
import { AutopilotMediaError } from "./autopilotMediaError.ts";
import type { RecordingAuthorization } from "./recordingAuthorization.ts";
import {
  parseNextCommandProjection,
  type NextCommandProjection,
} from "./autopilotProtocol.ts";

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
type TtsCommand = Extract<NextCommandProjection, { kind: "tts" }>;

export interface AutopilotMediaTransportDependencies {
  fetchImpl: FetchLike;
  selectCredential(sessionId: string): DeviceCredentialSelection;
  handleAuthorizationFailure(
    status: number,
    text: string,
    credential: DeviceCredentialSelection,
  ): boolean;
  csrf(method: string): Record<string, string>;
  nextCommand(sessionId: string): Promise<unknown | null>;
  /** Test-only override; production drain recovery uses a finite 12s deadline. */
  requestTimeoutMs?: number;
}

const COMMAND_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/;
const DRAIN_REQUEST_TIMEOUT_MS = 12_000;

interface ExactTtsAuthority {
  sessionId: string;
  commandKey: string;
  commandIdentity: string;
  capability: string;
}

function ttsCommandIdentity(command: TtsCommand): string {
  return JSON.stringify([
    command.schema_version,
    command.command_key,
    command.command_seq,
    command.state,
    command.command_revision,
    command.control_generation,
    command.runner_generation,
    command.item_ref,
    command.turn_seq,
    command.attempt_seq,
    command.prompt_level,
    command.payload.speech_key,
    command.payload.speech_text,
    command.payload.purpose,
  ]);
}

async function withDrainDeadline<T>(
  parentSignal: AbortSignal,
  deps: AutopilotMediaTransportDependencies,
  operation: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  if (parentSignal.aborted) {
    throw parentSignal.reason ?? new DOMException("收麦请求已取消", "AbortError");
  }
  const controller = new AbortController();
  const timeoutMs = deps.requestTimeoutMs ?? DRAIN_REQUEST_TIMEOUT_MS;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let removeParentAbort: () => void = () => {};
  const interrupted = new Promise<never>((_resolve, reject) => {
    const onParentAbort = () => {
      const reason = parentSignal.reason
        ?? new DOMException("收麦请求已取消", "AbortError");
      controller.abort(reason);
      reject(reason);
    };
    parentSignal.addEventListener("abort", onParentAbort, { once: true });
    removeParentAbort = () => parentSignal.removeEventListener("abort", onParentAbort);
    timer = setTimeout(() => {
      const error = new ApiError(408, "收麦状态请求超时，将在安全边界内重试");
      controller.abort(error);
      reject(error);
    }, timeoutMs);
  });
  try {
    return await Promise.race([operation(controller.signal), interrupted]);
  } finally {
    removeParentAbort();
    if (timer !== null) clearTimeout(timer);
  }
}

function exactActiveCredential(
  sessionId: string,
  deps: AutopilotMediaTransportDependencies,
): DeviceCredentialSelection {
  const credential = deps.selectCredential(sessionId);
  if (credential.source !== "active" || credential.record?.sessionId !== sessionId) {
    throw new ApiError(401, "当前场次没有可用的独立老人端设备凭据");
  }
  return credential;
}

// TTS 专用包装：录音/收麦路径继续拿原始 ApiError，语义不变；
// 只有 TTS 失败回执需要携带 credential_rotated 阶段。
function exactTtsActiveCredential(
  sessionId: string,
  deps: AutopilotMediaTransportDependencies,
): DeviceCredentialSelection {
  try {
    return exactActiveCredential(sessionId, deps);
  } catch (error) {
    throw new AutopilotMediaError(
      "audio_playback_failed", "当前场次没有可用的独立老人端设备凭据",
      { cause: error, failureStage: "credential_rotated" });
  }
}

function commandPath(sessionId: string, commandKey: string, action: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/autopilot/commands/`
    + `${encodeURIComponent(commandKey)}/${action}`;
}

async function exactCommandPost(
  sessionId: string,
  commandKey: string,
  action: "tts" | "recording-authorization" | "drain-ack",
  signal: AbortSignal,
  deps: AutopilotMediaTransportDependencies,
  selectedCredential?: DeviceCredentialSelection,
): Promise<Response> {
  const credential = selectedCredential ?? exactActiveCredential(sessionId, deps);
  const response = await deps.fetchImpl(commandPath(sessionId, commandKey, action), {
    method: "POST",
    signal,
    credentials: "omit",
    cache: "no-store",
    // No Content-Type and no body: both command text and recording authority
    // are derived from the exact frozen RuntimeCommand on the server.
    headers: {
      ...credential.headers,
      ...deps.csrf("POST"),
    },
  });
  if (!response.ok) {
    const text = await response.text();
    deps.handleAuthorizationFailure(response.status, text, credential);
    decodeJsonApiResponse({
      status: response.status,
      ok: false,
      statusText: response.statusText,
      text,
    });
  }
  return response;
}

async function revalidateExactTtsAuthority(
  authority: ExactTtsAuthority,
  signal: AbortSignal,
  deps: AutopilotMediaTransportDependencies,
): Promise<void> {
  if (signal.aborted) {
    throw signal.reason ?? new DOMException("语音播放已取消", "AbortError");
  }
  const before = exactTtsActiveCredential(authority.sessionId, deps);
  if (before.record?.capability !== authority.capability) {
    throw new AutopilotMediaError(
      "audio_playback_failed", "TTS 播放前设备凭据已变化，拒绝播放旧话术",
      { failureStage: "credential_rotated" });
  }
  const currentValue = await deps.nextCommand(authority.sessionId);
  if (signal.aborted) {
    throw signal.reason ?? new DOMException("语音播放已取消", "AbortError");
  }
  // Check again after /next: browser storage can rotate while that request is
  // in flight.  A replacement capability must synthesize its own bytes.
  const after = exactTtsActiveCredential(authority.sessionId, deps);
  if (after.record?.capability !== authority.capability) {
    throw new AutopilotMediaError(
      "audio_playback_failed", "TTS 播放前设备凭据已变化，拒绝播放旧话术",
      { failureStage: "credential_rotated" });
  }
  const current = currentValue === null ? null : parseNextCommandProjection(currentValue);
  if (current?.kind !== "tts" || current.state !== "pending"
      || current.command_key !== authority.commandKey
      || ttsCommandIdentity(current) !== authority.commandIdentity) {
    throw new AutopilotMediaError(
      "audio_playback_failed", "TTS 播放前服务器运行时或命令世代已变化，拒绝播放旧话术",
      { failureStage: "authority_changed" });
  }
}

/**
 * Return bytes only after two current-authority proofs: once when synthesis
 * returns and again after the response body has fully materialized.  The caller
 * can therefore create an object URL and call play() immediately without ever
 * receiving bytes whose session/runtime/command/generation/capability is stale.
 */
export async function fetchExactAutopilotTts(
  sessionId: string,
  expectedCommand: TtsCommand,
  signal: AbortSignal,
  deps: AutopilotMediaTransportDependencies,
): Promise<Blob | null> {
  const parsed = parseNextCommandProjection(expectedCommand);
  if (parsed.kind !== "tts" || parsed.state !== "pending") {
    throw new AutopilotMediaError(
      "audio_playback_failed", "仅 pending TTS 命令可请求精确语音",
      { failureStage: "executor_start_failed" });
  }
  const credential = exactTtsActiveCredential(sessionId, deps);
  const authority: ExactTtsAuthority = {
    sessionId,
    commandKey: parsed.command_key,
    commandIdentity: ttsCommandIdentity(parsed),
    capability: credential.record!.capability,
  };
  let response: Response;
  try {
    response = await exactCommandPost(
      sessionId,
      parsed.command_key,
      "tts",
      signal,
      deps,
      credential,
    );
  } catch (error) {
    // 取消不是网络失败；ApiError 语义保留在 cause 里，不在这里吞掉。
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new AutopilotMediaError(
      "audio_playback_failed", "TTS 合成请求失败",
      { cause: error, failureStage: "fetch_failed" });
  }
  if (response.status === 204) return null;

  // Synthesis runs outside the server command lock and may take seconds.
  await revalidateExactTtsAuthority(authority, signal, deps);
  const blob = await response.blob();
  if (blob.size <= 0 || !blob.type.startsWith("audio/")) {
    throw new AutopilotMediaError(
      "audio_playback_failed", "TTS 服务返回的音频无效",
      { failureStage: "blob_invalid" });
  }
  // Blob materialization can itself be asynchronous.  Re-prove exact authority
  // at the last await boundary before URL creation/play; stale bytes are dropped
  // here and never reach either Audio.play() or a browser-speech fallback.
  await revalidateExactTtsAuthority(authority, signal, deps);
  return blob;
}

/**
 * Report physical media idleness only after the local controller has fully
 * stopped. The exact paused command remains the server-side correlation key;
 * no client-selected state or free text is accepted.
 */
export function acknowledgeExactAutopilotDrain(
  sessionId: string,
  commandKey: string,
  signal: AbortSignal,
  deps: AutopilotMediaTransportDependencies,
): Promise<{ replayed: boolean; state_revision: number }> {
  return withDrainDeadline(signal, deps, (requestSignal) =>
    exactCommandPost(sessionId, commandKey, "drain-ack", requestSignal, deps))
    .then(async (response) => {
      const text = await response.text();
      const decoded = decodeJsonApiResponse({
        status: response.status,
        ok: response.ok,
        statusText: response.statusText,
        text,
      });
      if (decoded === null || typeof decoded !== "object" || Array.isArray(decoded)) {
        throw new Error("自动驾驶收麦回执不是对象");
      }
      const row = decoded as Record<string, unknown>;
      const keys = Object.keys(row).sort();
      if (keys.length !== 2 || keys[0] !== "replayed" || keys[1] !== "state_revision"
          || typeof row.replayed !== "boolean"
          || typeof row.state_revision !== "number"
          || !Number.isSafeInteger(row.state_revision)
          || row.state_revision < 0) {
        throw new Error("自动驾驶收麦回执不符合封闭契约");
      }
      return {
        replayed: row.replayed,
        state_revision: row.state_revision,
      };
    });
}

/** Recover one opaque server-derived drain target after a paused-page refresh. */
export function fetchExactAutopilotDrainTarget(
  sessionId: string,
  signal: AbortSignal,
  deps: AutopilotMediaTransportDependencies,
): Promise<{ command_key: string; state_revision: number }> {
  return withDrainDeadline(signal, deps, async (requestSignal) => {
    const credential = exactActiveCredential(sessionId, deps);
    const response = await deps.fetchImpl(
      `/sessions/${encodeURIComponent(sessionId)}/autopilot/drain-target`,
      {
        method: "GET",
        signal: requestSignal,
        credentials: "omit",
        cache: "no-store",
        headers: { ...credential.headers },
      },
    );
    const text = await response.text();
    if (!response.ok) {
      deps.handleAuthorizationFailure(response.status, text, credential);
    }
    const decoded = decodeJsonApiResponse({
      status: response.status,
      ok: response.ok,
      statusText: response.statusText,
      text,
    });
    if (decoded === null || typeof decoded !== "object" || Array.isArray(decoded)) {
      throw new Error("自动驾驶收麦目标不是对象");
    }
    const row = decoded as Record<string, unknown>;
    const keys = Object.keys(row).sort();
    if (keys.length !== 2 || keys[0] !== "command_key" || keys[1] !== "state_revision"
        || typeof row.command_key !== "string" || !COMMAND_KEY.test(row.command_key)
        || typeof row.state_revision !== "number"
        || !Number.isSafeInteger(row.state_revision) || row.state_revision < 0) {
      throw new Error("自动驾驶收麦目标不符合封闭契约");
    }
    return {
      command_key: row.command_key,
      state_revision: row.state_revision,
    };
  });
}

function parseExactRecordingAuthorization(value: unknown): RecordingAuthorization {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("自动驾驶录音授权响应不是对象");
  }
  const row = value as Record<string, unknown>;
  // 键闭集精确：新契约四键。旧后端的三键形状（无 recording_authorized 肯定授权）
  // 一律拒绝——新前端配旧后端绝不静默开麦。
  const keys = Object.keys(row).sort();
  if (keys.length !== 4
      || keys[0] !== "allowed" || keys[1] !== "is_simulation"
      || keys[2] !== "recording_authorized" || keys[3] !== "runtime_status"
      || row.allowed !== true || row.runtime_status !== "active"
      || row.recording_authorized !== true
      || typeof row.is_simulation !== "boolean") {
    throw new Error("自动驾驶录音授权未证明当前命令可开麦");
  }
  return {
    allowed: true,
    runtime_status: "active",
    is_simulation: row.is_simulation,
  };
}

/** Exact pending-record authorization; generic session admission is insufficient. */
export async function authorizeExactAutopilotRecording(
  sessionId: string,
  commandKey: string,
  signal: AbortSignal,
  deps: AutopilotMediaTransportDependencies,
): Promise<RecordingAuthorization> {
  const response = await exactCommandPost(
    sessionId, commandKey, "recording-authorization", signal, deps);
  const text = await response.text();
  const decoded = decodeJsonApiResponse({
    status: response.status,
    ok: response.ok,
    statusText: response.statusText,
    text,
  });
  return parseExactRecordingAuthorization(decoded);
}
