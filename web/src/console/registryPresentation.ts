// 登记表行的展示分层:可训练的活跃行按最近活动排前;已撤回/编号不合规的
// 行折叠进「已归档 / 不可训练」收起区。纯函数,方便在 node --test 下钉行为。
import type { PatientSummary } from "../types";

export const PATIENT_ID_VALID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export function patientIdInvalid(row: PatientSummary): boolean {
  return !PATIENT_ID_VALID_PATTERN.test(row.patient_id);
}

export function rowIsArchived(row: PatientSummary): boolean {
  return Boolean(row.withdrawal_status) || patientIdInvalid(row);
}

export interface RegistryPresentation {
  active: PatientSummary[];
  archived: PatientSummary[];
}

// 活跃区排序:最近训练日新的在前;从未训练的排在有训练记录之后;
// 同档再按研究编号,保证顺序稳定可预期。
export function compareByRecentActivity(
  left: PatientSummary,
  right: PatientSummary,
): number {
  const leftDate = left.last_training_date ?? "";
  const rightDate = right.last_training_date ?? "";
  if (leftDate !== rightDate) return leftDate > rightDate ? -1 : 1;
  return left.patient_id < right.patient_id ? -1
    : left.patient_id > right.patient_id ? 1 : 0;
}

export function presentRegistryRows(rows: PatientSummary[]): RegistryPresentation {
  const active: PatientSummary[] = [];
  const archived: PatientSummary[] = [];
  for (const row of rows) {
    (rowIsArchived(row) ? archived : active).push(row);
  }
  active.sort(compareByRecentActivity);
  archived.sort(compareByRecentActivity);
  return { active, archived };
}

export function archivedRowReason(row: PatientSummary): string {
  if (row.withdrawal_status) return "已撤回，不能再安排训练";
  return "编号含中文或特殊字符，训练流程不接受";
}
