import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import type { VisitPlanReceipt } from "../types.ts";
import { nextSittingNoForSlot, nextUnplannedWeek, occupiedSittingsForSlot, plannedWeeksSummary } from "./visitPlanWeekDefaults.ts";

const plan = (
  week_no: number,
  status: VisitPlanReceipt["status"],
  session_sitting_no = 1,
  phase_type = "正式训练",
  event_line = "正式训练",
): VisitPlanReceipt =>
  ({ week_no, status, session_sitting_no, phase_type, event_line } as VisitPlanReceipt);

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

test("同槽再训:序号推导按(周次+阶段+任务线)全键过滤,跨槽独立", () => {
  assert.equal(nextSittingNoForSlot(null, 2, "正式训练", "正式训练"), 1);
  assert.equal(nextSittingNoForSlot([], 2, "正式训练", "正式训练"), 1);
  // 该槽已开训(如场次中止后 plan 停在 started)→ 指到第 2 次。
  assert.equal(nextSittingNoForSlot([plan(2, "started")], 2, "正式训练", "正式训练"), 2);
  // 取消的安排不占序号;别的周不影响本周。
  assert.equal(nextSittingNoForSlot(
    [plan(2, "cancelled"), plan(3, "started")], 2, "正式训练", "正式训练"), 1);
  // 同槽已有第 1、2 次 → 第 3 次;draft/approved 同样占号。
  assert.equal(nextSittingNoForSlot([
    plan(2, "started", 1), plan(2, "approved", 2),
  ], 2, "正式训练", "正式训练"), 3);
  // ★第 1 周三个环节共用 sitting=1 是合法共存:另一环节的安排绝不推高本环节序号。
  assert.equal(nextSittingNoForSlot(
    [plan(1, "started", 1, "关系建立", "关系建立环节")],
    1, "基线测评", "基线测评窗"), 1);
  assert.equal(nextSittingNoForSlot(
    [plan(1, "started", 1, "关系建立", "关系建立环节")],
    1, "关系建立", "关系建立环节"), 2);
  // 占用清单同键同理:只列本槽,去重升序。
  assert.deepEqual(occupiedSittingsForSlot(
    [plan(2, "started", 2), plan(2, "approved", 1), plan(2, "cancelled", 3),
      plan(1, "started", 1, "关系建立", "关系建立环节")],
    2, "正式训练", "正式训练"), [1, 2]);
});

test("屏上接线:序号跟槽位推导、手动优先,已开训冲突的 409 指向序号而不是死胡同", () => {
  const screen = readFileSync(new URL("./VisitPlanCreateScreen.tsx", import.meta.url), "utf8");
  // 推导必须带全槽位键,否则第 1 周另一环节的安排会把序号误推成 2。
  assert.match(screen, /setSittingNo\(nextSittingNoForSlot\(plans, weekNo, slotPhase, eventLineFor\(weekNo, slotPhase\)\)\)/);
  assert.match(screen, /sittingTouched\.current = true;/);
  // 已开训占槽时,toast 必须指引改「同周第几次训练」——started 的安排既不能审核
  // 也不能取消,旧文案「先审核或取消」对它是死胡同,不得回归为唯一建议。
  assert.match(screen, /周第 \$\{sittingNo\} 次已开训，不能重复创建/);
  // 冲突方查找必须按全槽位键匹配,第 1 周不同环节的同序号安排不是冲突方。
  assert.match(screen, /plan\.phase_type === slotPhase && plan\.event_line === slotLine/);
  // 序号提示只说实查到的占用,不按输入框数值编造「该周已有第 N-1 次」。
  assert.match(screen, /occupiedSittingsForSlot\(plans, weekNo, slotPhase, eventLineFor\(weekNo, slotPhase\)\)/);
  assert.doesNotMatch(screen, /该周已有第 \$\{sittingNo - 1\} 次/);
});
