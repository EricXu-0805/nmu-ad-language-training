import type { Session } from "../types";

export type DataClassification = NonNullable<Session["data_classification"]>;
export type DataClassificationCounts = Record<DataClassification, number>;

export const DATA_CLASSIFICATIONS: readonly DataClassification[] = [
  "research",
  "simulation",
  "legacy_unknown",
] as const;

export const DATA_CLASSIFICATION_META = {
  research: {
    label: "真实研究",
    patientLabel: "真实研究档案",
    planLabel: "真实研究安排",
    sessionLabel: "真实研究场次",
    tone: "ok",
  },
  simulation: {
    label: "专用模拟",
    patientLabel: "专用模拟档案",
    planLabel: "模拟演练安排",
    sessionLabel: "模拟演练场次",
    tone: "warn",
  },
  legacy_unknown: {
    label: "历史/未知",
    patientLabel: "历史档案 · 分类未知",
    planLabel: "历史安排 · 分类未知",
    sessionLabel: "历史场次 · 分类未知",
    tone: "danger",
  },
} as const;

// 患者档案只信任服务端持久化的 is_simulation_subject。旧响应缺字段时
// 必须隔离到 legacy_unknown，绝不能因为 falsy 就猜成真实研究档案。
export function patientDataClassification(patient: { is_simulation_subject?: boolean }): DataClassification {
  if (patient.is_simulation_subject === true) return "simulation";
  if (patient.is_simulation_subject === false) return "research";
  return "legacy_unknown";
}

// 场次只信任服务端 data_classification。尤其禁止从旧 is_simulation、患者档案
// 或题库状态反推，否则历史数据会被静默混入真实研究或模拟数据。
export function sessionDataClassification(session: { data_classification?: string | null }): DataClassification {
  if (session.data_classification === "research" || session.data_classification === "simulation") {
    return session.data_classification;
  }
  return "legacy_unknown";
}

export function partitionByDataClassification<T>(
  rows: readonly T[],
  classify: (row: T) => DataClassification,
): Record<DataClassification, T[]> {
  const sections: Record<DataClassification, T[]> = {
    research: [],
    simulation: [],
    legacy_unknown: [],
  };
  for (const row of rows) sections[classify(row)].push(row);
  return sections;
}

export function dataClassificationCounts<T>(
  rows: readonly T[],
  classify: (row: T) => DataClassification,
): DataClassificationCounts {
  const sections = partitionByDataClassification(rows, classify);
  return {
    research: sections.research.length,
    simulation: sections.simulation.length,
    legacy_unknown: sections.legacy_unknown.length,
  };
}

export interface SessionCreationPolicy {
  allowed: boolean;
  isSimulation: boolean | null;
  reason: string | null;
}

export function sessionCreationPolicy(
  patientClassification: DataClassification | null,
  researchReady: boolean,
): SessionCreationPolicy {
  if (patientClassification == null) {
    return { allowed: false, isSimulation: null, reason: "正在核对档案类型" };
  }
  if (patientClassification === "legacy_unknown") {
    return {
      allowed: false,
      isSimulation: null,
      reason: "档案缺少可信的数据分类，必须先完成迁移和人工核对",
    };
  }
  if (patientClassification === "simulation") {
    return { allowed: true, isSimulation: true, reason: null };
  }
  if (!researchReady) {
    return {
      allowed: false,
      isSimulation: false,
      reason: "当前题库尚未通过真实研究质控，真实受试者不得开场",
    };
  }
  return { allowed: true, isSimulation: false, reason: null };
}

export function sessionMatchesPatientBoundary(
  patientClassification: DataClassification,
  sessionClassification: DataClassification,
): boolean {
  return patientClassification !== "legacy_unknown" && patientClassification === sessionClassification;
}
