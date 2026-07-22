import type { AuditEntry } from "../types";

const AUDIT_ACTION_LABELS: Record<string, string> = {
  score_lock: "锁分",
  scale_record: "录量表",
  abnormal: "异常/介入",
  audio_delete: "删录音",
  data_export: "导出",
  login: "登录",
  visit_plan_create: "建立训练安排",
  visit_plan_approve: "审批训练安排",
  visit_plan_start: "启动训练安排",
  visit_plan_cancel: "取消训练安排",
};

const VISIT_PLAN_SUMMARIES: Record<string, string> = {
  visit_plan_create: "已建立训练安排",
  visit_plan_approve: "训练安排已审批",
  visit_plan_start: "训练安排已启动",
  visit_plan_cancel: "训练安排已取消",
};

const STATUS_LABELS: Record<string, string> = {
  draft: "待审批",
  approved: "已审批",
  started: "已启动",
  cancelled: "已取消",
};

function metadataValue(summary: string, key: string): string | null {
  const token = summary.split(/\s+/).find((part) => part.startsWith(`${key}=`));
  return token?.slice(key.length + 1) || null;
}

export function auditActionLabel(action: string): string {
  if (action.startsWith("visit_plan_") && !AUDIT_ACTION_LABELS[action]) {
    return "更新训练安排";
  }
  return AUDIT_ACTION_LABELS[action] ?? action;
}

/**
 * 将只读审计事实转为研究者界面文案。
 *
 * 旧账本的 visit-plan 摘要含 `plan=vp_...`；不能修改旧行（会破坏
 * 哈希链），因此这里对该类动作采用允许列表重建文案。解析失败时
 * 也只显示固定业务描述，绝不回退显示可能含后台 ID 的原始摘要。
 */
export function auditDisplaySummary(
  entry: Pick<AuditEntry, "action" | "summary">,
): string {
  if (!entry.action.startsWith("visit_plan_")) return entry.summary;

  const parts = [VISIT_PLAN_SUMMARIES[entry.action] ?? "训练安排已更新"];
  const week = metadataValue(entry.summary, "week");
  if (week && /^\d{1,2}$/.test(week) && Number(week) > 0) {
    parts.push(`第 ${Number(week)} 周`);
  }
  const date = metadataValue(entry.summary, "date");
  if (date && /^\d{4}-\d{2}-\d{2}$/.test(date)) {
    parts.push(date);
  }
  const status = metadataValue(entry.summary, "status");
  const actionAlreadyStatesStatus =
    (entry.action === "visit_plan_approve" && status === "approved")
    || (entry.action === "visit_plan_start" && status === "started")
    || (entry.action === "visit_plan_cancel" && status === "cancelled");
  if (status && STATUS_LABELS[status] && !actionAlreadyStatesStatus) {
    parts.push(STATUS_LABELS[status]);
  }
  return parts.join(" · ");
}
