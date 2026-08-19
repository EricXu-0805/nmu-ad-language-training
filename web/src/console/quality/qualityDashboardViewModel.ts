import type {
  AIQualityDashboardContract,
  QualityDashboardDiagnosticReasonCounts,
  QualityDashboardDimensions,
  QualityDashboardGroup,
  QualityDataClassification,
  QualityVisibilityScope,
} from "./qualityDashboardContract";

export type QualityMetricState = "known" | "unknown" | "suppressed";

export interface QualityMetricViewModel {
  key: string;
  label: string;
  value: string;
  detail: string;
  state: QualityMetricState;
}

export interface QualityDimensionViewModel {
  key: keyof QualityDashboardDimensions;
  label: string;
  value: string;
  known: boolean;
}

export interface QualityNoticeViewModel {
  tone: "info" | "warn";
  title: string;
  text: string;
}

export interface QualityConfusionMatrixViewModel {
  truePositive: QualityMetricViewModel;
  falsePositive: QualityMetricViewModel;
  falseNegative: QualityMetricViewModel;
  trueNegative: QualityMetricViewModel;
}

export interface QualityResearchTruthViewModel {
  availability: "available" | "empty" | "unknown" | "suppressed";
  sectionTitle: string;
  metricsTitle: string;
  notice: QualityNoticeViewModel;
  comparisonKind: "research" | "simulation";
  reviewedDecisions: QualityMetricViewModel;
  agreementRate: QualityMetricViewModel;
  falsePositiveRate: QualityMetricViewModel;
  falseNegativeRate: QualityMetricViewModel;
  matrix: QualityConfusionMatrixViewModel;
}

export interface QualityDashboardGroupViewModel {
  key: string;
  heading: string;
  classification: QualityDataClassification;
  dimensions: QualityDimensionViewModel[];
  visibilityNotice: QualityNoticeViewModel;
  suppressionNotice: QualityNoticeViewModel;
  diagnosticsNotice: QualityNoticeViewModel;
  coverageMetrics: QualityMetricViewModel[];
  diagnosticMetrics: QualityMetricViewModel[];
  operationalMetrics: QualityMetricViewModel[];
  promptMetrics: QualityMetricViewModel[];
  safetyMetrics: QualityMetricViewModel[];
  latencyMetrics: QualityMetricViewModel[];
  researchTruth: QualityResearchTruthViewModel;
}

export interface AIQualityDashboardViewModel {
  generatedAt: string;
  groups: QualityDashboardGroupViewModel[];
}

interface MetricUnavailableContext {
  state: "unknown" | "suppressed";
  detail: string;
}

const UNKNOWN = "未知";
const SUPPRESSED = "已抑制";
const COUNT_FORMATTER = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 });

const DIMENSION_META: ReadonlyArray<{
  key: keyof QualityDashboardDimensions;
  label: string;
}> = [
  { key: "data_classification", label: "数据分区" },
  { key: "week_no", label: "周次" },
  { key: "phase_type", label: "阶段" },
  { key: "task_type", label: "任务类型" },
  { key: "content_group", label: "内容组" },
  { key: "provider_id", label: "AI 服务提供方" },
  { key: "device_profile", label: "设备画像" },
  { key: "protocol_version", label: "方案版本" },
  { key: "asr_engine_version", label: "ASR 引擎" },
  { key: "judge_engine_version", label: "判定引擎" },
] as const;

