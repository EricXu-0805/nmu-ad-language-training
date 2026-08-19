import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { StatusPill } from "../components/StatusPill";
import {
  buildSessionCloseoutRequest,
  closeoutFailureNeedsReconciliation,
  EMPTY_SESSION_CLOSEOUT_FLAGS,
  SESSION_CLOSEOUT_NOTE_MAX_LENGTH,
  sessionCloseoutDraftFromRecord,
  sessionCloseoutDraftMatchesRecord,
  type SessionCloseoutDraft,
  type SessionCloseoutEditorGateState,
  type SessionCloseoutObservationFlags,
  type SessionCloseoutOutcomeSummary,
  type SessionCloseoutRecord,
  type SessionCloseoutReportStatus,
  type SessionCloseoutSaveRequest,
} from "./sessionCloseout";

const OBSERVATION_OPTIONS: {
  key: keyof SessionCloseoutObservationFlags;
  label: string;
}[] = [
  { key: "fatigue_observed", label: "观察到明显疲劳表现" },
  { key: "distress_or_discomfort_observed", label: "观察到紧张或不适表现" },
  { key: "participant_declined_to_continue", label: "受试者明确表示不继续" },
  { key: "staff_assistance_occurred", label: "工作人员提供了操作或沟通协助" },
  { key: "environment_interruption_occurred", label: "环境出现中断" },
  { key: "device_or_network_interruption_occurred", label: "设备或网络出现中断" },
];

const newCloseoutIdempotencyKey = () => `closeout-${crypto.randomUUID()}`;

