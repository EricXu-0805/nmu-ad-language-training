import type { StatusTone } from "../components/StatusPill";
import type {
  AssessmentCategoryKey,
  AssessmentEvent,
  AssessmentEventsToday,
  AssessmentInstanceStatus,
  AssessmentTimepoint,
} from "../types";

export type VisibleAssessmentQueueStatus = "due" | "in_progress" | "awaiting_closeout";

export type VisibleAssessmentQueueEvent = AssessmentEvent & {
  status: VisibleAssessmentQueueStatus;
};

export interface AssessmentQueueStatusPresentation {
  statusLabel: string;
  priorityLabel: string;
  nextStep: string;
  tone: StatusTone;
}

export type AssessmentQueueView =
  | { kind: "loading" }
  | { kind: "error"; detail: string }
  | { kind: "empty"; asOfDate: string }
  | {
    kind: "ready";
    asOfDate: string;
    events: VisibleAssessmentQueueEvent[];
  };

const STATUS_PRIORITY: Record<VisibleAssessmentQueueStatus, number> = {
  awaiting_closeout: 0,
  in_progress: 1,
  due: 2,
};

function isVisibleQueueEvent(event: AssessmentEvent): event is VisibleAssessmentQueueEvent {
  return event.status === "due"
    || event.status === "in_progress"
    || event.status === "awaiting_closeout";
}

// today 端点本身已按权限、撤回与负责人裁剪；浏览器再只保留未终结状态。
// 即使服务端误返 closed/cancelled，也不会被当成当日待办重新呈现。
export function visibleAssessmentQueueEvents(
  events: readonly AssessmentEvent[],
): VisibleAssessmentQueueEvent[] {
  return events
    .filter(isVisibleQueueEvent)
    .toSorted((left, right) => (
      STATUS_PRIORITY[left.status] - STATUS_PRIORITY[right.status]
      || left.scheduled_date.localeCompare(right.scheduled_date)
      || left.patient_id.localeCompare(right.patient_id)
      || left.event_id.localeCompare(right.event_id)
    ));
}

export function assessmentQueueView(
  today: AssessmentEventsToday | null,
  error: string | null,
): AssessmentQueueView {
  if (error !== null) return { kind: "error", detail: error };
  if (today === null) return { kind: "loading" };
  const events = visibleAssessmentQueueEvents(today.events);
  return events.length === 0
    ? { kind: "empty", asOfDate: today.as_of_date }
    : { kind: "ready", asOfDate: today.as_of_date, events };
}

export function assessmentQueueStatusPresentation(
  event: VisibleAssessmentQueueEvent,
  asOfDate: string,
): AssessmentQueueStatusPresentation {
  if (event.status === "awaiting_closeout") {
    return {
      statusLabel: "评估已完成·待受控收尾",
      priorityLabel: "最高优先",
      nextStep: "请先由获批的正式评估流程完成现场收尾；服务端签发可切换证明前，不要切换受试者或开始训练。",
      tone: "warn",
    };
  }
  if (event.status === "in_progress") {
    return {
      statusLabel: "正式评估进行中",
      priorityLabel: "优先继续",
      nextStep: "存在未终结的正式评估。请由获批的独立评估流程继续；本训练入口不能代替评估操作。",
      tone: "primary",
    };
  }
  const overdue = event.scheduled_date < asOfDate;
  return {
    statusLabel: overdue ? "正式评估逾期待开始" : "正式评估待开始",
    priorityLabel: overdue ? "逾期核对" : "按计划核对",
    nextStep: "仅核对服务端排班。真实定义、授权制品与工作流政策完成批准前，不得在床旁执行或录入。",
    tone: overdue ? "warn" : "muted",
  };
}

export function assessmentTimepointLabel(timepoint: AssessmentTimepoint): string {
  if (timepoint === "pretest") return "前测";
  if (timepoint === "posttest") return "后测";
  return "随访";
}

export function assessmentCategoryLabel(category: AssessmentCategoryKey): string {
  return category === "untrained_standardized_naming"
    ? "未训练标准化命名"
    : "功能性沟通";
}

export function assessmentInstanceStatusLabel(
  status: AssessmentInstanceStatus,
  itemResponseCount: number,
  requiredItemCount: number,
): string {
  if (status === "completed") return "服务端已生成计分证据";
  if (status === "approved_deferred") return "已审批延期";
  if (status === "in_progress") {
    return `进行中·已记录 ${itemResponseCount}/${requiredItemCount} 项`;
  }
  return "待开始";
}
