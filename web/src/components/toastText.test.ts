import assert from "node:assert/strict";
import test from "node:test";
import { humanizeToastText } from "./toastText.ts";

test("P1-10:英文异常前缀被剥掉,包括嵌套前缀", () => {
  assert.equal(humanizeToastText("Error: 题库版本不一致，请刷新"), "题库版本不一致，请刷新");
  assert.equal(humanizeToastText("TypeError: 保存失败"), "保存失败");
  assert.equal(humanizeToastText("Error: TypeError: 保存失败"), "保存失败");
});

test("剥完仍是纯英文时补中文引导,不给用户裸英文", () => {
  assert.equal(
    humanizeToastText("TypeError: Failed to fetch"),
    "操作没有成功（Failed to fetch），请重试；反复出现请联系管理员");
  assert.equal(humanizeToastText("  Error:  "), "操作没有成功，请重试");
});

test("工程词对照表在出口处替换:严格契约/纪元/闭表/终态/降级/收口/门禁/终值/真值", () => {
  assert.equal(
    humanizeToastText("响应未通过严格契约校验"),
    "响应与本系统版本不匹配");
  assert.equal(humanizeToastText("质量看板正在切纪元"), "质量看板正在更新数据版本");
  assert.equal(humanizeToastText("权威原因(闭表)"), "权威原因(固定选项表)");
  assert.equal(
    humanizeToastText("档案将进入不可由普通接口恢复的 withdrawn 终态"),
    "档案将进入不可恢复的「已退出研究」的最终状态");
  assert.equal(humanizeToastText("TTS 已降级"), "TTS 已改用本机备用方案");
  assert.equal(humanizeToastText("床旁干预结束门禁未通过"), "床旁干预结束条件未满足");
  assert.equal(humanizeToastText("有 3 个环节缺终值记录"), "有 3 个环节缺最终记录");
  assert.equal(humanizeToastText("锁定研究真值"), "锁定研究评分");
});

test("普通人话原样通过,不被误伤", () => {
  for (const text of [
    "训练安排已保存并审核通过",
    "已收到老人端录音（13.3 秒），可在下方试听",
    "老人端未连接——请先在老人端完成配对",
  ]) {
    assert.equal(humanizeToastText(text), text);
  }
});