const DIAGNOSTIC_META: ReadonlyArray<{
  key: keyof QualityDashboardDiagnosticReasonCounts;
  label: string;
  detail: string;
}> = [
  { key: "restricted_or_withdrawn_sessions", label: "受限或已退出场次", detail: "因治理状态未进入质量聚合的场次" },
  { key: "classification_inconsistent_sessions", label: "分区矛盾场次", detail: "数据分区证据不一致的场次" },
  { key: "protocol_binding_invalid_sessions", label: "方案绑定无效场次", detail: "未能验证冻结题库或自动化方案绑定的场次" },
  { key: "structural_invalid_evidence_records", label: "结构无效证据记录", detail: "字段不完整、状态矛盾或缺少配对回执的记录" },
  { key: "lineage_invalid_turns", label: "证据链路无效轮次", detail: "与场次、题目位置或轮次归属不一致" },
  { key: "audio_evidence_unavailable_turns", label: "录音证据不可用轮次", detail: "无法证明录音证据完整的轮次" },
  { key: "ai_attempt_status_unknown_turns", label: "AI 尝试状态未知轮次", detail: "无法确认是否进入 AI 处理链" },
  { key: "ai_judgement_status_unknown_turns", label: "AI 判定状态未知轮次", detail: "无法确认 AI 是否完成判定" },
  { key: "asr_review_status_unknown_turns", label: "ASR 复核状态未知轮次", detail: "无法确认是否完成人工 ASR 复核" },
  { key: "human_truth_unavailable_turns", label: "复核参考不可用轮次", detail: "没有可用的锁定复核参考" },
  { key: "binary_prediction_unavailable_turns", label: "二分类判定不可用轮次", detail: "AI 输出无法映射为正确/错误二分类" },
  { key: "latency_unavailable_attempts", label: "AI 处理延迟不可计算尝试", detail: "缺少录音已上传或 AI 判类完成时间戳的尝试" },
] as const;

const VISIBILITY_SCOPE_COPY: Readonly<Record<QualityVisibilityScope, QualityNoticeViewModel>> = {
  owner_sessions: {
    tone: "info",
    title: "当前账号可见范围：本人负责场次",
    text: "overall 仅聚合当前账号负责且有权访问的场次；不可与其他角色的 overall 直接对比。",
  },
  terminal_sessions: {
    tone: "info",
    title: "当前账号可见范围：已进入终态的场次",
    text: "overall 仅聚合当前数据管理账号可查看的已完成、已中止或已失败场次；不可与其他角色的 overall 直接对比。",
  },
  all_sessions: {
    tone: "info",
    title: "当前账号可见范围：全部授权场次",
    text: "overall 聚合当前账号有权访问的全部场次；不可与范围更窄的 overall 直接对比。",
  },
};

function knownMetric(key: string, label: string, value: string, detail: string): QualityMetricViewModel {
  return { key, label, value, detail, state: "known" };
}

function unavailableMetric(
  key: string,
  label: string,
  context: MetricUnavailableContext,
): QualityMetricViewModel {
  return {
    key,
    label,
    value: context.state === "suppressed" ? SUPPRESSED : UNKNOWN,
    detail: context.detail,
    state: context.state,
  };
}

function countMetric(
  key: string,
  label: string,
  value: number | null,
  detail: string,
  unavailable: MetricUnavailableContext,
): QualityMetricViewModel {
  return value === null
    ? unavailableMetric(key, label, unavailable)
    : knownMetric(key, label, COUNT_FORMATTER.format(value), detail);
}

function ratioMetric(
  key: string,
  label: string,
  numerator: number | null,
  denominator: number | null,
  detail: string,
  unavailable: MetricUnavailableContext,
): QualityMetricViewModel {
  if (numerator === null || denominator === null) return unavailableMetric(key, label, unavailable);
  if (denominator === 0) {
    return unavailableMetric(key, label, { state: "unknown", detail: `${detail}；当前分母为 0，不计算百分比` });
  }
  return knownMetric(
    key,
    label,
    `${((numerator / denominator) * 100).toFixed(1)}%`,
    `${detail}（${COUNT_FORMATTER.format(numerator)}/${COUNT_FORMATTER.format(denominator)}）`,
  );
}