export function SessionCloseoutPanel({
  outcomeSummary,
  closeout,
  busy = false,
  error = null,
  onSave,
  onReconcile,
  onGateChange,
}: {
  outcomeSummary: SessionCloseoutOutcomeSummary;
  closeout: SessionCloseoutRecord | null;
  busy?: boolean;
  error?: string | null;
  // Promise 只应在服务器确认保存后 resolve；未知结果必须 reject，以便重试复用同一幂等键。
  onSave: (request: SessionCloseoutSaveRequest) => Promise<SessionCloseoutRecord>;
  onReconcile: () => Promise<SessionCloseoutRecord | null>;
  onGateChange?: (state: SessionCloseoutEditorGateState) => void;
}) {
  const titleId = useId();
  const choiceLegendId = useId();
  const noteHintId = useId();
  const [draft, setDraft] = useState<SessionCloseoutDraft>(() => sessionCloseoutDraftFromRecord(closeout));
  const [validationErrors, setValidationErrors] = useState<string[]>([]);
  const [saveFailure, setSaveFailure] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [reconciling, setReconciling] = useState(false);
  const [pendingRequest, setPendingRequest] = useState<SessionCloseoutSaveRequest | null>(null);
  const [reconciliationState, setReconciliationState] = useState<"required" | "confirmed_missing" | null>(null);
  const saveIntentKey = useRef(newCloseoutIdempotencyKey());
  const locked = closeout?.locked === true;
  const effectiveBusy = busy || submitting || reconciling;
  const closeoutRevision = closeout?.revision ?? null;
  const dirty = !sessionCloseoutDraftMatchesRecord(draft, closeout);

  useEffect(() => {
    setDraft(sessionCloseoutDraftFromRecord(closeout));
    setValidationErrors([]);
    setSaveFailure(null);
    setPendingRequest(null);
    setReconciliationState(null);
    saveIntentKey.current = newCloseoutIdempotencyKey();
  }, [closeoutRevision]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    onGateChange?.({
      dirty,
      submitting: submitting || reconciling,
      reconciliation_required: reconciliationState !== null,
    });
  }, [dirty, onGateChange, reconciliationState, reconciling, submitting]);

  useEffect(() => () => onGateChange?.({
    dirty: false,
    submitting: false,
    reconciliation_required: false,
  }), [onGateChange]);

  function chooseObservation(status: SessionCloseoutReportStatus) {
    setValidationErrors([]);
    setSaveFailure(null);
    setDraft((current) => status === "no_additional_observation"
      ? { ...current, report_status: status, note: "", ...EMPTY_SESSION_CLOSEOUT_FLAGS }
      : { ...current, report_status: status });
  }

  function setObservationFlag(key: keyof SessionCloseoutObservationFlags, checked: boolean) {
    setValidationErrors([]);
    setSaveFailure(null);
    setDraft((current) => ({ ...current, [key]: checked }));
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (effectiveBusy || locked || reconciliationState !== null) return;
    const intentKey = saveIntentKey.current;
    const result = buildSessionCloseoutRequest(draft, closeoutRevision, intentKey);
    if (!result.ok) {
      setValidationErrors(result.errors);
      return;
    }
    setValidationErrors([]);
    setSaveFailure(null);
    setSubmitting(true);
    try {
      await onSave(result.value);
      // 只有调用方确认保存成功后才结算本次意图；失败重试继续复用原 key。
      if (saveIntentKey.current === intentKey) saveIntentKey.current = newCloseoutIdempotencyKey();
    } catch (cause) {
      const message = cause instanceof Error && cause.message.trim()
        ? cause.message
        : "现场收尾尚未保存。";
      if (closeoutFailureNeedsReconciliation(cause)) {
        setPendingRequest(result.value);
        setReconciliationState("required");
        await reconcileAfterFailure(result.value, message);
      } else {
        setSaveFailure(message);
        saveIntentKey.current = newCloseoutIdempotencyKey();
      }
    } finally {
      setSubmitting(false);
    }
  }

  async function reconcileAfterFailure(request: SessionCloseoutSaveRequest, reason: string) {
    setReconciling(true);
    try {
      const authoritative = await onReconcile();
      if (authoritative) {
        setPendingRequest(null);
        setReconciliationState(null);
        setSaveFailure("服务器已找回保存结果，页面已按权威版本恢复。");
        return;
      }
      setPendingRequest(request);
      setReconciliationState("confirmed_missing");
      setSaveFailure(`${reason}。服务器上还没有这份记录，请点「用原内容安全重试」。`);
    } catch (cause) {
      setPendingRequest(request);
      setReconciliationState("required");
      setSaveFailure(`${reason}。尚无法核对服务器记录：${failureMessage(cause)}`);
    } finally {
      setReconciling(false);
    }
  }

  async function reconcileNow() {
    if (!pendingRequest || effectiveBusy) return;
    await reconcileAfterFailure(pendingRequest, saveFailure ?? "上次保存结果不确定");
  }

  async function retryExactRequest() {
    if (!pendingRequest || effectiveBusy || reconciliationState !== "confirmed_missing") return;
    setSubmitting(true);
    setSaveFailure(null);
    try {
      await onSave(pendingRequest);
      setPendingRequest(null);
      setReconciliationState(null);
      saveIntentKey.current = newCloseoutIdempotencyKey();
    } catch (cause) {
      setReconciliationState("required");
      await reconcileAfterFailure(pendingRequest, failureMessage(cause));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="form-section" aria-labelledby={titleId} aria-busy={effectiveBusy || undefined}>
      <div className="form-section-header">
        <div>
          <h3 id={titleId}>现场收尾记录</h3>
          <p className="muted">老人端干预已经停止。本区只记录可直接观察到的事实，不判断原因，也不修改 AI 或录音证据。</p>
        </div>
        <div className="row wrap">
          <StatusPill tone={locked ? "ok" : closeout ? "primary" : "muted"}>
            {closeout ? `版本 ${closeout.revision}` : "尚未保存"}
          </StatusPill>
          <StatusPill tone={locked ? "ok" : "warn"}>{locked ? "已锁定 · 只读" : "待确认"}</StatusPill>
        </div>
      </div>

      {(error || saveFailure) && (
        <Alert tone="danger" title="现场收尾保存失败"
          actions={reconciliationState === "required"
            ? <Button disabled={effectiveBusy} onClick={() => { void reconcileNow(); }}>核对服务器记录</Button>
            : reconciliationState === "confirmed_missing"
              ? <Button disabled={effectiveBusy} onClick={() => { void retryExactRequest(); }}>用原内容安全重试</Button>
              : undefined}>
          {error ?? saveFailure}
        </Alert>
      )}
      {validationErrors.length > 0 && (
        <Alert tone="warn" title="请完成现场观察确认">
          <ul>{validationErrors.map((message) => <li key={message}>{message}</li>)}</ul>
        </Alert>
      )}

      {locked && closeout ? (
        <ReadOnlyCloseout closeout={closeout} />
      ) : (
        <form className="col" onSubmit={(event) => { void submit(event); }}>
          <fieldset className="card col" disabled={effectiveBusy || reconciliationState !== null} aria-labelledby={choiceLegendId}>
            <legend id={choiceLegendId} className="field__label">本场是否有额外现场观察</legend>
            <label className="toggle-field">
              <input type="radio" name="closeout-observation-choice" value="no_additional_observation"
                checked={draft.report_status === "no_additional_observation"}
                onChange={() => chooseObservation("no_additional_observation")} />
              无额外观察
            </label>
            <label className="toggle-field">
              <input type="radio" name="closeout-observation-choice" value="observation_recorded"
                checked={draft.report_status === "observation_recorded"}
                onChange={() => chooseObservation("observation_recorded")} />
              已记录观察
            </label>
          </fieldset>

          {draft.report_status === "observation_recorded" && (
            <div className="card col">
              <fieldset className="col" disabled={effectiveBusy || reconciliationState !== null}>
                <legend className="field__label">可观察事实（可多选）</legend>
                <div className="form-grid">
                  {OBSERVATION_OPTIONS.map((option) => (
                    <label className="toggle-field" key={option.key}>
                      <input type="checkbox" checked={draft[option.key]}
                        onChange={(event) => setObservationFlag(option.key, event.target.checked)} />
                      {option.label}
                    </label>
                  ))}
                </div>
              </fieldset>

              <label className="field">
                <span className="field__label">现场备注（可选）</span>
                <textarea className="form-control" rows={5} maxLength={SESSION_CLOSEOUT_NOTE_MAX_LENGTH}
                  value={draft.note} disabled={effectiveBusy || reconciliationState !== null}
                  aria-describedby={noteHintId}
                  onChange={(event) => {
                    setValidationErrors([]);
                    setSaveFailure(null);
                    setDraft((current) => ({ ...current, note: event.target.value }));
                  }} />
                <span className="field__hint" id={noteHintId}>
                  只记录现场看到或听到的事实；{draft.note.length}/{SESSION_CLOSEOUT_NOTE_MAX_LENGTH} 字
                </span>
              </label>
            </div>
          )}

          <div className="form-actions">
            <Button type="submit" variant="primary" disabled={effectiveBusy || reconciliationState !== null}>
              {effectiveBusy ? "正在保存现场收尾…" : "保存现场收尾"}
            </Button>
          </div>
        </form>
      )}

      <details className="card col">
        <summary style={{ cursor: "pointer", fontWeight: 650 }}>
          查看本场自动汇总
        </summary>
        <p className="muted">以下为本场自动统计，仅供查看。</p>
        <div className="metric-grid" aria-label="服务器权威场次结果概览">
          <OutcomeMetric label="计划环节" value={outcomeSummary.expected_turns} />
          <OutcomeMetric label="匹配终值" value={outcomeSummary.matched_turns} />
          <OutcomeMetric label="AI 完成环节" value={outcomeSummary.completed_attempt_turns} />
          <OutcomeMetric label="录音证据环节" value={outcomeSummary.audio_evidenced_turns} />
          {outcomeSummary.total_attempts != null && (
            <OutcomeMetric label="AI 处理总次数" value={outcomeSummary.total_attempts} />
          )}
          {outcomeSummary.completed_attempts != null && (
            <OutcomeMetric label="处理成功次数" value={outcomeSummary.completed_attempts} />
          )}
          {outcomeSummary.needs_review_attempts != null && (
            <OutcomeMetric label="AI 标记待复核" value={outcomeSummary.needs_review_attempts} />
          )}
          {outcomeSummary.technical_failure_attempts != null && (
            <OutcomeMetric label="技术失败次数" value={outcomeSummary.technical_failure_attempts} />
          )}
          {outcomeSummary.technical_pause_count != null && (
            <OutcomeMetric label="技术安全暂停" value={outcomeSummary.technical_pause_count} />
          )}
          {outcomeSummary.researcher_takeover_count != null && (
            <OutcomeMetric label="人工接管" value={outcomeSummary.researcher_takeover_count} />
          )}
          {outcomeSummary.prompt_level_0_count != null && (
            <OutcomeMetric label="无提示作答数" value={outcomeSummary.prompt_level_0_count} />
          )}
          {outcomeSummary.prompt_level_1_count != null && (
            <OutcomeMetric label="1 级提示作答数" value={outcomeSummary.prompt_level_1_count} />
          )}
          {outcomeSummary.prompt_level_2_count != null && (
            <OutcomeMetric label="2 级提示作答数" value={outcomeSummary.prompt_level_2_count} />
          )}
          {outcomeSummary.prompt_level_3_count != null && (
            <OutcomeMetric label="3 级提示作答数" value={outcomeSummary.prompt_level_3_count} />
          )}
        </div>
      </details>
    </section>
  );
}

