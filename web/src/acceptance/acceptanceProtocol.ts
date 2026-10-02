// 真机验收的**记录仪**，不是判定器。
//
// 十三项验收今天靠一张 Markdown 表在纸上打勾（docs/handover/真机验收记录表.md）。
// 纸的问题不是不能用，是回执会丢：谁在哪台机器、哪个浏览器、哪个构建编号上
// 验的第几项，事后没人说得清；而"沿用旧结果"恰恰是这张表明令禁止的事。
//
// 所以这一层只做三件事：把十三项摊成可勾选的结构、自动记下机器说得出的事实
// （UA、构建编号、屏幕、时间，以及点「采集设备参数」时浏览器报得出的那些）、
// 产出一份能归档的 JSON 回执。
//
// 回执只冻结一次：记录编号在草稿建好时就定下，冻结那一刻把整份回执序列化成一串
// 字节，下载、复制拿到的都是这串字节。之后要改只能解冻换新编号——同一个编号要是
// 对应两份不同内容，事后就分不清哪份算数。
//
// 它**永远不会**替人判"通过"：任何一项没勾、或表头没填全，整份回执就是未完成。
// 全部勾通过也只出"现场记录齐了"，判定仍然是负责人的事。

import {
  formatDeviceFactsLines,
  isDeviceFactsShape,
  type AcceptanceDeviceFacts,
} from "./acceptanceDeviceFacts.ts";

export type ItemOutcome = "pass" | "fail" | "unset";

export interface AcceptanceStep {
  key: string;
  title: string;
  how: string;
  criterion: string;
}

export interface AcceptanceItem {
  no: number;
  title: string;
  steps: AcceptanceStep[];
}

