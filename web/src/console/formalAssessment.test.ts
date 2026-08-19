import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  assessmentEventAllowsCloseout,
  assessmentEventAllowsSwitch,
  assessmentWorkflowAllowsPatientSwitch,
  assertFormalAssessmentMutationReady,
  parseAssessmentEvent,
  parseAssessmentEventList,
  parseAssessmentEventsToday,
  type FormalAssessmentMutation,
  type FormalAssessmentMutationReadiness,
} from "./formalAssessment.ts";

const DIGEST = `sha256:${"a".repeat(64)}`;
const RESULT_DIGEST = `sha256:${"b".repeat(64)}`;
const CREATED = "2026-07-19T08:00:00+08:00";

function instance(
  category: "untrained_standardized_naming" | "functional_communication",
  suffix: string,
): Record<string, unknown> {
  return {
    instance_id: `instance-${suffix}-0001`,
    event_id: "assessment-event-0001",
    patient_id: "P-001",
    category_key: category,
    definition_bundle_id: "definition-bundle-0001",
    definition_bundle_digest: DIGEST,
    definition_id: `definition-${suffix}-0001`,
    instrument_id: `instrument-${suffix}-0001`,
    instrument_version: "v1",
    definition_digest: DIGEST,
    item_set_digest: DIGEST,
    administration_protocol_digest: DIGEST,
    response_schema_digest: DIGEST,
    result_schema_digest: DIGEST,
    missingness_rule_digest: DIGEST,
    stopping_rule_digest: DIGEST,
    scoring_algorithm_id: "server-score-v1",
    scoring_algorithm_version: "v1",
    scoring_algorithm_digest: DIGEST,
    score_min: 0,
    score_max: 4,
    score_direction: "higher_is_better",
    score_rounding_rule: "integer_exact",
    automatic_scoring_permitted: true,
    item_response_storage_permitted: true,
    result_storage_permitted: true,
    result_export_permitted: false,
    status: "due",
    revision: 1,
    item_response_count: 0,
    required_item_count: 2,
    data_classification: "simulation",
    formal_outcome_eligible: false,
    scoring_evidence: null,
    deferral: null,
    created_at: CREATED,
    completed_at: null,
    updated_at: CREATED,
  };
}

function event(): Record<string, unknown> {
  return {
    schema_version: "formal-assessment.v1",
    event_id: "assessment-event-0001",
    patient_id: "P-001",
    assigned_assessor_id: "researcher-0001",
    timepoint: "pretest",
    scheduled_date: "2026-07-19",
    status: "due",
    revision: 1,
    is_simulation: true,
    data_classification: "simulation",
    formal_outcome_eligible: false,
    definition_bundle_id: "definition-bundle-0001",
    definition_bundle_digest: DIGEST,
    instances: [
      instance("untrained_standardized_naming", "naming"),
      instance("functional_communication", "functional"),
    ],
    closeout: null,
    cancellation: null,
    created_at: CREATED,
    updated_at: CREATED,
  };
}

function completedInstance(row: Record<string, unknown>): Record<string, unknown> {
  const completedAt = "2026-07-19T08:10:00+08:00";
  return {
    ...row,
    status: "completed",
    revision: 4,
    item_response_count: 2,
    completed_at: completedAt,
    updated_at: completedAt,
    scoring_evidence: {
      evidence_id: `evidence-${String(row.instance_id)}`,
      instance_id: row.instance_id,
      event_id: row.event_id,
      patient_id: row.patient_id,
      category_key: row.category_key,
      definition_digest: row.definition_digest,
      item_response_set_digest: DIGEST,
      scoring_algorithm_id: row.scoring_algorithm_id,
      scoring_algorithm_version: row.scoring_algorithm_version,
      scoring_algorithm_digest: row.scoring_algorithm_digest,
      score: 2,
      result: { total_score: 2 },
      result_digest: RESULT_DIGEST,
      answered_item_count: 2,
      missing_item_count: 0,
      stopped_early: false,
      stopping_reason_code: null,
      formal_outcome_eligible: false,
      scored_at: completedAt,
    },
  };
}

