// 题号只从冻结计划的 presentation_order 来(服务端 ItemEvent.presentation_order 与
// 研究数据集的 presentation_order 是同一个 1 基序号),绝不从 item_id 字符串编号——
// item_id 跨周复用(143 个里 73 个),字符串本身不带序。
// 钱凯的纸质记录单按任务类型内编号(单要素 1–20、双要素 1–10、多要素 1–2),训练
// 手册把单要素分 4 组每组 5 题;研究者对表看的是类型内号,总序号只作弱化后缀。

export interface ItemSeq {
  /** 冻结计划 1 基总序号(= presentation_order)。 */
  seq: number;
  /** 类型内序号;presentation_order 不落在该类型的冻结区间时为 null。 */
  typeSeq: number | null;
  /** 单要素专用:第几组(每组 5 题)。 */
  group: number | null;
  groupSeq: number | null;
}

const SINGLE_COUNT = 20;
const DOUBLE_COUNT = 10;
const GROUP_SIZE = 5;

export function itemSeqLabel(
  taskType: string,
  presentationOrder: number | null | undefined,
): ItemSeq | null {
  if (!Number.isSafeInteger(presentationOrder) || (presentationOrder as number) < 1) return null;
  const seq = presentationOrder as number;
  if (taskType === "单要素" && seq <= SINGLE_COUNT) {
    return {
      seq,
      typeSeq: seq,
      group: Math.ceil(seq / GROUP_SIZE),
      groupSeq: ((seq - 1) % GROUP_SIZE) + 1,
    };
  }
  if (taskType === "双要素" && seq > SINGLE_COUNT && seq <= SINGLE_COUNT + DOUBLE_COUNT) {
    return { seq, typeSeq: seq - SINGLE_COUNT, group: null, groupSeq: null };
  }
  if (taskType === "多要素" && seq > SINGLE_COUNT + DOUBLE_COUNT) {
    return { seq, typeSeq: seq - SINGLE_COUNT - DOUBLE_COUNT, group: null, groupSeq: null };
  }
  return { seq, typeSeq: null, group: null, groupSeq: null };
}

/** 主显示:类型内号(对纸质记录单);单要素带组号。类型内号不可证明时只给总序号。 */
export function itemSeqText(taskType: string, seq: ItemSeq): string {
  if (seq.typeSeq === null) return `第 ${seq.seq} 题`;
  const primary = `${taskType}第 ${seq.typeSeq} 题`;
  return seq.group === null ? primary : `${primary} · 第 ${seq.group} 组`;
}

/** 一行摘要:总序号在前、类型内号在后,给折叠标题/清单这类没有第二行的地方。 */
export function itemSeqSummary(taskType: string, seq: ItemSeq): string {
  return seq.typeSeq === null
    ? `第 ${seq.seq} 题`
    : `第 ${seq.seq} 题 · ${itemSeqText(taskType, seq)}`;
}
