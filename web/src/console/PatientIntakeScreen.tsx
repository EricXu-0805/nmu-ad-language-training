import { useState } from "react";
import { api, ApiError } from "../api";
import { Button } from "../components/Button";
import { EnumSelect, Field, TextInput, TriStateField } from "../components/Field";
import { useToast } from "../components/Toast";
import { CONSENT_TYPES } from "../types";
import type { Patient } from "../types";

// 建档 + 合规字段(护栏2 全 nullable,"未评/待SOP"为一等公民)。绝不显示姓名,只用 patient_id。
export function PatientIntakeScreen({ onReady }: { onReady: (patientId: string) => void }) {
  const toast = useToast();
  const [p, setP] = useState<Patient>({ patient_id: "" });
  const [busy, setBusy] = useState(false);
  const set = <K extends keyof Patient>(k: K, v: Patient[K]) => setP((prev) => ({ ...prev, [k]: v }));

  async function submit() {
    if (!p.patient_id.trim()) { toast("请填写受试者编号 patient_id", "warn"); return; }
    setBusy(true);
    try {
      await api.createPatient(p);
      toast("建档成功", "ok");
      onReady(p.patient_id);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // 已存在不是错误,取回既有档案后继续。
        toast(`编号 ${p.patient_id} 已建档,直接进入建场次`, "info");
        onReady(p.patient_id);
      } else {
        toast(e instanceof ApiError ? e.detail : String(e), "danger");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="col" style={{ maxWidth: 720 }}>
      <h2>建档 · 受试者档案</h2>
      <p className="muted">合规字段可留空(SOP 未回时"未评"是一等公民,不等于"否")。此屏只用编号,绝不录姓名。</p>

      <div className="card col">
        <h3>入组资格</h3>
        <Field label="受试者编号 patient_id" hint="研究内部编号,勿用姓名/身份证">
          <TextInput value={p.patient_id} onChange={(e) => set("patient_id", e.target.value)} placeholder="如 P001" />
        </Field>
        <Field label="痴呆严重程度">
          <TextInput value={p.dementia_severity ?? ""} onChange={(e) => set("dementia_severity", e.target.value || null)} placeholder="轻度 / 中度" />
        </Field>
        <TriStateField label="普通话资格(mandarin_eligible)" value={p.mandarin_eligible} onChange={(v) => set("mandarin_eligible", v)} />
      </div>

      <div className="card col">
        <h3>合规(护栏2,可全留空预留)</h3>
        <Field label="知情同意类型">
          <EnumSelect options={CONSENT_TYPES} value={p.consent_type ?? null} onChange={(v) => set("consent_type", v as Patient["consent_type"])} />
        </Field>
        <Field label="同意签署人(consent_person)">
          <TextInput value={p.consent_person ?? ""} onChange={(e) => set("consent_person", e.target.value || null)} placeholder="本人 / 监护人角色(勿写真名)" />
        </Field>
        <TriStateField label="代理同意(proxy_consent)" value={p.proxy_consent} onChange={(v) => set("proxy_consent", v)} />
        <TriStateField label="允许录音(recording_allowed)" value={p.recording_allowed} onChange={(v) => set("recording_allowed", v)} />
        {p.recording_allowed === false && (
          <div className="card" style={{ background: "var(--c-danger-bg)", border: "1px solid var(--c-danger)", color: "var(--c-danger)" }}>
            该受试者不允许录音 → 训练屏将禁用录音,由研究者现场听记。
          </div>
        )}
      </div>

      <Button variant="primary" disabled={busy} onClick={submit}>{busy ? "提交中…" : "建档并继续"}</Button>
    </div>
  );
}