function failureMessage(cause: unknown): string {
  return cause instanceof Error && cause.message.trim()
    ? cause.message
    : "现场收尾尚未保存";
}

function OutcomeMetric({ label, value }: { label: string; value: number }) {
  return (
    <div className="metric-card">
      <span className="metric-card__label">{label}</span>
      <strong className="metric-card__value">{value}</strong>
    </div>
  );
}

function ReadOnlyCloseout({ closeout }: { closeout: SessionCloseoutRecord }) {
  const selected = OBSERVATION_OPTIONS.filter((option) => closeout[option.key]);
  return (
    <div className="card col">
      <strong>{closeout.report_status === "no_additional_observation" ? "无额外观察" : "已记录观察"}</strong>
      {closeout.report_status === "observation_recorded" && selected.length > 0 && (
        <ul>{selected.map((option) => <li key={option.key}>{option.label}</li>)}</ul>
      )}
      {closeout.note && <p style={{ whiteSpace: "pre-wrap" }}>{closeout.note}</p>}
      <p className="muted">
        {closeout.recorded_by ? `记录人 ${closeout.recorded_by}` : "记录人未返回"}
        {closeout.recorded_at ? ` · ${closeout.recorded_at.replace("T", " ").slice(0, 19)}` : ""}
      </p>
    </div>
  );
}
