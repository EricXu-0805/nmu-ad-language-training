import type {
  PatientWithdrawalReceipt,
  WithdrawalReasonCode,
  WithdrawnAudioGovernanceRow,
} from "../types";

export const WITHDRAWAL_REASON_LABELS: Record<WithdrawalReasonCode, string> = {
  participant_request: "受试者本人提出",
  representative_request: "法定代理人或代表提出",
  clinical_safety: "临床安全原因",
  ethics_or_protocol: "伦理或方案要求",
};

const RECEIPT_KEYS = [
  "schema_version", "event_id", "patient_id", "withdrawal_status",
  "consent_status", "expected_governance_revision", "governance_revision",
  "reason_code", "actor_display_id", "actor_role", "occurred_at",
  "affected_session_count", "affected_audio_count", "request_fingerprint",
  "idempotent",
] as const;

export function parsePatientWithdrawalReceipt(
  value: unknown,
  expectedPatientId?: string,
): PatientWithdrawalReceipt {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("研究撤回回执格式错误");
  }
  const row = value as Record<string, unknown>;
  const keys = Object.keys(row).sort();
  const expectedKeys = [...RECEIPT_KEYS].sort();
  if (keys.length !== expectedKeys.length
      || keys.some((key, index) => key !== expectedKeys[index])) {
    throw new Error("研究撤回回执字段不完整或包含未知字段");
  }
  const reason = row.reason_code;
  const validReason = typeof reason === "string"
    && Object.hasOwn(WITHDRAWAL_REASON_LABELS, reason);
  const nonnegativeInteger = (candidate: unknown) => Number.isInteger(candidate)
    && typeof candidate === "number" && candidate >= 0;
  if (row.schema_version !== 1
      || typeof row.event_id !== "string" || !row.event_id.startsWith("withdrawal-")
      || typeof row.patient_id !== "string" || !row.patient_id
      || (expectedPatientId !== undefined && row.patient_id !== expectedPatientId)
      || row.withdrawal_status !== "withdrawn"
      || row.consent_status !== "withdrawn"
      || !nonnegativeInteger(row.expected_governance_revision)
      || !nonnegativeInteger(row.governance_revision)
      || row.governance_revision !== (row.expected_governance_revision as number) + 1
      || !validReason
      || typeof row.actor_display_id !== "string" || !row.actor_display_id.trim()
      || row.actor_role !== "admin"
      || typeof row.occurred_at !== "string" || !row.occurred_at
      || !nonnegativeInteger(row.affected_session_count)
      || !nonnegativeInteger(row.affected_audio_count)
      || typeof row.request_fingerprint !== "string"
      || !/^[0-9a-f]{64}$/.test(row.request_fingerprint)
      || typeof row.idempotent !== "boolean") {
    throw new Error("研究撤回回执事实不一致");
  }
  return row as unknown as PatientWithdrawalReceipt;
}

export function parseWithdrawnAudioGovernanceRows(
  value: unknown,
): WithdrawnAudioGovernanceRow[] {
  if (!Array.isArray(value)) throw new Error("撤回录音治理列表格式错误");
  const exactKeys = [
    "raw_audio_id", "session_id", "patient_id", "status", "withdrawn",
    "withdrawal_status", "delete_gate_passed",
  ].sort();
  const seen = new Set<string>();
  return value.map((candidate) => {
    if (candidate === null || typeof candidate !== "object" || Array.isArray(candidate)) {
      throw new Error("撤回录音治理行格式错误");
    }
    const row = candidate as Record<string, unknown>;
    const keys = Object.keys(row).sort();
    if (keys.length !== exactKeys.length
        || keys.some((key, index) => key !== exactKeys[index])) {
      throw new Error("撤回录音治理行字段不完整或包含未知字段");
    }
    if (typeof row.raw_audio_id !== "string"
        || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$/.test(row.raw_audio_id)
        || seen.has(row.raw_audio_id)
        || typeof row.session_id !== "string" || !row.session_id
        || typeof row.patient_id !== "string" || !row.patient_id
        || typeof row.status !== "string" || !row.status
        || typeof row.withdrawn !== "boolean"
        || !(row.withdrawal_status === null
          || typeof row.withdrawal_status === "string")
        || (!row.withdrawn && !String(row.withdrawal_status ?? "").trim())
        || typeof row.delete_gate_passed !== "boolean") {
      throw new Error("撤回录音治理行事实不一致");
    }
    seen.add(row.raw_audio_id);
    return row as unknown as WithdrawnAudioGovernanceRow;
  });
}

export function newWithdrawalIdempotencyKey(
  random: Uint8Array | null = null,
): string {
  const bytes = random ?? crypto.getRandomValues(new Uint8Array(32));
  if (bytes.byteLength < 24) throw new Error("研究撤回幂等标识随机量不足");
  return `subject-withdrawal-${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

export function withdrawalReceiptMatches(
  receipt: PatientWithdrawalReceipt,
  patientId: string,
  expectedRevision: number,
  reasonCode: WithdrawalReasonCode,
): boolean {
  return receipt.patient_id === patientId
    && receipt.withdrawal_status === "withdrawn"
    && receipt.expected_governance_revision === expectedRevision
    && receipt.governance_revision === expectedRevision + 1
    && receipt.reason_code === reasonCode;
}