function deferredInstance(row: Record<string, unknown>): Record<string, unknown> {
  const approvedAt = "2026-07-19T08:12:00+08:00";
  return {
    ...row,
    status: "approved_deferred",
    revision: 2,
    updated_at: approvedAt,
    deferral: {
      deferral_id: `deferral-${String(row.instance_id)}`,
      instance_id: row.instance_id,
      event_id: row.event_id,
      patient_id: row.patient_id,
      category_key: row.category_key,
      definition_digest: row.definition_digest,
      reason_code: "participant_unavailable",
      deferred_until: "2026-07-26",
      approved_by: "admin-0001",
      approved_role: "admin",
      approved_at: approvedAt,
    },
  };
}

function closedEvent(): Record<string, unknown> {
  const source = event();
  const rows = source.instances as Record<string, unknown>[];
  return {
    ...source,
    status: "closed",
    revision: 9,
    instances: [completedInstance(rows[0]), deferredInstance(rows[1])],
    closeout: {
      closeout_id: "closeout-event-0001",
      event_id: source.event_id,
      patient_id: source.patient_id,
      event_revision: 9,
      report_status: "no_additional_observation",
      fatigue_observed: false,
      distress_or_discomfort_observed: false,
      participant_declined_to_continue: false,
      staff_assistance_occurred: false,
      environment_interruption_occurred: false,
      device_or_network_interruption_occurred: false,
      note: null,
      closed_by: "researcher-0001",
      closed_at: "2026-07-19T08:15:00+08:00",
      switch_allowed: true,
    },
    updated_at: "2026-07-19T08:15:00+08:00",
  };
}

test("strict event projection accepts the two exact due instances", () => {
  const parsed = parseAssessmentEvent(event(), { patientId: "P-001" });
  assert.equal(parsed.instances.length, 2);
  assert.equal(parsed.status, "due");
  assert.equal(assessmentEventAllowsCloseout(parsed), false);
  assert.equal(assessmentEventAllowsSwitch(parsed), false);
});

test("closed event requires one evidence-or-deferral terminal proof per category", () => {
  const parsed = parseAssessmentEvent(closedEvent(), {
    patientId: "P-001",
    eventId: "assessment-event-0001",
    definitionDigests: {
      untrained_standardized_naming: DIGEST,
      functional_communication: DIGEST,
    },
  });
  assert.equal(parsed.instances[0].scoring_evidence?.score, 2);
  assert.equal(parsed.instances[1].deferral?.approved_role, "admin");
  assert.equal(assessmentEventAllowsSwitch(parsed), true);
});

test("explicit missing markers do not have to equal the answered response count", () => {
  const source = closedEvent();
  const completed = (source.instances as Record<string, unknown>[])[0];
  const evidence = completed.scoring_evidence as Record<string, unknown>;
  // The response ledger may contain an explicit missing marker. It is still a
  // stored item response, but it is not an answered item for scoring.
  completed.item_response_count = 2;
  evidence.answered_item_count = 1;
  evidence.missing_item_count = 1;
  const parsed = parseAssessmentEvent(source, { patientId: "P-001" });
  assert.equal(parsed.instances[0].item_response_count, 2);
  assert.equal(parsed.instances[0].scoring_evidence?.answered_item_count, 1);
  assert.equal(parsed.instances[0].scoring_evidence?.missing_item_count, 1);

  const impossible = closedEvent();
  const impossibleCompleted = (impossible.instances as Record<string, unknown>[])[0];
  const impossibleEvidence = impossibleCompleted.scoring_evidence as Record<string, unknown>;
  impossibleEvidence.answered_item_count = 2;
  impossibleEvidence.missing_item_count = 1;
  assert.throws(
    () => parseAssessmentEvent(impossible, { patientId: "P-001" }),
    /条目计数/,
  );
});

test("cross-patient/event/category/digest responses fail closed", () => {
  assert.throws(() => parseAssessmentEvent(event(), { patientId: "P-002" }), /patient/);

  const wrongNestedEvent = event();
  const first = (wrongNestedEvent.instances as Record<string, unknown>[])[0];
  first.event_id = "assessment-event-other";
  assert.throws(() => parseAssessmentEvent(wrongNestedEvent, { patientId: "P-001" }), /eventId/);

  const wrongDigest = event();
  assert.throws(() => parseAssessmentEvent(wrongDigest, {
    patientId: "P-001",
    definitionDigests: { untrained_standardized_naming: RESULT_DIGEST },
  }), /definitionDigest/);

  const duplicateCategory = event();
  const duplicateRows = duplicateCategory.instances as Record<string, unknown>[];
  duplicateRows[1].category_key = duplicateRows[0].category_key;
  assert.throws(() => parseAssessmentEvent(duplicateCategory, { patientId: "P-001" }), /类别/);
});