/** 与 docs/handover/真机验收记录表.md 的十三项逐条对应。改这里要同步改那份表。 */
export const ACCEPTANCE_ITEMS: readonly AcceptanceItem[] = Object.freeze([
  {
    no: 1,
    // 开了提前回答检测的设备,播报中系统麦克风指示本来就会亮(只在本机分析、不录音),
    // 所以不能再拿「麦克风开了又关又开」当判据,看的是「正在听您说」与录音本身。
    title: "声音真正结束后才开始录音，「正在听您说」只出现一次",
    steps: [{
      key: "1",
      title: "正常题目走一轮",
      how: "让 AI 读完问句，看屏上「正在听您说」什么时候出现。播报中系统状态栏的麦克风指示亮着属正常：提前回答检测在本机听，只做分析。",
      criterion: "朗读结束 → 才出现「正在听您说」，这一题「正在听您说」只出现一次；播报中系统麦克风指示可以亮，但不保存声音（事后核对这一题只有一段录音，从朗读结束之后开始）。",
    }],
  },
  {
    no: 2,
    title: "没有文字回答：两层提示后告知答案，不再开第四次麦",
    steps: [{
      key: "2",
      title: "麦克风开着但不说话，连续三轮",
      how: "全程沉默，不要碰任何按钮。",
      criterion: "三次之后系统告知答案并停止，不再有第四次收音。",
    }],
  },
  {
    no: 3,
    title: "「再说一遍」：同一轮第一次重播、第二次暂停",
    steps: [{
      key: "3",
      title: "在同一提示层级里说两次「没听清」",
      how: "同一层级内连说两次，然后进入下一提示层级再试一次。",
      criterion: "第一次重播、第二次暂停；进入下一层级后重新按新一轮计算。",
    }],
  },
  {
    no: 4,
    title: "提示后答对：反馈播完并经服务器确认，才进入下一题",
    steps: [{
      key: "4",
      title: "在一级或二级提示后说出目标词",
      how: "答对之后盯住画面，看反馈和换题的先后。",
      criterion: "先在当前题的画面上听到正确反馈，播完之后才换下一题；不出现「看着橘子听到苹果」。",
    }],
  },
  {
    no: 5,
    title: "各种失败：全部关麦并停住，绝不自行恢复",
    steps: [
      {
        key: "5a", title: "拒绝麦克风权限",
        how: "浏览器弹权限时点「拒绝」。",
        criterion: "立刻停住并给出可读提示，不重试。",
      },
      {
        key: "5b", title: "拔掉或禁用麦克风",
        how: "收音进行中拔掉外接麦。",
        criterion: "立刻关麦并停住。",
      },
      {
        key: "5c", title: "播放失败",
        how: "把设备静音，或让别的程序占用音频。",
        criterion: "停住并提示，不跳过题目。",
      },
      {
        key: "5d", title: "上传失败",
        how: "断网之后说话。",
        criterion: "本机立即关麦并停止继续；能上报就由服务器暂停，不能确认就要求联系负责人。",
      },
      {
        key: "5e", title: "断网",
        how: "直接关掉 Wi-Fi。",
        criterion: "同上，且不会假装服务器已收到。",
      },
    ],
  },
  {
    no: 6,
    title: "重复操作：只有一个声音、一个麦克风",
    steps: [{
      key: "6",
      title: "快速双击开始 / 同时开两个同源页面",
      how: "把「开始」快速点两下；再开一个同源页面；让轮询重叠。",
      criterion: "全局只有一个声音在放、一个麦克风在用；服务器回执没有重复条目。",
    }],
  },
  {
    no: 7,
    title: "暂停 / 切后台 / 刷新 / 断网：立即物理关麦，恢复后不自行开麦",
    steps: [
      { key: "7a", title: "收音中按暂停", how: "老人端按「暂停练习」。", criterion: "立刻关麦；恢复后不会自己重新开麦。" },
      { key: "7b", title: "收音中切到别的应用", how: "切到别的 App 再切回来。", criterion: "同上。" },
      { key: "7c", title: "收音中刷新页面", how: "直接刷新。", criterion: "同上。" },
      { key: "7d", title: "收音中断网", how: "关 Wi-Fi 再开。", criterion: "同上。" },
    ],
  },
  {
    no: 8,
    title: "全程合成：核对服务器回执没有重复",
    steps: [{
      key: "8",
      title: "由负责人核对当次的服务器记录",
      how: "全程只用虚构受试者与合成语音；结束后逐条核对。",
      criterion: "每一步只有一条记录，没有重复计数。",
    }],
  },
  {
    no: 9,
    title: "现在回答",
    steps: [{
      key: "9",
      title: "提问播到一半点「现在回答」",
      how: "AI 读问句或提示读到一半时，点屏上的「现在回答」。",
      criterion: "朗读立刻停；屏上先说「朗读已停止，正在准备麦克风」，再出现「正在听您说」；这时回答，整句都在录音里并照常判分。反馈句、报答案句播报时没有这个按钮。",
    }],
  },
  {
    no: 10,
    title: "停播误停与漏停",
    steps: [
      {
        key: "10a", title: "播报中老人直接开口（漏停）",
        how: "AI 读问句时老人直接说出答案。先看播报时屏上写的是「想回答时可以开口」（这台设备在检测）还是「请听完再回答」（没在检测，本步只记这一点）。",
        criterion: "在检测时朗读停住，屏上提示看到「正在听您说」后从头回答；老人看到之后完整重说一遍，整句都在录音里。记下开口后朗读多久才停、有没有说了却没停。",
      },
      {
        key: "10b", title: "播报中只有外放声音或旁人说话（误停）",
        how: "播报中老人不说话；分别把平板音量调大、请旁边的人小声说话。",
        criterion: "朗读不应停下；停了就记下当时的声音来源。外放回声停下后只接着放同一句，不会多开一次录音。",
      },
    ],
  },
  {
    no: 11,
    title: "找词停顿不被静音收麦截断",
    steps: [{
      key: "11",
      title: "回答中间停下来想词",
      how: "回答时先说一两个字，停 3–4 秒想词，再说完整答案（例如「这个……（停 4 秒）……是锚」）。另做一次：被提前回答检测停播之后重说，重说前也停一下。",
      criterion: "停顿期间不收麦，整句答案都在录音里；说完之后才收麦（自动收麦约等 3 秒；服务器关了静音收麦时只在点「说完了可以点这里」或作答时间到点时收）。停播之后那次重说只靠按钮或作答时间收麦。记下停了多久、有没有被提前收麦。",
    }],
  },
  {
    no: 12,
    title: "老人已答对 / 跳过本题",
    steps: [
      {
        key: "12a", title: "AI 暂停后点「老人已答对」",
        how: "让 AI 在一题中途安全暂停（老人端点「暂停练习」），研究者在训练台点「老人已答对」，选原因，可写备注。",
        criterion: "确认框写清第几题、第几环节；保存后 AI 从下一个环节或下一题继续；AI 自己的判定原样保留，裁定连同账号名和原因一起记录。",
      },
      {
        key: "12b", title: "AI 暂停后点「跳过本题」",
        how: "同样在暂停时点「跳过本题」，选原因。",
        criterion: "本题剩下的环节记为跳过，不凭空生成回答或录音；AI 从下一题继续；导出里这一题的题号与跳过原因对得上。",
      },
    ],
  },
  {
    no: 13,
    title: "指定起点（含中止后同周第二场从第 N 题开始）",
    steps: [
      {
        key: "13a", title: "新场次从第 N 题开始",
        how: "在训练台「本场从哪一题开始」选第 N 题，填「跳过前面题目的原因」，点「确认从第 N 题开始」。",
        criterion: "AI 从第 N 题开始；前面的题只留跳过记录，不生成回答或录音；屏上题号与导出里的总题号一一对应。",
      },
      {
        key: "13b", title: "中止后同周第二场从第 N 题开始",
        how: "把一场中止，同一周再开第二场，从上一场停下的第 N 题开始。",
        criterion: "第二场能从第 N 题开始，不被第一场的记录卡住；两场的录音、题号与导出各自对得上，没有重复计分。",
      },
    ],
  },
]);

