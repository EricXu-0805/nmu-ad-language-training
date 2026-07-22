import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createScaleDrawerRefreshGate, requireScaleRowsForPatient } from "./scaleDrawerRefresh.ts";
import { parseScaleProtocolReadiness, workflowPolicyDigest } from "./scaleProtocol.ts";
import type { ScaleResult } from "../types.ts";

const DIGEST = `sha256:${"a".repeat(64)}`;
const CATEGORY_NULLABLE_FIELDS = [
  "definition_id", "instrument_id", "instrument_name", "instrument_version",
  "definition_digest", "language", "form", "license_source", "license_status",
  "digital_presentation_permitted", "spoken_administration_permitted",
  "automatic_scoring_permitted", "item_response_storage_permitted",
  "result_storage_permitted", "result_export_permitted", "item_set_digest",
  "administration_protocol_digest", "response_schema_digest", "result_schema_digest",
  "missingness_rule_digest", "stopping_rule_digest", "scoring_algorithm_id",
  "scoring_algorithm_version", "scoring_algorithm_digest", "score_min", "score_max",
  "score_direction", "score_rounding_rule", "respondent_role", "assessor_role",
  "assessor_qualification", "pretest_time_window", "posttest_time_window",
  "followup_time_window", "pi_approval", "clinical_approval", "statistics_approval",
  "copyright_approval",
] as const;
const WORKFLOW_FIELDS = [
  "workflow_policy_id", "workflow_policy_version", "workflow_policy_digest",
  "pretest_schedule_rule_digest", "posttest_schedule_rule_digest",
  "followup_schedule_rule_digest", "deferral_authority_rule_digest",
  "reschedule_rule_digest", "closeout_rule_digest", "assessor_assignment_rule_digest",
  "pi_approval", "clinical_approval", "statistics_approval",
] as const;

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const row = value as Record<string, unknown>;
  return `{${Object.keys(row).sort().map((key) =>
    `${JSON.stringify(key)}:${canonicalJson(row[key])}`).join(",")}}`;
}

