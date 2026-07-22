import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Field } from "../components/Field";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/ToastContext";
import type { JournalTurn } from "../hooks/useSessionJournal";
import { turnKey } from "../lib/ids";
import type { SessionPlan } from "../types";
import {
  evaluateResearchReviewGate,
  planDisplayFacts,
  resolveResearchScoringContract,
  type AudioEvidenceStatus,
} from "./researchReviewGate";
import { assertPortraitFree } from "./scoring/vm";

export function ResearchReviewPanel({
  plan,
  turns,
  reviewerId,
  readOnly,
  rawAudioIdsByTurnKey = {},
  onUpsertTurn,
  onChanged,
}: {
  plan: SessionPlan;
  turns: Record<string, JournalTurn>;
  reviewerId: string;
  readOnly: boolean;
  rawAudioIdsByTurnKey?: Record<string, string>;
  onUpsertTurn: (
    itemId: string,
    turnSeq: number,
    patch: Partial<JournalTurn> & Pick<JournalTurn, "turnId" | "responseRole">,
  ) => void;
  onChanged?: () => void;
}) {
  return (
    <section className="form-section">
      <div className="form-section-header">
        <div>
          <h3>{readOnly ? "研究真值（已锁定）" : "事后研究复核与锁分"}</h3>
          <p className="muted">
            {readOnly
              ? "最终完成态只展示服务器已锁定的研究真值，不能再修改。"
              : "逐环节对照 AI 原始转写与提示等级，人工确认研究真值后再锁分；这里不会重新驱动老人端。"}
          </p>
        </div>
      </div>

      <div className="col" style={{ gap: 12 }}>
        {plan.items.map((item) => {
          const itemTurns = item.turns.map((planned) => ({
            planned,
            row: turns[turnKey(item.item_id, planned.turn_seq)],
          }));
          const locked = itemTurns.filter(({ row }) => row?.locked).length;
          return (
            <details key={item.item_id} className="card">
              <summary style={{ cursor: "pointer", fontWeight: 700 }}>
                {item.item_id} · {item.task_type} · {locked}/{itemTurns.length} 已锁定
              </summary>
              <div className="col" style={{ gap: 12, marginTop: 12 }}>
                {itemTurns.map(({ planned, row }) => (
                  <ReviewTurnRow
                    key={`${item.item_id}#${planned.turn_seq}`}
                    itemId={item.item_id}
                    taskType={item.task_type}
                    itemBankVersionId={plan.item_bank_version_id}
                    itemDisplay={item.display}
                    turnSeq={planned.turn_seq}
                    role={planned.response_role}
                    scoringKey={planned.scoring_key}
                    row={row}
                    reviewerId={reviewerId}
                    readOnly={readOnly}
                    rawAudioId={rawAudioIdsByTurnKey[turnKey(item.item_id, planned.turn_seq)]}
                    onUpsertTurn={onUpsertTurn}
                    onChanged={onChanged}
                  />
                ))}
              </div>
            </details>
          );
        })}
      </div>
    </section>
  );
}

