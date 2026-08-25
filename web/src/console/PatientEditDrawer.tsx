import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EnumSelect, Field, TextInput, TriStateField } from "../components/Field";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/ToastContext";
import { useDialogFocusTrap } from "../components/useDialogFocusTrap";
import { CONSENT_TYPES } from "../types";
import type { CloudProcessingPolicy, Patient } from "../types";
import {
  cloudProcessingAllowDisabledReason,
  cloudProcessingDisclosure,
} from "./cloudProcessingPolicy";
import {
  buildProfilePatch,
  cloudAuthorizationLabel,
  cloudConsentBaseChanged,
  consentTypeLockedReason,
  draftFromPatient,
  patchIsEmpty,
  renameIssue,
  saveErrorText,
  type ProfileDraft,
} from "./patientEdit";

// 否定态(未同意/已撤回)不给选:撤回走「登记研究撤回」治理流程,登记笔误走
// 纸质记录更正——后端对否定态闭集同样 422,这里不提供入口。
const CONSENT_STATUSES = ["已同意"] as const;

// 档案编辑抽屉:手滑填错不用重新建档。普通字段走差量 PATCH;云处理授权是
// 治理动作,在本抽屉里有独立区块+单独确认,走专用端点;研究撤回、模拟/研究
// 身份仍各有专属流程,这里不出现。
export function PatientEditDrawer({ patientId, sessionCount, onClose, onSaved }: {
  patientId: string;
  sessionCount: number;
  onClose: () => void;
  onSaved: (updated: Patient) => void;
}) {
  const toast = useToast();
  const [original, setOriginal] = useState<ProfileDraft | null>(null);
  const [draft, setDraft] = useState<ProfileDraft | null>(null);
  const [patientAtOpen, setPatientAtOpen] = useState<Patient | null>(null);
  const [cloudPolicy, setCloudPolicy] = useState<CloudProcessingPolicy | null>(null);
  const [cloudPolicyError, setCloudPolicyError] = useState<string | null>(null);
  const [cloudConfirm, setCloudConfirm] = useState<"grant" | "revoke" | null>(null);
  const [newId, setNewId] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const cancelSafely = () => { if (!busy) onClose(); };
  const panelRef = useDialogFocusTrap<HTMLElement>({ open: true, onCancel: cancelSafely });

  useEffect(() => {
    let active = true;
    setLoadError(null);
    api.getPatient(patientId)
      .then((patient) => {
        if (!active) return;
        const loaded = draftFromPatient(patient);
        setOriginal(loaded);
        setDraft(loaded);
        setPatientAtOpen(patient);
      })
      .catch((error) => {
        if (active) setLoadError(error instanceof ApiError ? error.detail : String(error));
      });
    api.cloudProcessingPolicy()
      .then((policy) => { if (active) setCloudPolicy(policy); })
      .catch((error) => {
        if (active) setCloudPolicyError(error instanceof ApiError ? error.detail : String(error));
      });
    return () => { active = false; };
  }, [patientId]);

  const set = <K extends keyof ProfileDraft>(key: K, value: ProfileDraft[K]) => {
    setSaveError(null);
    setDraft((current) => (current ? { ...current, [key]: value } : current));
  };

  const idIssue = renameIssue(newId, sessionCount);
  const canRename = sessionCount === 0;

  const save = async () => {
    if (!original || !draft || busy) return;
    if (idIssue) { setSaveError(idIssue); return; }
    const patch = buildProfilePatch(original, draft, newId, patientId);
    if (patchIsEmpty(patch)) { onClose(); return; }
    setBusy(true);
    setSaveError(null);
    try {
      const updated = await api.updatePatient(patientId, patch);
      toast(patch.new_patient_id
        ? `档案已保存，编号已更正为 ${updated.patient_id}`
        : "档案已保存", "ok");
      onSaved(updated);
    } catch (error) {
      setSaveError(saveErrorText(error));
      setBusy(false);
    }
  };

  // 云处理授权变更:确认时重取 policy 与档案——告知条款、撤回状态或授权快照
  // 在抽屉打开后变过,这次就不写,提示重读。与建档口的独立确认流程同一口径。
  const confirmCloudChange = async (requested: boolean) => {
    if (!patientAtOpen || busy) return;
    setBusy(true);
    try {
      let confirmedPolicy: CloudProcessingPolicy | null = null;
      if (requested) {
        const currentPolicy = await api.cloudProcessingPolicy();
        if (!currentPolicy.configured || !currentPolicy.provider_id
            || !currentPolicy.notice_version) {
          throw new Error("服务器还没接入云端 AI 服务，现在无法授权");
        }
        if (currentPolicy.provider_id !== cloudPolicy?.provider_id
            || currentPolicy.notice_version !== cloudPolicy?.notice_version) {
          setCloudPolicy(currentPolicy);
          setCloudConfirm(null);
          toast("云处理方或告知版本已变化，未更新授权；请阅读最新告知后重新确认", "danger");
          return;
        }
        confirmedPolicy = currentPolicy;
      }
      const current = await api.getPatient(patientId);
      if (current.withdrawal_status || cloudConsentBaseChanged(patientAtOpen, current)) {
        setPatientAtOpen(current);
        setCloudConfirm(null);
        toast(
          current.withdrawal_status
            ? `档案已变为撤回状态「${current.withdrawal_status}」，云处理授权不可更改`
            : "档案在确认期间已变化，未更新授权；请按最新状态重新核对",
          "danger",
        );
        return;
      }
      const updated = await api.setPatientCloudProcessing(
        patientId, requested, current, confirmedPolicy);
      if (updated.cloud_processing_allowed !== requested) {
        throw new Error("服务器未返回已确认的云处理授权状态");
      }
      setCloudConfirm(null);
      toast(requested ? "云处理授权已按独立确认更新" : "云处理授权已撤销", requested ? "ok" : "warn");
      onSaved(updated);
    } catch (error) {
      toast(`云处理授权未更新：${saveErrorText(error)}`, "danger");
    } finally {
      setBusy(false);
    }
  };

  const cloudAllowed = patientAtOpen?.cloud_processing_allowed === true;
  const cloudGrantDisabledReason = patientAtOpen?.withdrawal_status
    ? "该档案已登记研究撤回，云处理授权已封存"
    : cloudProcessingAllowDisabledReason(cloudPolicy, cloudPolicyError);

  return (
    <div className="drawer-backdrop" onClick={cancelSafely}>
      <section ref={panelRef} className="drawer-panel fade-in" role="dialog" aria-modal="true"
        aria-labelledby="patient-edit-title" aria-busy={busy || undefined} tabIndex={-1}
        onClick={(event) => event.stopPropagation()}>
        <div className="drawer-header">
          <div>
            <h2 id="patient-edit-title">编辑档案</h2>
            <p className="muted">研究编号 <strong className="mono">{patientId}</strong> · 只改填错的信息，不用重新建档</p>
          </div>
          <Button onClick={cancelSafely} disabled={busy}>关闭</Button>
        </div>

        {loadError && (
          <Alert tone="danger" title="档案读取失败">{loadError}</Alert>
        )}
        {!original && !loadError && <StatusPill tone="muted">正在读取档案…</StatusPill>}

        {draft && (
          <fieldset disabled={busy} style={{ border: 0, margin: 0, padding: 0, minWidth: 0 }}>
            <div className="form-layout">
              <section className="form-section">
                <div className="form-section-header">
                  <div>
                    <h3>基本信息</h3>
                    <p className="muted">“未评”表示尚未核对，不会被当作“否”。</p>
                  </div>
                </div>
                <div className="form-grid">
                  <Field label="认知障碍程度（如已评估）" hint="按当前研究记录填写，可留空">
                    <TextInput value={draft.dementia_severity ?? ""}
                      onChange={(e) => set("dementia_severity", e.target.value || null)}
                      placeholder="例如：轻度 / 中度" />
                  </Field>
                  <TriStateField label="是否符合普通话训练要求"
                    value={draft.mandarin_eligible}
                    onChange={(v) => set("mandarin_eligible", v)} />
                </div>
              </section>

              <section className="form-section">
                <div className="form-section-header">
                  <div>
                    <h3>知情同意与录音授权</h3>
                    <p className="muted">以伦理批件和现场签署文件为准。研究撤回不在这里改，请走登记表的「登记研究撤回」。</p>
                  </div>
                </div>
                <div className="form-grid">
                  <Field label="知情同意状态"
                    hint="这里只能更正为「已同意」；受试者退出研究请走登记表的「登记研究撤回」">
                    <EnumSelect options={CONSENT_STATUSES} value={draft.consent_status ?? null}
                      onChange={(v) => set("consent_status", v)} placeholder="请选择" />
                  </Field>
                  <Field label="知情同意方式"
                    hint={consentTypeLockedReason(original ?? draft)
                      ?? "建档时留空的可以在这里补录"}>
                    <EnumSelect options={CONSENT_TYPES} value={draft.consent_type ?? null}
                      onChange={(v) => set("consent_type", v as Patient["consent_type"])}
                      disabled={consentTypeLockedReason(original ?? draft) !== null}
                      placeholder="请选择" />
                  </Field>
                  <Field label="签署人角色" hint="只填“本人”“配偶”“监护人”等角色，不填写真实姓名">
                    <TextInput value={draft.consent_person ?? ""}
                      onChange={(e) => set("consent_person", e.target.value || null)}
                      placeholder="例如：本人 / 监护人" />
                  </Field>
                  <TriStateField label="是否使用代理同意"
                    value={draft.proxy_consent} onChange={(v) => set("proxy_consent", v)} />
                  {draft.consent_type === "代理同意加本人赞同" && (
                    <TriStateField label="是否已取得受试者本人赞同"
                      value={draft.assent_obtained} onChange={(v) => set("assent_obtained", v)} />
                  )}
                  <TriStateField label="是否允许研究录音"
                    value={draft.recording_allowed} onChange={(v) => set("recording_allowed", v)} />
                  <TriStateField label="是否同意去标识数据二次使用"
                    value={draft.secondary_use_allowed}
                    onChange={(v) => set("secondary_use_allowed", v)} />
                </div>
              </section>

              <section className="form-section">
                <div className="form-section-header">
                  <div>
                    <h3>第三方云处理授权</h3>
                    <p className="muted">{cloudProcessingDisclosure(cloudPolicy)}</p>
                  </div>
                </div>
                <div className="form-grid">
                  <Field label="当前状态"
                    hint="AI 自动带练和 AI 转写需要此授权；变更走单独确认，不随「保存修改」一起提交">
                    <div className="col" style={{ gap: 8, alignItems: "flex-start" }}>
                      <StatusPill tone={cloudAllowed ? "warn" : "muted"}>
                        {patientAtOpen ? cloudAuthorizationLabel(patientAtOpen) : "正在读取…"}
                      </StatusPill>
                      {cloudAllowed ? (
                        <Button onClick={() => setCloudConfirm("revoke")} disabled={busy || !patientAtOpen}>
                          撤销云处理授权…
                        </Button>
                      ) : (
                        <>
                          <Button onClick={() => setCloudConfirm("grant")}
                            disabled={busy || !patientAtOpen || cloudGrantDisabledReason !== null}>
                            登记允许云处理…
                          </Button>
                          {cloudGrantDisabledReason && (
                            <span className="muted">{cloudGrantDisabledReason}</span>
                          )}
                        </>
                      )}
                    </div>
                  </Field>
                </div>
              </section>

              <section className="form-section">
                <div className="form-section-header">
                  <div>
                    <h3>研究编号更正</h3>
                    <p className="muted">
                      {canRename
                        ? "还没有任何训练数据，可以更正编号；改完后请通知使用旧编号的同事。"
                        : "编号是训练数据的关联键，已有训练数据后不能再改；如确属登记错误，请新建档案。"}
                    </p>
                  </div>
                </div>
                <Field label="新研究编号"
                  hint={canRename ? "留空表示不改；只能用字母、数字、点、横线，例如 NMU-001" : "已有训练数据，编号不能再改"}
                  error={idIssue ?? undefined}>
                  <TextInput value={newId} disabled={!canRename}
                    onChange={(e) => { setSaveError(null); setNewId(e.target.value); }}
                    placeholder={canRename ? "例如 NMU-001" : "已有训练数据，不能改"}
                    autoComplete="off" />
                </Field>
              </section>
            </div>
          </fieldset>
        )}

        <ConfirmDialog
          open={cloudConfirm === "grant"}
          title="单独确认允许云处理"
          body={`当前状态：${patientAtOpen ? cloudAuthorizationLabel(patientAtOpen) : "未知"}。确认后，回答录音和文字将按最新告知条款发给第三方。请确保已取得受试者（或代理人）对云处理的授权。`}
          confirmLabel={busy ? "正在核对…" : "已取得授权，确认登记"}
          onConfirm={() => { if (!busy) void confirmCloudChange(true); }}
          onCancel={() => { if (!busy) setCloudConfirm(null); }}
        />
        <ConfirmDialog
          open={cloudConfirm === "revoke"}
          title="单独确认撤销云处理授权"
          body="确认后服务器将撤销第三方云处理授权；后续云端 ASR 或判类会被阻止并安全暂停。"
          confirmLabel={busy ? "正在核对…" : "确认撤销授权"}
          onConfirm={() => { if (!busy) void confirmCloudChange(false); }}
          onCancel={() => { if (!busy) setCloudConfirm(null); }}
        />
        {saveError && <Alert tone="danger" title="没有保存成功">{saveError}</Alert>}
        <div className="drawer-actions">
          <Button onClick={cancelSafely} disabled={busy}>取消</Button>
          <Button variant="primary" onClick={() => { void save(); }}
            disabled={busy || !draft}>
            {busy ? "正在保存…" : "保存修改"}
          </Button>
        </div>
      </section>
    </div>
  );
}
