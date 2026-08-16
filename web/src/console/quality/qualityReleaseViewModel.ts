/**
 * 冻结发布纪元的视图模型。
 *
 * 有意复用 `AIQualityDashboardViewModel` 的形状，好让屏幕组件一行都不用改。
 * 差别全在内容上：这里没有精确计数，只有比率与档位。
 *
 * **null 一律显示成「已抑制」，绝不显示成 0。** 把 false_negative_rate: null
 * 渲染成 0 是在说"AI 一次假阴性都没有"，比不发布严重。
 */
import type {
  AIQualityDashboardViewModel,
  QualityDashboardGroupViewModel,
  QualityMetricViewModel,
  QualityNoticeViewModel,
} from "./qualityDashboardViewModel";
import type { AIQualityReleaseContract, QualityReleaseRow } from "./qualityReleaseContract";

const SUPPRESSED = "已抑制";

function rateMetric(
  key: string, label: string, value: number | null, detail: string,
): QualityMetricViewModel {
  if (value === null) {
    return {
      key, label, value: SUPPRESSED, state: "suppressed",
      detail: `${detail}（贡献人数低于冻结时的门槛，已抑制）`,
    };
  }
  return {
    key, label, value: `${(value * 100).toFixed(1)}%`, detail, state: "known",
  };
}

function bandMetric(
  key: string, label: string, value: string | null, detail: string,
): QualityMetricViewModel {
  if (value === null) {
    return { key, label, value: SUPPRESSED, detail, state: "suppressed" };
  }
  return { key, label, value, detail, state: "known" };
}

const RELEASE_VISIBILITY_NOTICE: QualityNoticeViewModel = {
  tone: "info",
  title: "当前显示的是一份冻结发布",
  text:
    "数字来自某一次切纪元时算好并存下的那一版，此后逐字节复读——"
    + "库里新增的场次、新入组的受试者都不会让它变动。要看新数据必须重新切纪元。",
};

const RELEASE_SUPPRESSION_NOTICE: QualityNoticeViewModel = {
  tone: "warn",
  title: "只发比率与档位，不发精确计数",
  text:
    "精确人数、混淆矩阵的四个原始格与全部覆盖计数一律不发布。"
    + "显示为「已抑制」的项，是因为支撑它的人数低于冻结时定下的门槛，不是 0。",
};

function buildGroup(row: QualityReleaseRow): QualityDashboardGroupViewModel {
  const released = row.suppression.status === "released";
  return {
    key: "frozen-release-overall",
    heading: "研究分区 overall（冻结发布）",
    classification: "research",
    dimensions: [{
      key: "data_classification", label: "数据分区", value: "研究", known: true,
    }],
    visibilityNotice: RELEASE_VISIBILITY_NOTICE,
    suppressionNotice: released ? RELEASE_SUPPRESSION_NOTICE : {
      tone: "warn",
      title: "本次没有可发布的数字",
      text: `拒绝原因：${row.suppression.reason ?? "未说明"}。`
        + "空白是设计生效，不是故障——不要通过调松阈值把它救回来。",
    },
    diagnosticsNotice: {
      tone: row.diagnostics.status === "complete" ? "info" : "warn",
      title: row.diagnostics.status === "complete"
        ? "冻结时证据完整" : "冻结时存在不完整证据",
      text: "逐项诊断计数在研究分区整块不发布——哪几项非零这件事本身就会说话。",
    },
    coverageMetrics: [
      bandMetric("cohort_size_band", "队列规模", row.release.cohort_size_band,
                 "本次冻结覆盖的受试者数所在档"),
      bandMetric("session_count_band", "场次数", row.release.session_count_band,
                 "本次冻结覆盖的场次数所在档"),
    ],
    diagnosticMetrics: [],
    operationalMetrics: [
      rateMetric("asr_manual_correction_rate", "ASR 人工校正率",
                 row.operational.asr_manual_correction_rate,
                 "已复核轮次中被人工改过转写的占比"),
      rateMetric("pause_rate", "技术暂停率", row.operational.pause_rate,
                 "可评估轮次中出现技术暂停的占比"),
      rateMetric("takeover_rate", "研究者接管率", row.operational.takeover_rate,
                 "可评估轮次中研究者接管的占比"),
    ],
    promptMetrics: [
      rateMetric("prompt_escalation_rate", "提示升级率",
                 row.operational.prompt_escalation_rate,
                 "0..2 级提示尝试中升到 1 级或 2 级的占比。"
                 + "分母有意不含「直接告知答案」——那一档床旁没有留存回执，"
                 + "把它算进分母会让减法解出它"),
    ],
    safetyMetrics: [],
    latencyMetrics: [
      bandMetric("latency_p50_band", "AI 处理延迟中位数",
                 row.operational.latency_p50_band, "毫秒，所在档"),
      bandMetric("latency_p95_band", "AI 处理延迟 p95",
                 row.operational.latency_p95_band, "毫秒，所在档"),
    ],
    researchTruth: {
      availability: row.research_truth.agreement_rate === null
        ? "suppressed" : "available",
      sectionTitle: "人机一致性（冻结发布）",
      metricsTitle: "只发比率",
      notice: {
        tone: "warn",
        title: "混淆矩阵的四个原始格永远不发布",
        text: "一个格子小到 1，就直接指向某一次具体的判错。这里只给三个比率。",
      },
      comparisonKind: "research",
      reviewedDecisions: bandMetric(
        "reviewed_decisions_band", "复核判定量",
        row.research_truth.reviewed_decisions_band, "所在档"),
      agreementRate: rateMetric(
        "agreement_rate", "人机一致率", row.research_truth.agreement_rate,
        "AI 二分类与锁定人工复核一致的占比"),
      falsePositiveRate: rateMetric(
        "false_positive_rate", "假阳性率", row.research_truth.false_positive_rate,
        "AI 判对而人工判错的占比"),
      falseNegativeRate: rateMetric(
        "false_negative_rate", "假阴性率", row.research_truth.false_negative_rate,
        "AI 判错而人工判对的占比"),
      matrix: {
        truePositive: suppressedCell("true_positive", "真阳性"),
        falsePositive: suppressedCell("false_positive", "假阳性"),
        falseNegative: suppressedCell("false_negative", "假阴性"),
        trueNegative: suppressedCell("true_negative", "真阴性"),
      },
    },
  };
}

function suppressedCell(key: string, label: string): QualityMetricViewModel {
  return {
    key, label, value: SUPPRESSED, state: "suppressed",
    detail: "冻结发布不给出混淆矩阵原始格",
  };
}

export function buildAIQualityReleaseViewModel(
  contract: AIQualityReleaseContract,
): AIQualityDashboardViewModel {
  return {
    generatedAt: contract.generated_at,
    groups: contract.rows.map(buildGroup),
  };
}
