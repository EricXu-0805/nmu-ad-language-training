import type { Session, VisitPlanReceipt, VisitPlanMutationRequest } from "../types";
import { localToday, runQuickDrill, type QuickDrillClient } from "./quickDrill.ts";

// 「保存并开始下一项任务」的系统推导:不发明新状态机、不绕过任何服务端门禁,
// 只在既有事实上按安全优先级挑一条已经合法的路:
//   ① 该受试者还有未收口的场次 → 回原场(服务端 assert_patient_ready_for_new_work
//      本来就禁止另开新工作,推导只是把这个事实提前呈现);
//   ② 已审核且到期的可执行安排 → 直接开场;
//   ③ 到期草稿(仅当前唯一可执行垂直) → 审核后开场;
//   ④ 都没有 → 建新安排(create→approve→start 逐步走账本;协议槽位含 sitting,
//      同人再次训练自动用下一个 sitting,不复用已占槽位)。
// 任何一步被服务端拒绝(内容未冻结/未授权/探针过期…)都原样抛给调用方展示,
// 不静默换路。当前唯一可开场垂直=第2周正式训练;真实研究档案会在审核关被
// ready_for_research 门禁如实拦下。

export interface NextTaskClient extends QuickDrillClient {
  listPatientVisitPlans(patientId: string): Promise<VisitPlanReceipt[]>;
  patientSessions(patientId: string): Promise<Session[]>;
}

export type NextTaskOutcome =
  | { kind: "resumed"; session: Session }
  | { kind: "started"; session: Session; via: "approved_plan" | "draft_plan" | "new_plan" }
  | { kind: "blocked"; reason: string };

export interface NextTaskOptions {
  // 仅模拟档案可折叠「审核+新建」(D2 拍板:一键只给模拟;真实受试者的研究审批
  // 是人的决策点,永远走规范多步流程,这里只如实指路,不代批)。
  autoApproveAndCreate: boolean;
}

// 收口前禁止另开新工作的运行态(与服务端 patient-ready 栅栏同口径)。
const OPEN_RUNTIME_STATUSES = new Set(["active", "paused", "intervention_completed"]);

function inRunnableVertical(plan: VisitPlanReceipt): boolean {
  return plan.week_no === 2
    && plan.phase_type === "正式训练"
    && plan.event_line === "正式训练";
}

export function pickOpenSession(sessions: Session[]): Session | null {
  return sessions.find((s) =>
    typeof s.runtime_status === "string"
    && OPEN_RUNTIME_STATUSES.has(s.runtime_status)) ?? null;
}

export function nextSittingNo(plans: VisitPlanReceipt[]): number {
  let highest = 0;
  for (const plan of plans) {
    if (plan.status !== "cancelled" && inRunnableVertical(plan)) {
      highest = Math.max(highest, plan.session_sitting_no);
    }
  }
  return highest + 1;
}

function mutation(prefix: string, seed: string, revision: number): VisitPlanMutationRequest {
  return { idempotency_key: `${prefix}-${seed}`, expected_revision: revision };
}

export async function runNextTask(
  client: NextTaskClient,
  patientId: string,
  options: NextTaskOptions,
  keySeed: string = crypto.randomUUID(),
): Promise<NextTaskOutcome> {
  const open = pickOpenSession(await client.patientSessions(patientId));
  if (open) {
    return { kind: "resumed", session: open };
  }
  const plans = await client.listPatientVisitPlans(patientId);
  const today = localToday();
  const dueApproved = plans.find((p) =>
    p.status === "approved" && inRunnableVertical(p) && p.scheduled_date <= today);
  if (dueApproved) {
    const started = await client.startVisitPlan(
      dueApproved, mutation("nt-start", keySeed, dueApproved.revision));
    return {
      kind: "started", via: "approved_plan",
      session: await client.getStartedVisitSession(started),
    };
  }
  const dueDraft = plans.find((p) =>
    p.status === "draft" && inRunnableVertical(p) && p.scheduled_date <= today);
  if (!options.autoApproveAndCreate) {
    return {
      kind: "blocked",
      reason: dueDraft
        ? "下一项任务已有草稿安排，需先在训练安排页人工审核通过后再开场"
        : "没有已审核且到期的训练安排；请先创建并审核训练安排",
    };
  }
  if (dueDraft) {
    const approved = await client.approveVisitPlan(
      dueDraft.plan_id, mutation("nt-approve", keySeed, dueDraft.revision));
    const started = await client.startVisitPlan(
      approved, mutation("nt-start", keySeed, approved.revision));
    return {
      kind: "started", via: "draft_plan",
      session: await client.getStartedVisitSession(started),
    };
  }
  const session = await runQuickDrill(
    client, patientId, keySeed, nextSittingNo(plans));
  return { kind: "started", via: "new_plan", session };
}
