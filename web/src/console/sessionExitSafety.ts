import type { SessionRuntimeState } from "../types";

const KNOWN_RUNTIME_STATUSES = new Set([
  "active", "paused", "intervention_completed", "completed", "aborted", "failed",
]);

export interface SessionExitApi {
  getSessionRuntime(sessionId: string): Promise<SessionRuntimeState>;
  pauseSession(sessionId: string): Promise<SessionRuntimeState>;
}

function requireBoundRuntime(
  runtime: SessionRuntimeState,
  sessionId: string,
): SessionRuntimeState {
  if (!runtime || runtime.sessionId !== sessionId
      || !KNOWN_RUNTIME_STATUSES.has(runtime.status)
      || !Number.isSafeInteger(runtime.revision) || runtime.revision < 0) {
    throw new Error("服务器返回的运行态未绑定当前场次，已阻止离开");
  }
  return runtime;
}

function provesTransitionAwayFromActive(
  candidate: SessionRuntimeState,
  activeRevision: number,
): boolean {
  return candidate.status !== "active" && candidate.revision > activeRevision;
}

/**
 * 任何会卸载床旁驾驶员的离开动作，都必须先证明服务器不再是 active。
 * pause 响应丢失时再读一次服务器真值；只有已暂停或终态才能离开。
 */
export async function makeSessionSafeToExit(
  runtimeApi: SessionExitApi,
  sessionId: string,
): Promise<SessionRuntimeState> {
  const current = requireBoundRuntime(await runtimeApi.getSessionRuntime(sessionId), sessionId);
  if (current.status !== "active") return current;

  try {
    const paused = requireBoundRuntime(await runtimeApi.pauseSession(sessionId), sessionId);
    if (!provesTransitionAwayFromActive(paused, current.revision)) {
      throw new Error("服务器未返回高于当前修订号的安全暂停或终态");
    }
    return paused;
  } catch (pauseError) {
    // 请求可能已经提交但响应超时；必须以第二次服务器读取对账，不能误留
    // 一个 active 场次，也不能因响应丢失而要求研究者重复操作。
    const reconciled = requireBoundRuntime(await runtimeApi.getSessionRuntime(sessionId), sessionId);
    if (provesTransitionAwayFromActive(reconciled, current.revision)) return reconciled;
    throw pauseError;
  }
}
