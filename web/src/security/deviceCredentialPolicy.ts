// 设备凭据选哪把:正常只用绑本场次的那把;补传/回执可用该场次留下的 recovery 凭据;
// 「旧场次孤儿录音」还要能拿现在绑着的那把去探一次——服务端只对同一位受试者、
// 同一台平板、没字节事实的槽给 410 作废,其余照旧 409,所以带过去不会写错任何东西。
// 不带任何凭据的请求只会得到 401,那段录音就永远清不掉(2026-09-13 演示)。
export interface DeviceCredentialCandidates {
  active: { sessionId: string; capability: string } | null;
  recovery: { sessionId: string; capability: string } | null;
}

export type DeviceCredentialPick =
  | { source: "active" | "recovery" | "active-foreign"; capability: string; sessionId: string }
  | { source: null };

export function pickDeviceCredential(
  candidates: DeviceCredentialCandidates,
  sessionId: string | undefined,
  options: { allowRecovery?: boolean; activeFallback?: boolean } = {},
): DeviceCredentialPick {
  const { active, recovery } = candidates;
  if (active && (!sessionId || active.sessionId === sessionId)) {
    return { source: "active", capability: active.capability, sessionId: active.sessionId };
  }
  if (options.allowRecovery && sessionId && recovery && recovery.sessionId === sessionId) {
    return { source: "recovery", capability: recovery.capability, sessionId: recovery.sessionId };
  }
  if (options.activeFallback && active && sessionId) {
    return { source: "active-foreign", capability: active.capability, sessionId: active.sessionId };
  }
  return { source: null };
}
