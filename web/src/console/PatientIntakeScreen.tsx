import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EnumSelect, Field, TextInput, TriStateField } from "../components/Field";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/ToastContext";
import { CONSENT_TYPES } from "../types";
import type { CloudProcessingPolicy, Patient } from "../types";
import { cloudProcessingChoiceIssue, cloudProcessingDisclosure } from "./cloudProcessingPolicy";
import {
  assessDuplicateIntake,
  capturePatientIntakeSubmission,
  duplicateCloudChangeMayBeApplied,
  duplicatePatientSnapshotChanged,
} from "./patientIntakeDuplicate";
import type { DuplicateIntakeAssessment } from "./patientIntakeDuplicate";

const CONSENT_STATUSES = ["已同意", "未同意", "已撤回"] as const;

interface DuplicateReview {
  submitted: Patient;
  existing: Patient;
  assessment: DuplicateIntakeAssessment;
  then: (patientId: string) => void;
}

// 建档 + 合规字段(护栏2 全 nullable,"未评/待SOP"为一等公民)。绝不显示姓名,只用 patient_id。
// context="registry":准备区登记(测试前),文案不提"进入场次";"intake" 仅保留兼容文案。
// onCreatePlan(仅 registry):保存档案后继续填写并审核测试前训练安排，不直接开训。
// onNextTask(仅 registry):保存后由系统推导下一项任务(回原场→开已审核安排→仅模拟
//   档案才折叠审核/新建),不绕过服务端计划账本;真实受试者的审核仍是人的决策点,
//   推导返回 blocked 时由外层带去 onCreatePlan 的规范多步流程。
export function PatientIntakeScreen({ onReady, onCreatePlan, onNextTask, context = "intake" }: {
  onReady: (patientId: string) => void; onCreatePlan?: (patientId: string) => void;
  onNextTask?: (patientId: string, isSimulationSubject: boolean) => void;
  context?: "intake" | "registry";
}) {
  const registry = context === "registry";
  const toast = useToast();
  const [p, setP] = useState<Patient>({
    patient_id: "",
    is_simulation_subject: false,
    governance_revision: 0,
  });
  const [purposeConfirmed, setPurposeConfirmed] = useState<"research" | "simulation" | null>(null);
  const [simulationAcknowledged, setSimulationAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [cloudPolicy, setCloudPolicy] = useState<CloudProcessingPolicy | null>(null);
  const [cloudPolicyError, setCloudPolicyError] = useState<string | null>(null);
  const [duplicateReview, setDuplicateReview] = useState<DuplicateReview | null>(null);
  const [duplicateConfirm, setDuplicateConfirm] = useState<"reuse" | "cloud" | null>(null);
  const [fieldErrors, setFieldErrors] = useState<{ patientId?: string; purpose?: string; simulationAck?: string }>({});
  const set = <K extends keyof Patient>(k: K, v: Patient[K]) => {
    setDuplicateReview(null);
    setDuplicateConfirm(null);
    setP((prev) => ({ ...prev, [k]: v }));
  };

  useEffect(() => {
    let active = true;
    api.cloudProcessingPolicy()
      .then((policy) => { if (active) setCloudPolicy(policy); })
      .catch((error) => {
        if (active) setCloudPolicyError(error instanceof ApiError ? error.detail : String(error));
      });
    return () => { active = false; };
  }, []);

  async function submit(then: (patientId: string) => void) {
    if (busy) return;
    const errors: { patientId?: string; purpose?: string; simulationAck?: string } = {};
    if (purposeConfirmed == null) errors.purpose = "请选择“真实研究档案”或“专用模拟档案”";
    if (p.is_simulation_subject && !simulationAcknowledged) errors.simulationAck = "保存前请勾选此项确认";
    if (!p.patient_id.trim()) errors.patientId = "请填写受试者研究编号";
    else if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(p.patient_id.trim())) {
      // 训练安排契约(visit_plan_contract)只认这个格式;中文/空格编号建了档
      // 也排不了训练,必须在建档口挡住并把原因说人话。
      errors.patientId = "编号只能用字母、数字、点、横线，例如 NMU-001——不要用中文或姓名";
    }
    if (errors.purpose || errors.simulationAck || errors.patientId) {
      setFieldErrors(errors);
      if (errors.purpose) toast("请先明确选择“真实研究档案”或“专用模拟档案”", "warn");
      else if (errors.simulationAck) toast("请确认专用模拟档案不会录入任何真实患者或受试者数据", "danger");
      else toast(errors.patientId ?? "请填写受试者研究编号", "warn");
      const targetId = errors.purpose ? "intake-purpose-section"
        : errors.simulationAck ? "intake-simulation-ack" : "intake-patient-id";
      document.getElementById(targetId)?.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    setFieldErrors({});
    const cloudIssue = cloudProcessingChoiceIssue(p.cloud_processing_allowed, cloudPolicy);
    if (cloudIssue) { toast(cloudIssue, "danger"); return; }
    const submitted = capturePatientIntakeSubmission(p);
    setBusy(true);
    try {
      // provider/version/time 均为服务器字段；建档 POST 不携带，随后用具名账号 PATCH
      // 表达“允许/不允许”，防止浏览器伪造机构或告知版本。
      const createPayload: Patient = { ...submitted };
      const cloudChoice = createPayload.cloud_processing_allowed;
      delete createPayload.cloud_processing_allowed;
      delete createPayload.cloud_processing_provider_id;
      delete createPayload.cloud_processing_notice_version;
      delete createPayload.cloud_processing_consented_at;
      delete createPayload.cloud_processing_revoked_at;
      const created = await api.createPatient(createPayload);
      if (cloudChoice != null) {
        await api.setPatientCloudProcessing(
          submitted.patient_id,
          cloudChoice,
          created,
          cloudChoice ? cloudPolicy : null,
        );
      }
      toast(registry ? "受试者已登记" : "受试者档案已保存", "ok");
      then(submitted.patient_id);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // 编号已存在不算错误,但绝不能静默沿用:409 时表单内容不会写入服务器,
        // 若研究者刚改了合规字段(尤其录音授权)而系统按旧档案执行,是真实伦理事故。
        await resolveExisting(submitted, then);
      } else {
        toast(e instanceof ApiError ? e.detail : String(e), "danger");
      }
    } finally {
      setBusy(false);
    }
  }

  // 409 后只进入核对，不写入、不跳转。重复编号不是档案合并接口；任何授权变化都必须
  // 在展示既有状态之后走独立确认，确认前还会重新读取服务器状态防止并发覆盖。
  async function resolveExisting(submitted: Patient, then: (patientId: string) => void) {
    let existing: Patient;
    try {
      existing = await api.getPatient(submitted.patient_id);
    } catch {
      toast(`研究编号 ${submitted.patient_id} 已存在，但档案读取失败，请回登记表核对`, "danger");
      return;
    }
    if (existing.withdrawal_status) {
      toast(`受试者 ${submitted.patient_id} 撤回状态为「${existing.withdrawal_status}」，不可登记或安排训练`, "danger");
      return;
    }
    const assessment = assessDuplicateIntake(submitted, existing);
    setDuplicateReview({ submitted, existing, assessment, then });
    setDuplicateConfirm(null);
    toast(
      assessment.blockingConflictLabels.length > 0
        ? `研究编号 ${submitted.patient_id} 已存在且档案内容冲突；本次表单未写入服务器`
        : `研究编号 ${submitted.patient_id} 已存在，请先核对既有授权`,
      assessment.blockingConflictLabels.length > 0 ? "danger" : "info",
    );
  }

  async function confirmReuseExisting() {
    const review = duplicateReview;
    if (!review?.assessment.canReuseWithoutChanges) return;
    setBusy(true);
    try {
      const current = await api.getPatient(review.existing.patient_id);
      if (current.withdrawal_status || duplicatePatientSnapshotChanged(review.existing, current)) {
        setDuplicateReview({
          ...review,
          existing: current,
          assessment: assessDuplicateIntake(review.submitted, current),
        });
        setDuplicateConfirm(null);
        toast(
          current.withdrawal_status
            ? `档案已变为撤回状态「${current.withdrawal_status}」，不会继续创建安排`
            : "档案在确认期间已变化；请按最新状态重新核对",
          "danger",
        );
        return;
      }
      setDuplicateConfirm(null);
      setDuplicateReview(null);
      toast(`已沿用研究编号 ${current.patient_id} 的既有档案；本次表单未改变任何授权`, "info");
      review.then(current.patient_id);
    } catch (error) {
      toast(`既有档案未能重新核对：${error instanceof ApiError ? error.detail : String(error)}`, "danger");
    } finally {
      setBusy(false);
    }
  }

  async function confirmCloudChange() {
    const review = duplicateReview;
    if (!review || !duplicateCloudChangeMayBeApplied(review.assessment, {
      duplicateReviewed: true,
      dedicatedAuthorizationConfirmed: true,
    })) return;
    const requested = review.submitted.cloud_processing_allowed;
    if (requested == null) return;
    setBusy(true);
    try {
      let confirmedPolicy: CloudProcessingPolicy | null = null;
      if (requested) {
        const currentPolicy = await api.cloudProcessingPolicy();
        const policyIssue = cloudProcessingChoiceIssue(true, currentPolicy);
        if (policyIssue) throw new Error(policyIssue);
        if (currentPolicy.provider_id !== cloudPolicy?.provider_id
            || currentPolicy.notice_version !== cloudPolicy?.notice_version) {
          setCloudPolicy(currentPolicy);
          setDuplicateConfirm(null);
          toast("云处理方或告知版本已变化，未更新授权；请阅读最新告知后重新确认", "danger");
          return;
        }
        confirmedPolicy = currentPolicy;
      }
      const current = await api.getPatient(review.existing.patient_id);
      if (current.withdrawal_status || duplicatePatientSnapshotChanged(review.existing, current)) {
        setDuplicateReview({
          ...review,
          existing: current,
          assessment: assessDuplicateIntake(review.submitted, current),
        });
        setDuplicateConfirm(null);
        toast(
          current.withdrawal_status
            ? `档案已变为撤回状态「${current.withdrawal_status}」，不会更新任何授权`
            : "档案在确认期间已变化，未更新授权；请按最新状态重新核对",
          "danger",
        );
        return;
      }
      const updated = await api.setPatientCloudProcessing(
        current.patient_id,
        requested,
        current,
        confirmedPolicy,
      );
      if (updated.cloud_processing_allowed !== requested) {
        throw new Error("服务器未返回已确认的云处理授权状态");
      }
      setDuplicateConfirm(null);
      setDuplicateReview(null);
      toast(requested ? "云处理授权已按独立确认更新" : "云处理授权已撤销", requested ? "ok" : "warn");
      review.then(updated.patient_id);
    } catch (error) {
      toast(`既有档案未更新云处理授权：${error instanceof ApiError ? error.detail : String(error)}`, "danger");
    } finally {
      setBusy(false);
    }
  }

  const formatTriState = (value: boolean | null | undefined) => value === true ? "是" : value === false ? "否" : "未评";
  const formatCloudAuthorization = (patient: Patient) => {
    if (patient.cloud_processing_allowed === true) return "允许";
    if (patient.cloud_processing_revoked_at) return "不允许（已有拒绝/撤销记录）";
    if (patient.cloud_processing_allowed === false) return "不允许";
    return "未选择";
  };

  return (
    <div className="page-shell page-shell--medium">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">{registry ? "准备区 · 登记受试者" : "步骤 1 / 4 · 受试者建档"}</p>
          <h2 className="page-title">{registry ? "登记受试者档案" : "建立受试者档案"}</h2>
          <p className="page-description">
            只填写研究编号和本次训练需要的信息，不录入姓名、身份证号等直接身份信息。
          </p>
        </div>
      </header>

      <fieldset
        disabled={busy}
        aria-busy={busy}
        aria-label="受试者档案编辑"
        style={{ border: 0, margin: 0, padding: 0, minWidth: 0 }}
      >
        <div className="form-layout">
        <section className="form-section" id="intake-purpose-section">
          <div className="form-section-header">
            <div>
              <h3>档案用途（必须明确选择）</h3>
              <p className="muted">保存后档案类型不能混用，请选准。</p>
            </div>
            <StatusPill tone={purposeConfirmed == null ? "danger" : p.is_simulation_subject ? "warn" : "ok"}>
              {purposeConfirmed == null ? "尚未选择" : p.is_simulation_subject ? "专用模拟档案" : "真实研究档案"}
            </StatusPill>
          </div>
          <div className="segmented-control" role="group" aria-label="档案用途">
            <button
              type="button"
              className="segmented-control__button"
              aria-pressed={purposeConfirmed === "research"}
              onClick={() => {
                set("is_simulation_subject", false);
                setPurposeConfirmed("research");
                setSimulationAcknowledged(false);
                setFieldErrors((prev) => prev.purpose ? { ...prev, purpose: undefined } : prev);
              }}
            >
              真实研究档案
            </button>
            <button
              type="button"
              className="segmented-control__button"
              aria-pressed={purposeConfirmed === "simulation"}
              onClick={() => {
                set("is_simulation_subject", true);
                setPurposeConfirmed("simulation");
                setSimulationAcknowledged(false);
                setFieldErrors((prev) => prev.purpose ? { ...prev, purpose: undefined } : prev);
              }}
            >
              专用模拟档案
            </button>
          </div>
          {fieldErrors.purpose && <p className="field__error" role="alert">{fieldErrors.purpose}</p>}
          {p.is_simulation_subject && purposeConfirmed === "simulation" && (
            <Alert tone="danger" title="专用模拟档案：严禁录入真人数据">
              <p>这里只能使用虚构资料、合成语音，或由工作人员本人进行流程演练。真实患者、老人或其他受试者不得标记为模拟，也不得把真人录音、回答或量表放入此档案。</p>
              <label className="toggle-field" id="intake-simulation-ack">
                <input
                  type="checkbox"
                  checked={simulationAcknowledged}
                  onChange={(event) => {
                    setSimulationAcknowledged(event.target.checked);
                    setFieldErrors((prev) => prev.simulationAck ? { ...prev, simulationAck: undefined } : prev);
                  }}
                />
                我确认本档案仅用于合成数据或工作人员演练，不包含真人受试者数据
              </label>
              {fieldErrors.simulationAck && <p className="field__error" role="alert">{fieldErrors.simulationAck}</p>}
            </Alert>
          )}
          {purposeConfirmed === "research" && (
            <Alert tone="info" title="真实研究数据边界">
              该档案只用于真实研究训练。
            </Alert>
          )}
        </section>

        <section className="form-section">
          <div className="form-section-header">
            <div>
              <h3>基本信息与入组资格</h3>
              <p className="muted">“未评”表示尚未完成核对，不会被系统自动当作“否”。</p>
            </div>
            <StatusPill tone={p.patient_id.trim() ? "ok" : "muted"}>{p.patient_id.trim() ? "编号已填写" : "等待填写"}</StatusPill>
          </div>
          <div className="form-grid">
            <Field label="受试者研究编号" hint="只用字母/数字/点/横线，例如 NMU-001；请勿填写姓名、中文、手机号或身份证号" required
              error={fieldErrors.patientId}>
              <TextInput id="intake-patient-id" value={p.patient_id}
                onChange={(e) => {
                  set("patient_id", e.target.value);
                  setFieldErrors((prev) => prev.patientId ? { ...prev, patientId: undefined } : prev);
                }}
                placeholder="例如 P001" autoComplete="off" required />
            </Field>
            <Field label="认知障碍程度（如已评估）" hint="按当前研究记录填写，可暂时留空">
              <TextInput value={p.dementia_severity ?? ""} onChange={(e) => set("dementia_severity", e.target.value || null)} placeholder="例如：轻度 / 中度" />
            </Field>
            <TriStateField label="是否符合普通话训练要求" value={p.mandarin_eligible} onChange={(v) => set("mandarin_eligible", v)} />
          </div>
        </section>

        <section className="form-section">
          <div className="form-section-header">
            <div>
              <h3>知情同意与录音授权</h3>
              <p className="muted">请以伦理批件和现场签署文件为准；尚未核对的项目可保留为“未评”。</p>
            </div>
          </div>
          <div className="form-grid">
            <Field label="知情同意状态">
              <EnumSelect options={CONSENT_STATUSES} value={p.consent_status ?? null}
                onChange={(v) => set("consent_status", v)} placeholder="请选择（未确认不得开真实场次）" />
            </Field>
            <Field label="知情同意方式">
              <EnumSelect options={CONSENT_TYPES} value={p.consent_type ?? null} onChange={(v) => set("consent_type", v as Patient["consent_type"])} placeholder="请选择（可稍后补录）" />
            </Field>
            <Field label="签署人角色" hint="只填写“本人”“配偶”“监护人”等角色，不填写真实姓名">
              <TextInput value={p.consent_person ?? ""} onChange={(e) => set("consent_person", e.target.value || null)} placeholder="例如：本人 / 监护人" />
            </Field>
            <TriStateField label="是否使用代理同意" value={p.proxy_consent} onChange={(v) => set("proxy_consent", v)} />
            {p.consent_type === "代理同意加本人赞同" && (
              <TriStateField label="是否已取得受试者本人赞同" value={p.assent_obtained} onChange={(v) => set("assent_obtained", v)} />
            )}
            <TriStateField label="是否允许研究录音" value={p.recording_allowed} onChange={(v) => set("recording_allowed", v)} />
            <TriStateField label="是否同意去标识数据二次使用" value={p.secondary_use_allowed} onChange={(v) => set("secondary_use_allowed", v)} />
          </div>
          {!p.is_simulation_subject && p.secondary_use_allowed !== true && (
            <Alert tone="warn" title="去标识导出未授权">
              未明确同意时，数据不进入研究导出；训练不受影响。
            </Alert>
          )}
          {!p.is_simulation_subject && p.consent_status !== "已同意" && (
            <Alert tone="warn" title="真实研究场次尚未放行">
              知情同意状态必须明确为“已同意”；未确认、未同意或已撤回时只能保存档案，不能开始真实研究场次。
            </Alert>
          )}
          {p.recording_allowed === false && (
            <Alert tone="danger" title="录音已明确禁止">
              禁止录音时不能使用需要录音的训练。授权变化后请重新核对再安排。
            </Alert>
          )}
        </section>

        <section className="form-section">
          <div className="form-section-header">
            <div>
              <h3>第三方云端 AI 处理授权（独立选择）</h3>
              <p className="muted">这不是录音授权，也不是二次使用授权；撤销它不会自动退出整个研究或删除原声。</p>
            </div>
            <StatusPill tone={p.cloud_processing_allowed === true ? "warn" : p.cloud_processing_allowed === false ? "ok" : "muted"}>
              {p.cloud_processing_allowed === true ? "允许云处理" : p.cloud_processing_allowed === false ? "不允许云处理" : "尚未选择"}
            </StatusPill>
          </div>
          <Alert tone={cloudPolicy?.configured ? "warn" : "danger"} title="会发送哪些数据">
            <p>{cloudProcessingDisclosure(cloudPolicy)}</p>
            <p>允许后，回答录音和文字会发给上述服务方处理；不允许时一律不外发。</p>
            {cloudPolicyError && <p>云处理告知条款读取失败：{cloudPolicyError}</p>}
          </Alert>
          <TriStateField
            label="是否明确允许上述第三方云处理"
            value={p.cloud_processing_allowed}
            onChange={(value) => set("cloud_processing_allowed", value)}
          />
        </section>
        </div>
      </fieldset>

      {duplicateReview && (
        <section className="form-section" aria-labelledby="duplicate-review-title" aria-busy={busy}>
          <div className="form-section-header">
            <div>
              <h3 id="duplicate-review-title">研究编号已存在：本次表单尚未写入</h3>
              <p className="muted">重复编号不会自动合并档案，也不会自动改变录音或第三方云处理授权。请先核对既有档案。</p>
            </div>
            <StatusPill tone={duplicateReview.assessment.blockingConflictLabels.length > 0 ? "danger" : "warn"}>
              {duplicateReview.assessment.blockingConflictLabels.length > 0 ? "存在档案冲突" : "等待人工核对"}
            </StatusPill>
          </div>
          <Alert
            tone={duplicateReview.assessment.blockingConflictLabels.length > 0 ? "danger" : "warn"}
            title={duplicateReview.assessment.blockingConflictLabels.length > 0 ? "不能从建档页合并" : "关键授权对照"}
          >
            {duplicateReview.assessment.blockingConflictLabels.length > 0 && (
              <p>以下字段与既有档案不一致：{duplicateReview.assessment.blockingConflictLabels.join("、")}。请回登记表走专门的档案核对流程。</p>
            )}
            <div className="evidence-detail-grid">
              <div><strong>知情同意</strong><p>既有：{duplicateReview.existing.consent_status ?? "未评"}<br />本次：{duplicateReview.submitted.consent_status ?? "未评"}</p></div>
              <div><strong>研究录音</strong><p>既有：{formatTriState(duplicateReview.existing.recording_allowed)}<br />本次：{formatTriState(duplicateReview.submitted.recording_allowed)}</p></div>
              <div><strong>第三方云处理</strong><p>既有：{formatCloudAuthorization(duplicateReview.existing)}<br />本次：{formatCloudAuthorization(duplicateReview.submitted)}</p></div>
              <div><strong>既有云处理依据</strong><p>处理方：{duplicateReview.existing.cloud_processing_provider_id ?? "未记录"}<br />告知版本：{duplicateReview.existing.cloud_processing_notice_version ?? "未记录"}</p></div>
            </div>
            {duplicateReview.assessment.existingCloudRevocationRecorded && (
              <p><strong>此前已拒绝或撤销过云处理。</strong>须现场重新取得授权后才能变更。</p>
            )}
          </Alert>
          <div className="form-actions">
            <Button disabled={busy}
              onClick={() => { setDuplicateConfirm(null); setDuplicateReview(null); }}>关闭并修改本次表单</Button>
            {duplicateReview.assessment.canReuseWithoutChanges && (
              <Button disabled={busy}
                onClick={() => setDuplicateConfirm("reuse")}>沿用既有档案，不更改授权</Button>
            )}
            {duplicateReview.assessment.canRequestCloudChange && (
              <Button variant="danger" disabled={busy}
                onClick={() => setDuplicateConfirm("cloud")}>
                {duplicateReview.assessment.cloudChangeDirection === "grant" ? "申请重新授权云处理" : "申请撤销云处理授权"}
              </Button>
            )}
          </div>
        </section>
      )}

      <div className="form-actions">
        <p className="muted grow">
          {registry && onCreatePlan ? "保存档案后继续创建测试前训练安排，也可仅保存档案、稍后再安排；档案不记录姓名。"
            : registry ? "保存后返回登记表，可继续登记或去训练台选人；档案不记录姓名。"
            : "保存后可继续创建训练安排，档案不会记录受试者姓名。"}
        </p>
        {registry && (onCreatePlan || onNextTask) && (
          <Button disabled={busy} onClick={() => submit(onReady)}>保存，稍后开始</Button>
        )}
        <Button variant="primary" disabled={busy}
          onClick={() => submit(
            onNextTask
              ? (patientId) => onNextTask(
                patientId,
                Boolean(p.is_simulation_subject) && purposeConfirmed === "simulation")
              : registry && onCreatePlan ? onCreatePlan : onReady)}>
          {busy
            ? "正在保存…"
            : onNextTask
              ? "保存并开始下一项任务"
              : p.is_simulation_subject && purposeConfirmed === "simulation"
                ? onCreatePlan ? "保存专用模拟档案并安排演练" : "保存专用模拟档案"
                : registry ? (onCreatePlan ? "保存并创建训练安排" : "保存登记") : "保存档案，进入下一步"}
        </Button>
      </div>

      <ConfirmDialog
        open={duplicateConfirm === "reuse"}
        title="确认只沿用既有档案？"
        body="继续后将使用服务器中的既有档案创建安排；本次表单没有写入，也不会改变录音或第三方云处理授权。"
        confirmLabel={busy ? "正在重新核对…" : "确认沿用，不做更改"}
        onConfirm={() => { if (!busy) void confirmReuseExisting(); }}
        onCancel={() => { if (!busy) setDuplicateConfirm(null); }}
      />
      <ConfirmDialog
        open={duplicateConfirm === "cloud"}
        title={duplicateReview?.assessment.cloudChangeDirection === "grant" ? "单独确认重新授权云处理" : "单独确认撤销云处理授权"}
        body={duplicateReview?.assessment.cloudChangeDirection === "grant"
          ? `既有状态：${duplicateReview ? formatCloudAuthorization(duplicateReview.existing) : "未知"}。确认后，回答录音和文字将按最新告知条款发给第三方。请确保已重新取得授权。`
          : "确认后服务器将撤销第三方云处理授权；后续云端 ASR 或判类会被阻止并安全暂停。"}
        confirmLabel={busy ? "正在核对…" : duplicateReview?.assessment.cloudChangeDirection === "grant" ? "已重新取得授权，确认变更" : "确认撤销授权"}
        onConfirm={() => { if (!busy) void confirmCloudChange(); }}
        onCancel={() => { if (!busy) setDuplicateConfirm(null); }}
      />
    </div>
  );
}
