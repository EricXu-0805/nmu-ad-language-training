// 正式评估事件的创建入口（收据 187 的 U6）纯视图模型。
//
// 后端 POST /patients/{id}/assessment-events 与 api.createAssessmentEvent 早就
// 存在，缺的只是浏览器里的入口。这里只做三件事：本地挡住必然失败的请求、
// 管住幂等键的生命周期、把失败如实分成「服务器明确拒绝」和「结果未知」。
//
// **不复制任何门禁语义**：就绪判定、政策窗口、角色资格全部由服务端说了算，
// 本地拒绝仅限于"连请求都构造不出来"的情形。
import type { AssessmentTimepoint } from "../types";

export const ASSESSMENT_TIMEPOINTS: [AssessmentTimepoint, string][] = [
  ["pretest", "前测"],
  ["posttest", "后测"],
  ["followup", "随访"],
];

const TIMEPOINT_KEYS = new Set<string>(ASSESSMENT_TIMEPOINTS.map(([key]) => key));
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

export interface AssessmentCreateDraft {
  patientId: string;
  timepoint: string;
  scheduledDate: string;
}

export type AssessmentCreateDraftCheck =
  | { ok: true; timepoint: AssessmentTimepoint; scheduledDate: string; patientId: string }
  | { ok: false; reason: string };

export function checkAssessmentCreateDraft(
  draft: AssessmentCreateDraft,
): AssessmentCreateDraftCheck {
  const patientId = draft.patientId.trim();
  if (!patientId) return { ok: false, reason: "请先选择或填写受试者研究编号" };
  if (!TIMEPOINT_KEYS.has(draft.timepoint)) {
    return { ok: false, reason: "请选择评估时点(前测/后测/随访)" };
  }
  const scheduledDate = draft.scheduledDate.trim();
  if (!ISO_DATE.test(scheduledDate)) {
    return { ok: false, reason: "排期日期必须是 YYYY-MM-DD" };
  }
  // 只挡"根本不是一个日期"，不挡"哪天允许施测"——时点窗口是冻结政策的事，
  // 由服务端 assessment_workflow_policy_schedule_violation 如实拒绝。
  const parsed = new Date(`${scheduledDate}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())
      || parsed.toISOString().slice(0, 10) !== scheduledDate) {
    return { ok: false, reason: "排期日期不是一个真实存在的日期" };
  }
  return {
    ok: true,
    patientId,
    timepoint: draft.timepoint as AssessmentTimepoint,
    scheduledDate,
  };
}

export interface AssessmentCreateFailure {
  /** rejected = 服务器明确没建；unknown = 建没建不知道，绝不能当成没建。 */
  kind: "rejected" | "unknown";
  message: string;
  /** unknown 必须用同一个幂等键重试，否则可能建出第二个事件。 */
  retrySameKey: boolean;
}

function statusOf(error: unknown): number | null {
  return error !== null && typeof error === "object" && "status" in error
    && typeof (error as { status: unknown }).status === "number"
    ? (error as { status: number }).status
    : null;
}

function messageOf(error: unknown, fallback: string): string {
  if (error !== null && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail.trim();
  }
  return fallback;
}

export function classifyAssessmentCreateFailure(error: unknown): AssessmentCreateFailure {
  const status = statusOf(error);
  // status 0 是本地网络错误：请求可能已经到达服务器并成功建了事件。
  // 5xx / 408 / 429 同理——服务器可能已经落库再挂掉。这三类一律"未知"。
  if (status === null || status === 0 || status >= 500 || status === 408 || status === 429) {
    return {
      kind: "unknown",
      message: messageOf(
        error, "无法确认服务器是否已经建立该评估事件，请用同一个操作重试并核对队列"),
      retrySameKey: true,
    };
  }
  return {
    kind: "rejected",
    message: messageOf(error, "服务器没有接受这次创建，请核对后重试"),
    retrySameKey: false,
  };
}

export function newAssessmentIdempotencyKey(): string {
  return `assess-create-${crypto.randomUUID()}`;
}
