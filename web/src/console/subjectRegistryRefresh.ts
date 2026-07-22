import type { PatientSummary, PatientWithdrawalReceipt } from "../types";

export interface SubjectRegistryRefreshGate {
  begin: () => number;
  accepts: (ticket: number) => boolean;
  invalidate: () => void;
}

// 登记表可能同时存在首次加载、手动重试和撤回后的权威刷新。只有最新一代
// 请求可以提交 React 状态；unmount、页面切换或治理终态回执会主动废弃在途响应。
export function createSubjectRegistryRefreshGate(): SubjectRegistryRefreshGate {
  let generation = 0;

  return {
    begin() {
      generation += 1;
      return generation;
    },
    accepts(ticket) {
      return ticket === generation;
    },
    invalidate() {
      generation += 1;
    },
  };
}

// 撤回接口返回的回执本身就是受试者治理终态的权威事实。不要等下一次列表读取
// 才禁用训练安排；同时保留列表摘要中与撤回无关的字段。
export function applyWithdrawalReceiptToRegistry(
  rows: readonly PatientSummary[] | null,
  receipt: PatientWithdrawalReceipt,
): PatientSummary[] | null {
  if (rows === null) return null;
  return rows.map((row) => row.patient_id === receipt.patient_id
    ? {
        ...row,
        consent_status: receipt.consent_status,
        withdrawal_status: receipt.withdrawal_status,
        governance_revision: receipt.governance_revision,
        withdrawal_event_id: receipt.event_id,
        withdrawal_reason_code: receipt.reason_code,
        withdrawal_occurred_at: receipt.occurred_at,
        research_eligible: false,
        research_eligibility_issues: [
          ...(row.research_eligibility_issues ?? [])
            .filter((issue) => !issue.startsWith("withdrawal_status=")),
          `withdrawal_status=${receipt.withdrawal_status}`,
        ],
      }
    : row);
}
