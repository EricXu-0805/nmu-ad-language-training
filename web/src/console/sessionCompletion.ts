export interface LocalCompletionGateInput {
  journalReady: boolean;
  planReady: boolean;
  weekNo: number;
  lockedTurns: number;
  totalTurns: number;
}

export interface LocalCompletionGate {
  canRequest: boolean;
  label: string;
  detail: string;
}

export interface LocalInterventionCompletionGateInput {
  journalReady: boolean;
  planReady: boolean;
  weekNo: number;
  totalTurns: number;
}

export interface CompletionIssueView {
  code?: string;
  detail: string;
  itemId?: string;
  turnSeq?: number;
  responseRole?: string;
}

export interface CompletionAssessmentView {
  ready: boolean;
  expectedTurns?: number;
  matchedTurns?: number;
  lockedTurns?: number;
  completedAttemptTurns?: number;
  audioEvidencedTurns?: number;
  issues: CompletionIssueView[];
}

export interface CompletionFailureView {
  message: string;
  assessment?: CompletionAssessmentView;
}

type UnknownRecord = Record<string, unknown>;

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as UnknownRecord
    : null;
}

function finiteNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function parseJsonObject(value: string): UnknownRecord | null {
  try { return record(JSON.parse(value)); }
  catch { return null; }
}

function unwrapDetail(value: unknown): unknown {
  const valueRecord = record(value);
  return valueRecord && "detail" in valueRecord ? valueRecord.detail : value;
}

export function localCompletionGate({
  journalReady,
  planReady,
  weekNo,
  lockedTurns,
  totalTurns,
}: LocalCompletionGateInput): LocalCompletionGate {
  if (!journalReady || !planReady) {
    return {
      canRequest: false,
      label: "正在核对服务器记录",
      detail: "服务器记录和冻结计划均加载完成后，才能请求完成本场次。",
    };
  }
  if (weekNo === 1) {
    return {
      canRequest: false,
      label: "关系建立完成口径待实现",
      detail: "第 1 周不能按空训练计划自动完成；须先实现关系建立脚本、录音与退出条件的独立完成口径。",
    };
  }
  if (totalTurns <= 0) {
    return {
      canRequest: false,
      label: "场次计划没有可核对环节",
      detail: "冻结计划为空或异常，系统已保持本场未完成，请检查场次安排。",
    };
  }
  if (lockedTurns < totalTurns) {
    return {
      canRequest: false,
      label: `仍有 ${totalTurns - lockedTurns} 个环节待锁定`,
      detail: `当前已锁定 ${lockedTurns}/${totalTurns} 个计划环节，请返回训练补齐。`,
    };
  }
  if (lockedTurns > totalTurns) {
    return {
      canRequest: false,
      label: "场次记录数量与计划不一致",
      detail: `当前有 ${lockedTurns} 个已锁定记录，但冻结计划只有 ${totalTurns} 个环节，须先核查重复或计划外记录。`,
    };
  }
  return {
    canRequest: true,
    label: "可请求服务器完成核验",
    detail: "本地完整度已对齐；服务器仍会逐项核对冻结计划与唯一锁定研究真值。",
  };
}

export function localInterventionCompletionGate({
  journalReady,
  planReady,
  weekNo,
  totalTurns,
}: LocalInterventionCompletionGateInput): LocalCompletionGate {
  if (!journalReady || !planReady) {
    return {
      canRequest: false,
      label: "正在核对服务器证据",
      detail: "服务器记录和冻结计划均加载完成后，才能请求结束床旁干预。",
    };
  }
  if (weekNo === 1) {
    return {
      canRequest: false,
      label: "关系建立结束口径待实现",
      detail: "第 1 周须先建立独立的脚本步骤、录音与退出条件，不能用空训练计划宣布结束。",
    };
  }
  if (totalTurns <= 0) {
    return {
      canRequest: false,
      label: "场次计划没有可核对环节",
      detail: "冻结计划为空或异常，系统保持当前场次，请检查场次安排。",
    };
  }
  return {
    canRequest: true,
    label: "可请求 AI 证据完整性核验",
    detail: "服务器会逐环节核对权威 AI attempt、原始录音及绑定关系；不要求床旁等待人工确认或锁分。",
  };
}

function parseIssue(value: unknown): CompletionIssueView | null {
  if (typeof value === "string" && value.trim()) return { detail: value.trim() };
  const issue = record(value);
  if (!issue) return null;
  const detail = nonEmptyString(issue.detail) ?? nonEmptyString(issue.message);
  if (!detail) return null;
  return {
    detail,
    code: nonEmptyString(issue.code),
    itemId: nonEmptyString(issue.item_id),
    turnSeq: finiteNumber(issue.turn_seq),
    responseRole: nonEmptyString(issue.response_role),
  };
}

function parseAssessment(value: unknown): CompletionAssessmentView | undefined {
  const assessment = record(value);
  if (!assessment) return undefined;
  const issues = Array.isArray(assessment.issues)
    ? assessment.issues.map(parseIssue).filter((issue): issue is CompletionIssueView => issue !== null)
    : [];
  const hasAssessmentShape = "ready" in assessment
    || "expected_turns" in assessment
    || "locked_turns" in assessment
    || "issues" in assessment;
  if (!hasAssessmentShape) return undefined;
  return {
    ready: assessment.ready === true,
    expectedTurns: finiteNumber(assessment.expected_turns),
    matchedTurns: finiteNumber(assessment.matched_turns),
    lockedTurns: finiteNumber(assessment.locked_turns),
    completedAttemptTurns: finiteNumber(assessment.completed_attempt_turns),
    audioEvidencedTurns: finiteNumber(assessment.audio_evidenced_turns),
    issues,
  };
}

export function parseCompletionFailure(error: unknown): CompletionFailureView {
  const errorRecord = record(error);
  const rawDetail = errorRecord && "detailData" in errorRecord
    ? errorRecord.detailData
    : errorRecord && "detail" in errorRecord
      ? errorRecord.detail
      : error;
  const unwrapped = unwrapDetail(rawDetail);
  const parsed = typeof unwrapped === "string" ? parseJsonObject(unwrapped) : record(unwrapped);
  const payload = parsed && "detail" in parsed ? record(parsed.detail) ?? parsed : parsed;
  const assessment = parseAssessment(payload?.assessment)
    ?? parseAssessment(payload)
    ?? parseAssessment(errorRecord?.assessment);
  const message = nonEmptyString(payload?.message)
    ?? nonEmptyString(payload?.detail)
    ?? nonEmptyString(unwrapped)
    ?? nonEmptyString(errorRecord?.detail)
    ?? "服务器未确认场次完成，请核对记录后重试。";
  return { message, assessment };
}

export function completionIssueLabel(issue: CompletionIssueView): string {
  const position = [
    issue.itemId,
    issue.turnSeq == null ? undefined : `第 ${issue.turnSeq} 环节`,
    issue.responseRole,
  ].filter((part): part is string => Boolean(part)).join(" · ");
  return position ? `${position}：${issue.detail}` : issue.detail;
}
