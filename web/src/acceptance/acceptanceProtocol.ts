// 真机验收的**记录仪**，不是判定器。
//
// 八项验收今天靠一张 Markdown 表在纸上打勾（docs/handover/真机验收记录表.md）。
// 纸的问题不是不能用，是回执会丢：谁在哪台机器、哪个浏览器、哪个构建编号上
// 验的第几项，事后没人说得清；而"沿用旧结果"恰恰是这张表明令禁止的事。
//
// 所以这一层只做三件事：把八项摊成可勾选的结构、自动记下机器说得出的事实
// （UA、构建编号、屏幕、时间）、产出一份能归档的 JSON 回执。
//
// 它**永远不会**替人判"通过"：任何一项没勾、或表头没填全，整份回执就是未完成。
// 全部勾通过也只出"现场记录齐了"，判定仍然是负责人的事。

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

/** 与 docs/handover/真机验收记录表.md 的八项逐条对应。改这里要同步改那份表。 */
export const ACCEPTANCE_ITEMS: readonly AcceptanceItem[] = Object.freeze([
  {
    no: 1,
    title: "声音真正结束后才开麦，而且只开一次",
    steps: [{
      key: "1",
      title: "正常题目走一轮",
      how: "让 AI 读完问句，看麦克风什么时候打开。",
      criterion: "肉眼看到朗读结束 → 才出现收音状态；不出现「开了又关又开」。",
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
  header: Record<string, string>;
  outcomes: Record<string, ItemOutcome>;
  notes: Record<string, string>;
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
  "recorded-all-pass": "现场记录已齐（八项全部勾了通过）",
};

export function emptyDraft(): AcceptanceDraft {
  return {
    header: Object.fromEntries(ACCEPTANCE_HEADER_FIELDS.map((f) => [f.key, ""])),
    outcomes: Object.fromEntries(ACCEPTANCE_STEP_KEYS.map((key) => [key, "unset" as ItemOutcome])),
    notes: {},
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
  schema: "nmu-device-acceptance.v1";
  verdict: AcceptanceVerdict;
  machine: MachineFacts;
  header: Record<string, string>;
  steps: { key: string; item: number; title: string; outcome: ItemOutcome; note: string }[];
  caveat: string;
}

const CAVEAT = "这份文件只是现场记录，不构成任何批准。八项全部通过也不等于这台设备"
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
    schema: "nmu-device-acceptance.v1",
    verdict: summarizeAcceptance(draft),
    machine,
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
    `结论：${VERDICT_LABEL[receipt.verdict]}`,
    "",
    ...ACCEPTANCE_HEADER_FIELDS.map(
      (f) => `${f.label}：${receipt.header[f.key] || "（未填）"}`),
    `页面版本号：${receipt.machine.buildId}`,
    `屏幕：${receipt.machine.screen}`,
    `UA：${receipt.machine.userAgent}`,
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
  return `真机验收-${device}-${date}.json`;
}
