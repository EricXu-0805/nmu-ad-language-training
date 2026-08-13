import { useState } from "react";
import { api } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Field, TextInput } from "../components/Field";
import { StatusPill } from "../components/StatusPill";
import type {
  AssessmentCancellationReason,
  AssessmentCloseoutReportStatus,
  AssessmentDeferralReason,
  AssessmentEvent,
  AssessmentInstance,
  ScaleProtocolReadiness,
} from "../types";
import {
  assessmentActionGates,
  parseAssessmentMutationFailure,
  parseResponseInput,
  performAssessmentMutation,
  type AssessmentMutationFailure,
} from "./assessmentExecution";
import { assessmentCategoryLabel, assessmentInstanceStatusLabel } from "./assessmentQueue";

const CANCEL_REASONS: [AssessmentCancellationReason, string][] = [
  ["schedule_changed", "排期变更"],
  ["participant_unavailable", "受试者无法到场"],
  ["protocol_correction", "研究计划修正"],
  ["duplicate_event", "重复事件"],
];
const DEFER_REASONS: [AssessmentDeferralReason, string][] = [
  ["participant_unavailable", "受试者无法到场"],
  ["clinical_or_safety", "临床/安全原因"],
  ["technical_failure", "技术故障"],
  ["authorized_reschedule", "已批准改约"],
];
const CLOSE_FLAGS: [keyof CloseFlags, string][] = [
  ["fatigue_observed", "观察到疲劳"],
  ["distress_or_discomfort_observed", "观察到不适/情绪反应"],
  ["participant_declined_to_continue", "受试者拒绝继续"],
  ["staff_assistance_occurred", "有工作人员协助"],
  ["environment_interruption_occurred", "环境打断"],
  ["device_or_network_interruption_occurred", "设备/网络中断"],
];

interface CloseFlags {
  fatigue_observed: boolean;
  distress_or_discomfort_observed: boolean;
  participant_declined_to_continue: boolean;
  staff_assistance_occurred: boolean;
  environment_interruption_occurred: boolean;
  device_or_network_interruption_occurred: boolean;
}

function key(scope: string): string {
  return `assess-${scope}-${crypto.randomUUID()}`;
}

function FailureAlert({ failure }: { failure: AssessmentMutationFailure }) {
  return (
    <Alert tone="danger" title="服务器拒绝了该评估操作">
      <p>{failure.message}</p>
      {failure.hint && <p>{failure.hint}</p>}
      {failure.blockingHints.length > 0 && (
        <ul>{failure.blockingHints.map((hint) => <li key={hint}>{hint}</li>)}</ul>
      )}
    </Alert>
  );
}

