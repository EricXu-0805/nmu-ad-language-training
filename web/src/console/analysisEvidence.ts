import type {
  AttemptEvent,
  AudioAsset,
  InteractionEvent,
  ItemEvent,
  Session,
  TurnEvent,
} from "../types";

export type EvidenceIssueTone = "danger" | "warn";

export interface EvidenceIssue {
  code: string;
  tone: EvidenceIssueTone;
  message: string;
}

export interface ParsedInteraction {
  event: InteractionEvent;
  payload: Record<string, unknown> | null;
  payloadError: string | null;
  summary: string;
}

export interface TruthComparison {
  state: "unavailable" | "same" | "different";
  aiScore: number | null;
  researchScore: number | null;
  message: string;
}

export interface AttemptEvidence {
  attempt: AttemptEvent;
  interactions: ParsedInteraction[];
  audio: AudioAsset | null;
  isTurnSource: boolean;
  issues: EvidenceIssue[];
}

export interface TurnEvidence {
  key: string;
  itemId: string;
  turnSeq: number;
  responseRole: string | null;
  item: ItemEvent | null;
  turn: TurnEvent | null;
  attempts: AttemptEvidence[];
  interactions: ParsedInteraction[];
  issues: EvidenceIssue[];
  comparison: TruthComparison;
}

export interface EvidenceTimeline {
  turns: TurnEvidence[];
  sessionInteractions: ParsedInteraction[];
  unboundAudios: AudioAsset[];
  issues: EvidenceIssue[];
  summary: {
    totalAttempts: number;
    completedAttempts: number;
    technicalFailures: number;
    incompleteAttempts: number;
    sourceBoundTurns: number;
    lockedTruths: number;
    dangerIssues: number;
    warningIssues: number;
  };
}

export interface EvidenceJournalInput {
  session: Session;
  items: ItemEvent[];
  turns: TurnEvent[];
  audios: AudioAsset[];
  attempts: AttemptEvent[];
  interactions: InteractionEvent[];
}

interface MutableTurn {
  key: string;
  itemId: string;
  turnSeq: number;
  item: ItemEvent | null;
  turns: TurnEvent[];
  attempts: AttemptEvent[];
  interactions: ParsedInteraction[];
}

const FEEDBACK_LABELS: Record<string, string> = {
  self: "自发正确反馈",
  cued1_unknown: "一级提示后成功（未提及路径）",
  cued1_close: "一级提示后成功（接近答案路径）",
  cued1_silence: "一级提示后成功（沉默路径）",
  cued2: "二级提示后成功",
  namefix_l: "左侧命名纠正",
  namefix_r: "右侧命名纠正",
};

function issue(code: string, tone: EvidenceIssueTone, message: string): EvidenceIssue {
  return { code, tone, message };
}

function nullableEqual(left: unknown, right: unknown): boolean {
  return (left ?? null) === (right ?? null);
}

function sessionExpectedSimulation(session: Session): boolean | null {
  if (session.data_classification === "research") return false;
  if (session.data_classification === "simulation") return true;
  return null;
}

function audioMatchesSession(audio: AudioAsset, session: Session): boolean {
  return audio.session_id === session.session_id
    && audio.data_classification === session.data_classification
    && audio.is_simulation === session.is_simulation;
}

function parsePayload(event: InteractionEvent): ParsedInteraction {
  let payload: Record<string, unknown> | null = null;
  let payloadError: string | null = null;
  try {
    const decoded: unknown = JSON.parse(event.payload_json);
    if (decoded === null || typeof decoded !== "object" || Array.isArray(decoded)) {
      payloadError = "payload_json 不是对象";
    } else {
      payload = decoded as Record<string, unknown>;
    }
  } catch {
    payloadError = "payload_json 无法解析";
  }
  return { event, payload, payloadError, summary: interactionSummary(event.event_type, payload) };
}

function value(payload: Record<string, unknown> | null, key: string): string | null {
  const raw = payload?.[key];
  if (typeof raw === "string" && raw) return raw;
  if (typeof raw === "number" || typeof raw === "boolean") return String(raw);
  return null;
}