test("unknown fields and 0-or-2 fake evidence shapes are rejected", () => {
  assert.throws(() => parseAssessmentEvent({ ...event(), invented_score: 0 }, {
    patientId: "P-001",
  }), /未允许字段/);

  const missingEvidence = closedEvent();
  const missingRows = missingEvidence.instances as Record<string, unknown>[];
  missingRows[0].scoring_evidence = null;
  assert.throws(() => parseAssessmentEvent(missingEvidence, { patientId: "P-001" }), /completed/);

  const duplicateEvidence = closedEvent();
  const duplicateEvidenceRow = (duplicateEvidence.instances as Record<string, unknown>[])[0];
  duplicateEvidenceRow.scoring_evidence = [
    duplicateEvidenceRow.scoring_evidence,
    duplicateEvidenceRow.scoring_evidence,
  ];
  assert.throws(() => parseAssessmentEvent(duplicateEvidence, { patientId: "P-001" }), /不是对象/);
});

test("score range, definition bundle and classification snapshots stay bound", () => {
  const outside = closedEvent();
  const outsideRow = (outside.instances as Record<string, unknown>[])[0];
  (outsideRow.scoring_evidence as Record<string, unknown>).score = 5;
  assert.throws(() => parseAssessmentEvent(outside, { patientId: "P-001" }), /超出/);

  const wrongBundle = event();
  (wrongBundle.instances as Record<string, unknown>[])[0].definition_bundle_digest = RESULT_DIGEST;
  assert.throws(() => parseAssessmentEvent(wrongBundle, { patientId: "P-001" }), /bundle/);

  const leakedFormalSimulation = event();
  leakedFormalSimulation.formal_outcome_eligible = true;
  assert.throws(() => parseAssessmentEvent(leakedFormalSimulation, { patientId: "P-001" }), /模拟/);
});

test("cancelled due event has its own exact switch proof and no closeout", () => {
  const cancelled = {
    ...event(),
    status: "cancelled",
    revision: 2,
    cancellation: {
      reason_code: "schedule_changed",
      cancelled_by: "researcher-0001",
      cancelled_at: "2026-07-19T08:05:00+08:00",
      switch_allowed: true,
    },
    updated_at: "2026-07-19T08:05:00+08:00",
  };
  const parsed = parseAssessmentEvent(cancelled, { patientId: "P-001" });
  assert.equal(assessmentEventAllowsSwitch(parsed), true);
  assert.equal(parsed.closeout, null);
});

test("today/list envelopes remain exact and reject duplicate events", () => {
  assert.equal(parseAssessmentEventList([], { patientId: "P-001" }).length, 0);
  const today = parseAssessmentEventsToday({ as_of_date: "2026-07-19", events: [event()] });
  assert.equal(today.events[0].patient_id, "P-001");
  assert.throws(() => parseAssessmentEventsToday({
    as_of_date: "2026-07-19",
    events: [event(), event()],
  }), /重复/);
});

test("patient switch gate also respects unfinished training runtime", () => {
  const inProgress = parseAssessmentEvent({ ...event(), status: "in_progress" }, {
    patientId: "P-001",
  });
  assert.equal(assessmentWorkflowAllowsPatientSwitch([inProgress], []), false);
  assert.equal(assessmentWorkflowAllowsPatientSwitch([], ["active"]), false);
  assert.equal(assessmentWorkflowAllowsPatientSwitch([], ["intervention_completed"]), false);
  assert.equal(assessmentWorkflowAllowsPatientSwitch([], ["completed"]), true);
});

const READY_TO_WRITE: FormalAssessmentMutationReadiness = {
  schema_version: "scale-protocol-readiness.v4",
  definition_ready: true,
  definition_artifact_enforcement_ready: true,
  definition_artifacts_ready: true,
  formal_result_contract_ready: true,
  workflow_policy_ready: true,
  workflow_contract_ready: true,
  workflow_policy_enforcement_ready: true,
  workflow_ready: true,
  ready_for_research: true,
  instance_creation_enabled: true,
  automatic_scoring_enabled: true,
  training_metrics_are_formal_scale_results: false,
};

const ASSESSMENT_MUTATIONS: FormalAssessmentMutation[] = [
  "create", "start", "cancel", "response", "complete", "approve_defer", "close",
];