export const ACCEPTANCE_STEP_KEYS: readonly string[] = Object.freeze(
  ACCEPTANCE_ITEMS.flatMap((item) => item.steps.map((step) => step.key)),
);

export interface AcceptanceHeaderField {
  key: string;
  label: string;
  placeholder: string;
}

/** 这些字段机器猜不出来，必须现场的人填。少一个都不算完成。 */
export const ACCEPTANCE_HEADER_FIELDS: readonly AcceptanceHeaderField[] = Object.freeze([
  { key: "device", label: "设备型号", placeholder: "例：iPad 第 10 代 / 联想小新 Pro 14" },
  // 浏览器认不出是哪一台平板：同型号的几台只能靠贴纸上的编号分开。
  { key: "assetTag", label: "设备资产编号", placeholder: "平板背面贴纸上的编号；没有贴纸就填「无」" },
  { key: "os", label: "操作系统与版本", placeholder: "例：iPadOS 18.5" },
  { key: "browser", label: "浏览器与版本", placeholder: "例：Safari 18.5（要人工填，自动记录不能代替）" },
  { key: "microphone", label: "麦克风（内置/外接型号）", placeholder: "例：内置 / 得胜 PCM-390" },
  { key: "speaker", label: "扬声器（内置/外接型号）", placeholder: "例：内置 / 漫步者 R1000" },
  { key: "room", label: "房间与网络", placeholder: "例：三楼活动室，Wi-Fi「YLY-5G」，有轻微回声" },
  { key: "acceptor", label: "验收人", placeholder: "填真名，不要填账号" },
  { key: "date", label: "验收日期", placeholder: "例：2026-08-20" },
]);

export interface MachineFacts {
  userAgent: string;
  buildId: string;
  screen: string;
  finishedAtIso: string;
}

export interface AcceptanceDraft {
  /** 草稿一建好就定下；冻结、下载都认这个编号。 */
  receiptId: string;
  /** 解冻改正出来的新记录指向被它取代的那份；第一次填写为 null。 */
  supersedesReceiptId: string | null;
  startedAtIso: string;
  header: Record<string, string>;
  outcomes: Record<string, ItemOutcome>;
  notes: Record<string, string>;
  /** 点「采集设备参数」才有；没点为 null。 */
  deviceFacts: AcceptanceDeviceFacts | null;
}

export type AcceptanceVerdict =
  | "incomplete-header"
  | "incomplete-steps"
  | "recorded-with-failures"
  | "recorded-all-pass";

export const VERDICT_LABEL: Record<AcceptanceVerdict, string> = {
  "incomplete-header": "未完成：表头还有没填的，这份记录不能归档",
  "incomplete-steps": "未完成：还有没勾的项，这份记录不能归档",
  "recorded-with-failures": "已记录，有未通过项——这台设备不得投入使用",
  "recorded-all-pass": "现场记录已齐（十三项全部勾了通过）",
};

export const ACCEPTANCE_RECEIPT_SCHEMA = "nmu-device-acceptance.v2";

const RECEIPT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function isReceiptId(value: unknown): value is string {
  return typeof value === "string" && RECEIPT_ID.test(value);
}

interface ReceiptIdRandomSource {
  randomUUID?(): string;
  getRandomValues<T extends Uint8Array<ArrayBuffer>>(bytes: T): T;
}