export function interactionSummary(eventType: string, payload: Record<string, unknown> | null): string {
  const engine = value(payload, "asr_engine_version") ?? value(payload, "judge_engine_version");
  const error = value(payload, "error_code");
  switch (eventType) {
    case "attempt_received":
      return `收到作答录音 · 提示级 ${value(payload, "prompt_level") ?? "?"}`;
    case "asr_completed":
      return `ASR 完成${engine ? ` · ${engine}` : ""}${value(payload, "asr_confidence") ? ` · 置信度 ${value(payload, "asr_confidence")}` : ""}`;
    case "asr_failed":
      return `ASR 技术失败${engine ? ` · ${engine}` : ""}${error ? ` · ${error}` : ""}`;
    case "judgement_completed":
      return `AI operational 判类完成 · ${value(payload, "answer_type") ?? "类型缺失"}${engine ? ` · ${engine}` : ""}`;
    case "judgement_failed":
      return `AI 判类技术失败${engine ? ` · ${engine}` : ""}${error ? ` · ${error}` : ""}`;
    case "cue_selected":
      return `下发提示 · ${value(payload, "prompt_level") ?? "?"} 级${value(payload, "cue_type") ? ` · ${value(payload, "cue_type")}` : ""}`;
    case "feedback_selected": {
      const key = value(payload, "feedback_key");
      return `下发反馈 · ${key ? (FEEDBACK_LABELS[key] ?? key) : "反馈键缺失"}`;
    }
    case "technical_pause":
      return `技术安全暂停${error ? ` · ${error}` : ""}`;
    case "researcher_takeover":
      return `人工接管 · ${value(payload, "reason_code") ?? "原因缺失"}`;
    default:
      return `未识别事件 · ${eventType}`;
  }
}

function requiredEvents(attempt: AttemptEvent, interactions: ParsedInteraction[]): EvidenceIssue[] {
  const types = new Set(interactions.map((row) => row.event.event_type));
  const issues: EvidenceIssue[] = [];
  if (!types.has("attempt_received")) {
    issues.push(issue("attempt_received_missing", "danger", "缺少 attempt_received，这次处理的起点证据不完整。"));
  }
  if (attempt.processing_status === "completed") {
    if (!types.has("asr_completed")) issues.push(issue("asr_event_missing", "danger", "状态为 completed，但缺少 asr_completed 事件。"));
    if (!types.has("judgement_completed")) issues.push(issue("judge_event_missing", "danger", "状态为 completed，但缺少 judgement_completed 事件。"));
    const judgement = interactions.find((row) => row.event.event_type === "judgement_completed");
    if (judgement?.payload?.truth_scope !== "operational_only") {
      issues.push(issue("truth_scope_invalid", "danger", "AI 判类未明确标注 operational_only，不能视为研究真值。"));
    }
  }
  if (attempt.processing_status === "technical_failure") {
    if (!types.has("technical_pause")) issues.push(issue("technical_pause_missing", "danger", "技术失败缺少 technical_pause 安全暂停证据。"));
    if (!types.has("asr_failed") && !types.has("judgement_failed")) {
      issues.push(issue("failure_stage_missing", "danger", "技术失败未标明发生在 ASR 还是 AI 判类阶段。"));
    }
  }
  return issues;
}

function turnAttemptConsistency(turn: TurnEvent, attempt: AttemptEvent): EvidenceIssue[] {
  const issues: EvidenceIssue[] = [];
  const mismatches: string[] = [];
  if (turn.turn_seq !== attempt.turn_seq) mismatches.push("环节序号");
  if (!nullableEqual(turn.response_role, attempt.response_role)) mismatches.push("环节角色");
  if (turn.raw_audio_id !== attempt.raw_audio_id) mismatches.push("录音引用");
  if (!nullableEqual(turn.asr_text, attempt.asr_text)) mismatches.push("ASR 原文");
  if (!nullableEqual(turn.asr_confidence, attempt.asr_confidence)) mismatches.push("ASR 置信度");
  if (!nullableEqual(turn.prompt_level, attempt.prompt_level)) mismatches.push("提示等级");
  if (!nullableEqual(turn.cue_type, attempt.cue_type)) mismatches.push("提示类型");
  if (!nullableEqual(turn.duration_seconds, attempt.duration_seconds)) mismatches.push("录音时长");
  if (!nullableEqual(turn.ai_answer_type, attempt.operational_answer_type)) mismatches.push("AI 运营类型");
  if (!nullableEqual(turn.ai_score, attempt.operational_score)) mismatches.push("AI 运营分");
  if (!nullableEqual(turn.ai_needs_review, attempt.operational_needs_review)) mismatches.push("需复核标记");
  if (!nullableEqual(turn.ai_judge_mode, attempt.judge_mode)) mismatches.push("判类模式");
  if (mismatches.length > 0) {
    issues.push(issue("turn_attempt_mismatch", "danger", `Turn 与 source attempt 不一致：${mismatches.join("、")}。`));
  }
  if (turn.judge_portrait_used || attempt.judge_portrait_used) {
    issues.push(issue("portrait_leak", "danger", "证据标记为使用了画像数据，违反判分禁入约束。"));
  }
  return issues;
}

