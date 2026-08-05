import assert from "node:assert/strict";
import test from "node:test";
import { ApiError } from "../apiResponse.ts";
import {
  isProviderReadinessPrewriteConflict,
  parseProviderReadiness,
  providerReadinessLabel,
} from "./providerReadiness.ts";

const capability = (overrides = {}) => ({
  required: true,
  configured: true,
  success: true,
  engine_version: "fake/model-v1",
  failure_code: null,
  ...overrides,
});

// A frozen literal, deliberately not derived from any production constant, so
// an implementation that rewrites the response runtime_contract to today's
// value cannot make these cases pass.
const LEGACY_RUNTIME_SENTINEL = "legacy_runtime_contract_v0_frozen_sentinel";

const ready = () => ({
  schema_version: "provider-readiness.v2",
  runtime_contract: "p0a_sim_first_single_v1",
  status: "ready",
  start_allowed: true,
  required_capabilities_ready: true,
  all_configured_capabilities_ready: true,
  matches_current_config: true,
  tts: capability(),
  asr: capability(),
  llm: capability({ required: false }),
  checked_at: "2026-07-19T10:00:00",
  expires_at: "2026-07-19T10:30:00",
  actor_display_id: "ADMIN-1",
  probe_failure_code: null,
});

test("strict parser accepts a current probe and preserves optional LLM semantics", () => {
  const parsed = parseProviderReadiness(ready());
  assert.equal(parsed.startAllowed, true);
  assert.equal(parsed.llm.required, false);
  assert.equal(parsed.allConfiguredCapabilitiesReady, true);
  assert.match(providerReadinessLabel(parsed), /均已实测通过/);
});

test("optional LLM failure can permit required path but never claims all configured ready", () => {
  const parsed = parseProviderReadiness({
    ...ready(),
    all_configured_capabilities_ready: false,
    llm: capability({
      required: false,
      success: false,
      failure_code: "llm_result_empty",
    }),
  });
  assert.equal(parsed.startAllowed, true);
  assert.equal(parsed.allConfiguredCapabilitiesReady, false);
  assert.match(providerReadinessLabel(parsed), /非必需 LLM/);
});

test("unknown fields and internally contradictory ready responses fail closed", () => {
  assert.throws(() => parseProviderReadiness({ ...ready(), api_key: "secret" }), /严格契约/);
  assert.throws(() => parseProviderReadiness({
    ...ready(),
    status: "expired",
  }), /内部矛盾/);
  assert.throws(() => parseProviderReadiness({
    ...ready(),
    asr: capability({ success: false, failure_code: null }),
  }), /成功状态与错误码矛盾/);
  assert.throws(() => parseProviderReadiness({
    ...ready(),
    status: "config_mismatch",
    start_allowed: false,
    matches_current_config: true,
  }), /内部矛盾/);
  assert.throws(() => parseProviderReadiness({
    ...ready(),
    start_allowed: false,
    status: "required_capability_failed",
    required_capabilities_ready: false,
    all_configured_capabilities_ready: true,
    llm: capability({
      required: false,
      configured: true,
      success: false,
      failure_code: "llm_result_empty",
    }),
  }), /内部矛盾/);
});

