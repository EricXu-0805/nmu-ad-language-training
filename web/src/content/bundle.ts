// 冻结题库/脚本随包副本(构建期从 platform/content 同步到 public/content)。
// 是前端唯一可得的双要素功能线索源(后端 /plan.display 不暴露它)。
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

export interface Week1Script {
  script_version_id: string;
  phase_type: string;
  event_line: string;
  robot_name_config_key: string;
  zodiac_closed_list: string[];
  slots: Record<string, unknown>;
  silence_seconds: number;
  generic_fallback_line: string;
  sections: { section_key: string; title?: string; lines?: string[]; questions?: unknown[]; [k: string]: unknown }[];
}

let bankCache: ItemBankBundle | null = null;
let scriptCache: Week1Script | null = null;

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`加载 ${url} 失败(${res.status})`);
  return (await res.json()) as T;
}

export function useItemBankBundle(): { bundle: ItemBankBundle | null; error: string | null } {
  const [bundle, setBundle] = useState<ItemBankBundle | null>(bankCache);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (bankCache) return;
    fetchJson<ItemBankBundle>("/content/item_bank_v1.json")
      .then((b) => { bankCache = b; setBundle(b); })
      .catch((e) => setError(String(e)));
  }, []);
  return { bundle, error };
}

export function useWeek1Script(): { script: Week1Script | null; error: string | null } {
  const [script, setScript] = useState<Week1Script | null>(scriptCache);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (scriptCache) return;
    fetchJson<Week1Script>("/content/week1_script.json")
      .then((s) => { scriptCache = s; setScript(s); })
      .catch((e) => setError(String(e)));
  }, []);
  return { script, error };
}

export const findSingle = (b: ItemBankBundle, itemId: string): BankSingleItem | undefined =>
  b.single_element.find((x) => x.item_id === itemId);
export const findDouble = (b: ItemBankBundle, itemId: string): BankDoubleItem | undefined =>
  b.double_element.find((x) => x.item_id === itemId);

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
      `题库版本不一致,拒开训练:随包=${bundleVer} / 计划=${planVer} / 后端=${itemBankVer}`,
    );
  }
}
