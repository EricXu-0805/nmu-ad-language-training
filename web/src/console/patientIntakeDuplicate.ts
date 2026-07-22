import type { Patient } from "../types";

export const DUPLICATE_INTAKE_FIELD_LABELS: Partial<Record<keyof Patient, string>> = {
  is_simulation_subject: "档案用途",
  dementia_severity: "认知障碍程度",
  mandarin_eligible: "普通话要求",
  consent_status: "知情同意状态",
  consent_type: "知情同意方式",
  consent_person: "签署人角色",
  proxy_consent: "代理同意",
  assent_obtained: "受试者本人赞同",
  recording_allowed: "录音授权",
  cloud_processing_allowed: "第三方云处理授权",
};

const DUPLICATE_SNAPSHOT_FIELDS: (keyof Patient)[] = [
  ...Object.keys(DUPLICATE_INTAKE_FIELD_LABELS) as (keyof Patient)[],
  "cloud_processing_provider_id",
  "cloud_processing_notice_version",
  "cloud_processing_consented_at",
  "cloud_processing_revoked_at",
  "withdrawal_status",
  "governance_revision",
];

export interface DuplicateIntakeAssessment {
  blockingConflictLabels: string[];
  cloudChangeRequested: boolean;
  cloudChangeDirection: "grant" | "revoke" | null;
  existingCloudRevocationRecorded: boolean;
  canReuseWithoutChanges: boolean;
  canRequestCloudChange: boolean;
}

// 提交一开始就固定本次研究者核对过的内容。后续网络等待期间即使草稿状态
// 发生变化，409 核对也必须继续使用这一份快照，不能混入较晚的编辑。
export function capturePatientIntakeSubmission(patient: Patient): Patient {
  return { ...patient, patient_id: patient.patient_id.trim() };
}

// 重复编号不是“更新档案”入口。除云处理授权外，任何已填写字段不一致都阻断；
// 云处理授权即使是唯一差异，也只能进入独立复核流程，不能随 409 恢复自动写入。
export function assessDuplicateIntake(
  submitted: Patient,
  existing: Patient,
): DuplicateIntakeAssessment {
  const blockingConflictLabels = (Object.keys(DUPLICATE_INTAKE_FIELD_LABELS) as (keyof Patient)[])
    .filter((key) => {
      if (key === "cloud_processing_allowed") return false;
      const filled = submitted[key];
      if (filled == null || filled === "") return false;
      return (existing[key] ?? null) !== filled;
    })
    .map((key) => DUPLICATE_INTAKE_FIELD_LABELS[key])
    .filter((label): label is string => Boolean(label));
  const requestedCloud = submitted.cloud_processing_allowed;
  const cloudChangeRequested = requestedCloud != null
    && existing.cloud_processing_allowed !== requestedCloud;

  return {
    blockingConflictLabels,
    cloudChangeRequested,
    cloudChangeDirection: !cloudChangeRequested
      ? null
      : requestedCloud ? "grant" : "revoke",
    existingCloudRevocationRecorded: existing.cloud_processing_revoked_at != null,
    canReuseWithoutChanges: blockingConflictLabels.length === 0,
    canRequestCloudChange: blockingConflictLabels.length === 0 && cloudChangeRequested,
  };
}

export function duplicateCloudChangeMayBeApplied(
  assessment: DuplicateIntakeAssessment,
  confirmation: { duplicateReviewed: boolean; dedicatedAuthorizationConfirmed: boolean },
): boolean {
  return assessment.canRequestCloudChange
    && confirmation.duplicateReviewed
    && confirmation.dedicatedAuthorizationConfirmed;
}

// 独立确认前再次读取服务器档案。授权、撤回或本次核对字段任一变化都必须重新展示，
// 避免研究者确认的是旧状态而覆盖并发更新。
export function duplicatePatientSnapshotChanged(reviewed: Patient, current: Patient): boolean {
  if (reviewed.patient_id !== current.patient_id) return true;
  return DUPLICATE_SNAPSHOT_FIELDS.some(
    (key) => (reviewed[key] ?? null) !== (current[key] ?? null),
  );
}
