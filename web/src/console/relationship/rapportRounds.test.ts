import assert from "node:assert/strict";
import test from "node:test";
import {
  afterReplyAction, autoAdvanceTarget, nextQuestionArmDelayMs, roundLabel,
  shouldAutoArmOnEntry, speechDelayMs,
} from "./rapportRounds.ts";

const base = {
  autoMode: true, final: false, invitesMore: true,
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

test("afterReplyAction: 脚本回应句(姓名/年龄不进云)= 不邀请续说,答完直接换下一问", () => {
  // 服务端对 script 模式给 invitesMore=false、final=false(轮次按 auto 行数);
  // 自动带练不能因为 final=false 就等它「再聊一轮」——脚本句没有第二轮。
  assert.equal(afterReplyAction({ ...base, invitesMore: false, final: false, qIdx: 1,
    questionCount: 5 }), "advance");
});

test("afterReplyAction: 末轮且已是本节最后一问 → 本节完,交给换节判断", () => {
  assert.equal(afterReplyAction({ ...base, final: true, qIdx: 3 }), "section_done");
});

test("afterReplyAction: 自动带练关掉 → 什么都不做", () => {
  assert.equal(afterReplyAction({ ...base, autoMode: false }), "none");
  assert.equal(afterReplyAction({ ...base, autoMode: false, final: true }), "none");
});

const sections = [
  { speaker: "研究者", questionCount: 0 },   // 认识机器人
  { speaker: "机器人", questionCount: 5 },   // 自我介绍
  { speaker: "机器人", questionCount: 4 },   // 介绍机构环境
  { speaker: "机器人", questionCount: 0 },   // 道别
];

test("autoAdvanceTarget: 机器人节问完自动进下一机器人节,道别也算(只念一句告别)", () => {
  assert.equal(autoAdvanceTarget(sections, 1), 2);
  assert.equal(autoAdvanceTarget(sections, 2), 3);
});

test("autoAdvanceTarget: 最后一节之后没有目标;下一节要研究者当面说就停下等人", () => {
  assert.equal(autoAdvanceTarget(sections, 3), null);
  assert.equal(autoAdvanceTarget([
    { speaker: "机器人", questionCount: 2 }, { speaker: "研究者", questionCount: 0 },
  ], 0), null);
});

test("shouldAutoArmOnEntry: 自动带练下进到机器人节的任一问都自动开麦,研究者节/道别不开", () => {
  assert.equal(shouldAutoArmOnEntry({ autoMode: true, speaker: "机器人", questionCount: 5, qIdx: 0 }), true);
  assert.equal(shouldAutoArmOnEntry({ autoMode: true, speaker: "机器人", questionCount: 5, qIdx: 4 }), true);
  assert.equal(shouldAutoArmOnEntry({ autoMode: true, speaker: "机器人", questionCount: 0, qIdx: 0 }), false);
  assert.equal(shouldAutoArmOnEntry({ autoMode: true, speaker: "研究者", questionCount: 0, qIdx: 0 }), false);
  assert.equal(shouldAutoArmOnEntry({ autoMode: false, speaker: "机器人", questionCount: 5, qIdx: 0 }), false);
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
