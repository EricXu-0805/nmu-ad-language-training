import assert from "node:assert/strict";
import test from "node:test";
import {
  afterReplyAction, nextQuestionArmDelayMs, roundLabel, speechDelayMs,
} from "./rapportRounds.ts";

const base = {
  autoReply: true, autoOpenHere: true, final: false, invitesMore: true,
  qIdx: 0, questionCount: 4,
};

test("afterReplyAction: 非末轮且这句在邀请老人接着说 → 续麦", () => {
  assert.equal(afterReplyAction(base), "rearm");
});

test("afterReplyAction: 末轮且本节还有问 → 自动换问", () => {
  assert.equal(afterReplyAction({ ...base, final: true }), "advance");
});

test("afterReplyAction: 收束句(不邀请续说)即使非末轮也不续麦,按本问聊完处理", () => {
  // j2「那我们聊点别的吧」/ k1「好的，谢谢您告诉我」之后开麦 = 自相矛盾的指令。
  assert.equal(afterReplyAction({ ...base, invitesMore: false }), "advance");
  assert.equal(
    afterReplyAction({ ...base, invitesMore: false, qIdx: 3 }), "section_done");
});

test("afterReplyAction: 末轮且已是本节最后一问 → 提示换节,不越节", () => {
  assert.equal(afterReplyAction({ ...base, final: true, qIdx: 3 }), "section_done");
});

test("afterReplyAction: 自动回应关掉或本节不开放 → 什么都不做", () => {
  assert.equal(afterReplyAction({ ...base, autoReply: false }), "none");
  assert.equal(afterReplyAction({ ...base, autoOpenHere: false, final: true }), "none");
});

test("speechDelayMs: 起播余量够盖住轮询+云合成,长句封顶,随字数增长", () => {
  assert.ok(speechDelayMs("好") >= 4000);
  assert.equal(speechDelayMs("好".repeat(100)), 16000);
  assert.ok(speechDelayMs("好".repeat(20)) > speechDelayMs("好".repeat(10)));
});

test("nextQuestionArmDelayMs: 按下一问真实长度估时,长问句给得更久", () => {
  const short = nextQuestionArmDelayMs("您平时喜欢做些什么呢？");
  const long = nextQuestionArmDelayMs("这里有没有您熟悉的朋友、同伴或者工作人员呢？");
  assert.ok(long > short);
  // 22 字那条光念就约 6.6 秒:旧的固定 7000 会在问句念完前开麦。
  assert.ok(long > 7000);
  assert.ok(nextQuestionArmDelayMs(null) >= 4000);
});

test("roundLabel", () => {
  assert.equal(roundLabel(1, 2), "第 1 / 2 轮");
});
