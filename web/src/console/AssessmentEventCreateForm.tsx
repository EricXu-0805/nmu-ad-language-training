import { useState } from "react";
import { api } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Field, TextInput } from "../components/Field";
import type { AssessmentEvent, ScaleProtocolReadiness } from "../types";
import {
  ASSESSMENT_TIMEPOINTS,
  checkAssessmentCreateDraft,
  classifyAssessmentCreateFailure,
  newAssessmentIdempotencyKey,
} from "./assessmentCreate";

// U6（收据 187）：正式评估的浏览器创建入口。后端与接口客户端早就存在，
// 之前 web/src 里零调用者——排期只能靠人直接打接口，这条链在界面上是断的。
// 这里只补入口，不改后端授权边界：就绪为假时整块不渲染。
export function AssessmentEventCreateForm({ readiness, onCreated }: {
  readiness: ScaleProtocolReadiness;
  onCreated: (event: AssessmentEvent) => void;
}) {
  const [patientId, setPatientId] = useState("");
  const [timepoint, setTimepoint] = useState("");
  const [scheduledDate, setScheduledDate] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState(newAssessmentIdempotencyKey);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [unknownOutcome, setUnknownOutcome] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);

  async function submit() {
    const checked = checkAssessmentCreateDraft({ patientId, timepoint, scheduledDate });
    if (!checked.ok) { setLocalError(checked.reason); return; }
    setLocalError(null);
    setBusy(true);
    try {
      const event = await api.createAssessmentEvent(checked.patientId, {
        timepoint: checked.timepoint,
        scheduled_date: checked.scheduledDate,
        idempotency_key: idempotencyKey,
      }, readiness);
      setUnknownOutcome(null);
      setCreated(`${event.timepoint} · ${event.scheduled_date}`);
      setIdempotencyKey(newAssessmentIdempotencyKey());
      setRawDraftCleared();
      onCreated(event);
    } catch (error) {
      const failure = classifyAssessmentCreateFailure(error);
      if (failure.kind === "unknown") {
        // 服务器可能已经建成了。换幂等键重试会建出第二个事件，所以键必须留着。
        setUnknownOutcome(failure.message);
      } else {
        setUnknownOutcome(null);
        setLocalError(failure.message);
      }
      if (!failure.retrySameKey) setIdempotencyKey(newAssessmentIdempotencyKey());
    } finally {
      setBusy(false);
    }
  }

  function setRawDraftCleared() {
    setPatientId("");
    setTimepoint("");
    setScheduledDate("");
  }

  return (
    <div className="form-section" aria-label="新建正式评估事件">
      <h4>新建正式评估</h4>
      <p className="muted">
        排期只是建立待办；能不能真的施测由服务端的冻结政策与就绪状态决定，这里不做预判。
      </p>
      {localError && <Alert tone="warn" title="未提交">{localError}</Alert>}
      {unknownOutcome && (
        <Alert tone="danger" title="结果未知，请先核对队列再决定">
          <p>{unknownOutcome}</p>
          <p>
            这次请求可能已经在服务器上建成了事件。<strong>不要改动上面的内容</strong>，
            先点「刷新评估队列」核对；确实没有再点一次「建立评估事件」——
            同一个操作重复提交不会建出第二个。
          </p>
        </Alert>
      )}
      {created && <Alert tone="ok" title="已建立">{created}</Alert>}
      <div className="row wrap" style={{ alignItems: "flex-end" }}>
        <Field label="受试者研究编号">
          <TextInput value={patientId} disabled={busy}
            onChange={(entry) => setPatientId(entry.target.value)} placeholder="例:P-001" />
        </Field>
        <Field label="评估时点">
          <select className="form-control" value={timepoint} disabled={busy}
            onChange={(entry) => setTimepoint(entry.target.value)}>
            <option value="">（未选）</option>
            {ASSESSMENT_TIMEPOINTS.map(([code, label]) => (
              <option key={code} value={code}>{label}</option>
            ))}
          </select>
        </Field>
        <Field label="排期日期">
          <input className="form-control" type="date" value={scheduledDate} disabled={busy}
            onChange={(entry) => setScheduledDate(entry.target.value)} />
        </Field>
        <Button variant="primary" disabled={busy} onClick={() => { void submit(); }}>
          {busy ? "提交中…" : "建立评估事件"}
        </Button>
      </div>
    </div>
  );
}
