// 具名研究者工作台使用的冻结题库/脚本由服务器固定账号路由返回。
// 答案整包不再进入 public/dist；受试者设备只读服务端当前游标绑定的
// /patient-presentation 最小投影。
// boot 断言 bundle 版本 = plan 版本 = /content/item-bank 版本,不等则 fail-closed 拒开训练屏。
import { useEffect, useState } from "react";

export interface BankSingleItem {
  item_id: string;
  target_word: string;
  acceptable_expressions: string[];
  related_but_inaccurate: string[];
  initial_prompt: string;
  success_line: string;
  cues: Record<"1" | "2", { cue_type: string; text: string | null }>;
  tell_answer: string;
  image_id: string | null;
}
export interface BankDoubleItem {
  item_id: string;
  pair_title: string;
  left_word: string;
  right_word: string;
  left_function_cue: string;
  right_function_cue: string;
  relation_cue: string;
  image_id: string | null;
}
export interface ItemBankBundle {
  item_bank_version_id: string;
  single_element: BankSingleItem[];
  double_element: BankDoubleItem[];
}

export interface Week1Question { slot_field?: string; ask: string; success?: string }
// speaker="研究者" 的节是当面话术(不进 TTS、不广播给老人端);speaker="机器人" 的节才由小语开口。
export interface Week1Section {
  key: string;
  speaker: string;
  note?: string;
  questions?: Week1Question[];
  line?: string;
}
export interface Week1Script {
  script_version_id: string;
  phase_type: string;
  event_line: string;
  robot_name_config_key: string;
  zodiac_closed_list: string[];
  slots: Record<string, unknown>;
  silence_seconds: number;
  generic_fallback_line: string;
  sections: Week1Section[];
}

// fail-closed:脚本 JSON 与代码期望的字段错位曾静默把"自我介绍段打红线标记"整个绕过
// (代码找 section_key,文件里是 key,tsc 因盲转型全绿)——加载时校验,缺字段直接拒用。
function assertWeek1Shape(s: Week1Script): Week1Script {
  if (!Array.isArray(s.sections) || s.sections.length === 0) throw new Error("week1 脚本无 sections");
  for (const sec of s.sections) {
    if (!sec.key || !sec.speaker) throw new Error(`week1 脚本节缺 key/speaker:${JSON.stringify(sec).slice(0, 80)}`);
    if (sec.speaker === "机器人" && !sec.questions?.length && !sec.line) {
      throw new Error(`week1 机器人节 ${sec.key} 既无 questions 也无 line`);
    }
  }
  return s;
}

// 第1周开放式回应库：机器人在老人答完后说的那句，全部事先定稿。
// 分组由研究者点选——他刚听完老人说什么，判断比任何自动分类都准。
export interface Week1Reply {
  id: string;
  text: string;
  when: string;
  invites_more: boolean;
  group: string;
}
export interface Week1ReplyGroup { key: string; hint: string }
export interface Week1ReplyBank {
  bank_version_id: string;
  qc_status: string;
  signed_by: string | null;
  applies_to: { section_key: string; question_idx: number }[];
  fallback: string;
  groups: Week1ReplyGroup[];
  replies: Week1Reply[];
}

// 同一个理由:文件与代码字段错位过一次(见上)。分组键对不上就整包拒用,
// 免得屏上出一排点了没有句子的按钮。
function assertReplyBankShape(b: Week1ReplyBank): Week1ReplyBank {
  if (!Array.isArray(b.replies) || b.replies.length === 0) throw new Error("回应库无 replies");
  if (!Array.isArray(b.groups) || b.groups.length === 0) throw new Error("回应库无 groups");
  const keys = new Set(b.groups.map((g) => g.key));
  for (const r of b.replies) {
    if (!r.id || !r.text || !r.group) throw new Error(`回应句缺 id/text/group:${JSON.stringify(r).slice(0, 80)}`);
    if (!keys.has(r.group)) throw new Error(`回应句 ${r.id} 引用了未登记的分组 ${r.group}`);
  }
  if (!Array.isArray(b.applies_to)) throw new Error("回应库无 applies_to");
  return b;
}

// 自动驾驶协议(通用固定话术,逐字来自源文档;【物品名】槽位由题库目标词回填)
export interface AutopilotProtocol {
  protocol_version_id: string;
  silence_seconds: number;
  naming: {
    success_after_cue1: { unknown: string; close: string; silence: string };
    success_after_cue2: string;
  };
  double: { namefix_left: string; namefix_right: string };
}
export type FbKey =
  | "self" | "cued1_unknown" | "cued1_close" | "cued1_silence"
  | "cued2" | "namefix_l" | "namefix_r";

const bankCacheByWeek = new Map<number, ItemBankBundle>();
let scriptCache: Week1Script | null = null;
let protocolCache: AutopilotProtocol | null = null;
let replyBankCache: Week1ReplyBank | null = null;

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url, {
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new Error(`加载 ${url} 失败(${res.status})`);
  return (await res.json()) as T;
}

