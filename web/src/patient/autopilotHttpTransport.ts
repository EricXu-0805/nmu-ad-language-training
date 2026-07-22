import {
  handleDeviceAuthorizationFailure,
  selectDeviceCredential,
} from "../api.ts";
import { ApiError, apiNetworkError, decodeJsonApiResponse } from "../apiResponse.ts";
import { csrfHeader } from "../security/csrf.ts";
import type { AutopilotAck } from "./autopilotProtocol.ts";
import type { AutopilotTransport } from "./autopilotController.ts";

const REQUEST_TIMEOUT_MS = 12_000;

async function deviceRequest(
  method: "GET" | "POST",
  path: string,
  sessionId: string,
  body?: unknown,
): Promise<unknown> {
  const credential = selectDeviceCredential(sessionId);
  if (credential.source !== "active" || credential.record?.sessionId !== sessionId) {
    throw new ApiError(401, "当前场次没有可用的独立老人端设备凭据");
  }
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(path, {
      method,
      signal: controller.signal,
      credentials: "omit",
      cache: "no-store",
      headers: {
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...credential.headers,
        ...csrfHeader(method),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    if (!response.ok) {
      handleDeviceAuthorizationFailure(response.status, text, credential);
    }
    return decodeJsonApiResponse({
      status: response.status,
      ok: response.ok,
      statusText: response.statusText,
      text,
    });
  } catch (error) {
    if (controller.signal.aborted) {
      throw new ApiError(408, "自动驾驶命令请求超时，已安全暂停");
    }
    throw apiNetworkError(error);
  } finally {
    window.clearTimeout(timeout);
  }
}

/** Device-only transport: it never sends or falls back to an account cookie. */
export const autopilotHttpTransport: AutopilotTransport = {
  next: (sessionId) => deviceRequest(
    "GET",
    `/sessions/${encodeURIComponent(sessionId)}/autopilot/next`,
    sessionId,
  ),
  ack: (sessionId, commandKey, ack: AutopilotAck) => deviceRequest(
    "POST",
    `/sessions/${encodeURIComponent(sessionId)}/autopilot/commands/${encodeURIComponent(commandKey)}/acks`,
    sessionId,
    ack,
  ),
};