// 正式评估执行抽屉(收据 150 S5):条目键由 PI 冻结施测表载明,线上契约有意
// 不携带条目清单(不透明键),故按键录入;进度以服务端 required/answered 计数为准。
export function AssessmentExecutionDrawer({ event, readiness, onReceipt, onDismiss }: {
  event: AssessmentEvent;
  readiness: ScaleProtocolReadiness;
  onReceipt: (next: AssessmentEvent) => void;
  onDismiss: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<AssessmentMutationFailure | null>(null);
  const [cancelReason, setCancelReason] = useState<AssessmentCancellationReason>("schedule_changed");
  const gates = assessmentActionGates(event);

  // 返回"服务器是否真的接受了"。调用方据此决定要不要清草稿——finally 里
  // 永远不做清理，因为 finally 分不出成功和失败。
  async function run(action: () => Promise<AssessmentEvent>): Promise<boolean> {
    if (busy) return false;
    setBusy(true);
    setFailure(null);
    try {
      const outcome = await performAssessmentMutation(action, onReceipt);
      if (!outcome.ok) {
        setFailure(outcome.failure);
        return false;
      }
      return true;
    } catch (error) {
      // onReceipt 自己抛错也算没成交：宁可让研究者重来一次，也不能清掉草稿。
      setFailure(parseAssessmentMutationFailure(error));
      return false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="form-section" aria-label="正式评估执行">
      <div className="form-section-header">
        <div>
          <p className="page-kicker">正式评估·执行</p>
          <h3>{event.patient_id} · {event.timepoint} · {event.scheduled_date}</h3>
          <p className="muted">{gates.nextStep}</p>
        </div>
        <div className="row wrap">
          <StatusPill tone={event.status === "in_progress" ? "ok" : "muted"}>{event.status}</StatusPill>
          <Button onClick={onDismiss} disabled={busy}>收起</Button>
        </div>
      </div>
      {failure && <FailureAlert failure={failure} />}

      {gates.canStart && (
        <div className="row wrap">
          <Button variant="primary" disabled={busy}
            onClick={() => void run(() => api.startAssessmentEvent(event, {
              expected_event_revision: event.revision,
              idempotency_key: key("start"),
            }, readiness))}>启动评估事件</Button>
          <select className="form-control" value={cancelReason} disabled={busy}
            onChange={(entry) => setCancelReason(entry.target.value as AssessmentCancellationReason)}>
            {CANCEL_REASONS.map(([code, label]) => <option key={code} value={code}>{label}</option>)}
          </select>
          <Button variant="danger" disabled={busy}
            onClick={() => void run(() => api.cancelAssessmentEvent(event, {
              expected_event_revision: event.revision,
              idempotency_key: key("cancel"),
              reason_code: cancelReason,
            }, readiness))}>取消(未开始)</Button>
        </div>
      )}

      {event.instances.map((instance) => (
        <InstanceCard key={instance.instance_id} event={event} instance={instance}
          readiness={readiness} busy={busy}
          enabled={gates.instanceActions[instance.instance_id]}
          run={run} />
      ))}

      {gates.canClose && (
        <CloseoutForm event={event} readiness={readiness} busy={busy} run={run} />
      )}
    </section>
  );
}

function InstanceCard({ event, instance, readiness, busy, enabled, run }: {
  event: AssessmentEvent;
  instance: AssessmentInstance;
  readiness: ScaleProtocolReadiness;
  busy: boolean;
  enabled: { canRespond: boolean; canComplete: boolean; canDefer: boolean };
  run: (action: () => Promise<AssessmentEvent>) => Promise<boolean>;
}) {
  const [itemKey, setItemKey] = useState("");
  const [rawValue, setRawValue] = useState("");
  const [expectedItemRevision, setExpectedItemRevision] = useState(0);
  const [grant, setGrant] = useState<{ itemKey: string; digest: string; revision: number } | null>(null);
  const [grantBusy, setGrantBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [deferReason, setDeferReason] = useState<AssessmentDeferralReason>("participant_unavailable");
  const [deferredUntil, setDeferredUntil] = useState("");

  async function issueGrant() {
    const trimmed = itemKey.trim();
    if (!trimmed) { setLocalError("先填写条目键再签发录音授权"); return; }
    setGrantBusy(true);
    setLocalError(null);
    try {
      // 重签失败不得毁掉手上那份仍然有效的授权：原实现在 catch 里
      // setGrant(null)，于是一次网络抖动就把已经签好的授权清成 null，
      // 研究者再保存作答时会变成"无授权"提交。
      const outcome = await performAssessmentMutation(
        () => api.issueAssessmentRecordingAuthorization(
          event, instance, trimmed, readiness),
        (issued) => {
          setGrant({
            itemKey: trimmed,
            digest: issued.authorizedArtifactDigest,
            revision: issued.itemRevision,
          });
          setExpectedItemRevision(issued.itemRevision - 1);
        });
      if (!outcome.ok) {
        setLocalError(outcome.failure.message);
      }
    } finally {
      setGrantBusy(false);
    }
  }

  async function submitResponse() {
    const trimmed = itemKey.trim();
    if (!trimmed) { setLocalError("请填写 PI 施测表上的条目键"); return; }
    const parsed = parseResponseInput(rawValue);
    if (!parsed.ok) { setLocalError(parsed.reason ?? "作答无效"); return; }
    setLocalError(null);
    const artifact = grant && grant.itemKey === trimmed ? grant.digest : undefined;
    const saved = await run(() => api.submitAssessmentItemResponse(event, instance, trimmed, {
      response: artifact === undefined
        ? { value: parsed.value }
        : { value: parsed.value, authorized_artifact_digest: artifact },
      expected_event_revision: event.revision,
      expected_instance_revision: instance.revision,
      expected_item_revision: expectedItemRevision,
      idempotency_key: key("response"),
    }, readiness));
    if (saved !== true) return;
    setGrant(null);
    setRawValue("");
    setExpectedItemRevision(0);
  }

  return (
    <article className="review-card">
      <div className="review-card-main">
        <strong>{assessmentCategoryLabel(instance.category_key)}</strong>
        <span className="muted">
          {assessmentInstanceStatusLabel(
            instance.status, instance.item_response_count, instance.required_item_count)}
          · 计分范围 {instance.score_min}–{instance.score_max}
        </span>
        {localError && <Alert tone="warn" title="本地未通过">{localError}</Alert>}
        {enabled.canRespond && (
          <div className="row wrap" style={{ alignItems: "flex-end" }}>
            <Field label="条目键(见 PI 施测表)">
              <TextInput value={itemKey} onChange={(entry) => { setItemKey(entry.target.value); setExpectedItemRevision(0); }} placeholder="例:naming_01" />
            </Field>
            <Field label="作答值">
              <TextInput value={rawValue} onChange={(entry) => setRawValue(entry.target.value)} placeholder="数值" />
            </Field>
            <Field label="同条目已有修订数(冲突时按服务端提示刷新)">
              <TextInput value={String(expectedItemRevision)}
                onChange={(entry) => setExpectedItemRevision(Math.max(0, Number(entry.target.value) || 0))} />
            </Field>
            <Button disabled={busy || grantBusy} onClick={() => void issueGrant()}>
              {grantBusy ? "签发中…" : grant ? "重签录音授权" : "签发录音授权"}
            </Button>
            <Button variant="primary" disabled={busy} onClick={submitResponse}>保存作答</Button>
          </div>
        )}
        {grant && (
          <p className="muted mono">
            录音授权已签发:{grant.itemKey} · 第 {grant.revision} 修订 · {grant.digest.slice(0, 20)}…
          </p>
        )}
        {enabled.canComplete && (
          <div className="row wrap">
            <Button disabled={busy}
              onClick={() => void run(() => api.completeAssessmentInstance(event, instance, {
                expected_event_revision: event.revision,
                expected_instance_revision: instance.revision,
                idempotency_key: key("complete"),
              }, readiness))}>服务端计分并完成本表</Button>
            <select className="form-control" value={deferReason} disabled={busy}
              onChange={(entry) => setDeferReason(entry.target.value as AssessmentDeferralReason)}>
              {DEFER_REASONS.map(([code, label]) => <option key={code} value={code}>{label}</option>)}
            </select>
            <input className="form-control" type="date" value={deferredUntil} disabled={busy}
              onChange={(entry) => setDeferredUntil(entry.target.value)} />
            <Button disabled={busy || !deferredUntil}
              onClick={() => void run(() => api.approveAssessmentDeferral(event, instance, {
                expected_event_revision: event.revision,
                expected_instance_revision: instance.revision,
                idempotency_key: key("defer"),
                reason_code: deferReason,
                deferred_until: deferredUntil,
              }, readiness))}>管理员批准延期</Button>
          </div>
        )}
        {instance.scoring_evidence && (
          <p className="muted">
            服务端得分 {instance.scoring_evidence.score}(作答 {instance.scoring_evidence.answered_item_count} 条,
            引擎 {instance.scoring_evidence.scoring_algorithm_id})
          </p>
        )}
      </div>
    </article>
  );
}

function CloseoutForm({ event, readiness, busy, run }: {
  event: AssessmentEvent;
  readiness: ScaleProtocolReadiness;
  busy: boolean;
  run: (action: () => Promise<AssessmentEvent>) => Promise<boolean>;
}) {
  const [flags, setFlags] = useState<CloseFlags>({
    fatigue_observed: false,
    distress_or_discomfort_observed: false,
    participant_declined_to_continue: false,
    staff_assistance_occurred: false,
    environment_interruption_occurred: false,
    device_or_network_interruption_occurred: false,
  });
  const [note, setNote] = useState("");
  const anyFlag = Object.values(flags).some(Boolean);
  const reportStatus: AssessmentCloseoutReportStatus = anyFlag || note.trim()
    ? "observation_recorded"
    : "no_additional_observation";

  return (
    <div className="col" style={{ gap: 8 }}>
      <h4>现场收尾并关闭事件</h4>
      <div className="row wrap">
        {CLOSE_FLAGS.map(([field, label]) => (
          <label key={field} className="row" style={{ gap: 4 }}>
            <input type="checkbox" checked={flags[field]} disabled={busy}
              onChange={(entry) => setFlags({ ...flags, [field]: entry.target.checked })} />
            {label}
          </label>
        ))}
      </div>
      <Field label="备注(有观察项时必填)">
        <TextInput value={note} onChange={(entry) => setNote(entry.target.value)} placeholder="现场观察备注" />
      </Field>
      <Button variant="primary" disabled={busy}
        onClick={() => void run(() => api.closeAssessmentEvent(event, {
          expected_event_revision: event.revision,
          idempotency_key: key("close"),
          report_status: reportStatus,
          ...flags,
          note: note.trim() ? note.trim() : null,
        }, readiness))}>关闭评估事件</Button>
    </div>
  );
}
