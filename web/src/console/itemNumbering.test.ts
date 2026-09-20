import assert from "node:assert/strict";
import test from "node:test";
import { itemSeqLabel, itemSeqSummary, itemSeqText } from "./itemNumbering.ts";

test("冻结计划 32 题的结构:单要素 1–20 分 4 组每组 5 题,双要素 21–30,多要素 31–32", () => {
  for (let order = 1; order <= 20; order += 1) {
    assert.deepEqual(itemSeqLabel("单要素", order), {
      seq: order,
      typeSeq: order,
      group: Math.ceil(order / 5),
      groupSeq: ((order - 1) % 5) + 1,
    }, `single ${order}`);
  }
  assert.deepEqual(itemSeqLabel("单要素", 5), { seq: 5, typeSeq: 5, group: 1, groupSeq: 5 });
  assert.deepEqual(itemSeqLabel("单要素", 6), { seq: 6, typeSeq: 6, group: 2, groupSeq: 1 });
  assert.deepEqual(itemSeqLabel("单要素", 20), { seq: 20, typeSeq: 20, group: 4, groupSeq: 5 });
  for (let order = 21; order <= 30; order += 1) {
    assert.deepEqual(itemSeqLabel("双要素", order),
      { seq: order, typeSeq: order - 20, group: null, groupSeq: null }, `double ${order}`);
  }
  assert.deepEqual(itemSeqLabel("多要素", 31), { seq: 31, typeSeq: 1, group: null, groupSeq: null });
  assert.deepEqual(itemSeqLabel("多要素", 32), { seq: 32, typeSeq: 2, group: null, groupSeq: null });
});

test("序号不落在该类型的冻结区间、或类型不在训练三型里:只给总序号,不硬算类型内号", () => {
  assert.deepEqual(itemSeqLabel("双要素", 3), { seq: 3, typeSeq: null, group: null, groupSeq: null });
  assert.deepEqual(itemSeqLabel("单要素", 25), { seq: 25, typeSeq: null, group: null, groupSeq: null });
  assert.deepEqual(itemSeqLabel("多要素", 7), { seq: 7, typeSeq: null, group: null, groupSeq: null });
  assert.deepEqual(itemSeqLabel("关系建立", 1), { seq: 1, typeSeq: null, group: null, groupSeq: null });
  // 没有 presentation_order(人工面建的 ItemEvent 目前为 NULL)就没有题号,不从 item_id 编。
  assert.equal(itemSeqLabel("单要素", null), null);
  assert.equal(itemSeqLabel("单要素", undefined), null);
  assert.equal(itemSeqLabel("单要素", 0), null);
  assert.equal(itemSeqLabel("单要素", 2.5), null);
});

test("文案:主显示是类型内号(对纸质记录单),单要素带组号;摘要把总序号放前面", () => {
  const single = itemSeqLabel("单要素", 7)!;
  assert.equal(itemSeqText("单要素", single), "单要素第 7 题 · 第 2 组");
  assert.equal(itemSeqSummary("单要素", single), "第 7 题 · 单要素第 7 题 · 第 2 组");
  const double = itemSeqLabel("双要素", 23)!;
  assert.equal(itemSeqText("双要素", double), "双要素第 3 题");
  assert.equal(itemSeqSummary("双要素", double), "第 23 题 · 双要素第 3 题");
  const unmapped = itemSeqLabel("双要素", 3)!;
  assert.equal(itemSeqText("双要素", unmapped), "第 3 题");
  assert.equal(itemSeqSummary("双要素", unmapped), "第 3 题");
});
