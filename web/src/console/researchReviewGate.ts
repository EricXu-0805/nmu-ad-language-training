import type { TaskType } from "../types";

export type AudioEvidenceStatus = "missing" | "idle" | "loading" | "ready" | "error";

export interface ResearchScoreOption {
  value: "0" | "1";
  label: string;
}

export interface ResearchScoringContract {
  id: string;
  taskType: TaskType;
  scoringKey: string;
  responseRole: string;
  options: readonly ResearchScoreOption[];
}

export type ResearchScoringContractResult =
  | { ok: true; contract: ResearchScoringContract }
  | { ok: false; reason: string };

const BINARY_NAMING_OPTIONS = [
  { value: "1", label: "命名正确（1）" },
  { value: "0", label: "未命名正确（0）" },
] as const;

const BINARY_FUNCTION_OPTIONS = [
  { value: "1", label: "功能描述正确（1）" },
  { value: "0", label: "功能描述不正确（0）" },
] as const;

// 会议表原有“部分识别 0.5”档；2026-08-19 钱凯口径“必须正确才给分”后取消。
const RELATION_OPTIONS = [
  { value: "1", label: "识别（1）" },
  { value: "0", label: "未识别（0）" },
] as const;

const BINARY_KEY_ELEMENT_OPTIONS = [
  { value: "1", label: "正确识别（1）" },
  { value: "0", label: "未识别（0）" },
] as const;

const KNOWN_CONTRACTS: readonly ResearchScoringContract[] = [
  {
    id: "single.final_correct.v1",
    taskType: "单要素",
    scoringKey: "final_correct",
    responseRole: "命名",
    options: BINARY_NAMING_OPTIONS,
  },
  {
    id: "double.left_name.v1",
    taskType: "双要素",
    scoringKey: "left_name",
    responseRole: "左命名",
    options: BINARY_NAMING_OPTIONS,
  },
  {
    id: "double.left_function.v1",
    taskType: "双要素",
    scoringKey: "left_function",
    responseRole: "左作用",
    options: BINARY_FUNCTION_OPTIONS,
  },
  {
    id: "double.right_name.v1",
    taskType: "双要素",
    scoringKey: "right_name",
    responseRole: "右命名",
    options: BINARY_NAMING_OPTIONS,
  },
  {
    id: "double.right_function.v1",
    taskType: "双要素",
    scoringKey: "right_function",
    responseRole: "右作用",
    options: BINARY_FUNCTION_OPTIONS,
  },
  {
    id: "double.relation.v1",
    taskType: "双要素",
    scoringKey: "relation",
    responseRole: "关系识别",
    options: RELATION_OPTIONS,
  },
  // 多要素只计算关键要素识别（会议决策评价体系6）：情境/事物/人物/动作各 0/1。
  ...(["情境", "事物", "人物", "动作"] as const).map((key) => ({
    id: `multi.${key}.v1`,
    taskType: "多要素" as TaskType,
    scoringKey: key,
    responseRole: key,
    options: BINARY_KEY_ELEMENT_OPTIONS,
  })),
] as const;

export function resolveResearchScoringContract(
  taskType: string,
  scoringKey: string | null,
  responseRole: string,
): ResearchScoringContractResult {
  const contract = KNOWN_CONTRACTS.find((candidate) => (
    candidate.taskType === taskType
    && candidate.scoringKey === scoringKey
    && candidate.responseRole === responseRole
  ));
  if (contract) return { ok: true, contract };
  return {
    ok: false,
    reason: `未识别评分合同：task_type=${taskType || "（空）"}，scoring_key=${scoringKey ?? "null"}，response_role=${responseRole || "（空）"}。`,
  };
}

export type ResearchReviewGateBlockerCode =
  | "unknown_scoring_contract"
  | "empty_confirmed_text"
  | "missing_reviewer"
  | "missing_prompt_level"
  | "missing_audio_reference"
  | "audio_not_loaded"
  | "audio_load_failed"
  | "missing_score"
  | "invalid_score";

