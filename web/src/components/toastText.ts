// 全站 toast 错误出口的统一剥壳(P1-10):
// 1) 非 ApiError 直出的英文异常前缀(Error:/TypeError:…)一律剥掉;
// 2) 工程词 → 人话对照表在出口处替换——toast 是一次性提示,不是工程日志;
// 3) 研究准入缺项字段名(consent_type 等)统一翻成人话(P1-1)。
// 屏上常驻文案不走这里;要改常驻文案就改源头。
import { humanizeEligibilityText } from "../console/researchEligibility.ts";

const ERROR_PREFIX = /^(?:[A-Z][A-Za-z]*)?(?:Error|Exception)\s*:\s*/;

// 顺序敏感:长短语在前,避免先替换短词把长短语拆散。
const JARGON_MAP: readonly [string, string][] = [
  ["未通过严格契约校验", "与本系统版本不匹配"],
  ["严格契约", "数据格式核对"],
  ["CAS 修订", "版本核对"],
  ["幂等键", "防重复标识"],
  ["不可由普通接口恢复的 withdrawn 终态", "不可恢复的「已退出研究」的最终状态"],
  ["withdrawn 终态", "「已退出研究」的最终状态"],
  ["不可由普通接口恢复的", "不可恢复的"],
  ["冻结发布纪元", "冻结数据版本"],
  ["切纪元", "更新数据版本"],
  ["纪元", "数据版本"],
  ["闭表", "固定选项表"],
  ["已降级", "已改用本机备用方案"],
  ["证据收口", "证据登记"],
  ["收口", "收尾确认"],
  ["门禁未通过", "条件未满足"],
  ["门禁", "前置条件"],
  ["落库", "保存"],
  ["对账", "核对服务器记录"],
  ["终值环节", "已有最终记录的环节"],
  ["终值记录", "最终记录"],
  ["终值", "最终记录"],
  ["研究真值", "研究评分"],
  ["真值", "最终评分"],
  ["终态", "最终状态"],
];

const HAS_CJK = /[㐀-鿿]/;

export function humanizeToastText(text: string): string {
  let out = text.trim();
  // 前缀可能嵌套(如 "Error: TypeError: ...")。
  for (let i = 0; i < 3 && ERROR_PREFIX.test(out); i += 1) {
    out = out.replace(ERROR_PREFIX, "");
  }
  for (const [jargon, human] of JARGON_MAP) {
    out = out.split(jargon).join(human);
  }
  out = humanizeEligibilityText(out).trim();
  if (!out) return "操作没有成功，请重试";
  // 剥完前缀仍是纯英文/技术串:补一句中文引导,不让老人/护理员面对裸英文。
  if (!HAS_CJK.test(out)) return `操作没有成功（${out}），请重试；反复出现请联系管理员`;
  return out;
}
