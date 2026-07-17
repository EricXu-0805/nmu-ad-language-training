import { useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { EnumSelect, Field, TextInput, TriStateField } from "../components/Field";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/ToastContext";
import { CONSENT_TYPES } from "../types";
import type { Patient } from "../types";

// 409 冲突核对覆盖的合规字段(建档表单可填的全部档案字段)
const FIELD_LABELS: Partial<Record<keyof Patient, string>> = {
  dementia_severity: "认知障碍程度",
  mandarin_eligible: "普通话要求",
  consent_type: "知情同意方式",
  consent_person: "签署人角色",
  proxy_consent: "代理同意",
  recording_allowed: "录音授权",
};

// 建档 + 合规字段(护栏2 全 nullable,"未评/待SOP"为一等公民)。绝不显示姓名,只用 patient_id。
// context="registry":准备区登记(测试前),文案不提"进入场次";"intake" 保留旧线性流文案。
// onTrain(仅 registry):保存成功后直接为该受试者进入场次设置——新受试者建档→开训一条直线。
export function PatientIntakeScreen({ onReady, onTrain, context = "intake" }: {
  onReady: (patientId: string) => void; onTrain?: (patientId: string) => void; context?: "intake" | "registry";
}) {
  const registry = context === "registry";
  const toast = useToast();
  const [p, setP] = useState<Patient>({ patient_id: "" });
  const [busy, setBusy] = useState(false);
  const set = <K extends keyof Patient>(k: K, v: Patient[K]) => setP((prev) => ({ ...prev, [k]: v }));

  async function submit(then: (patientId: string) => void) {
    if (busy) return;
    if (!p.patient_id.trim()) { toast("请填写受试者研究编号", "warn"); return; }
    setBusy(true);
    try {
      await api.createPatient(p);
      toast(registry ? "受试者已登记" : "受试者档案已保存", "ok");
      then(p.patient_id);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // 编号已存在不算错误,但绝不能静默沿用:409 时表单内容不会写入服务器,
        // 若研究者刚改了合规字段(尤其录音授权)而系统按旧档案执行,是真实伦理事故。
        await resolveExisting(then);
      } else {
        toast(e instanceof ApiError ? e.detail : String(e), "danger");
      }
    } finally {
      setBusy(false);
    }
  }

  // 409 后取回既有档案核对:撤回状态一律拦下;表单已填字段与档案不一致则警示并回登记表,
  // 三者皆无才沿用既有档案继续走 then(开训或回列表)。
  async function resolveExisting(then: (patientId: string) => void) {
    let existing: Patient;
    try {
      existing = await api.getPatient(p.patient_id);
    } catch {
      toast(`研究编号 ${p.patient_id} 已存在，但档案读取失败，请回登记表核对`, "danger");
      return;
    }
    if (existing.withdrawal_status) {
      toast(`受试者 ${p.patient_id} 撤回状态为「${existing.withdrawal_status}」，不可登记或开训`, "danger");
      return;
    }
    const conflicts = (Object.keys(FIELD_LABELS) as (keyof Patient)[]).filter((k) => {
      const filled = p[k];
      if (filled == null || filled === "") return false;
      return (existing[k] ?? null) !== filled;
    });
    if (conflicts.length > 0) {
      toast(`研究编号 ${p.patient_id} 已存在，表单未写入服务器：${conflicts.map((k) => FIELD_LABELS[k]).join("、")}与既有档案不一致，请在登记表核对`, "danger");
      if (registry) onReady(p.patient_id);
      return;
    }
    toast(registry ? `研究编号 ${p.patient_id} 已在登记表中，沿用既有档案` : `研究编号 ${p.patient_id} 已存在，已进入场次设置`, "info");
    then(p.patient_id);
  }

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

      <div className="form-layout">
        <section className="form-section">
          <div className="form-section-header">
            <div>
              <h3>基本信息与入组资格</h3>
              <p className="muted">“未评”表示尚未完成核对，不会被系统自动当作“否”。</p>
            </div>
            <StatusPill tone={p.patient_id.trim() ? "ok" : "muted"}>{p.patient_id.trim() ? "编号已填写" : "等待填写"}</StatusPill>
          </div>
          <div className="form-grid">
            <Field label="受试者研究编号" hint="使用课题内部编号；请勿填写姓名、手机号或身份证号" required>
              <TextInput value={p.patient_id} onChange={(e) => set("patient_id", e.target.value)} placeholder="例如 P001" autoComplete="off" required />
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
            <Field label="知情同意方式">
              <EnumSelect options={CONSENT_TYPES} value={p.consent_type ?? null} onChange={(v) => set("consent_type", v as Patient["consent_type"])} placeholder="请选择（可稍后补录）" />
            </Field>
            <Field label="签署人角色" hint="只填写“本人”“配偶”“监护人”等角色，不填写真实姓名">
              <TextInput value={p.consent_person ?? ""} onChange={(e) => set("consent_person", e.target.value || null)} placeholder="例如：本人 / 监护人" />
            </Field>
            <TriStateField label="是否使用代理同意" value={p.proxy_consent} onChange={(v) => set("proxy_consent", v)} />
            <TriStateField label="是否允许研究录音" value={p.recording_allowed} onChange={(v) => set("recording_allowed", v)} />
          </div>
          {p.recording_allowed === false && (
            <Alert tone="danger" title="录音已明确禁止">
              训练流程会关闭所有录音入口，请由研究者现场听记，不得绕过该设置。
            </Alert>
          )}
        </section>
      </div>

      <div className="form-actions">
        <p className="muted grow">
          {registry && onTrain ? "可保存后直接为这位受试者开训，也可仅登记、稍后再训；档案不记录姓名。"
            : registry ? "保存后返回登记表，可继续登记或去训练台选人；档案不记录姓名。"
            : "保存后将进入本次场次设置，档案不会记录受试者姓名。"}
        </p>
        {registry && onTrain && (
          <Button disabled={busy} onClick={() => submit(onReady)}>仅保存登记</Button>
        )}
        <Button variant="primary" disabled={busy}
          onClick={() => submit(registry && onTrain ? onTrain : onReady)}>
          {busy ? "正在保存…" : registry ? (onTrain ? "保存并直接开训" : "保存登记") : "保存档案，进入下一步"}
        </Button>
      </div>
    </div>
  );
}