function latencyMetric(
  key: string,
  label: string,
  value: number | null,
  sampleCount: number | null,
  unavailable: MetricUnavailableContext,
): QualityMetricViewModel {
  if (value === null || sampleCount === null) return unavailableMetric(key, label, unavailable);
  if (sampleCount === 0) {
    return unavailableMetric(key, label, { state: "unknown", detail: "当前没有可计算延迟的尝试" });
  }
  return knownMetric(
    key,
    label,
    `${COUNT_FORMATTER.format(value)} 毫秒`,
    `基于 ${COUNT_FORMATTER.format(sampleCount)} 个从录音已上传到 AI 判类完成的尝试；不包含床旁播放、采集和上传阶段`,
  );
}

function dimensionValue(key: keyof QualityDashboardDimensions, value: string | number | null): string {
  if (value !== null) return value === "research" ? "真实研究 overall" : "模拟演练 overall";
  if (key === "device_profile") return "v2 首版未发布（设备画像覆盖未知）";
  if (key === "provider_id") return "v2 首版未发布（提供方覆盖未知）";
  return "overall 首版不按此维度分组";
}

function buildDimensions(dimensions: QualityDashboardDimensions): QualityDimensionViewModel[] {
  return DIMENSION_META.map(({ key, label }) => ({
    key,
    label,
    value: dimensionValue(key, dimensions[key]),
    known: dimensions[key] !== null,
  }));
}

function suppressionNotice(group: QualityDashboardGroup): QualityNoticeViewModel {
  const suppression = group.suppression;
  if (suppression.status === "not_applicable") {
    return {
      tone: "info",
      title: "模拟分区不适用真实研究小单元门槛",
      text: "本区只用于流程与模型调试；任何复核比较都不可并入真实研究结论。",
    };
  }
  if (suppression.status === "released") {
    return {
      tone: "info",
      title: "真实研究 overall 已通过隐私门槛",
      text: `本次汇总覆盖 ${String(suppression.distinct_subjects)} 名受试者（隐私要求至少 ${String(suppression.minimum_distinct_subjects)} 名）。样本量是否足够需另行评估。`,
    };
  }
  const reasonText = {
    research_threshold_unconfigured: "不同受试者最小公开阈值尚未配置",
    research_threshold_invalid: "不同受试者最小公开阈值配置无效",
    research_small_cell: `未达到至少 ${String(suppression.minimum_distinct_subjects)} 名不同受试者的公开阈值`,
    research_release_not_frozen: "真实研究质量发布批次尚未冻结",
  }[suppression.reason!];
  const releaseBoundary = suppression.reason === "research_release_not_frozen"
    ? "待研究数据冻结版本发布后开放。"
    : "";
  return {
    tone: "warn",
    title: "真实研究 overall 已做隐私抑制",
    text: `${reasonText}；为保护隐私，本组的受试者数与各项计数暂不显示。${releaseBoundary}`,
  };
}

function diagnosticsNotice(group: QualityDashboardGroup): QualityNoticeViewModel {
  if (group.diagnostics.status === "suppressed") {
    return {
      tone: "warn",
      title: "覆盖诊断已随隐私汇总抑制",
      text: "“已抑制”表示因隐私要求而隐藏，不等于 0。",
    };
  }
  if (group.diagnostics.status === "complete") {
    return {
      tone: "info",
      title: "当前固定覆盖诊断为完整",
      text: "证据覆盖完整（不代表 AI 判定准确）。",
    };
  }
  return {
    tone: "warn",
    title: "存在证据排除或覆盖不足",
    text: "部分数据被排除，未知指标的原因见下列计数；被排除的数据不等于正确、错误或 0。",
  };
}

function unavailableContext(group: QualityDashboardGroup): MetricUnavailableContext {
  if (group.suppression.status === "suppressed") {
    return { state: "suppressed", detail: "已按真实研究小单元隐私规则抑制，不能解释为 0" };
  }
  return { state: "unknown", detail: "证据不足，暂无法计算" };
}

