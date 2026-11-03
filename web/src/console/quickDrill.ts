import type {
  Session,
  VisitPlanCreateRequest,
  VisitPlanMutationRequest,
  VisitPlanReceipt,
} from "../types";
import { WEEK2_SINGLE20_DEMO_PROFILE_VERSION } from "../autopilot/demoProfile.ts";

// 快速演练:把研究规范流程的「建档 → 创建安排 → 审核 → 原子开场」四步,对**模拟档案**折叠成一键。
// ★关键不变量:不新增、不绕过任何服务端门禁——依旧逐步调用 /visit-plans 的 create/approve/start,
// 每个场次照样进计划账本、照样受数据分区与服务端开场准入约束
// (server: visit_plan_operational_issues 放行第 2–8 周正式训练与第 1 周关系建立)。与规范多步流程的唯一区别是把
// 给操作端做流程测试的点击折叠成一次。仅供模拟演练;真实受试者永远走规范多步流程。

// 准入放行 2–8 周,演练有意固定第 2 周:唯一的模拟演练档案(week2-single20-demo)只为第 2 周定版。
const QUICK_DRILL_WEEK = 2;
const QUICK_DRILL_PHASE: VisitPlanCreateRequest["phase_type"] = "正式训练";
const QUICK_DRILL_EVENT: VisitPlanCreateRequest["event_line"] = "正式训练";

export const QUICK_DRILL_CONTEXT_LABEL = "第 2 周 · 正式训练";

// 客户端只用到这 4 个方法;抽成结构类型便于单测注入假实现,真实 `api` 天然满足。
export interface QuickDrillClient {
  createVisitPlan(body: VisitPlanCreateRequest): Promise<VisitPlanReceipt>;
  approveVisitPlan(planId: string, body: VisitPlanMutationRequest): Promise<VisitPlanReceipt>;
  startVisitPlan(approvedPlan: VisitPlanReceipt, body: VisitPlanMutationRequest): Promise<VisitPlanReceipt>;
  getStartedVisitSession(receipt: VisitPlanReceipt): Promise<Session>;
}

export function localToday(): string {
  // 服务端 _research_today 默认 Asia/Shanghai,研究现场同区,故本地日期即研究当天。
  // 只需保证 scheduled_date 不晚于研究当天(start_plan 仅拦「未到期」,允许迟开)。
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

// 对已建好的模拟档案原子编排 create → approve → start → 取场次。
// keySeed 让三条命令的幂等键成套稳定(可注入以便测试);默认每次调用一把新键。
// sittingNo:协议槽位含 sitting——同人再次演练必须用新 sitting,否则撞
// uq_visit_plan_protocol_slot_key(服务端 409);由调用方(nextTask 推导)给出。
export async function runQuickDrill(
  client: QuickDrillClient,
  patientId: string,
  keySeed: string = crypto.randomUUID(),
  sittingNo: number = 1,
): Promise<Session> {
  const created = await client.createVisitPlan({
    idempotency_key: `qd-create-${keySeed}`,
    patient_id: patientId,
    scheduled_date: localToday(),
    session_sitting_no: sittingNo,
    week_no: QUICK_DRILL_WEEK,
    phase_type: QUICK_DRILL_PHASE,
    event_line: QUICK_DRILL_EVENT,
    autopilot_profile_version_id: WEEK2_SINGLE20_DEMO_PROFILE_VERSION,
  });
  const approved = await client.approveVisitPlan(created.plan_id, {
    idempotency_key: `qd-approve-${keySeed}`,
    expected_revision: created.revision,
  });
  const started = await client.startVisitPlan(approved, {
    idempotency_key: `qd-start-${keySeed}`,
    expected_revision: approved.revision,
  });
  return client.getStartedVisitSession(started);
}
