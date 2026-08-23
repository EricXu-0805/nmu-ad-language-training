import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { EnumSelect, Field, TextInput, TriStateField } from "../components/Field";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/ToastContext";
import { useDialogFocusTrap } from "../components/useDialogFocusTrap";
import { CONSENT_TYPES } from "../types";
import type { Patient } from "../types";
import {
  buildProfilePatch,
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

// 档案编辑抽屉:手滑填错不用重新建档。只覆盖非治理字段;撤回、云处理授权、
// 模拟/研究身份各有专属流程,这里一律不出现。
export function PatientEditDrawer({ patientId, sessionCount, onClose, onSaved }: {
  patientId: string;
  sessionCount: number;
  onClose: () => void;
  onSaved: (updated: Patient) => void;
}) {
  const toast = useToast();
  const [original, setOriginal] = useState<ProfileDraft | null>(null);
  const [draft, setDraft] = useState<ProfileDraft | null>(null);
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
      })
      .catch((error) => {
        if (active) setLoadError(error instanceof ApiError ? error.detail : String(error));
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
                    <p className="muted">以伦理批件和现场签署文件为准。研究撤回和云端 AI 授权不在这里改，各有专门入口。</p>
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