function buildResearchTruth(
  group: QualityDashboardGroup,
  unavailable: MetricUnavailableContext,
): QualityResearchTruthViewModel {
  const truth = group.research_truth;
  const isResearch = group.dimensions.data_classification === "research";
  const cells = [truth.true_positive, truth.true_negative, truth.false_positive, truth.false_negative];
  const matrixKnown = cells.every((count) => count !== null) && truth.reviewed_decisions !== null;
  const matrixTotal = matrixKnown
    ? cells.reduce<number>((sum, count) => sum + (count ?? 0), 0)
    : null;
  const availability = group.suppression.status === "suppressed"
    ? "suppressed"
    : !matrixKnown
      ? "unknown"
      : matrixTotal === 0
        ? "empty"
        : "available";
  const sectionTitle = isResearch
    ? "人工锁定研究真值（复核口径）"
    : "模拟复核参考（不可用于研究结论）";
  const notice: QualityNoticeViewModel = availability === "suppressed"
    ? {
      tone: "warn",
      title: "研究真值已做隐私抑制",
      text: "小单元内的复核计数和混淆矩阵均不发布，不能从运行指标反推。",
    }
    : availability === "empty"
      ? {
        tone: "warn",
        title: isResearch ? "尚无二分类可核查研究判定" : "尚无二分类模拟复核参考",
        text: "已核查样本为 0；所有准确性百分比保持未知。",
      }
      : availability === "unknown"
        ? {
          tone: "warn",
          title: isResearch ? "人工研究真值不可用" : "模拟复核参考不可用",
          text: "请结合覆盖与固定诊断原因；不得由 AI 运行结果推断准确性。",
        }
        : {
          tone: "info",
          title: isResearch ? "人工研究真值已释放（样本量仍需审阅）" : "模拟复核参考已生成",
          text: isResearch
            ? "仅用于核查 AI 判定；样本量是否足够需另行评估。"
            : "仅用于模拟流程与模型调试，不得并入真实研究结果。",
        };
  const positiveActual = truth.true_positive !== null && truth.false_negative !== null
    ? truth.true_positive + truth.false_negative
    : null;
  const negativeActual = truth.true_negative !== null && truth.false_positive !== null
    ? truth.true_negative + truth.false_positive
    : null;
  const agreements = truth.true_positive !== null && truth.true_negative !== null
    ? truth.true_positive + truth.true_negative
    : null;
  const reviewedLabel = isResearch ? "人工已锁定研究判定" : "模拟复核判定";
  const comparisonLabel = isResearch ? "AI 与人工研究真值一致率" : "AI 与模拟复核一致率";
  return {
    availability,
    sectionTitle,
    metricsTitle: isResearch ? "人工研究真值比较指标" : "模拟复核比较指标",
    notice,
    comparisonKind: isResearch ? "research" : "simulation",
    reviewedDecisions: countMetric(
      "reviewed-decisions",
      reviewedLabel,
      truth.reviewed_decisions,
      isResearch ? "仅统计已锁定的人工研究真值" : "只统计模拟复核参考",
      unavailable,
    ),
    agreementRate: ratioMetric("agreement-rate", comparisonLabel, agreements, matrixTotal, "一致判定/完整混淆矩阵", unavailable),
    falsePositiveRate: ratioMetric(
      "false-positive-rate",
      "假阳性率（FP rate）",
      truth.false_positive,
      negativeActual,
      "AI 判正确、复核判错误 / 复核判错误总数",
      unavailable,
    ),
    falseNegativeRate: ratioMetric(
      "false-negative-rate",
      "假阴性率（FN rate）",
      truth.false_negative,
      positiveActual,
      "AI 判错误、复核判正确 / 复核判正确总数",
      unavailable,
    ),
    matrix: {
      truePositive: countMetric("true-positive", "真阳性 TP", truth.true_positive, "AI 与复核都判正确", unavailable),
      falsePositive: countMetric("false-positive", "假阳性 FP", truth.false_positive, "AI 判正确、复核判错误", unavailable),
      falseNegative: countMetric("false-negative", "假阴性 FN", truth.false_negative, "AI 判错误、复核判正确", unavailable),
      trueNegative: countMetric("true-negative", "真阴性 TN", truth.true_negative, "AI 与复核都判错误", unavailable),
    },
  };
}