const REQUIRED_TRUE_FLAGS = [
  "definition_ready",
  "definition_artifact_enforcement_ready",
  "definition_artifacts_ready",
  "formal_result_contract_ready",
  "workflow_policy_ready",
  "workflow_contract_ready",
  "workflow_policy_enforcement_ready",
  "workflow_ready",
  "ready_for_research",
  "instance_creation_enabled",
] as const;

test("all formal assessment writes require the exact complete v4 readiness receipt", () => {
  for (const mutation of ASSESSMENT_MUTATIONS) {
    assert.doesNotThrow(() => assertFormalAssessmentMutationReady(READY_TO_WRITE, mutation));
  }
  assert.doesNotThrow(() => assertFormalAssessmentMutationReady({
    ...READY_TO_WRITE,
    automatic_scoring_enabled: false,
  }, "start"));
  assert.throws(() => assertFormalAssessmentMutationReady({
    ...READY_TO_WRITE,
    automatic_scoring_enabled: false,
  }, "complete"), /尚未就绪/);
});

test("formal assessment write gate rejects every missing v4 readiness fact", () => {
  const requiredFields = [
    "schema_version",
    ...REQUIRED_TRUE_FLAGS,
    "training_metrics_are_formal_scale_results",
  ] as const;
  for (const field of requiredFields) {
    const candidate: Record<string, unknown> = { ...READY_TO_WRITE };
    delete candidate[field];
    assert.throws(
      () => assertFormalAssessmentMutationReady(
        candidate as FormalAssessmentMutationReadiness,
        "start",
      ),
      /尚未就绪/,
      `missing ${field} must fail closed`,
    );
  }
  const candidate: Record<string, unknown> = { ...READY_TO_WRITE };
  delete candidate.automatic_scoring_enabled;
  assert.throws(
    () => assertFormalAssessmentMutationReady(
      candidate as FormalAssessmentMutationReadiness,
      "complete",
    ),
    /尚未就绪/,
    "missing automatic_scoring_enabled must block completion",
  );
});

test("formal assessment write gate rejects false or legacy readiness facts", () => {
  for (const field of REQUIRED_TRUE_FLAGS) {
    assert.throws(
      () => assertFormalAssessmentMutationReady({ ...READY_TO_WRITE, [field]: false }, "start"),
      /尚未就绪/,
      `${field}=false must fail closed`,
    );
  }
  assert.throws(() => assertFormalAssessmentMutationReady({
    ...READY_TO_WRITE,
    schema_version: "scale-protocol-readiness.v3" as "scale-protocol-readiness.v4",
  }, "start"), /尚未就绪/);
  assert.throws(() => assertFormalAssessmentMutationReady({
    ...READY_TO_WRITE,
    training_metrics_are_formal_scale_results: true,
  }, "start"), /尚未就绪/);
});

test("formal assessment write gate rejects truthy flag spoofing", () => {
  for (const field of REQUIRED_TRUE_FLAGS) {
    const spoofed = {
      ...READY_TO_WRITE,
      [field]: "true",
    } as unknown as FormalAssessmentMutationReadiness;
    assert.throws(
      () => assertFormalAssessmentMutationReady(spoofed, "start"),
      /尚未就绪/,
      `${field} string spoof must fail closed`,
    );
  }
  assert.throws(() => assertFormalAssessmentMutationReady({
    ...READY_TO_WRITE,
    training_metrics_are_formal_scale_results: "false",
  } as unknown as FormalAssessmentMutationReadiness, "start"), /尚未就绪/);
  assert.throws(() => assertFormalAssessmentMutationReady({
    ...READY_TO_WRITE,
    automatic_scoring_enabled: "true",
  } as unknown as FormalAssessmentMutationReadiness, "complete"), /尚未就绪/);
});

test("ScaleDrawer loads the read-only formal projection before legacy rows", () => {
  const source = readFileSync(new URL("./ScaleDrawer.tsx", import.meta.url), "utf8");
  assert.match(source, /api\.listPatientAssessmentEvents\(patientId\)/);
  assert.ok(source.indexOf("<h3>正式评估记录</h3>")
    < source.indexOf("<h3>历史未验证记录"));
  assert.doesNotMatch(source, /api\.(create|start|cancel|submit|complete|approve|close)Assessment/);
  assert.equal(source.includes("scoring_evidence.score"), false);
});