function digest(value: unknown): string {
  return `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
}

function pythonFloatJson(value: number): string {
  if (Object.is(value, -0)) return "-0.0";
  if (value === 0) return "0.0";
  const exponent = Math.floor(Math.log10(Math.abs(value)));
  if (exponent < -4 || exponent >= 16) {
    const [mantissa, rawExponent] = value.toExponential().split("e");
    const numericExponent = Number(rawExponent);
    return `${mantissa}e${numericExponent >= 0 ? "+" : "-"}${String(
      Math.abs(numericExponent),
    ).padStart(2, "0")}`;
  }
  return Number.isInteger(value) ? `${String(value)}.0` : String(value);
}

function runtimeDefinitionDigest(projection: Record<string, unknown>): string {
  const encoded = `{${Object.keys(projection).sort().map((key) =>
    `${JSON.stringify(key)}:${key === "score_min" || key === "score_max"
      ? pythonFloatJson(projection[key] as number)
      : canonicalJson(projection[key])}`).join(",")}}`;
  return `sha256:${createHash("sha256").update(encoded).digest("hex")}`;
}

function scaleRow(patientId: string, id: number): ScaleResult {
  return { id, patient_id: patientId, phase_type: "前测", scale_name: "legacy-container" };
}

function emptyCategory(categoryKey: string, label: string): Record<string, unknown> {
  const category: Record<string, unknown> = {
    category_key: categoryKey,
    label,
    required: true,
    scoring_ready: false,
  };
  for (const field of CATEGORY_NULLABLE_FIELDS) category[field] = null;
  return category;
}

function emptyWorkflowPolicy(): Record<string, unknown> {
  return Object.fromEntries(WORKFLOW_FIELDS.map((field) => [field, null]));
}

function categoryBlockingFields(category: Record<string, unknown>): string[] {
  const fields = [
    "definition_id", "instrument_id", "instrument_name", "instrument_version", "language",
    "form", "license_source", "scoring_algorithm_id", "scoring_algorithm_version",
    "score_rounding_rule", "respondent_role", "assessor_role", "assessor_qualification",
    "pretest_time_window", "posttest_time_window", "followup_time_window",
    "definition_digest", "item_set_digest", "administration_protocol_digest",
    "response_schema_digest", "result_schema_digest", "missingness_rule_digest",
    "stopping_rule_digest", "scoring_algorithm_digest",
  ].filter((field) => category[field] === null);
  if (category.license_status !== "authorized") fields.push("license_status");
  for (const field of [
    "digital_presentation_permitted", "spoken_administration_permitted",
    "automatic_scoring_permitted", "item_response_storage_permitted",
    "result_storage_permitted", "result_export_permitted",
  ]) if (category[field] !== true) fields.push(field);
  if (category.score_min === null) fields.push("score_min");
  if (category.score_max === null) fields.push("score_max");
  if (typeof category.score_min === "number" && typeof category.score_max === "number"
      && category.score_min >= category.score_max) fields.push("score_range");
  if (category.score_direction === null) fields.push("score_direction");
  for (const field of [
    "pi_approval", "clinical_approval", "statistics_approval", "copyright_approval",
  ]) {
    const fact = category[field] as Record<string, unknown> | null;
    if (fact === null || fact.scope_digest !== category.definition_digest) fields.push(field);
  }
  return fields;
}

function workflowBlockingFields(policy: Record<string, unknown>): string[] {
  const fields = [
    "workflow_policy_id", "workflow_policy_version", "workflow_policy_digest",
    "pretest_schedule_rule_digest", "posttest_schedule_rule_digest",
    "followup_schedule_rule_digest", "deferral_authority_rule_digest",
    "reschedule_rule_digest", "closeout_rule_digest", "assessor_assignment_rule_digest",
  ].filter((field) => policy[field] === null);
  const prerequisites = [
    "workflow_policy_id", "workflow_policy_version", "pretest_schedule_rule_digest",
    "posttest_schedule_rule_digest", "followup_schedule_rule_digest",
    "deferral_authority_rule_digest", "reschedule_rule_digest", "closeout_rule_digest",
    "assessor_assignment_rule_digest",
  ];
  if (prerequisites.every((field) => policy[field] !== null)
      && policy.workflow_policy_digest !== workflowPolicyDigest(policy as never)
      && !fields.includes("workflow_policy_digest")) fields.push("workflow_policy_digest");
  for (const field of ["pi_approval", "clinical_approval", "statistics_approval"]) {
    const fact = policy[field] as Record<string, unknown> | null;
    if (fact === null || fact.scope_digest !== policy.workflow_policy_digest) fields.push(field);
  }
  return fields;
}

interface ProtocolOptions {
  categories?: Record<string, unknown>[];
  workflowPolicy?: Record<string, unknown>;
  definitionBundleId?: string | null;
  definitionBundleDigest?: string | null;
  definitionArtifactEnforcementReady?: boolean;
  definitionArtifactsReady?: boolean;
  formalResultContractReady?: boolean;
  workflowContractReady?: boolean;
  workflowPolicyEnforcementReady?: boolean;
}

function protocol(options: ProtocolOptions = {}): Record<string, unknown> {
  const categories = options.categories ?? [
    emptyCategory("untrained_standardized_naming", "未训练词标准化命名测验"),
    emptyCategory("functional_communication", "功能沟通量表"),
  ];
  const workflowPolicy = options.workflowPolicy ?? emptyWorkflowPolicy();
  const definitionBundleId = options.definitionBundleId ?? null;
  const definitionBundleDigest = options.definitionBundleDigest ?? null;
  const categoryBlockers = categories.flatMap((category) => {
    const blocked = categoryBlockingFields(category);
    category.scoring_ready = blocked.length === 0;
    return blocked.map((field) => ({
      code: `${category.category_key}.${field}.not_ready`,
      category_key: category.category_key,
      field,
      message: `${category.label}：${field} 尚未冻结。`,
    }));
  });
  const blockers = [...categoryBlockers];
  if (definitionBundleId === null) blockers.push({
    code: "platform.definition_bundle_id.not_ready", category_key: "platform",
    field: "definition_bundle_id", message: "定义包标识未冻结。",
  });
  if (definitionBundleDigest === null) blockers.push({
    code: "platform.definition_bundle_digest.not_ready", category_key: "platform",
    field: "definition_bundle_digest", message: "定义包摘要未冻结。",
  });
  const definitionReady = definitionBundleId !== null && definitionBundleDigest !== null
    && categories.every((category) => category.scoring_ready === true);
  const workflowFields = workflowBlockingFields(workflowPolicy);
  for (const field of workflowFields) blockers.push({
    code: `workflow_policy.${field}.not_ready`, category_key: "workflow_policy", field,
    message: `工作流 ${field} 尚未冻结。`,
  });
  const workflowPolicyReady = workflowFields.length === 0;
  const definitionArtifactEnforcementReady =
    options.definitionArtifactEnforcementReady ?? false;
  const definitionArtifactsReady = options.definitionArtifactsReady ?? false;
  const formalResultContractReady = options.formalResultContractReady ?? false;
  const workflowContractReady = options.workflowContractReady ?? false;
  const workflowPolicyEnforcementReady = options.workflowPolicyEnforcementReady ?? false;
  if (!definitionArtifactEnforcementReady) blockers.push({
    code: "platform.definition_artifact_enforcement.not_ready", category_key: "platform",
    field: "definition_artifact_enforcement", message: "定义制品可信执行未就绪。",
  });
  if (!workflowPolicyEnforcementReady) blockers.push({
    code: "platform.workflow_policy_enforcement.not_ready", category_key: "platform",
    field: "workflow_policy_enforcement", message: "工作流政策可信执行未就绪。",
  });
  if (!definitionArtifactsReady) blockers.push({
    code: "platform.definition_artifacts.not_ready", category_key: "platform",
    field: "definition_artifacts", message: "运行时定义制品未就绪。",
  });
  if (!formalResultContractReady) blockers.push({
    code: "platform.formal_result_contract.not_ready", category_key: "platform",
    field: "formal_result_contract", message: "正式结果合同未就绪。",
  });
  if (!workflowContractReady) blockers.push({
    code: "platform.workflow_contract.not_ready", category_key: "platform",
    field: "workflow_contract", message: "工作流合同未就绪。",
  });
  const workflowReady = workflowPolicyReady && workflowContractReady
    && workflowPolicyEnforcementReady;
  const ready = definitionReady && definitionArtifactEnforcementReady
    && definitionArtifactsReady
    && formalResultContractReady && workflowReady;
  return {
    schema_version: "scale-protocol-readiness.v4",
    status: !definitionReady
      ? "awaiting_pi_definition"
      : !workflowPolicyReady
        ? "awaiting_workflow_policy"
        : !formalResultContractReady || !workflowContractReady
            || !definitionArtifactEnforcementReady || !workflowPolicyEnforcementReady
          ? "awaiting_platform_implementation"
          : !definitionArtifactsReady
            ? "awaiting_definition_artifacts"
            : "ready_for_research",
    definition_bundle_id: definitionBundleId,
    definition_bundle_digest: definitionBundleDigest,
    definition_ready: definitionReady,
    definition_artifact_enforcement_ready: definitionArtifactEnforcementReady,
    definition_artifacts_ready: definitionArtifactsReady,
    formal_result_contract_ready: formalResultContractReady,
    workflow_policy_ready: workflowPolicyReady,
    workflow_contract_ready: workflowContractReady,
    workflow_policy_enforcement_ready: workflowPolicyEnforcementReady,
    workflow_ready: workflowReady,
    ready_for_research: ready,
    instance_creation_enabled: ready,
    automatic_scoring_enabled: ready,
    training_metrics_are_formal_scale_results: false,
    categories,
    workflow_policy: workflowPolicy,
    blocking_issues: blockers,
  };
}

function completeCategory(category: Record<string, unknown>): void {
  for (const field of [
    "definition_id", "instrument_id", "instrument_name", "instrument_version", "language",
    "form", "license_source", "scoring_algorithm_id", "scoring_algorithm_version",
    "score_rounding_rule", "respondent_role", "assessor_role", "assessor_qualification",
    "pretest_time_window", "posttest_time_window", "followup_time_window",
  ]) category[field] = `frozen-${field}`;
  for (const field of [
    "item_set_digest", "administration_protocol_digest", "response_schema_digest",
    "result_schema_digest", "missingness_rule_digest", "stopping_rule_digest",
    "scoring_algorithm_digest",
  ]) category[field] = DIGEST;
  category.license_status = "authorized";
  for (const field of [
    "digital_presentation_permitted", "spoken_administration_permitted",
    "automatic_scoring_permitted", "item_response_storage_permitted",
    "result_storage_permitted", "result_export_permitted",
  ]) category[field] = true;
  category.score_min = 0;
  category.score_max = 100;
  category.score_direction = "higher_is_better";
  category.definition_digest = runtimeDefinitionDigest({
    schema: "assessment-definition.v1",
    definition_id: category.definition_id,
    category_key: category.category_key,
    instrument_id: category.instrument_id,
    instrument_version: category.instrument_version,
    item_set_digest: category.item_set_digest,
    administration_protocol_digest: category.administration_protocol_digest,
    response_schema_digest: category.response_schema_digest,
    result_schema_digest: category.result_schema_digest,
    missingness_rule_digest: category.missingness_rule_digest,
    stopping_rule_digest: category.stopping_rule_digest,
    scoring_algorithm_id: category.scoring_algorithm_id,
    scoring_algorithm_version: category.scoring_algorithm_version,
    scoring_algorithm_digest: category.scoring_algorithm_digest,
    score_min: category.score_min,
    score_max: category.score_max,
    score_direction: category.score_direction,
    score_rounding_rule: category.score_rounding_rule,
    automatic_scoring_permitted: category.automatic_scoring_permitted,
    item_response_storage_permitted: category.item_response_storage_permitted,
    result_storage_permitted: category.result_storage_permitted,
    result_export_permitted: category.result_export_permitted,
  });
  for (const field of [
    "pi_approval", "clinical_approval", "statistics_approval", "copyright_approval",
  ]) category[field] = {
    approved_by: `named-${field}`,
    approved_at: "2026-07-19T12:00:00+08:00",
    scope_digest: category.definition_digest,
  };
}

function completeDefinitions(): {
  categories: Record<string, unknown>[];
  bundleId: string;
  bundleDigest: string;
} {
  const categories = [
    emptyCategory("untrained_standardized_naming", "未训练词标准化命名测验"),
    emptyCategory("functional_communication", "功能沟通量表"),
  ];
  categories.forEach(completeCategory);
  const bundleId = "frozen-two-outcome-bundle-v1";
  const bundleDigest = digest({
    schema: "assessment-definition-bundle.v1",
    bundle_id: bundleId,
    definitions: [...categories]
      .sort((left, right) => String(left.category_key).localeCompare(String(right.category_key)))
      .map((category) => ({
        category_key: category.category_key,
        definition_id: category.definition_id,
        definition_digest: category.definition_digest,
      })),
    formal_research_approved: true,
  });
  return { categories, bundleId, bundleDigest };
}

function completeWorkflowPolicy(): Record<string, unknown> {
  const policy = emptyWorkflowPolicy();
  policy.workflow_policy_id = "frozen-formal-assessment-workflow";
  policy.workflow_policy_version = "v1";
  for (const field of [
    "pretest_schedule_rule_digest", "posttest_schedule_rule_digest",
    "followup_schedule_rule_digest", "deferral_authority_rule_digest",
    "reschedule_rule_digest", "closeout_rule_digest", "assessor_assignment_rule_digest",
  ]) policy[field] = DIGEST;
  policy.workflow_policy_digest = workflowPolicyDigest(policy as never);
  for (const field of ["pi_approval", "clinical_approval", "statistics_approval"]) {
    policy[field] = {
      approved_by: `named-workflow-${field}`,
      approved_at: "2026-07-19T12:00:00+08:00",
      scope_digest: policy.workflow_policy_digest,
    };
  }
  return policy;
}

test("量表抽屉只接纳同一受试者的最新请求代次", () => {
  const gate = createScaleDrawerRefreshGate();
  const slowFirst = gate.begin("P-001");
  const newerRetry = gate.begin("P-001");
  assert.equal(gate.accepts(slowFirst, "P-001"), false);
  assert.equal(gate.accepts(newerRetry, "P-001"), true);
  assert.equal(gate.accepts(newerRetry, "P-002"), false);
  gate.cancel();
  assert.equal(gate.accepts(newerRetry, "P-001"), false);
});

test("量表抽屉整批拒绝跨受试者返回", () => {
  assert.deepEqual(requireScaleRowsForPatient([scaleRow("P-001", 1)], "P-001"), [
    scaleRow("P-001", 1),
  ]);
  assert.throws(() => requireScaleRowsForPatient([
    scaleRow("P-001", 1), scaleRow("P-002", 2),
  ], "P-001"), /不属于当前受试者/);
});

test("v4 parser accepts the exact empty fail-closed manifest", () => {
  const parsed = parseScaleProtocolReadiness(protocol());
  assert.equal(parsed.schema_version, "scale-protocol-readiness.v4");
  assert.equal(parsed.status, "awaiting_pi_definition");
  assert.equal(parsed.definition_ready, false);
  assert.equal(parsed.definition_artifact_enforcement_ready, false);
  assert.equal(parsed.definition_artifacts_ready, false);
  assert.equal(parsed.workflow_policy_ready, false);
  assert.equal(parsed.workflow_contract_ready, false);
  assert.equal(parsed.workflow_policy_enforcement_ready, false);
  assert.equal(parsed.instance_creation_enabled, false);
  assert.equal(parsed.categories[0].definition_id, null);
  assert.equal(parsed.workflow_policy.closeout_rule_digest, null);
  assert.ok(parsed.blocking_issues.some((issue) => issue.field === "definition_artifacts"));
  assert.ok(parsed.blocking_issues.some(
    (issue) => issue.code === "platform.definition_artifact_enforcement.not_ready"
      && issue.field === "definition_artifact_enforcement",
  ));
  assert.ok(parsed.blocking_issues.some(
    (issue) => issue.code === "platform.workflow_policy_enforcement.not_ready"
      && issue.field === "workflow_policy_enforcement",
  ));
});

test("v3, missing fields, and unknown top-level fields are rejected", () => {
  assert.throws(() => parseScaleProtocolReadiness({
    ...protocol(), schema_version: "scale-protocol-readiness.v3",
  }), /版本/);
  for (const field of [
    "definition_artifact_enforcement_ready", "workflow_policy_enforcement_ready",
  ]) {
    const missing = protocol();
    delete missing[field];
    assert.throws(
      () => parseScaleProtocolReadiness(missing), /字段不完整/,
      `missing ${field} must fail closed`,
    );
  }
  assert.throws(() => parseScaleProtocolReadiness({ ...protocol(), legacy_ready: true }), /未知字段/);
  assert.throws(() => parseScaleProtocolReadiness({
    ...protocol(), definition_artifact_enforcement_ready: "true",
  }), /布尔值/);
  assert.throws(() => parseScaleProtocolReadiness({
    ...protocol(), workflow_policy_enforcement_ready: "true",
  }), /布尔值/);
});

test("new definition/result/storage facts are mandatory and approvals bind definition digest", () => {
  const partial = protocol();
  const first = (partial.categories as Record<string, unknown>[])[0];
  Object.assign(first, {
    definition_id: "definition-v1",
    instrument_id: "instrument-v1",
    instrument_name: "instrument",
    instrument_version: "v1",
    definition_digest: DIGEST,
  });
  first.scoring_ready = true;
  assert.throws(() => parseScaleProtocolReadiness(partial), /计分状态矛盾/);

  const complete = completeDefinitions();
  const completeSource = protocol({
    categories: complete.categories,
    definitionBundleId: complete.bundleId,
    definitionBundleDigest: complete.bundleDigest,
  });
  assert.equal(parseScaleProtocolReadiness(completeSource).categories[0].scoring_ready, true);
  (complete.categories[0].clinical_approval as Record<string, unknown>).scope_digest = DIGEST;
  const rebuilt = protocol({
    categories: complete.categories,
    definitionBundleId: complete.bundleId,
    definitionBundleDigest: complete.bundleDigest,
  });
  assert.equal(parseScaleProtocolReadiness(rebuilt).categories[0].scoring_ready, false);
});

test("workflow digest is canonical and every policy approval binds its exact scope", () => {
  const policy = completeWorkflowPolicy();
  const expected = digest({
    schema: "formal-assessment-workflow-policy.v1",
    workflow_policy_id: policy.workflow_policy_id,
    workflow_policy_version: policy.workflow_policy_version,
    pretest_schedule_rule_digest: policy.pretest_schedule_rule_digest,
    posttest_schedule_rule_digest: policy.posttest_schedule_rule_digest,
    followup_schedule_rule_digest: policy.followup_schedule_rule_digest,
    deferral_authority_rule_digest: policy.deferral_authority_rule_digest,
    reschedule_rule_digest: policy.reschedule_rule_digest,
    closeout_rule_digest: policy.closeout_rule_digest,
    assessor_assignment_rule_digest: policy.assessor_assignment_rule_digest,
  });
  assert.equal(workflowPolicyDigest(policy as never), expected);

  policy.workflow_policy_digest = DIGEST;
  for (const field of ["pi_approval", "clinical_approval", "statistics_approval"]) {
    (policy[field] as Record<string, unknown>).scope_digest = DIGEST;
  }
  const source = protocol({ workflowPolicy: policy });
  const parsed = parseScaleProtocolReadiness(source);
  assert.equal(parsed.workflow_policy_ready, false);
  assert.ok(parsed.blocking_issues.some((issue) => issue.field === "workflow_policy_digest"));
});

test("runtime definition hashing matches the Python float canonicalization", () => {
  const category = emptyCategory(
    "untrained_standardized_naming", "未训练词标准化命名测验",
  );
  completeCategory(category);
  assert.equal(
    category.definition_digest,
    "sha256:26f7a392109800c9506fb535de0defe77a7aa39e7f9322be558d6ed1adc5a78c",
  );
});

test("complete definitions stop at workflow policy instead of claiming platform readiness", () => {
  const complete = completeDefinitions();
  const parsed = parseScaleProtocolReadiness(protocol({
    categories: complete.categories,
    definitionBundleId: complete.bundleId,
    definitionBundleDigest: complete.bundleDigest,
  }));
  assert.equal(parsed.definition_ready, true);
  assert.equal(parsed.status, "awaiting_workflow_policy");
  assert.equal(parsed.ready_for_research, false);
});

test("frozen definitions and policy remain blocked until both trusted enforcement layers exist", () => {
  const complete = completeDefinitions();
  const parsed = parseScaleProtocolReadiness(protocol({
    categories: complete.categories,
    workflowPolicy: completeWorkflowPolicy(),
    definitionBundleId: complete.bundleId,
    definitionBundleDigest: complete.bundleDigest,
    formalResultContractReady: true,
    workflowContractReady: true,
  }));
  assert.equal(parsed.status, "awaiting_platform_implementation");
  assert.equal(parsed.definition_artifact_enforcement_ready, false);
  assert.equal(parsed.definition_artifacts_ready, false);
  assert.equal(parsed.workflow_policy_enforcement_ready, false);
  assert.equal(parsed.workflow_ready, false);
  assert.equal(parsed.ready_for_research, false);
  assert.ok(parsed.blocking_issues.some(
    (issue) => issue.code === "platform.definition_artifact_enforcement.not_ready"
      && issue.field === "definition_artifact_enforcement",
  ));
  assert.ok(parsed.blocking_issues.some(
    (issue) => issue.code === "platform.workflow_policy_enforcement.not_ready"
      && issue.field === "workflow_policy_enforcement",
  ));
});

test("complete policy still waits for the exact installed definition artifacts", () => {
  const complete = completeDefinitions();
  const parsed = parseScaleProtocolReadiness(protocol({
    categories: complete.categories,
    workflowPolicy: completeWorkflowPolicy(),
    definitionBundleId: complete.bundleId,
    definitionBundleDigest: complete.bundleDigest,
    definitionArtifactEnforcementReady: true,
    formalResultContractReady: true,
    workflowContractReady: true,
    workflowPolicyEnforcementReady: true,
  }));
  assert.equal(parsed.status, "awaiting_definition_artifacts");
  assert.equal(parsed.workflow_ready, true);
  assert.equal(parsed.instance_creation_enabled, false);
});

test("trusted platform flags can complete v4 only with exact artifacts, policy, and digests", () => {
  const complete = completeDefinitions();
  const parsed = parseScaleProtocolReadiness(protocol({
    categories: complete.categories,
    workflowPolicy: completeWorkflowPolicy(),
    definitionBundleId: complete.bundleId,
    definitionBundleDigest: complete.bundleDigest,
    definitionArtifactEnforcementReady: true,
    definitionArtifactsReady: true,
    formalResultContractReady: true,
    workflowContractReady: true,
    workflowPolicyEnforcementReady: true,
  }));
  assert.equal(parsed.status, "ready_for_research");
  assert.equal(parsed.ready_for_research, true);
  assert.equal(parsed.instance_creation_enabled, true);
  assert.equal(parsed.automatic_scoring_enabled, true);
  assert.equal(parsed.blocking_issues.length, 0);
});

test("artifact-ready claims with mutated definition or bundle digest are rejected", () => {
  const complete = completeDefinitions();
  complete.categories[0].definition_digest = DIGEST;
  for (const field of [
    "pi_approval", "clinical_approval", "statistics_approval", "copyright_approval",
  ]) (complete.categories[0][field] as Record<string, unknown>).scope_digest = DIGEST;
  const source = protocol({
    categories: complete.categories,
    workflowPolicy: completeWorkflowPolicy(),
    definitionBundleId: complete.bundleId,
    definitionBundleDigest: complete.bundleDigest,
    definitionArtifactEnforcementReady: true,
    definitionArtifactsReady: true,
    formalResultContractReady: true,
    workflowContractReady: true,
    workflowPolicyEnforcementReady: true,
  });
  assert.throws(() => parseScaleProtocolReadiness(source), /定义摘要/);
});

test("workflow/overall/create/automatic flags cannot be promoted independently", () => {
  assert.throws(() => parseScaleProtocolReadiness({
    ...protocol(), workflow_ready: true,
  }), /政策、平台合同与可信执行/);
  assert.throws(() => parseScaleProtocolReadiness({
    ...protocol(), instance_creation_enabled: true,
  }), /总体就绪/);
  assert.throws(() => parseScaleProtocolReadiness({
    ...protocol(), automatic_scoring_enabled: true,
  }), /总体就绪/);
  assert.throws(() => parseScaleProtocolReadiness({
    ...protocol(), training_metrics_are_formal_scale_results: true,
  }), /训练指标/);
});

test("blockers and nested exact shapes cannot omit or add facts", () => {
  const source = protocol();
  const blockers = source.blocking_issues as Record<string, unknown>[];
  assert.throws(() => parseScaleProtocolReadiness({
    ...source, blocking_issues: blockers.slice(1),
  }), /阻断项/);
  const extraCategory = protocol();
  (extraCategory.categories as Record<string, unknown>[])[0].invented = true;
  assert.throws(() => parseScaleProtocolReadiness(extraCategory), /未知字段/);
  const extraPolicy = protocol();
  (extraPolicy.workflow_policy as Record<string, unknown>).invented = true;
  assert.throws(() => parseScaleProtocolReadiness(extraPolicy), /未知字段/);
});

test("malformed digests and approval timestamps are rejected", () => {
  const invalidDigest = protocol();
  (invalidDigest.categories as Record<string, unknown>[])[0].definition_digest = "bad";
  assert.throws(() => parseScaleProtocolReadiness(invalidDigest), /sha256/);

  const invalidApproval = protocol();
  const first = (invalidApproval.categories as Record<string, unknown>[])[0];
  first.pi_approval = { approved_by: "pi", approved_at: "2026-07-19", scope_digest: DIGEST };
  assert.throws(() => parseScaleProtocolReadiness(invalidApproval), /带时区/);
});

test("ScaleDrawer explains six independent v4 readiness layers without overstating enforcement", () => {
  const source = readFileSync(new URL("./ScaleDrawer.tsx", import.meta.url), "utf8");
  assert.equal(source.includes("已冻结可用"), false);
  assert.match(source, /定义\/逐题证据可信执行/);
  assert.match(source, /运行时定义制品/);
  assert.match(source, /工作流政策/);
  assert.match(source, /工作流政策可信执行/);
  assert.match(source, /平台结果与工作流合同/);
  assert.match(source, /等待平台可信执行能力/);
  assert.match(source, /instance_creation_enabled/);
});
