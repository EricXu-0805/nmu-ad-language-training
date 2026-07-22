import assert from "node:assert/strict";
import test from "node:test";

import type { PatientSummary, PatientWithdrawalReceipt } from "../types.ts";
import {
  applyWithdrawalReceiptToRegistry,
  createSubjectRegistryRefreshGate,
} from "./subjectRegistryRefresh.ts";

test("registry refresh is latest-wins and unmount invalidates in-flight reads", () => {
  const gate = createSubjectRegistryRefreshGate();
  const slowInitialRead = gate.begin();
  const currentRefresh = gate.begin();

  assert.equal(gate.accepts(slowInitialRead), false);
  assert.equal(gate.accepts(currentRefresh), true);

  gate.invalidate();
  assert.equal(gate.accepts(currentRefresh), false);
});

test("withdrawal receipt immediately projects the authoritative terminal state", () => {
  const rows: PatientSummary[] = [{
    patient_id: "P-001",
    is_simulation_subject: false,
    consent_status: "consented",
    governance_revision: 3,
    research_eligible: true,
    research_eligibility_issues: ["withdrawal_status=legacy"],
    session_count: 2,
  }, {
    patient_id: "P-002",
    is_simulation_subject: false,
    governance_revision: 0,
    session_count: 0,
  }];
  const receipt: PatientWithdrawalReceipt = {
    schema_version: 1,
    event_id: "withdrawal-event-1",
    patient_id: "P-001",
    withdrawal_status: "withdrawn",
    consent_status: "withdrawn",
    expected_governance_revision: 3,
    governance_revision: 4,
    reason_code: "participant_request",
    actor_display_id: "reviewer-1",
    actor_role: "admin",
    occurred_at: "2026-07-19T12:00:00",
    affected_session_count: 2,
    affected_audio_count: 5,
    request_fingerprint: "fingerprint",
    idempotent: false,
  };

  const next = applyWithdrawalReceiptToRegistry(rows, receipt);
  assert.notEqual(next, rows);
  assert.deepEqual(next?.[0], {
    ...rows[0],
    consent_status: "withdrawn",
    withdrawal_status: "withdrawn",
    governance_revision: 4,
    withdrawal_event_id: "withdrawal-event-1",
    withdrawal_reason_code: "participant_request",
    withdrawal_occurred_at: "2026-07-19T12:00:00",
    research_eligible: false,
    research_eligibility_issues: ["withdrawal_status=withdrawn"],
  });
  assert.equal(next?.[1], rows[1], "unrelated patients keep their existing object identity");
});
