import type { ScaleResult } from "../types";

export interface ScaleDrawerRefreshTicket {
  generation: number;
  patientId: string;
}

export interface ScaleDrawerRefreshGate {
  begin: (patientId: string) => ScaleDrawerRefreshTicket;
  accepts: (ticket: ScaleDrawerRefreshTicket, currentPatientId: string) => boolean;
  cancel: () => void;
}

// 同一抽屉可能因重试或切换受试者产生重叠请求。只有最新一代、且仍属于
// 当前受试者的响应才可以提交到 React 状态；关闭或 effect cleanup 会使在途代次失效。
export function createScaleDrawerRefreshGate(): ScaleDrawerRefreshGate {
  let currentGeneration = 0;

  return {
    begin(patientId) {
      currentGeneration += 1;
      return { generation: currentGeneration, patientId };
    },
    accepts(ticket, currentPatientId) {
      return ticket.generation === currentGeneration
        && ticket.patientId === currentPatientId;
    },
    cancel() {
      currentGeneration += 1;
    },
  };
}

// URL 中的受试者编号是这次读取的权威边界。服务端若返回任何其他编号（含缺失），
// 整批都拒绝展示，避免把跨受试者数据混入当前抽屉。
export function requireScaleRowsForPatient(
  rows: readonly ScaleResult[],
  patientId: string,
): ScaleResult[] {
  if (rows.some((row) => row.patient_id !== patientId)) {
    throw new Error("服务器返回了不属于当前受试者的量表记录，已阻止展示");
  }
  return [...rows];
}
