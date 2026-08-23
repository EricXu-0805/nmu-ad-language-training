import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// 演示前文案卫生回归(2026-08 复审):同一功能一个名字、免责小字不吓人、
// 说明文字对 iPad 可见(不藏 title)、按钮引用与真实按钮名一致。
const autopilotControl = readFileSync(
  new URL("./scoring/ServerAutopilotControl.tsx", import.meta.url), "utf8");
const trainingConsole = readFileSync(
  new URL("./scoring/TrainingConsoleScreen.tsx", import.meta.url), "utf8");
const registry = readFileSync(
  new URL("./SubjectRegistryScreen.tsx", import.meta.url), "utf8");
const pinPrompt = readFileSync(
  new URL("../components/PinPrompt.tsx", import.meta.url), "utf8");
const editDrawer = readFileSync(
  new URL("./PatientEditDrawer.tsx", import.meta.url), "utf8");

test("自动带练面板只用一个功能名:不再出现「自动干预」「人工处置」", () => {
  assert.doesNotMatch(autopilotControl, /自动干预/);
  assert.doesNotMatch(autopilotControl, /人工处置/);
  assert.match(autopilotControl, /启动 AI 自动带练（演练）/);
  assert.match(autopilotControl, /转为人工操作/);
});

test("免责小字改为诚实但不吓人的措辞,并给出动作", () => {
  assert.doesNotMatch(autopilotControl, /原型版/);
  assert.doesNotMatch(autopilotControl, /临床终审/);
  assert.doesNotMatch(autopilotControl, /话术/);
  assert.match(autopilotControl, /训练引导语为研究初版，尚未经临床定稿；请按研究方案核对后使用。/);
});

test("启动拒因说人话,常驻拒因条引用真实按钮名", () => {
  assert.doesNotMatch(autopilotControl, /安全收麦尚未得到服务器确认/);
  assert.match(autopilotControl, /老人端麦克风还未确认关闭/);
  // 拒因条让人去点的按钮必须真实存在(按钮名是「启动 AI 自动带练」)。
  assert.match(autopilotControl, /处理后可重新点「启动 AI 自动带练」/);
  assert.doesNotMatch(autopilotControl, /重新点「启动」/);
});

test("线索缺文本的禁用原因不暴露内部分工,括号全角", () => {
  assert.doesNotMatch(trainingConsole, /内容组/);
  assert.doesNotMatch(trainingConsole, /暂缺题库文本\(/);
  assert.match(trainingConsole, /」的提示语暂未配置，本环节请口头提示/);
});

test("录音回报看门狗提示补上「停止太快」这个最常见成因", () => {
  assert.match(trainingConsole, /可能是停止太快（老人端还没开始录）/);
});

test("登记表配对码解释是可见文字,不藏在 iPad 点不出的 title 里", () => {
  assert.doesNotMatch(registry, /title="老人端一次输入/);
  assert.match(registry, /在受试者平板输入一次，之后每次训练自动连接/);
});

test("患者端配对弹框不用第三人称称呼老人,不用内部词「开场」", () => {
  assert.doesNotMatch(pinPrompt, /这位老人/);
  assert.doesNotMatch(pinPrompt, /开场/);
  assert.match(pinPrompt, /以后每次训练自动连接/);
});

test("编辑抽屉:同意状态单选项有解释;编号禁改时不再显示自相矛盾的 hint", () => {
  assert.match(editDrawer, /只能更正为「已同意」/);
  assert.match(editDrawer, /hint=\{canRename\s*\?\s*"留空表示不改/);
  assert.match(editDrawer, /已有训练数据，编号不能再改/);
});
