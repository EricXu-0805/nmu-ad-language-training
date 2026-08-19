/** 第 1 周关系建立的本地门禁输入;null=位置尚未核对成功(fail-closed)。 */
export interface RapportGateStatus {
  atFarewell: boolean;
}

export interface LocalCompletionGateInput {
  journalReady: boolean;
  planReady: boolean;
  weekNo: number;
  lockedTurns: number;
  totalTurns: number;
  rapport?: RapportGateStatus | null;
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
  rapport?: RapportGateStatus | null;
}

// 服务端仍是权威:这里只镜像「停在道别节」这一个可见事实;录音指令收回与
// 音频/评分行核验由服务端完成判定(rapport_* issue)如实回显。
function rapportGate(rapport: RapportGateStatus | null | undefined): LocalCompletionGate {
  if (!rapport) {
    return {
      canRequest: false,
      label: "正在核对关系建立位置",
      detail: "第 1 周需先与服务器核对关系建立位置，才能结束本场。",
    };
  }
  if (!rapport.atFarewell) {
    return {
      canRequest: false,
      label: "须先播报道别话术",
      detail: "完成道别后才能结束本场；要提前结束请走中止流程。",
    };
  }
  return {
    canRequest: true,
    label: "可以请求结束本场",
    detail: "服务器会核对道别位置和录音后确认结束。",
  };
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
  protocol?: "rapport";
  expectedTurns?: number;
  matchedTurns?: number;
  lockedTurns?: number;
  completedAttemptTurns?: number;
  audioEvidencedTurns?: number;
  atFarewell?: boolean;
  recordingIdle?: boolean;
  audioTotal?: number;
  audioVerified?: number;
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
  rapport,
}: LocalCompletionGateInput): LocalCompletionGate {
  if (!journalReady || !planReady) {
    return {
      canRequest: false,
      label: "正在核对服务器记录",
      detail: "服务器记录加载完成后，才能完成本场。",
    };
  }
  if (weekNo === 1) {
    return rapportGate(rapport);
  }
  if (totalTurns <= 0) {
    return {
      canRequest: false,
      label: "场次计划没有可核对环节",
      detail: "本场题目清单为空或异常，暂不能完成，请检查场次安排。",
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
      detail: `当前有 ${lockedTurns} 个已锁定记录，但计划只有 ${totalTurns} 个环节，请先核查是否有重复记录。`,
    };
  }
  return {
    canRequest: true,
    label: "可以请求最终完成",
    detail: "全部环节已锁定；服务器确认后本场最终完成。",
  };
}

export function localInterventionCompletionGate({
  journalReady,
  planReady,
  weekNo,
  totalTurns,
  rapport,
}: LocalInterventionCompletionGateInput): LocalCompletionGate {
  if (!journalReady || !planReady) {
    return {
      canRequest: false,
      label: "正在核对服务器证据",
      detail: "服务器记录加载完成后，才能结束现场训练。",
    };
  }
  if (weekNo === 1) {
    return rapportGate(rapport);
  }
  if (totalTurns <= 0) {
    return {
      canRequest: false,
      label: "场次计划没有可核对环节",
      detail: "本场题目清单为空或异常，暂不能结束，请检查场次安排。",
    };
  }
  return {
    canRequest: true,
    label: "可以结束现场训练",
    detail: "服务器会核对每题的 AI 记录和录音；人工确认和评分可事后再做。",
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
    || "at_farewell" in assessment
    || "issues" in assessment;
  if (!hasAssessmentShape) return undefined;
  return {
    ready: assessment.ready === true,
    protocol: assessment.protocol === "rapport" ? "rapport" : undefined,
    expectedTurns: finiteNumber(assessment.expected_turns),
    matchedTurns: finiteNumber(assessment.matched_turns),
    lockedTurns: finiteNumber(assessment.locked_turns),
    completedAttemptTurns: finiteNumber(assessment.completed_attempt_turns),
    audioEvidencedTurns: finiteNumber(assessment.audio_evidenced_turns),
    atFarewell: typeof assessment.at_farewell === "boolean" ? assessment.at_farewell : undefined,
    recordingIdle: typeof assessment.recording_idle === "boolean" ? assessment.recording_idle : undefined,
    audioTotal: finiteNumber(assessment.audio_total),
    audioVerified: finiteNumber(assessment.audio_verified),
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