export interface ResearchReviewGateBlocker {
  code: ResearchReviewGateBlockerCode;
  message: string;
}

export interface ResearchReviewGateInput {
  taskType: string;
  scoringKey: string | null;
  responseRole: string;
  confirmedText: string;
  reviewerId: string;
  promptLevel: number | null | undefined;
  rawAudioId: string | null | undefined;
  audioStatus: AudioEvidenceStatus;
  selectedScore: string | null;
}

export interface ResearchReviewGateResult {
  contract: ResearchScoringContractResult;
  blockers: readonly ResearchReviewGateBlocker[];
  canSaveConfirmation: boolean;
  canLockScore: boolean;
}

export function evaluateResearchReviewGate(
  input: ResearchReviewGateInput,
): ResearchReviewGateResult {
  const contract = resolveResearchScoringContract(
    input.taskType,
    input.scoringKey,
    input.responseRole,
  );
  const blockers: ResearchReviewGateBlocker[] = [];

  if (!contract.ok) {
    blockers.push({ code: "unknown_scoring_contract", message: contract.reason });
  }
  if (!input.confirmedText.trim()) {
    blockers.push({
      code: "empty_confirmed_text",
      message: "人工确认文本不能为空；无作答请明确记录为“未作答”。",
    });
  }
  if (!input.reviewerId.trim()) {
    blockers.push({
      code: "missing_reviewer",
      message: "当前账号没有可审计的评分人标识。",
    });
  }
  if (!Number.isInteger(input.promptLevel) || ![0, 1, 2, 3].includes(input.promptLevel as number)) {
    blockers.push({
      code: "missing_prompt_level",
      message: "服务器没有返回有效的权威提示等级（0–3）。",
    });
  }

  if (!input.rawAudioId?.trim()) {
    blockers.push({
      code: "missing_audio_reference",
      message: "计划环节缺少原始录音引用，禁止锁分。",
    });
  } else if (input.audioStatus === "error") {
    blockers.push({
      code: "audio_load_failed",
      message: "原始录音鉴权加载失败，禁止锁分。",
    });
  } else if (input.audioStatus !== "ready") {
    blockers.push({
      code: "audio_not_loaded",
      message: "请先鉴权加载并核对原始录音，再锁分。",
    });
  }

  if (input.selectedScore === null) {
    blockers.push({ code: "missing_score", message: "请选择研究评分。" });
  } else if (!contract.ok || !contract.contract.options.some((option) => option.value === input.selectedScore)) {
    blockers.push({
      code: "invalid_score",
      message: "所选分值不属于当前冻结评分合同。",
    });
  }

  const canSaveConfirmation = contract.ok && Boolean(input.confirmedText.trim());
  return {
    contract,
    blockers,
    canSaveConfirmation,
    canLockScore: contract.ok && blockers.length === 0,
  };
}

export interface PlanDisplayFact {
  path: string;
  value: string;
}

// display 来自场次绑定计划。这里仅逐层展开 API 实际返回的 JSON 值，
// 不补默认目标、不从 item_id/role 猜测题意，也不改写内容。
export function planDisplayFacts(display: Record<string, unknown>): PlanDisplayFact[] {
  const facts: PlanDisplayFact[] = [];

  function visit(value: unknown, path: string): void {
    if (value === null) {
      facts.push({ path, value: "null" });
      return;
    }
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      facts.push({ path, value: String(value) });
      return;
    }
    if (Array.isArray(value)) {
      if (value.length === 0) facts.push({ path, value: "[]" });
      value.forEach((entry, index) => visit(entry, `${path}[${index}]`));
      return;
    }
    if (typeof value === "object") {
      const entries = Object.entries(value as Record<string, unknown>);
      if (entries.length === 0) facts.push({ path, value: "{}" });
      entries.forEach(([key, entry]) => visit(entry, path ? `${path}.${key}` : key));
    }
  }

  Object.entries(display).forEach(([key, value]) => visit(value, key));
  return facts;
}
