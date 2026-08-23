import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import type { VisitPlanReceipt } from "../types.ts";
import { nextUnplannedWeek, plannedWeeksSummary } from "./visitPlanWeekDefaults.ts";

const plan = (week_no: number, status: VisitPlanReceipt["status"]): VisitPlanReceipt =>
  ({ week_no, status } as VisitPlanReceipt);

test("P1-2:没有任何安排(或还没加载出来)默认第 1 周,不再写死第 2 周", () => {
  assert.equal(nextUnplannedWeek(null), 1);
  assert.equal(nextUnplannedWeek([]), 1);
});

test("默认周次=下一个空缺:已排 1、2 → 3;跳过被取消的;全排满停在 8", () => {
  assert.equal(nextUnplannedWeek([plan(1, "started"), plan(2, "approved")]), 3);
  // 取消的安排不占周次。
  assert.equal(nextUnplannedWeek([plan(1, "cancelled")]), 1);
  // 中间空缺优先补(第 2 周被取消,第 1、3 周已排 → 排第 2 周)。
  assert.equal(nextUnplannedWeek([
    plan(1, "started"), plan(2, "cancelled"), plan(3, "approved"),
  ]), 2);
  assert.equal(nextUnplannedWeek(
    Array.from({ length: 8 }, (_, i) => plan(i + 1, "started"))), 8);
});

test("下拉旁的说明列出已开训/待进行周次", () => {
  assert.equal(plannedWeeksSummary(null), "正在读取该受试者的既有安排…");
  assert.equal(plannedWeeksSummary([]), "这位受试者还没有训练安排，通常从第 1 周开始");
  const summary = plannedWeeksSummary([
    plan(1, "started"), plan(2, "approved"), plan(3, "draft"), plan(4, "cancelled"),
  ]);
  assert.match(summary, /已开训：第 1 周/);
  assert.match(summary, /待进行：第 2、3 周/);
  assert.doesNotMatch(summary, /4/);
});

test("屏上接线:推导只在用户没动过周次时生效,周次输入会置 touched", () => {
  const screen = readFileSync(new URL("./VisitPlanCreateScreen.tsx", import.meta.url), "utf8");
  assert.match(screen, /if \(plans === null \|\| weekTouched\.current\) return;/);
  assert.match(screen, /weekTouched\.current = true;/);
  assert.match(screen, /hint=\{plannedWeeksSummary\(plans\)\}/);
  assert.doesNotMatch(screen, /useState\(2\)/);
});