function buildGroup(group: QualityDashboardGroup): QualityDashboardGroupViewModel {
  const operational = group.operational;
  const unavailable = unavailableContext(group);
  const promptAttemptContextValues = [
    operational.prompt_level_0_count,
    operational.prompt_level_1_count,
    operational.prompt_level_2_count,
  ];
  const promptAttemptContextTotal = promptAttemptContextValues.every((count) => count !== null)
    ? promptAttemptContextValues.reduce<number>((sum, count) => sum + (count ?? 0), 0)
    : null;
  const promptEscalations = promptAttemptContextTotal === null
    ? null
    : (operational.prompt_level_1_count ?? 0)
      + (operational.prompt_level_2_count ?? 0);
  const tellAnswerUnavailable: MetricUnavailableContext = group.suppression.status === "suppressed"
    ? unavailable
    : {
      state: "unknown",
      detail: "旧版本未记录此项，显示“未知”而非 0",
    };
  const coverage = group.coverage;
  const classification = group.dimensions.data_classification;
  return {
    key: classification,
    heading: classification === "research" ? "真实研究 overall" : "模拟演练 overall",
    classification,
    dimensions: buildDimensions(group.dimensions),
    visibilityNotice: VISIBILITY_SCOPE_COPY[group.visibility_scope],
    suppressionNotice: suppressionNotice(group),
    diagnosticsNotice: diagnosticsNotice(group),
    coverageMetrics: [
      countMetric("visible-sessions", "当前分区可见场次", coverage.visible_sessions, "访问治理后可见的场次", unavailable),
      countMetric("included-sessions", "纳入质量聚合场次", coverage.included_sessions, "通过治理与一致性检查的场次", unavailable),
      countMetric("source-turns", "源轮次", coverage.source_turns, "纳入场次中的方案轮次", unavailable),
      countMetric("audio-evidenced-turns", "有录音证据轮次", coverage.audio_evidenced_turns, "具备可验证录音证据的轮次", unavailable),
      countMetric("attempts-observed", "已观察尝试", coverage.attempts_observed, "进入聚合的尝试证据", unavailable),
      countMetric("prompt-known-attempts", "提示层级已知尝试", coverage.prompt_level_known_attempts, "提示层级证据完整的尝试", unavailable),
      countMetric("processing-known-attempts", "处理状态已知尝试", coverage.processing_status_known_attempts, "处理状态证据完整的尝试", unavailable),
      countMetric("latency-known-attempts", "AI 处理延迟可计算尝试", coverage.latency_known_attempts, "录音已上传与 AI 判类完成时间戳均可核查", unavailable),
      countMetric("ai-attempt-known-turns", "AI 尝试状态已知轮次", coverage.ai_attempt_status_known_turns, "AI 尝试状态可核查的轮次", unavailable),
      countMetric("ai-judgement-known-turns", "AI 判定状态已知轮次", coverage.ai_judgement_status_known_turns, "AI 判定状态可核查的轮次", unavailable),
      countMetric("asr-review-known-turns", "ASR 复核状态已知轮次", coverage.asr_review_status_known_turns, "ASR 人工复核状态可核查的轮次", unavailable),
      countMetric("truth-locked-turns", "复核参考已锁定轮次", coverage.human_truth_locked_turns, "存在锁定复核参考的轮次", unavailable),
      countMetric("binary-eligible", "二分类可核查判定", coverage.binary_eligible_reviewed_decisions, "同时具备 AI 二分类与锁定复核参考", unavailable),
      countMetric("binary-excluded", "二分类排除判定", coverage.binary_excluded_decisions, "不满足二分类比较条件，不能计入混淆矩阵", unavailable),
    ],
    diagnosticMetrics: DIAGNOSTIC_META.map(({ key, label, detail }) => countMetric(
      `diagnostic-${key}`,
      label,
      group.diagnostics.reason_counts[key],
      detail,
      unavailable,
    )),
    operationalMetrics: [
      ratioMetric("coverage", "AI 判定覆盖率", operational.ai_judged_turns, operational.eligible_turns, "完成 AI 判定的轮次/可评估轮次", unavailable),
      countMetric("eligible-turns", "可评估轮次", operational.eligible_turns, "按冻结方案应评估的轮次", unavailable),
      countMetric("ai-attempted-turns", "AI 已尝试轮次", operational.ai_attempted_turns, "进入 AI 处理链的轮次", unavailable),
      countMetric("ai-judged-turns", "AI 已完成判定", operational.ai_judged_turns, "AI 运行判定；不是复核真值", unavailable),
      ratioMetric("asr-correction-rate", "ASR 人工修正率", operational.asr_corrected_turns, operational.asr_reviewed_turns, "人工修正/人工已复核 ASR", unavailable),
    ],
    promptMetrics: [
      ratioMetric("prompt-escalation-rate", "录音尝试提示上下文升级率", promptEscalations, promptAttemptContextTotal, "处于轻提示或明确提示上下文的录音尝试 / 0..2 级上下文已知的录音尝试；不包含未落账的告知答案呈现", unavailable),
      ratioMetric("tell-answer-rate", "告知答案率", operational.prompt_level_3_count, operational.total_attempts, "告知答案呈现 / 全部录音尝试", tellAnswerUnavailable),
      countMetric("prompt-level-0", "0 级：无提示", operational.prompt_level_0_count, "录音尝试所处提示上下文：自发作答", unavailable),
      countMetric("prompt-level-1", "1 级：轻提示", operational.prompt_level_1_count, "录音尝试所处提示上下文：轻提示后作答", unavailable),
      countMetric("prompt-level-2", "2 级：明确提示", operational.prompt_level_2_count, "录音尝试所处提示上下文：明确提示后作答", unavailable),
      countMetric("prompt-level-3", "3 级：告知答案呈现", operational.prompt_level_3_count, "录音尝试所处提示上下文：已告知答案", tellAnswerUnavailable),
    ],
    safetyMetrics: [
      ratioMetric("technical-failure-rate", "技术失败率", operational.technical_failure_attempts, operational.total_attempts, "技术失败尝试/全部尝试；技术失败不代表回答错误", unavailable),
      countMetric("technical-failures", "技术失败", operational.technical_failure_attempts, "AI、设备或网络处理失败尝试", unavailable),
      countMetric("technical-pauses", "安全暂停", operational.technical_pause_count, "系统或研究人员触发的技术暂停", unavailable),
      countMetric("researcher-takeovers", "人工接管", operational.researcher_takeover_count, "研究人员显式取得控制权", unavailable),
    ],
    latencyMetrics: [
      latencyMetric("latency-p50", "AI 处理延迟 P50", operational.latency_p50_ms, operational.latency_sample_count, unavailable),
      latencyMetric("latency-p95", "AI 处理延迟 P95", operational.latency_p95_ms, operational.latency_sample_count, unavailable),
      countMetric("latency-samples", "AI 处理延迟样本数", operational.latency_sample_count, "从录音已上传到 AI 判类完成都有时间戳的尝试", unavailable),
    ],
    researchTruth: buildResearchTruth(group, unavailable),
  };
}

export function buildAIQualityDashboardViewModel(
  contract: AIQualityDashboardContract,
): AIQualityDashboardViewModel {
  return {
    generatedAt: contract.generated_at,
    groups: contract.rows.map(buildGroup),
  };
}
