import assert from "node:assert/strict";
import test from "node:test";
import {
  archivedRowReason,
  patientIdInvalid,
  presentRegistryRows,
  rowIsArchived,
} from "./registryPresentation.ts";
import type { PatientSummary } from "../types.ts";

function row(overrides: Partial<PatientSummary>): PatientSummary {
  return {
    patient_id: "P-1",
    is_simulation_subject: false,
    governance_revision: 0,
    session_count: 0,
    ...overrides,
  };
}

test("已撤回与编号不合规的行进归档区,其余留在活跃区", () => {
  assert.equal(rowIsArchived(row({ withdrawal_status: "withdrawn" })), true);
  assert.equal(rowIsArchived(row({ patient_id: "测试1" })), true);
  assert.equal(rowIsArchived(row({ patient_id: "NMU-001" })), false);
  assert.equal(patientIdInvalid(row({ patient_id: "张三" })), true);
  assert.equal(patientIdInvalid(row({ patient_id: "A.b_c-9" })), false);
});

test("活跃区按最近训练日倒序;没训练过的排后面,同档按编号稳定排序", () => {
  const { active, archived } = presentRegistryRows([
    row({ patient_id: "P-NEVER-B" }),
    row({ patient_id: "P-OLD", last_training_date: "2026-08-01" }),
    row({ patient_id: "P-GONE", withdrawal_status: "withdrawn", last_training_date: "2026-08-19" }),
    row({ patient_id: "P-NEW", last_training_date: "2026-08-20" }),
    row({ patient_id: "P-NEVER-A" }),
    row({ patient_id: "坏编号" }),
  ]);
  assert.deepEqual(active.map((r) => r.patient_id),
    ["P-NEW", "P-OLD", "P-NEVER-A", "P-NEVER-B"]);
  assert.deepEqual(archived.map((r) => r.patient_id), ["P-GONE", "坏编号"]);
});

test("归档原因分层:撤回优先于编号问题,文案是给人看的短句", () => {
  assert.match(archivedRowReason(row({ withdrawal_status: "withdrawn", patient_id: "坏编号" })), /已撤回/);
  assert.match(archivedRowReason(row({ patient_id: "坏编号" })), /编号/);
});