test("every v2 status projection parses with its own invariants", () => {
  const cases: Array<[string, Record<string, unknown>]> = [
    ["missing", {
      ...ready(),
      status: "missing", start_allowed: false,
      required_capabilities_ready: false,
      all_configured_capabilities_ready: false,
      matches_current_config: false,
      tts: capability({ success: false, failure_code: "probe_missing" }),
      asr: capability({ success: false, failure_code: "probe_missing" }),
      llm: capability({
        required: false, success: false, failure_code: "probe_missing",
      }),
      checked_at: null, expires_at: null, actor_display_id: null,
      probe_failure_code: "probe_missing",
    }],
    ["expired", { ...ready(), status: "expired", start_allowed: false }],
    // A mismatched row may keep its historical successes as diagnostics; that
    // is not an internal contradiction, but it must never permit a start.
    ["config_mismatch", {
      ...ready(),
      status: "config_mismatch", start_allowed: false,
      matches_current_config: false,
    }],
    ["required_capability_failed", {
      ...ready(),
      status: "required_capability_failed", start_allowed: false,
      required_capabilities_ready: false,
      all_configured_capabilities_ready: false,
      tts: capability({ success: false, failure_code: "tts_audio_invalid" }),
    }],
    ["ready", ready()],
  ];

  for (const [expected, envelope] of cases) {
    const parsed = parseProviderReadiness(envelope);
    assert.equal(parsed.status, expected);
    assert.equal(parsed.schemaVersion, "provider-readiness.v2");
    assert.equal(parsed.startAllowed, expected === "ready");
  }

  const mismatch = parseProviderReadiness(cases[2][1]);
  assert.equal(mismatch.tts.success, true, "diagnostics survive a mismatch");
  assert.equal(mismatch.startAllowed, false);
});

test("only the current schema and the current runtime contract are accepted", () => {
  for (const envelope of [
    { ...ready(), schema_version: "provider-readiness.v1" },
    { ...ready(), schema_version: "provider-readiness.v3" },
    { ...ready(), schema_version: "provider-readiness" },
    { ...ready(), runtime_contract: LEGACY_RUNTIME_SENTINEL },
    { ...ready(), runtime_contract: "p0a_sim_first_single_v2" },
    // A genuine historical probe reaches the client as config_mismatch, so an
    // implementation that only refuses an old runtime outside that status would
    // pass every ready-shaped case above and still admit the real one.
    {
      ...ready(),
      status: "config_mismatch",
      start_allowed: false,
      matches_current_config: false,
      runtime_contract: LEGACY_RUNTIME_SENTINEL,
    },
  ]) {
    assert.throws(() => parseProviderReadiness(envelope), /严格契约/);
  }
});

test("unconfigured capabilities can neither succeed nor carry a required ready", () => {
  // The rule lives in the shared parseCapability, so every engine that flows
  // through it has to refuse the claim.  Requiredness is spelled out instead of
  // inherited from the helper default, which would make all three rows required
  // and spare an implementation that only guards required capabilities.
  for (const [engine, required] of [
    ["tts", true], ["asr", true], ["llm", false],
  ] as const) {
    assert.throws(() => parseProviderReadiness({
      ...ready(),
      [engine]: capability({ required, configured: false, success: true }),
    }), /未配置却声称检查通过/);
  }

  // A required LLM that was never configured cannot make the scope startable.
  assert.throws(() => parseProviderReadiness({
    ...ready(),
    all_configured_capabilities_ready: false,
    llm: capability({
      required: true, configured: false, success: false,
      failure_code: "llm_not_required_not_configured",
    }),
  }), /内部矛盾/);
});

test("missing fields are refused just as firmly as unknown ones", () => {
  for (const key of [
    "schema_version", "runtime_contract", "status", "start_allowed",
    "matches_current_config", "checked_at", "probe_failure_code", "llm",
  ]) {
    const { [key]: _dropped, ...incomplete } = ready() as Record<string, unknown>;
    assert.throws(() => parseProviderReadiness(incomplete), /严格契约/);
  }
  const { required: _r, ...partialCapability } = capability();
  assert.throws(
    () => parseProviderReadiness({ ...ready(), asr: partialCapability }),
    /不完整/);
});

test("only structured readiness 409s are known pre-write rejections", () => {
  assert.equal(isProviderReadinessPrewriteConflict(new ApiError(
    409,
    "missing",
    { code: "provider_readiness_missing" },
    "nested-detail",
  )), true);
  assert.equal(isProviderReadinessPrewriteConflict(new ApiError(
    409,
    "concurrent",
    { code: "autopilot_concurrency_conflict" },
    "nested-detail",
  )), false);
  assert.equal(isProviderReadinessPrewriteConflict(new ApiError(
    500, "failure", { code: "provider_readiness_missing" }, "nested-detail",
  )), false);
});