export function newReceiptId(source: ReceiptIdRandomSource = globalThis.crypto): string {
  if (typeof source.randomUUID === "function") return source.randomUUID();
  // randomUUID 只在 https 页面里有；平板用局域网 http 地址打开时退到 getRandomValues 拼 v4。
  const bytes = source.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** 屏上和文件名里用的 8 位短编号；完整编号写在回执里。 */
export function shortReceiptId(receiptId: string): string {
  return receiptId.slice(0, 8);
}

export function emptyDraft(
  receiptId: string = newReceiptId(), startedAtIso: string = new Date().toISOString(),
): AcceptanceDraft {
  return {
    receiptId,
    supersedesReceiptId: null,
    startedAtIso,
    header: Object.fromEntries(ACCEPTANCE_HEADER_FIELDS.map((f) => [f.key, ""])),
    outcomes: Object.fromEntries(ACCEPTANCE_STEP_KEYS.map((key) => [key, "unset" as ItemOutcome])),
    notes: {},
    deviceFacts: null,
  };
}

export function missingHeaderFields(draft: AcceptanceDraft): string[] {
  return ACCEPTANCE_HEADER_FIELDS
    .filter((field) => !(draft.header[field.key] ?? "").trim())
    .map((field) => field.label);
}

export function unsetSteps(draft: AcceptanceDraft): string[] {
  return ACCEPTANCE_STEP_KEYS.filter((key) => (draft.outcomes[key] ?? "unset") !== "pass"
    && (draft.outcomes[key] ?? "unset") !== "fail");
}

export function failedSteps(draft: AcceptanceDraft): string[] {
  return ACCEPTANCE_STEP_KEYS.filter((key) => draft.outcomes[key] === "fail");
}

export function summarizeAcceptance(draft: AcceptanceDraft): AcceptanceVerdict {
  // 表头先判：设备/浏览器没记清的记录，勾得再全也没法归档到设备矩阵里。
  if (missingHeaderFields(draft).length > 0) return "incomplete-header";
  if (unsetSteps(draft).length > 0) return "incomplete-steps";
  return failedSteps(draft).length > 0 ? "recorded-with-failures" : "recorded-all-pass";
}

export interface AcceptanceReceipt {
  schema: typeof ACCEPTANCE_RECEIPT_SCHEMA;
  receiptId: string;
  supersedesReceiptId: string | null;
  verdict: AcceptanceVerdict;
  startedAtIso: string;
  machine: MachineFacts;
  deviceFacts: AcceptanceDeviceFacts | null;
  header: Record<string, string>;
  steps: { key: string; item: number; title: string; outcome: ItemOutcome; note: string }[];
  caveat: string;
}

const CAVEAT = "这份文件只是现场记录，不构成任何批准。十三项全部通过也不等于这台设备"
  + "可以在养老院投入使用——要由具名负责人另行判定。任何一项未通过或未勾，"
  + "修复后必须重新验收该项，不得沿用本次结果。";

export function buildAcceptanceReceipt(
  draft: AcceptanceDraft, machine: MachineFacts,
): AcceptanceReceipt {
  const steps = ACCEPTANCE_ITEMS.flatMap((item) => item.steps.map((step) => ({
    key: step.key,
    item: item.no,
    title: step.title,
    outcome: draft.outcomes[step.key] ?? "unset",
    note: (draft.notes[step.key] ?? "").trim(),
  })));
  return {
    schema: ACCEPTANCE_RECEIPT_SCHEMA,
    receiptId: draft.receiptId,
    supersedesReceiptId: draft.supersedesReceiptId,
    verdict: summarizeAcceptance(draft),
    startedAtIso: draft.startedAtIso,
    // 逐键重建：冻结出来的字节只由内容决定，不随调用方对象的键序变。
    machine: {
      userAgent: machine.userAgent,
      buildId: machine.buildId,
      screen: machine.screen,
      finishedAtIso: machine.finishedAtIso,
    },
    deviceFacts: draft.deviceFacts,
    header: Object.fromEntries(
      ACCEPTANCE_HEADER_FIELDS.map((f) => [f.key, (draft.header[f.key] ?? "").trim()]),
    ),
    steps,
    caveat: CAVEAT,
  };
}

const OUTCOME_MARK: Record<ItemOutcome, string> = {
  pass: "✓ 通过", fail: "✗ 未通过", unset: "· 未勾",
};

/** 可以直接贴进设备矩阵或邮件正文的纯文本。 */
export function formatAcceptanceText(receipt: AcceptanceReceipt): string {
  const lines = [
    `真机验收记录 ${receipt.machine.finishedAtIso}`,
    `记录编号：${receipt.receiptId}`,
    ...(receipt.supersedesReceiptId ? [`改正记录：取代记录 ${receipt.supersedesReceiptId}`] : []),
    `开始填写：${receipt.startedAtIso}`,
    `结论：${VERDICT_LABEL[receipt.verdict]}`,
    "",
    ...ACCEPTANCE_HEADER_FIELDS.map(
      (f) => `${f.label}：${receipt.header[f.key] || "（未填）"}`),
    `页面版本号：${receipt.machine.buildId}`,
    `屏幕：${receipt.machine.screen}`,
    `UA：${receipt.machine.userAgent}`,
    ...formatDeviceFactsLines(receipt.deviceFacts),
    "",
  ];
  for (const item of ACCEPTANCE_ITEMS) {
    lines.push(`${item.no}. ${item.title}`);
    for (const step of item.steps) {
      const row = receipt.steps.find((entry) => entry.key === step.key);
      const note = row?.note ? `　备注：${row.note}` : "";
      lines.push(`   [${step.key}] ${OUTCOME_MARK[row?.outcome ?? "unset"]} ${step.title}${note}`);
    }
  }
  lines.push("", receipt.caveat);
  return lines.join("\n");
}

/** 屏上显示用的 8 位短版本号；完整编号仍完整地写进回执与纯文本。 */
export function shortBuildId(buildId: string): string {
  if (/^\d+$/.test(buildId)) {
    return BigInt(buildId).toString(16).padStart(8, "0").slice(0, 8);
  }
  return buildId.slice(0, 8);
}

export function receiptFilename(receipt: AcceptanceReceipt): string {
  const date = (receipt.header.date || "无日期").replace(/[^0-9A-Za-z-]/g, "");
  const device = (receipt.header.device || "无设备").replace(/[^0-9A-Za-z一-龥-]/g, "");
  return `真机验收-${device}-${date}-${shortReceiptId(receipt.receiptId)}.json`;
}

export interface FrozenAcceptanceReceipt {
  receipt: AcceptanceReceipt;
  /** 冻结那一刻序列化的字节：下载、复制 JSON、存回本机都原样用它。 */
  json: string;
  text: string;
  filename: string;
}

function frozenFrom(receipt: AcceptanceReceipt, json: string): FrozenAcceptanceReceipt {
  return { receipt, json, text: formatAcceptanceText(receipt), filename: receiptFilename(receipt) };
}

export function freezeAcceptanceReceipt(draft: AcceptanceDraft, machine: MachineFacts): FrozenAcceptanceReceipt {
  const receipt = buildAcceptanceReceipt(draft, machine);
  return frozenFrom(receipt, JSON.stringify(receipt, null, 2));
}

const OUTCOMES: readonly string[] = ["pass", "fail", "unset"];
const MACHINE_KEYS = ["userAgent", "buildId", "screen", "finishedAtIso"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isAcceptanceReceipt(value: unknown): value is AcceptanceReceipt {
  if (!isRecord(value) || !isRecord(value.machine) || !isRecord(value.header)
      || !Array.isArray(value.steps)) return false;
  const machine = value.machine;
  return value.schema === ACCEPTANCE_RECEIPT_SCHEMA
    && isReceiptId(value.receiptId)
    && (value.supersedesReceiptId === null || isReceiptId(value.supersedesReceiptId))
    && typeof value.verdict === "string" && Object.hasOwn(VERDICT_LABEL, value.verdict)
    && typeof value.startedAtIso === "string"
    && MACHINE_KEYS.every((key) => typeof machine[key] === "string")
    && (value.deviceFacts === null || isDeviceFactsShape(value.deviceFacts))
    && Object.values(value.header).every((field) => typeof field === "string")
    && value.steps.every((row: unknown) => isRecord(row) && typeof row.key === "string"
      && typeof row.note === "string" && OUTCOMES.includes(row.outcome as string))
    && typeof value.caveat === "string";
}

/** 从本机存着的冻结字节还原：字节原样保留；解析不出或不是 v2 回执就返回 null。 */
export function readFrozenAcceptanceReceipt(json: string): FrozenAcceptanceReceipt | null {
  let value: unknown;
  try { value = JSON.parse(json); } catch { return null; }
  return isAcceptanceReceipt(value) ? frozenFrom(value, json) : null;
}

/** 冻结后发现填错：内容原样搬进一份新编号的草稿，并写明它取代哪一份。 */
export function reviseFrozenDraft(draft: AcceptanceDraft, receiptId: string = newReceiptId()): AcceptanceDraft {
  return { ...draft, receiptId, supersedesReceiptId: draft.receiptId };
}
