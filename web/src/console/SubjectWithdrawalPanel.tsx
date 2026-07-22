import { useState } from "react";

import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { useDialogFocusTrap } from "../components/useDialogFocusTrap";
import type {
  PatientSummary,
  PatientWithdrawalReceipt,
  WithdrawalReasonCode,
} from "../types";
import {
  newWithdrawalIdempotencyKey,
  WITHDRAWAL_REASON_LABELS,
  withdrawalReceiptMatches,
} from "./subjectWithdrawal";

export function SubjectWithdrawalPanel({ patient, onCancel, onDone }: {
  patient: PatientSummary;
  onCancel: () => void;
  onDone: (receipt: PatientWithdrawalReceipt) => void;
}) {
  const [reason, setReason] = useState<WithdrawalReasonCode>("participant_request");
  const [acknowledged, setAcknowledged] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stateChanged, setStateChanged] = useState(false);
  // Created once when the action opens and deliberately retained after every
  // timeout/error.  A retry can therefore never create a second withdrawal.
  const [idempotencyKey] = useState(() => newWithdrawalIdempotencyKey());

  const cancelSafely = () => {
    if (busy) return;
    if (confirmOpen) {
      setConfirmOpen(false);
      return;
    }
    onCancel();
  };
  const panelRef = useDialogFocusTrap<HTMLElement>({
    open: true,
    onCancel: cancelSafely,
    initialFocus: confirmOpen ? "first-button" : "first-control",
    focusKey: confirmOpen,
  });

  const submit = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const receipt = await api.withdrawPatient(patient.patient_id, {
        idempotency_key: idempotencyKey,
        expected_governance_revision: patient.governance_revision,
        reason_code: reason,
      });
      onDone(receipt);
      return;
    } catch (original) {
      // A timeout can happen after the server committed.  Reconcile against the
      // authoritative receipt before showing a retry; never rotate the key.
      try {
        const receipt = await api.patientWithdrawal(patient.patient_id);
        if (withdrawalReceiptMatches(
          receipt, patient.patient_id, patient.governance_revision, reason,
        )) {
          onDone(receipt);
          return;
        }
      } catch { /* no exact receipt: preserve original failure and same key */ }
      if (original instanceof ApiError && original.status === 409) {
        setStateChanged(true);
        setError(`${original.detail}。权威治理状态已变化，请取消返回登记表刷新后再决定；本窗口不会自动覆盖。`);
        return;
      }
      const detail = original instanceof ApiError ? original.detail
        : original instanceof Error ? original.message
          : "服务器未确认研究撤回结果";
      setError(`${detail}。请保持本窗口并用同一按钮重试；系统会先对账，不会重复登记。`);
    } finally {
      setBusy(false);
      setConfirmOpen(false);
    }
  };

  return (
    <div className="dialog-backdrop" onClick={cancelSafely}>
      <section ref={panelRef} className="dialog-panel fade-in" role="dialog" aria-modal="true"
        aria-busy={busy || undefined} tabIndex={-1}
        aria-labelledby="subject-withdrawal-title" onClick={(event) => event.stopPropagation()}>
        <div className="dialog-header">
          <h3 id="subject-withdrawal-title">
            {confirmOpen ? "最终确认：受试者退出整个研究？" : "登记研究撤回"}
          </h3>
        </div>
        {confirmOpen ? (
          <>
            <Alert tone="danger" title={`真实研究档案 ${patient.patient_id}`}>
              档案将进入不可由普通接口恢复的 withdrawn 终态；相关录音只会先隔离，物理删除仍须管理员逐条执行。
            </Alert>
            <p className="muted">撤回原因：{WITHDRAWAL_REASON_LABELS[reason]}。</p>
            <div className="dialog-actions">
              <Button disabled={busy} onClick={() => setConfirmOpen(false)}>返回核对</Button>
              <Button variant="danger" disabled={busy} onClick={() => { void submit(); }}>
                {busy ? "正在登记…" : "确认登记研究撤回"}
              </Button>
            </div>
          </>
        ) : (
          <>
            <Alert tone="danger" title={`真实研究档案 ${patient.patient_id}`}>
              这是受试者级治理终态：系统会停止在途训练与 AI、隔离该受试者全部录音，并关闭普通内容读取。普通“提前结束场次”不使用这里，也不会删除证据。
            </Alert>
            <label className="field">
              <span>权威原因（闭表）</span>
              <select className="form-control" value={reason} disabled={busy}
                onChange={(event) => setReason(event.target.value as WithdrawalReasonCode)}>
                {(Object.entries(WITHDRAWAL_REASON_LABELS) as [WithdrawalReasonCode, string][])
                  .map(([value, label]) => <option value={value} key={value}>{label}</option>)}
              </select>
            </label>
            <label className="check-row">
              <input type="checkbox" checked={acknowledged} disabled={busy}
                onChange={(event) => setAcknowledged(event.target.checked)} />
              <span>我确认这是“受试者退出整个研究”，不是某一场普通暂停或提前结束。</span>
            </label>
            {error && <Alert tone="danger" title="撤回结果尚未确认">{error}</Alert>}
            <div className="dialog-actions">
              <Button disabled={busy} onClick={cancelSafely}>取消</Button>
              <Button variant="danger" disabled={!acknowledged || busy || stateChanged}
                onClick={() => setConfirmOpen(true)}>
                {busy ? "正在对账…" : "进入最终确认"}
              </Button>
            </div>
          </>
        )}
      </section>
    </div>
  );
}
