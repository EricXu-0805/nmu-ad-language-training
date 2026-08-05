export type ProviderReadinessStatus =
  | "missing"
  | "expired"
  | "config_mismatch"
  | "required_capability_failed"
  | "ready";

export interface ProviderCapabilityReadiness {
  required: boolean;
  configured: boolean;
  success: boolean;
  engineVersion: string;
  failureCode: string | null;
}

export interface ProviderReadiness {
  // Exactly one accepted response schema, never a v1∪v2 union: the server
  // always projects the current version, and a historical v1 probe surfaces as
  // config_mismatch inside a v2 envelope rather than as a v1 response.
  schemaVersion: "provider-readiness.v2";
  runtimeContract: "p0a_sim_first_single_v1";
  status: ProviderReadinessStatus;
  startAllowed: boolean;
  requiredCapabilitiesReady: boolean;
  allConfiguredCapabilitiesReady: boolean;
  matchesCurrentConfig: boolean;
  tts: ProviderCapabilityReadiness;
  asr: ProviderCapabilityReadiness;
  llm: ProviderCapabilityReadiness;
  checkedAt: string | null;
  expiresAt: string | null;
  actorDisplayId: string | null;
  probeFailureCode: string | null;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function exactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const sortedExpected = [...expected].sort();
  return actual.length === sortedExpected.length
    && actual.every((key, index) => key === sortedExpected[index]);
}

function optionalSafeCode(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && /^[a-z][a-z0-9_]{0,95}$/.test(value));
}

function parseCapability(value: unknown): ProviderCapabilityReadiness {
  const candidate = record(value);
  if (!candidate || !exactKeys(candidate, [
    "required", "configured", "success", "engine_version", "failure_code",
  ]) || typeof candidate.required !== "boolean"
      || typeof candidate.configured !== "boolean"
      || typeof candidate.success !== "boolean"
      || typeof candidate.engine_version !== "string" || !candidate.engine_version.trim()
      || !optionalSafeCode(candidate.failure_code)) {
    throw new Error("AI 服务能力检查响应不完整");
  }
  if (candidate.success === (candidate.failure_code !== null)) {
    throw new Error("AI 服务能力检查成功状态与错误码矛盾");
  }
  // An unconfigured engine was never probed, so it cannot have succeeded.
  if (!candidate.configured && candidate.success) {
    throw new Error("AI 服务能力未配置却声称检查通过");
  }
  return {
    required: candidate.required,
    configured: candidate.configured,
    success: candidate.success,
    engineVersion: candidate.engine_version,
    failureCode: candidate.failure_code,
  };
}

function optionalTimestamp(value: unknown): value is string | null {
  return value === null || (typeof value === "string"
    && Number.isFinite(Date.parse(value)));
}

export function parseProviderReadiness(value: unknown): ProviderReadiness {
  const candidate = record(value);
  const statuses: ProviderReadinessStatus[] = [
    "missing", "expired", "config_mismatch", "required_capability_failed", "ready",
  ];
  if (!candidate || !exactKeys(candidate, [
    "schema_version", "runtime_contract", "status", "start_allowed",
    "required_capabilities_ready", "all_configured_capabilities_ready",
    "matches_current_config", "tts", "asr", "llm",
    "checked_at", "expires_at", "actor_display_id", "probe_failure_code",
  ]) || candidate.schema_version !== "provider-readiness.v2"
      || candidate.runtime_contract !== "p0a_sim_first_single_v1"
      || !statuses.includes(candidate.status as ProviderReadinessStatus)
      || typeof candidate.start_allowed !== "boolean"
      || typeof candidate.required_capabilities_ready !== "boolean"
      || typeof candidate.all_configured_capabilities_ready !== "boolean"
      || typeof candidate.matches_current_config !== "boolean"
      || !optionalTimestamp(candidate.checked_at)
      || !optionalTimestamp(candidate.expires_at)
      || !(candidate.actor_display_id === null
        || (typeof candidate.actor_display_id === "string" && candidate.actor_display_id.trim()))
      || !optionalSafeCode(candidate.probe_failure_code)) {
    throw new Error("AI 服务就绪检查响应不符合严格契约");
  }
  const tts = parseCapability(candidate.tts);
  const asr = parseCapability(candidate.asr);
  const llm = parseCapability(candidate.llm);
  const startAllowed = candidate.start_allowed;
  const status = candidate.status as ProviderReadinessStatus;
  const matchesCurrentConfig = candidate.matches_current_config;
  const statusMatchesConfigFact = status === "missing" || status === "config_mismatch"
    ? !matchesCurrentConfig : matchesCurrentConfig;
  const allConfiguredSuccesses = tts.success && asr.success
    && (!llm.configured || llm.success);
  if (startAllowed !== (candidate.status === "ready")
      || (startAllowed && (!candidate.required_capabilities_ready
        || !candidate.matches_current_config || !tts.success || !asr.success))
      || llm.required && startAllowed && !llm.success
      || !statusMatchesConfigFact
      || status === "required_capability_failed"
        && candidate.required_capabilities_ready
      || status === "missing"
        && (candidate.required_capabilities_ready
          || candidate.all_configured_capabilities_ready)
      || candidate.all_configured_capabilities_ready && !allConfiguredSuccesses) {
    throw new Error("AI 服务就绪状态内部矛盾");
  }
  return {
    schemaVersion: "provider-readiness.v2",
    runtimeContract: "p0a_sim_first_single_v1",
    status,
    startAllowed,
    requiredCapabilitiesReady: candidate.required_capabilities_ready,
    allConfiguredCapabilitiesReady: candidate.all_configured_capabilities_ready,
    matchesCurrentConfig,
    tts, asr, llm,
    checkedAt: candidate.checked_at,
    expiresAt: candidate.expires_at,
    actorDisplayId: candidate.actor_display_id as string | null,
    probeFailureCode: candidate.probe_failure_code,
  };
}

const PREWRITE_CODES = new Set([
  "provider_readiness_missing",
  "provider_readiness_expired",
  "provider_readiness_config_mismatch",
  "provider_readiness_required_capability_failed",
  "provider_readiness_unavailable",
]);

export function isProviderReadinessPrewriteConflict(error: unknown): boolean {
  const candidate = record(error);
  if (candidate?.status !== 409) return false;
  const detail = record(candidate.detailData);
  return typeof detail?.code === "string" && PREWRITE_CODES.has(detail.code);
}

export function providerReadinessLabel(readiness: ProviderReadiness | null): string {
  if (!readiness) return "正在核对 AI 服务实测状态";
  if (readiness.status === "ready") {
    return readiness.allConfiguredCapabilitiesReady
      ? "AI 核心与已配置辅助能力均已实测通过"
      : "AI 核心能力已通过；非必需 LLM 未全部通过";
  }
  if (readiness.status === "expired") return "AI 服务实测已过期，需管理员重新检查";
  if (readiness.status === "config_mismatch") return "AI 配置已变更，需管理员重新检查";
  if (readiness.status === "required_capability_failed") return "AI 必需能力实测未通过";
  return "尚无 AI 服务合成检查记录";
}
