// 正式评估执行(收据 150 S5)的纯视图模型:按事件/实例状态推导可用动作,
// 把服务端拒绝(就绪 blocking_codes/政策 code/服务 code)如实翻译给研究者。
// 服务端永远是权威;这里只防「必然失败的请求」并解释失败,不复制门禁语义。
import type {
  AssessmentEvent,
  AssessmentInstance,
} from "../types";

export interface AssessmentActionGates {
  canStart: boolean;
  canCancel: boolean;
  canClose: boolean;
  instanceActions: Record<string, {
    canRespond: boolean;
    canComplete: boolean;
    canDefer: boolean;
  }>;
  nextStep: string;
}

export function assessmentActionGates(event: AssessmentEvent): AssessmentActionGates {
  const instanceActions: AssessmentActionGates["instanceActions"] = {};
  for (const instance of event.instances) {
    instanceActions[instance.instance_id] = {
      canRespond: event.status === "in_progress" && instance.status === "in_progress",
      canComplete: event.status === "in_progress" && instance.status === "in_progress",
      canDefer: event.status === "in_progress" && instance.status === "in_progress",
    };
  }
  const nextStep = event.status === "due"
    ? "启动评估事件后才能录入条目作答"
    : event.status === "in_progress"
      ? "逐条录入作答;两类实例都计分或获批延期后进入收尾"
      : event.status === "awaiting_closeout"
        ? "填写现场收尾记录并关闭事件"
        : "该测评已结束,只能查看";
  return {
    canStart: event.status === "due",
    canCancel: event.status === "due",
    canClose: event.status === "awaiting_closeout",
    instanceActions,
    nextStep,
  };
}

const READINESS_BLOCKER_HINT: Record<string, string> = {
  "platform.definition_bundle_id.not_ready": "正式两表定义包尚未冻结",
  "platform.definition_artifacts.not_ready": "已安装定义包与 manifest 尚未逐项一致",
  "workflow_policy.executable_file.not_ready": "可执行工作流政策文件缺失或与 manifest 不一致",
};

const MUTATION_CODE_HINT: Record<string, string> = {
  formal_assessment_not_ready: "正式量表尚未全链就绪,服务端已拒绝写入",
  assessment_workflow_policy_schedule_violation: "排期不在冻结政策允许的时点窗口内",
  assessment_workflow_policy_deferral_forbidden: "冻结政策要求管理员批准延期",
  assessment_workflow_policy_deferral_too_long: "延期超出冻结政策上限",
  assessment_workflow_policy_assessor_mismatch: "冻结政策要求被分配评估员本人执行",
  assessment_workflow_policy_binding_invalid: "可执行政策文件与 manifest 冻结事实不一致",
  assessment_workflow_policy_file_missing: "manifest 政策事实已冻结,但缺少可执行政策文件",
  assessment_artifact_not_authorized: "录音授权收据无效、过期或与当前条目/修订不符,请重新签发",
  assessment_artifact_already_bound: "该录音授权收据已绑定过其他作答",
  assessment_item_revision_conflict: "该条目已有更新的作答修订,请刷新后重试",
  assessment_patient_event_open: "该受试者还有未收尾的评估事件",
  assessment_definitions_not_ready: "正式量表定义包尚未安装",
  assessment_item_unknown: "条目不在冻结定义内",
};

export interface AssessmentMutationFailure {
  message: string;
  code?: string;
  hint?: string;
  blockingHints: string[];
}

type UnknownRecord = Record<string, unknown>;

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as UnknownRecord
    : null;
}

export function parseAssessmentMutationFailure(error: unknown): AssessmentMutationFailure {
  const errorRecord = record(error);
  const rawDetail = errorRecord && "detailData" in errorRecord
    ? errorRecord.detailData
    : errorRecord && "detail" in errorRecord
      ? errorRecord.detail
      : error;
  const detail = record(rawDetail) ?? record(record(rawDetail)?.detail) ?? null;
  const code = typeof detail?.code === "string" ? detail.code : undefined;
  const message = typeof detail?.message === "string" && detail.message.trim()
    ? detail.message.trim()
    : typeof errorRecord?.detail === "string" && errorRecord.detail.trim()
      ? errorRecord.detail.trim()
      : "服务器未接受该评估操作,请核对后重试。";
  const blockingHints: string[] = [];
  const blocking = detail?.blocking_codes;
  if (Array.isArray(blocking)) {
    for (const item of blocking) {
      if (typeof item === "string") {
        blockingHints.push(READINESS_BLOCKER_HINT[item] ?? item);
      }
    }
  }
  return {
    message,
    code,
    hint: code ? MUTATION_CODE_HINT[code] : undefined,
    blockingHints,
  };
}

export type AssessmentMutationOutcome =
  | { ok: true }
  | { ok: false; failure: AssessmentMutationFailure };

/** 只在服务器真的接受之后才交付回执；失败一律不碰调用方的草稿。
 *
 * 存在的理由是一个具体缺陷：原来保存作答走 `run(...).then(清理)`，而 run 把
 * 失败吞成 setFailure，于是 409/403/5xx/断网/回执解析失败也会走到 .then，
 * 把研究者刚敲进去的作答值、修订号和录音授权一起清掉。研究者当场看不出
 * 已经丢了什么，只能重填——而重填时那份录音授权已经作废。
 */
export async function performAssessmentMutation<T>(
  action: () => Promise<T>,
  onSuccess: (value: T) => void,
): Promise<AssessmentMutationOutcome> {
  let value: T;
  try {
    value = await action();
  } catch (error) {
    return { ok: false, failure: parseAssessmentMutationFailure(error) };
  }
  onSuccess(value);
  return { ok: true };
}

export interface ResponseInputResult {
  ok: boolean;
  value?: number;
  reason?: string;
}

export function parseResponseInput(raw: string): ResponseInputResult {
  const trimmed = raw.trim();
  if (!trimmed) return { ok: false, reason: "请输入数值作答" };
  const value = Number(trimmed);
  if (!Number.isFinite(value)) {
    return { ok: false, reason: "作答必须是有限数值" };
  }
  return { ok: true, value };
}

export function nextExpectedItemRevision(
  instance: AssessmentInstance, respondedRevisions: Record<string, number>,
  itemKey: string,
): number {
  // 服务端以 max(item_revision) 为准;本地只在同屏内累计,冲突交给
  // assessment_item_revision_conflict 如实回显后刷新。
  return respondedRevisions[`${instance.instance_id}:${itemKey}`] ?? 0;
}
