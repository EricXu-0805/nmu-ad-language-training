import type { TaskType } from "../types";

export interface PatientPlanTurn {
  turn_seq: number;
  response_role: string;
}

export interface PatientPlanItem {
  item_ref: string;
  task_type: TaskType;
  presentation_order: number;
  turns: PatientPlanTurn[];
}

export interface PatientSessionPlan {
  item_bank_version_id: string;
  week_no: number;
  event_line: string;
  total_items: number;
  total_turns: number;
  items: PatientPlanItem[];
}

type UnknownRecord = Record<string, unknown>;
const PLAN_KEYS = [
  "item_bank_version_id", "week_no", "event_line", "total_items", "total_turns", "items",
] as const;
const ITEM_KEYS = ["item_ref", "task_type", "presentation_order", "turns"] as const;
const TURN_KEYS = ["turn_seq", "response_role"] as const;
const TASK_TYPES = ["单要素", "双要素", "多要素"] as const;
const ITEM_REF = /^itm-[0-9]{4}$/;

function exactRecord(value: unknown, keys: readonly string[]): UnknownRecord | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as UnknownRecord;
  const actual = Object.keys(row);
  if (actual.length !== keys.length || !keys.every((key) => Object.hasOwn(row, key))) return null;
  return row;
}

function safeText(value: unknown, max: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= max
    && !/[\p{Cc}\p{Cf}]/u.test(value);
}

function nonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

export function parsePatientSessionPlan(value: unknown): PatientSessionPlan {
  const plan = exactRecord(value, PLAN_KEYS);
  if (!plan || !safeText(plan.item_bank_version_id, 128)
      || !Number.isSafeInteger(plan.week_no) || (plan.week_no as number) < 1 || (plan.week_no as number) > 8
      || !safeText(plan.event_line, 64)
      || !nonNegativeInteger(plan.total_items) || !nonNegativeInteger(plan.total_turns)
      || !Array.isArray(plan.items)) {
    throw new Error("受试者计划响应结构无效");
  }

  const items: PatientPlanItem[] = plan.items.map((rawItem, itemIdx) => {
    const item = exactRecord(rawItem, ITEM_KEYS);
    const expectedRef = `itm-${String(itemIdx + 1).padStart(4, "0")}`;
    if (!item || typeof item.item_ref !== "string" || !ITEM_REF.test(item.item_ref)
        || item.item_ref !== expectedRef
        || !TASK_TYPES.some((type) => type === item.task_type)
        || !Number.isSafeInteger(item.presentation_order) || (item.presentation_order as number) < 1
        || !Array.isArray(item.turns) || item.turns.length < 1) {
      throw new Error("受试者计划题位结构无效");
    }
    const seenTurns = new Set<number>();
    const turns: PatientPlanTurn[] = item.turns.map((rawTurn) => {
      const turn = exactRecord(rawTurn, TURN_KEYS);
      if (!turn || !Number.isSafeInteger(turn.turn_seq) || (turn.turn_seq as number) < 1
          || seenTurns.has(turn.turn_seq as number)
          || !safeText(turn.response_role, 64)) {
        throw new Error("受试者计划环节结构无效");
      }
      seenTurns.add(turn.turn_seq as number);
      return { turn_seq: turn.turn_seq as number, response_role: turn.response_role };
    });
    return {
      item_ref: item.item_ref,
      task_type: item.task_type as TaskType,
      presentation_order: item.presentation_order as number,
      turns,
    };
  });

  const actualTurns = items.reduce((total, item) => total + item.turns.length, 0);
  if (plan.total_items !== items.length || plan.total_turns !== actualTurns) {
    throw new Error("受试者计划计数与题位不一致");
  }
  return {
    item_bank_version_id: plan.item_bank_version_id,
    week_no: plan.week_no as number,
    event_line: plan.event_line,
    total_items: plan.total_items,
    total_turns: plan.total_turns,
    items,
  };
}