function ReviewTurnRow({
  itemId,
  taskType,
  itemBankVersionId,
  itemDisplay,
  turnSeq,
  role,
  scoringKey,
  row,
  reviewerId,
  readOnly,
  rawAudioId,
  onUpsertTurn,
  onChanged,
}: {
  itemId: string;
  taskType: string;
  itemBankVersionId: string;
  itemDisplay: Record<string, unknown>;
  turnSeq: number;
  role: string;
  scoringKey: string | null;
  row?: JournalTurn;
  reviewerId: string;
  readOnly: boolean;
  rawAudioId?: string;
  onUpsertTurn: (
    itemId: string,
    turnSeq: number,
    patch: Partial<JournalTurn> & Pick<JournalTurn, "turnId" | "responseRole">,
  ) => void;
  onChanged?: () => void;
}) {
  const toast = useToast();
  const [confirmed, setConfirmed] = useState(row?.confirmedText ?? row?.asrText ?? "");
  const [score, setScore] = useState<string | null>(row?.elementValue == null ? null : String(row.elementValue));
  const [busy, setBusy] = useState<"confirm" | "lock" | null>(null);
  const [confirmLock, setConfirmLock] = useState(false);
  const [audioStatus, setAudioStatus] = useState<AudioEvidenceStatus>(rawAudioId?.trim() ? "idle" : "missing");
  const [scoreError, setScoreError] = useState<string | null>(null);
  const scoreRef = useRef<HTMLSelectElement>(null);
  const confirmationRequestRef = useRef<{
    turnId: number;
    expectedRevision: number;
    text: string;
    idempotencyKey: string;
  } | null>(null);

  useEffect(() => {
    setConfirmed(row?.confirmedText ?? row?.asrText ?? "");
    setScore(row?.elementValue == null ? null : String(row.elementValue));
    setScoreError(null);
  }, [row?.confirmedText, row?.asrText, row?.elementValue]);

  useEffect(() => {
    setAudioStatus(rawAudioId?.trim() ? "idle" : "missing");
  }, [rawAudioId]);

  if (!row) {
    return (
      <div className="card" style={{ background: "var(--c-bg)" }}>
        <div className="row wrap">
          <strong>环节 {turnSeq} · {role}</strong>
          <StatusPill tone="danger">缺少终值 Turn 证据</StatusPill>
        </div>
        <p className="muted">服务器没有返回该计划环节的终值记录，不能确认或锁分。</p>
      </div>
    );
  }
  const turn = row;

  const scoringContract = resolveResearchScoringContract(taskType, scoringKey, role);
  const scoreOptions = scoringContract.ok ? scoringContract.contract.options : [];
  const selectedScoreOption = scoreOptions.find((option) => option.value === score);
  const unsavedText = confirmed.trim() !== (turn.confirmedText ?? "").trim();
  const authoritativePromptLevel = turn.promptLevel;
  const gate = evaluateResearchReviewGate({
    taskType,
    scoringKey,
    responseRole: role,
    confirmedText: confirmed,
    reviewerId,
    promptLevel: authoritativePromptLevel,
    rawAudioId,
    audioStatus,
    selectedScore: score,
  });
  const displayFacts = planDisplayFacts(itemDisplay);

  function isConfirmationConflict(error: unknown): boolean {
    if (!(error instanceof ApiError) || error.status !== 409) return false;
    const detail = error.detailData;
    if (detail === null || typeof detail !== "object" || Array.isArray(detail)) return false;
    const code = (detail as { code?: unknown }).code;
    return code === "turn_confirmation_revision_conflict"
      || code === "turn_confirmation_replay_superseded"
      || code === "turn_confirmation_concurrency_conflict";
  }

  async function persistConfirmation(text: string) {
    const expectedRevision = turn.confirmationRevision ?? 0;
    let pending = confirmationRequestRef.current;
    if (!pending
        || pending.turnId !== turn.turnId
        || pending.expectedRevision !== expectedRevision
        || pending.text !== text) {
      pending = {
        turnId: turn.turnId,
        expectedRevision,
        text,
        idempotencyKey: `turn-confirm-${crypto.randomUUID()}`,
      };
      confirmationRequestRef.current = pending;
    }
    try {
      const updated = await api.confirmTurn(turn.turnId, {
        confirmed_response_text: text,
        expected_revision: pending.expectedRevision,
        idempotency_key: pending.idempotencyKey,
      });
      confirmationRequestRef.current = null;
      onUpsertTurn(itemId, turnSeq, {
        turnId: turn.turnId,
        responseRole: role,
        confirmed: true,
        confirmedText: updated.confirmed_response_text ?? text,
        confirmationRevision: updated.confirmation_revision,
      });
      return updated;
    } catch (error) {
      if (isConfirmationConflict(error)) {
        // 服务端已明确拒绝这一版：丢弃待决键并刷新权威 journal，
        // 绝不在前端猜测“可能已保存”。网络模糊失败则保留键供精确重试。
        confirmationRequestRef.current = null;
        onChanged?.();
      }
      throw error;
    }
  }

  async function saveConfirmation(): Promise<boolean> {
    const text = confirmed.trim();
    if (!text) {
      toast("人工确认文本不能为空；无作答请明确记录为“未作答”", "warn");
      return false;
    }
    if (!gate.contract.ok) {
      toast(`${gate.contract.reason} 已禁止保存研究复核。`, "danger");
      return false;
    }
    if (!unsavedText && turn.confirmed) return true;
    setBusy("confirm");
    try {
      await persistConfirmation(text);
      toast("人工确认文本已保存，AI 原始转写未被覆盖", "ok");
      return true;
    } catch (error) {
      toast(error instanceof ApiError ? error.detail : String(error), "danger");
      return false;
    } finally {
      setBusy(null);
    }
  }

  function focusScoreError(message: string) {
    setScoreError(message);
    scoreRef.current?.focus();
  }

  function requestLock() {
    const currentGate = evaluateResearchReviewGate({
      taskType,
      scoringKey,
      responseRole: role,
      confirmedText: confirmed,
      reviewerId,
      promptLevel: authoritativePromptLevel,
      rawAudioId,
      audioStatus,
      selectedScore: score,
    });
    const scoreBlocker = currentGate.blockers.find((blocker) => (
      blocker.code === "missing_score" || blocker.code === "invalid_score"
    ));
    if (scoreBlocker) {
      focusScoreError(scoreBlocker.message);
      return;
    }
    setScoreError(null);
    if (!currentGate.canLockScore) {
      toast(currentGate.blockers[0]?.message ?? "研究证据未通过门禁，不能锁分。", "danger");
      return;
    }
    setConfirmLock(true);
  }

  async function lockScore() {
    const currentGate = evaluateResearchReviewGate({
      taskType,
      scoringKey,
      responseRole: role,
      confirmedText: confirmed,
      reviewerId,
      promptLevel: authoritativePromptLevel,
      rawAudioId,
      audioStatus,
      selectedScore: score,
    });
    if (!currentGate.canLockScore || !currentGate.contract.ok) {
      const blocker = currentGate.blockers[0];
      if (blocker?.code === "missing_score" || blocker?.code === "invalid_score") {
        focusScoreError(blocker.message);
      } else {
        toast(blocker?.message ?? "研究证据未通过门禁，不能锁分。", "danger");
      }
      return;
    }
    const scoreOption = currentGate.contract.contract.options.find((option) => option.value === score);
    if (!scoreOption) {
      focusScoreError("所选分值不属于当前冻结评分合同。");
      return;
    }
    setBusy("lock");
    try {
      const text = confirmed.trim();
      if (!text) throw new Error("人工确认文本不能为空；无作答请明确记录为“未作答”");
      if (unsavedText || !turn.confirmed) {
        await persistConfirmation(text);
      }
      const payload = {
        element_value: Number(scoreOption.value),
      };
      assertPortraitFree(payload as unknown as Record<string, unknown>);
      const updated = await api.lockTurn(turn.turnId, payload);
      onUpsertTurn(itemId, turnSeq, {
        turnId: turn.turnId,
        responseRole: role,
        confirmed: true,
        confirmedText: updated.confirmed_response_text ?? text,
        confirmationRevision: updated.confirmation_revision,
        locked: true,
        elementValue: updated.element_value ?? Number(scoreOption.value),
        promptLevel: updated.prompt_level ?? authoritativePromptLevel,
      });
      setConfirmLock(false);
      onChanged?.();
      toast("研究真值已锁定；该环节不可再修改", "ok");
    } catch (error) {
      toast(error instanceof ApiError ? error.detail : String(error), "danger");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="card" style={{ background: "var(--c-bg)" }}>
      <div className="row wrap" style={{ justifyContent: "space-between" }}>
        <div className="row wrap">
          <strong>环节 {turnSeq} · {role}</strong>
          {row.sourceAttemptId != null
            ? <StatusPill tone="primary">AI attempt #{row.sourceAttemptId}</StatusPill>
            : <StatusPill tone="danger">无 source attempt</StatusPill>}
          {row.aiNeedsReview && <StatusPill tone="warn">AI 标记需复核</StatusPill>}
        </div>
        <StatusPill tone={row.locked ? "ok" : "warn"}>{row.locked ? "研究真值已锁定" : "待人工复核"}</StatusPill>
      </div>

      <div className="item-reference" style={{ marginTop: 12 }} aria-label={`${itemId} 冻结计划证据`}>
        <span>场次冻结题库版本</span><strong>{itemBankVersionId}</strong>
        <span>scoring_key</span><strong>{scoringKey ?? "null"}</strong>
        {displayFacts.length > 0
          ? displayFacts.map((fact) => (
            <span key={fact.path}><strong>{fact.path}</strong>：{fact.value}</span>
          ))
          : <span><strong>display</strong>：{"{}"}</span>}
      </div>

      <div className="row wrap" style={{ marginTop: 12 }}>
        {scoringContract.ok
          ? <StatusPill tone="primary">评分合同 {scoringContract.contract.id}</StatusPill>
          : <StatusPill tone="danger">未知评分合同，保存与锁分已关闭</StatusPill>}
        {!scoringContract.ok && <span className="field__error" role="alert">{scoringContract.reason}</span>}
      </div>

      <div className="form-grid" style={{ marginTop: 12 }}>
        <div className="field">
          <span className="field__label">AI 原始转写（只读）</span>
          <div className="card" style={{ background: "var(--c-surface)" }}>
            {row.asrText?.trim() || "（无转写文本）"}
          </div>
          <span className="field__hint">
            {row.asrConfidence == null ? "置信度未返回" : `ASR 置信度 ${Math.round(row.asrConfidence * 100)}%`}
            {row.aiAnswerType ? ` · operational 判类：${row.aiAnswerType}` : " · operational 判类未返回"}
          </span>
        </div>
        <Field label="人工确认的研究文本" hint={row.locked ? "锁定后不可修改" : "不覆盖 AI 原文；无作答请明确填写“未作答”"}>
          {readOnly || row.locked ? (
            <div className="card" style={{ background: "var(--c-surface)" }}>{row.confirmedText || "（未确认）"}</div>
          ) : (
            <textarea className="form-control" rows={3} value={confirmed} disabled={busy !== null}
              onChange={(event) => setConfirmed(event.target.value)} />
          )}
        </Field>
      </div>

      <div className="field" style={{ marginTop: 12 }}>
        <span className="field__label">原声回放（受控）</span>
        <div role="group" aria-label={`${itemId} · ${role} 原声回放`}>
          {rawAudioId?.trim()
            ? <ReviewEvidenceAudio rawAudioId={rawAudioId} onStatusChange={setAudioStatus} />
            : <StatusPill tone="warn">录音引用未提供</StatusPill>}
        </div>
        <span className="field__hint">仅在具名有权账号显式点击后请求；成功完成鉴权加载并可由浏览器读取后，才开放锁分。</span>
      </div>

      <div className="form-grid" style={{ marginTop: 12 }}>
        {!readOnly && !row.locked && scoringContract.ok && (
          <Field label="研究评分" required error={scoreError}>
            <select ref={scoreRef} className="form-control" value={score ?? ""}
              required aria-required="true" aria-invalid={scoreError ? "true" : undefined}
              disabled={busy !== null}
              onChange={(event) => {
                setScore(event.target.value || null);
                setScoreError(null);
              }}>
              <option value="">请选择</option>
              {scoreOptions.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </Field>
        )}
        <Field label="本环节最高提示等级（服务器证据，只读）" hint="0 自发 · 1 轻提示 · 2 明确提示 · 3 已告知答案；研究复核不能覆盖原始提示轨迹。">
          <div className="card" style={{ background: "var(--c-surface)" }}>
            {authoritativePromptLevel == null ? "服务器未返回" : `${authoritativePromptLevel} 级`}
          </div>
        </Field>
        {!readOnly && !row.locked && (
          <div className="form-actions" style={{ alignSelf: "end" }}>
            <Button aria-label={`保存 ${itemId} · ${role} 的人工确认文本`}
              disabled={busy !== null || !gate.canSaveConfirmation} onClick={() => { void saveConfirmation(); }}>
              {busy === "confirm" ? "正在保存…" : unsavedText || !row.confirmed ? "保存人工确认" : "确认文本已保存"}
            </Button>
            <Button variant="primary" aria-label={`锁定 ${itemId} · ${role} 的研究真值`}
              disabled={busy !== null || !gate.contract.ok
                || gate.blockers.some((blocker) => blocker.code !== "missing_score" && blocker.code !== "invalid_score")}
              onClick={requestLock}>
              锁定研究真值
            </Button>
          </div>
        )}
      </div>

      {!readOnly && !row.locked && gate.blockers.some((blocker) => (
        blocker.code === "missing_audio_reference"
        || blocker.code === "audio_not_loaded"
        || blocker.code === "audio_load_failed"
      )) && (
        <p className="field__error" role="alert" style={{ marginTop: 12 }}>
          {gate.blockers.find((blocker) => blocker.code.startsWith("audio_") || blocker.code === "missing_audio_reference")?.message}
        </p>
      )}

      <ConfirmDialog open={confirmLock} title="确认锁定研究真值？"
        body={`题目“${itemId}”环节“${role}”将按 ${scoringContract.ok ? scoringContract.contract.id : "未知合同"} 锁定为 ${selectedScoreOption?.label ?? "未选"}；服务器证据中的提示等级为 ${authoritativePromptLevel == null ? "未返回" : authoritativePromptLevel}。系统会先保存当前人工确认文本；锁定后不可修改。`}
        confirmLabel={busy === "lock" ? "正在锁定…" : "确认锁定"}
        onConfirm={() => { if (!busy) void lockScore(); }}
        onCancel={() => { if (!busy) setConfirmLock(false); }} />
    </div>
  );
}

function ReviewEvidenceAudio({
  rawAudioId,
  onStatusChange,
}: {
  rawAudioId: string;
  onStatusChange: (status: AudioEvidenceStatus) => void;
}) {
  const [armed, setArmed] = useState(false);
  const [src, setSrc] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [phase, setPhase] = useState<AudioEvidenceStatus>("idle");

  function publishPhase(next: AudioEvidenceStatus) {
    setPhase(next);
    onStatusChange(next);
  }

  useEffect(() => {
    setArmed(false);
    setSrc(null);
    setError(null);
    setPhase("idle");
    onStatusChange("idle");
  }, [rawAudioId, onStatusChange]);

  useEffect(() => {
    if (!armed) return;
    let cancelled = false;
    let objectUrl: string | null = null;
    setSrc(null);
    setError(null);
    setPhase("loading");
    onStatusChange("loading");
    api.getAudioBlob(rawAudioId)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        const message = cause instanceof Error ? cause.message : String(cause);
        setError(message);
        setPhase("error");
        onStatusChange("error");
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [armed, onStatusChange, rawAudioId, retry]);

  if (!armed) {
    return <Button onClick={() => setArmed(true)}>加载并核对录音</Button>;
  }
  if (error) {
    return (
      <span className="row wrap">
        <StatusPill tone="danger">鉴权回放载入失败</StatusPill>
        <span className="field__error" role="alert">{error}</span>
        <Button onClick={() => setRetry((value) => value + 1)}>重试回放</Button>
      </span>
    );
  }
  if (!src) return <StatusPill tone="muted">正在鉴权并准备回放…</StatusPill>;
  return (
    <span className="row wrap">
      <StatusPill tone={phase === "ready" ? "ok" : "muted"}>
        {phase === "ready" ? "录音证据已载入" : "正在验证录音格式…"}
      </StatusPill>
      <audio
        aria-label={`原始录音 ${rawAudioId}`}
        controls
        preload="metadata"
        src={src}
        style={{ height: 36 }}
        onLoadedMetadata={() => publishPhase("ready")}
        onError={() => {
          setError("浏览器无法读取该录音；请检查录音文件后重试。");
          publishPhase("error");
        }}
      />
    </span>
  );
}
