export const SESSION_ABORT_REASONS = [
  ["participant_declined", "受试者不愿继续"],
  ["clinical_safety", "临床安全原因"],
  ["technical_failure", "设备或系统故障"],
  ["researcher_decision", "研究者按方案决定中止"],
] as const;

export type SessionAbortReasonCode = typeof SESSION_ABORT_REASONS[number][0];

export interface SessionAbortIntent {
  reason_code: SessionAbortReasonCode;
  expected_revision: number;
  idempotency_key: string;
}

const LABELS = new Map<string, string>(SESSION_ABORT_REASONS);

export function sessionAbortReasonLabel(value: string | null | undefined): string {
  if (value == null) return "原因未记录";
  return LABELS.get(value) ?? "未知的受控原因";
}

export function createSessionAbortIntent(
  reasonCode: SessionAbortReasonCode,
  expectedRevision: number,
  idempotencyKey: string,
): SessionAbortIntent {
  if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 0) {
    throw new Error("中止场次缺少可核对的运行修订");
  }
  if (!/^abort-[A-Za-z0-9-]{16,}$/.test(idempotencyKey)) {
    throw new Error("中止场次幂等键无效");
  }
  return Object.freeze({
    reason_code: reasonCode,
    expected_revision: expectedRevision,
    idempotency_key: idempotencyKey,
  });
}
