import {
  WEEK2_SINGLE20_DEMO_PROFILE_VERSION,
} from "./autopilot/demoProfile.ts";
import type { PlanItem, PlanTurn, SessionPlan, TaskType } from "./types";

type UnknownRecord = Record<string, unknown>;

const PLAN_KEYS = [
  "item_bank_version_id", "week_no", "event_line",
  "autopilot_profile_version_id", "completion_scope",
  "resolved_position_count", "unsupported_position_count",
  "operational_autopilot_ready", "total_items", "total_turns", "items",
] as const;
const ITEM_KEYS = [
  "item_id", "task_type", "image_id", "presentation_order", "display", "turns",
] as const;
const TURN_KEYS = ["turn_seq", "response_role", "scoring_key"] as const;
const TASK_TYPES = new Set<TaskType>(["单要素", "双要素", "多要素", "关系建立"]);

function exactRecord(value: unknown, keys: readonly string[]): UnknownRecord | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as UnknownRecord;
  const actual = Object.keys(row);
  return actual.length === keys.length && keys.every((key) => Object.hasOwn(row, key))
    ? row
    : null;
}

function safeText(value: unknown, max = 128): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= max
    && value.trim() === value && !/[\p{Cc}\p{Cf}]/u.test(value);
}

function nonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

export function parseAccountSessionPlan(value: unknown): SessionPlan {
  const plan = exactRecord(value, PLAN_KEYS);
  if (!plan
      || !safeText(plan.item_bank_version_id)
      || !Number.isSafeInteger(plan.week_no) || (plan.week_no as number) < 1
      || (plan.week_no as number) > 8
      || !safeText(plan.event_line, 64)
      || !(plan.autopilot_profile_version_id === null
        || plan.autopilot_profile_version_id === WEEK2_SINGLE20_DEMO_PROFILE_VERSION)
      || (plan.completion_scope !== "canonical_full_source"
        && plan.completion_scope !== "demo_plan_only")
      || !nonNegativeInteger(plan.resolved_position_count)
      || !nonNegativeInteger(plan.unsupported_position_count)
      || typeof plan.operational_autopilot_ready !== "boolean"
      || !nonNegativeInteger(plan.total_items)
      || !nonNegativeInteger(plan.total_turns)
      || !Array.isArray(plan.items)) {
    throw new Error("训练计划响应不符合严格契约");
  }

  const seenItems = new Set<string>();
  const items: PlanItem[] = plan.items.map((rawItem) => {
    const item = exactRecord(rawItem, ITEM_KEYS);
    if (!item || !safeText(item.item_id) || seenItems.has(item.item_id)
        || typeof item.task_type !== "string" || !TASK_TYPES.has(item.task_type as TaskType)
        || !(item.image_id === null || safeText(item.image_id))
        || !Number.isSafeInteger(item.presentation_order)
        || (item.presentation_order as number) < 1
        || item.display === null || typeof item.display !== "object" || Array.isArray(item.display)
        || !Array.isArray(item.turns) || item.turns.length < 1) {
      throw new Error("训练计划题位不符合严格契约");
    }
    seenItems.add(item.item_id);
    const seenTurns = new Set<number>();
    const turns: PlanTurn[] = item.turns.map((rawTurn) => {
      const turn = exactRecord(rawTurn, TURN_KEYS);
      if (!turn || !Number.isSafeInteger(turn.turn_seq) || (turn.turn_seq as number) < 1
          || seenTurns.has(turn.turn_seq as number)
          || !safeText(turn.response_role, 64)
          || !(turn.scoring_key === null || safeText(turn.scoring_key))) {
        throw new Error("训练计划环节不符合严格契约");
      }
      seenTurns.add(turn.turn_seq as number);
      return {
        turn_seq: turn.turn_seq as number,
        response_role: turn.response_role,
        scoring_key: turn.scoring_key as string | null,
      };
    });
    return {
      item_id: item.item_id,
      task_type: item.task_type as TaskType,
      image_id: item.image_id as string | null,
      presentation_order: item.presentation_order as number,
      display: item.display as Record<string, unknown>,
      turns,
    };
  });

  const actualTurns = items.reduce((total, item) => total + item.turns.length, 0);
  const isDemo = plan.autopilot_profile_version_id === WEEK2_SINGLE20_DEMO_PROFILE_VERSION;
  const metadataCoherent = isDemo
    ? plan.completion_scope === "demo_plan_only"
      && plan.week_no === 2
      && plan.event_line === "正式训练"
      && plan.resolved_position_count === 20
      && plan.unsupported_position_count === 0
      && plan.operational_autopilot_ready === true
    : plan.completion_scope === "canonical_full_source"
      && plan.resolved_position_count === actualTurns
      && (!plan.operational_autopilot_ready || plan.unsupported_position_count === 0);
  if (plan.total_items !== items.length
      || plan.total_turns !== actualTurns
      || plan.resolved_position_count !== actualTurns
      || !metadataCoherent) {
    throw new Error("训练计划计数与运行范围不一致");
  }
  return {
    item_bank_version_id: plan.item_bank_version_id,
    week_no: plan.week_no as number,
    event_line: plan.event_line,
    autopilot_profile_version_id: plan.autopilot_profile_version_id,
    completion_scope: plan.completion_scope,
    resolved_position_count: plan.resolved_position_count,
    unsupported_position_count: plan.unsupported_position_count,
    operational_autopilot_ready: plan.operational_autopilot_ready,
    total_items: plan.total_items,
    total_turns: plan.total_turns,
    items,
  };
}