export function useItemBankBundle(week: number = 2): { bundle: ItemBankBundle | null; error: string | null } {
  const [bundle, setBundle] = useState<ItemBankBundle | null>(bankCacheByWeek.get(week) ?? null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const cached = bankCacheByWeek.get(week) ?? null;
    setBundle(cached);
    setError(null);
    if (cached) return;
    let active = true;
    fetchJson<ItemBankBundle>(`/content/item-bank-bundle?week=${week}`)
      .then((b) => {
        bankCacheByWeek.set(week, b);
        if (active) setBundle(b);
      })
      .catch((e) => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [week]);
  return { bundle, error };
}

export function useWeek1Script(): { script: Week1Script | null; error: string | null } {
  const [script, setScript] = useState<Week1Script | null>(scriptCache);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (scriptCache) return;
    fetchJson<Week1Script>("/content/week1-script")
      .then((s) => { const ok = assertWeek1Shape(s); scriptCache = ok; setScript(ok); })
      .catch((e) => setError(String(e)));
  }, []);
  return { script, error };
}

export function useWeek1ReplyBank(): { bank: Week1ReplyBank | null; error: string | null } {
  const [bank, setBank] = useState<Week1ReplyBank | null>(replyBankCache);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (replyBankCache) return;
    fetchJson<Week1ReplyBank>("/content/week1-reply-bank")
      .then((b) => { const ok = assertReplyBankShape(b); replyBankCache = ok; setBank(ok); })
      .catch((e) => setError(String(e)));
  }, []);
  return { bank, error };
}

export function useAutopilotProtocol(): { protocol: AutopilotProtocol | null; error: string | null } {
  const [protocol, setProtocol] = useState<AutopilotProtocol | null>(protocolCache);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (protocolCache) return;
    fetchJson<AutopilotProtocol>("/content/autopilot-protocol")
      .then((p) => { protocolCache = p; setProtocol(p); })
      .catch((e) => setError(String(e)));
  }, []);
  return { protocol, error };
}

export const findSingle = (b: ItemBankBundle, itemId: string): BankSingleItem | undefined =>
  b.single_element.find((x) => x.item_id === itemId);
export const findDouble = (b: ItemBankBundle, itemId: string): BankDoubleItem | undefined =>
  b.double_element.find((x) => x.item_id === itemId);

// 自动驾驶反馈话术查表(纯,两端共用):fbKey→协议模板/题库 success_line,
// 【物品名】只用题库目标词回填;任何缺字段返回 null——留空,绝不拼接兜底(单一内容源)。
export function resolveFeedbackLine(bundle: ItemBankBundle | null, protocol: AutopilotProtocol | null,
                                    fbKey: string, fbItemId: string): string | null {
  if (!bundle || !protocol) return null;
  const fill = (tpl: string | undefined, word: string | undefined): string | null =>
    tpl && word ? tpl.replaceAll("【物品名】", word) : null;
  const single = () => findSingle(bundle, fbItemId);
  const double = () => findDouble(bundle, fbItemId);
  switch (fbKey) {
    case "self": return single()?.success_line ?? null;
    case "cued1_unknown": return protocol.naming?.success_after_cue1?.unknown ?? null;
    case "cued1_close": return fill(protocol.naming?.success_after_cue1?.close, single()?.target_word);
    case "cued1_silence": return fill(protocol.naming?.success_after_cue1?.silence, single()?.target_word);
    case "cued2": return fill(protocol.naming?.success_after_cue2, single()?.target_word);
    case "namefix_l": return fill(protocol.double?.namefix_left, double()?.left_word);
    case "namefix_r": return fill(protocol.double?.namefix_right, double()?.right_word);
    default: return null;
  }
}

// 线索查表(纯,两端共用):单要素 1→cues1 / 2→cues2 / 3→tell_answer;
// 双要素 作用→left/right_function_cue、关系→relation_cue(等级≥1 即显,双要素无多级)。
// 只接 string|null,无任何拼接兜底——null 渲染空,交研究者决定升级。
export function lookupCue(bundle: ItemBankBundle | null, itemId: string, taskType: string,
                          role: string, level: number): string | null {
  if (!bundle || level < 1) return null;
  if (taskType === "单要素") {
    const s = findSingle(bundle, itemId);
    if (!s) return null;
    if (level === 1) return s.cues["1"]?.text ?? null;
    if (level === 2) return s.cues["2"]?.text ?? null;
    return s.tell_answer ?? null; // level 3
  }
  if (taskType === "双要素") {
    const d = findDouble(bundle, itemId);
    if (!d) return null;
    if (role === "左作用") return d.left_function_cue ?? null;
    if (role === "右作用") return d.right_function_cue ?? null;
    if (role === "关系识别") return d.relation_cue ?? null;
    return null; // 命名环节无预置线索文本
  }
  return null;
}

// 三方版本断言:bundle=plan=item-bank,任一不等 → 抛错(fail-closed,拒开训练屏)。
export function assertVersionsMatch(bundleVer: string, planVer: string, itemBankVer: string): void {
  if (bundleVer !== planVer || bundleVer !== itemBankVer) {
    throw new Error(
      `题库版本不一致，请刷新页面重试（随包=${bundleVer} 计划=${planVer} 服务器=${itemBankVer}）`,
    );
  }
}
