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

const ready = () => ({
  schema_version: "provider-readiness.v1",
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