export function compareOperationalToResearch(turn: TurnEvent | null, source: AttemptEvent | null): TruthComparison {
  const aiScore = typeof source?.operational_score === "number" ? source.operational_score : null;
  const researchValue = turn?.element_value ?? turn?.reviewed_score;
  const researchScore = typeof researchValue === "number" ? researchValue : null;
  if (!turn?.score_locked || researchScore === null) {
    return { state: "unavailable", aiScore, researchScore, message: "尚未形成人工锁定的研究真值，不可宣称 AI 判定正确或错误。" };
  }
  if (aiScore === null) {
    return { state: "unavailable", aiScore, researchScore, message: "AI operational 分缺失；只能展示人工研究真值。" };
  }
  if (aiScore === researchScore) {
    return { state: "same", aiScore, researchScore, message: `数值一致（AI ${aiScore} / 研究真值 ${researchScore}），但两者语义仍不等同。` };
  }
  return { state: "different", aiScore, researchScore, message: `数值不一致：AI operational ${aiScore}，人工研究真值 ${researchScore}。` };
}

export function buildEvidenceTimeline(input: EvidenceJournalInput): EvidenceTimeline {
  const { session, items, turns, audios, attempts, interactions } = input;
  const sessionIssues: EvidenceIssue[] = [];
  const expectedSimulation = sessionExpectedSimulation(session);
  if (expectedSimulation === null) {
    sessionIssues.push(issue("classification_unknown", "danger", "场次 data_classification 缺失或未知，证据只能在隔离区核查。"));
  } else if (session.is_simulation !== expectedSimulation) {
    sessionIssues.push(issue("session_boundary_mismatch", "danger", "场次 is_simulation 与 data_classification 不一致。"));
  }

  const orderedInteractions = [...interactions].sort((a, b) => a.event_seq - b.event_seq);
  orderedInteractions.forEach((event, index) => {
    if (!Number.isInteger(event.event_seq) || event.event_seq !== index + 1) {
      sessionIssues.push(issue("event_sequence_gap", "danger", "场次 interaction event_seq 不连续或重复，时间线完整性需核查。"));
    }
    if (event.session_id !== session.session_id || (expectedSimulation !== null && event.is_simulation !== expectedSimulation)) {
      sessionIssues.push(issue("interaction_boundary_mismatch", "danger", `interaction #${event.event_seq} 与当前场次或数据分区不一致。`));
    }
  });

  const parsedInteractions = orderedInteractions.map(parsePayload);
  const audioById = new Map(audios.map((audio) => [audio.raw_audio_id, audio]));
  const itemByEventId = new Map<number, ItemEvent>();
  const itemOrder = new Map<string, number>();
  for (const item of items) {
    if (item.id != null) itemByEventId.set(item.id, item);
    if (!itemOrder.has(item.item_id)) itemOrder.set(item.item_id, item.presentation_order ?? item.id ?? Number.MAX_SAFE_INTEGER);
  }
  const attemptById = new Map(attempts.map((attempt) => [attempt.id, attempt]));
  const interactionsByAttempt = new Map<number, ParsedInteraction[]>();
  const mutableTurns = new Map<string, MutableTurn>();
  const keyFor = (itemId: string, turnSeq: number) => `${itemId}#${turnSeq}`;
  const ensureTurn = (key: string, itemId: string, turnSeq: number, item: ItemEvent | null): MutableTurn => {
    const existing = mutableTurns.get(key);
    if (existing) {
      if (!existing.item && item) existing.item = item;
      return existing;
    }
    const created = { key, itemId, turnSeq, item, turns: [], attempts: [], interactions: [] };
    mutableTurns.set(key, created);
    return created;
  };

  for (const turn of turns) {
    const item = itemByEventId.get(turn.item_event_id) ?? null;
    const itemId = item?.item_id ?? `未知题目事件-${turn.item_event_id}`;
    ensureTurn(keyFor(itemId, turn.turn_seq), itemId, turn.turn_seq, item).turns.push(turn);
  }
  for (const attempt of attempts) {
    ensureTurn(keyFor(attempt.item_id, attempt.turn_seq), attempt.item_id, attempt.turn_seq, null).attempts.push(attempt);
  }
  const sessionInteractions: ParsedInteraction[] = [];
  for (const parsed of parsedInteractions) {
    const event = parsed.event;
    const linkedAttempt = event.attempt_id != null ? attemptById.get(event.attempt_id) : null;
    if (linkedAttempt) {
      const bucket = interactionsByAttempt.get(linkedAttempt.id) ?? [];
      bucket.push(parsed);
      interactionsByAttempt.set(linkedAttempt.id, bucket);
      continue;
    }
    if (event.item_id && event.turn_seq != null) {
      ensureTurn(keyFor(event.item_id, event.turn_seq), event.item_id, event.turn_seq, null).interactions.push(parsed);
    } else {
      sessionInteractions.push(parsed);
    }
  }

  const usedAudioIds = new Set<string>();
  const evidenceTurns: TurnEvidence[] = [];
  for (const row of mutableTurns.values()) {
    row.attempts.sort((a, b) => a.attempt_seq - b.attempt_seq || a.id - b.id);
    row.interactions.sort((a, b) => a.event.event_seq - b.event.event_seq);
    const rowIssues: EvidenceIssue[] = [];
    if (row.turns.length > 1) rowIssues.push(issue("duplicate_turn", "danger", "同一题目环节存在多条 Turn 研究真值记录。"));
    const turn = row.turns[0] ?? null;
    if (turn?.raw_audio_id) usedAudioIds.add(turn.raw_audio_id);
    const source = turn?.source_attempt_id != null ? attemptById.get(turn.source_attempt_id) ?? null : null;
    if (turn && turn.source_attempt_id == null) rowIssues.push(issue("source_attempt_missing", "danger", "Turn 未指向 source_attempt_id，无法证明终值来自哪次 AI 证据。"));
    if (turn?.source_attempt_id != null && !source) rowIssues.push(issue("source_attempt_not_found", "danger", `Turn 指向的 attempt #${turn.source_attempt_id} 不在场次账本中。`));
    if (!turn && row.attempts.length > 0) rowIssues.push(issue("turn_not_closed", "warn", "已有逐次 AI 证据，但尚未收口为 Turn 研究环节。"));
    if (turn?.judge_portrait_used) rowIssues.push(issue("turn_portrait_used", "danger", "Turn 标记为使用了画像数据，违反判分禁入约束。"));
    if (turn && source) {
      if (source.processing_status !== "completed") rowIssues.push(issue("source_not_completed", "danger", "Turn 指向的 source attempt 不是 completed。"));
      if (source.item_id !== row.itemId || source.turn_seq !== row.turnSeq) {
        rowIssues.push(issue("source_attempt_location_mismatch", "danger", "Turn 指向了其他题目或环节的 source attempt。"));
      }
      rowIssues.push(...turnAttemptConsistency(turn, source));
    }
    if (turn?.score_locked) {
      if ((turn.element_value ?? turn.reviewed_score) == null) rowIssues.push(issue("locked_score_missing", "danger", "Turn 标记已锁分，但研究分值缺失。"));
      if (!turn.reviewer_id) rowIssues.push(issue("reviewer_missing", "danger", "研究真值已锁定，但缺少锁分人身份。"));
      if (turn.confirmed_response_text == null) rowIssues.push(issue("confirmed_text_missing", "danger", "研究真值已锁定，但缺少人工确认文本。"));
    }
    for (const interaction of row.interactions) {
      if (interaction.event.attempt_id != null && !attemptById.has(interaction.event.attempt_id)) {
        rowIssues.push(issue("interaction_attempt_missing", "danger", `interaction #${interaction.event.event_seq} 指向不存在的 attempt #${interaction.event.attempt_id}。`));
      }
      if (interaction.payloadError) rowIssues.push(issue("payload_invalid", "danger", `interaction #${interaction.event.event_seq} ${interaction.payloadError}。`));
    }

    const attemptEvidence = row.attempts.map((attempt): AttemptEvidence => {
      const linked = [...(interactionsByAttempt.get(attempt.id) ?? [])].sort((a, b) => a.event.event_seq - b.event.event_seq);
      const attemptIssues: EvidenceIssue[] = [];
      const audio = audioById.get(attempt.raw_audio_id) ?? null;
      usedAudioIds.add(attempt.raw_audio_id);
      if (!audio) {
        attemptIssues.push(issue("audio_missing", "danger", `录音引用 ${attempt.raw_audio_id} 缺少对应音频资产。`));
      } else {
        if (!audioMatchesSession(audio, session)) attemptIssues.push(issue("audio_boundary_mismatch", "danger", "音频资产与场次归属或数据分区不一致。"));
        if (audio.turn_key !== keyFor(attempt.item_id, attempt.turn_seq)) attemptIssues.push(issue("audio_turn_mismatch", "danger", `音频 turn_key ${audio.turn_key ?? "缺失"} 与 attempt 位置不一致。`));
      }
      if (attempt.session_id !== session.session_id || (expectedSimulation !== null && attempt.is_simulation !== expectedSimulation)) {
        attemptIssues.push(issue("attempt_boundary_mismatch", "danger", "attempt 与当前场次或数据分区不一致。"));
      }
      if (attempt.judge_portrait_used) attemptIssues.push(issue("attempt_portrait_used", "danger", "attempt 标记为使用画像数据，不得用于运营判类或研究。"));
      if (attempt.processing_status === "technical_failure") {
        attemptIssues.push(issue("technical_failure", "danger", `AI 处理技术失败${attempt.error_code ? `：${attempt.error_code}` : "，错误码缺失"}。这不代表受试者回答错误。`));
      } else if (attempt.processing_status !== "completed") {
        attemptIssues.push(issue("attempt_incomplete", "warn", `attempt 停在 ${attempt.processing_status}，不可用于推进或研究结论。`));
      } else {
        if (typeof attempt.asr_text !== "string") attemptIssues.push(issue("asr_text_missing", "danger", "completed attempt 缺少权威 ASR 原文。"));
        if (!attempt.asr_engine_version) attemptIssues.push(issue("asr_engine_missing", "danger", "completed attempt 缺少 ASR 引擎版本。"));
        if (!attempt.operational_answer_type) attemptIssues.push(issue("operational_type_missing", "danger", "completed attempt 缺少 AI operational 类型。"));
        if (typeof attempt.operational_needs_review !== "boolean") attemptIssues.push(issue("review_flag_missing", "danger", "completed attempt 缺少需复核标记。"));
        if (!attempt.judge_mode || !attempt.judge_engine_version) attemptIssues.push(issue("judge_provenance_missing", "danger", "completed attempt 缺少 AI 判类模式或引擎版本。"));
      }
      for (const interaction of linked) {
        if (interaction.payloadError) attemptIssues.push(issue("payload_invalid", "danger", `interaction #${interaction.event.event_seq} ${interaction.payloadError}。`));
        if (interaction.event.item_id !== attempt.item_id
          || interaction.event.turn_seq !== attempt.turn_seq
          || interaction.event.attempt_seq !== attempt.attempt_seq) {
          attemptIssues.push(issue("interaction_attempt_mismatch", "danger", `interaction #${interaction.event.event_seq} 的题目/环节/次序与 attempt 不一致。`));
        }
      }
      attemptIssues.push(...requiredEvents(attempt, linked));
      return { attempt, interactions: linked, audio, isTurnSource: turn?.source_attempt_id === attempt.id, issues: attemptIssues };
    });

    evidenceTurns.push({
      key: row.key,
      itemId: row.itemId,
      turnSeq: row.turnSeq,
      responseRole: turn?.response_role ?? row.attempts[0]?.response_role ?? null,
      item: row.item,
      turn,
      attempts: attemptEvidence,
      interactions: row.interactions,
      issues: rowIssues,
      comparison: compareOperationalToResearch(turn, source),
    });
  }

  evidenceTurns.sort((left, right) => {
    const leftOrder = itemOrder.get(left.itemId) ?? Number.MAX_SAFE_INTEGER;
    const rightOrder = itemOrder.get(right.itemId) ?? Number.MAX_SAFE_INTEGER;
    return leftOrder - rightOrder || left.itemId.localeCompare(right.itemId) || left.turnSeq - right.turnSeq;
  });
  const unboundAudios = audios.filter((audio) => !usedAudioIds.has(audio.raw_audio_id));
  const allIssues = [
    ...sessionIssues,
    ...evidenceTurns.flatMap((turn) => [
      ...turn.issues,
      ...turn.attempts.flatMap((attempt) => attempt.issues),
    ]),
  ];
  return {
    turns: evidenceTurns,
    sessionInteractions,
    unboundAudios,
    issues: sessionIssues,
    summary: {
      totalAttempts: attempts.length,
      completedAttempts: attempts.filter((attempt) => attempt.processing_status === "completed").length,
      technicalFailures: attempts.filter((attempt) => attempt.processing_status === "technical_failure").length,
      incompleteAttempts: attempts.filter((attempt) => attempt.processing_status !== "completed" && attempt.processing_status !== "technical_failure").length,
      sourceBoundTurns: turns.filter((turn) => turn.source_attempt_id != null).length,
      lockedTruths: turns.filter((turn) => turn.score_locked).length,
      dangerIssues: allIssues.filter((row) => row.tone === "danger").length,
      warningIssues: allIssues.filter((row) => row.tone === "warn").length,
    },
  };
}
