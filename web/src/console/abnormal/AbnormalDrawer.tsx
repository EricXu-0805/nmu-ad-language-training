import { useState } from "react";
import { api, ApiError } from "../../api";
import { Button } from "../../components/Button";
import { EnumSelect, Field, TextInput } from "../../components/Field";
import { useToast } from "../../components/ToastContext";
import { useDialogFocusTrap } from "../../components/useDialogFocusTrap";
import { ABNORMAL_TYPES, CUE_INTERVENTIONS, INTERVENTION_TYPES } from "../../types";
import type { PhaseType } from "../../types";

// 记异常/介入。正式训练周的代说物品名/称呼 → 前端即时红条 + 预勾影响判分有效性(镜像后端强制)。
export function AbnormalDrawer({ sessionId, phaseType, currentItemEventId, open, onClose }: {
  sessionId: string;
  phaseType: PhaseType | null;
  currentItemEventId?: number | null;
  open: boolean;
  onClose: () => void;
}) {
  const toast = useToast();
  const [intervention, setIntervention] = useState<string | null>(null);
  const [abnormal, setAbnormal] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const cancelSafely = () => { if (!busy) onClose(); };
  const panelRef = useDialogFocusTrap<HTMLElement>({
    open,
    onCancel: cancelSafely,
    initialFocus: "first-button",
  });

  const cueBreach = phaseType === "正式训练" && intervention != null &&
    (CUE_INTERVENTIONS as readonly string[]).includes(intervention);

  if (!open) return null;

  async function submit() {
    if (!intervention && !abnormal) { toast("请选择异常类型或介入类型", "warn"); return; }
    setBusy(true);
    try {
      await api.recordAbnormal(sessionId, {
        item_event_id: currentItemEventId ?? null,
        abnormal_type: abnormal,
        intervention_type: intervention,
        affects_scoring_validity: cueBreach,   // 后端仍会对越界介入强制置位,这里前端先如实反映
        note: note || null,
      });
      toast("已记录异常/介入", "ok");
      setIntervention(null); setAbnormal(null); setNote("");
      onClose();
    } catch (e) {
      toast(e instanceof ApiError ? e.detail : String(e), "danger");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="drawer-backdrop" onClick={cancelSafely}>
      <section ref={panelRef} className="drawer-panel fade-in" role="dialog" aria-modal="true"
        aria-busy={busy || undefined} tabIndex={-1}
        aria-labelledby="abnormal-drawer-title" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-header">
          <div><div className="page-kicker">现场记录</div><h2 id="abnormal-drawer-title">异常与研究者介入</h2></div>
          <Button disabled={busy} onClick={cancelSafely}>关闭</Button>
        </div>
        <p className="muted">当前阶段：{phaseType ?? "—"}{currentItemEventId ? ` · 当前题记录 #${currentItemEventId}` : " · 整个场次"}</p>

        <Field label="研究者介入类型">
          <EnumSelect options={INTERVENTION_TYPES} value={intervention} disabled={busy} onChange={setIntervention} />
        </Field>
        <Field label="受试者或环境异常">
          <EnumSelect options={ABNORMAL_TYPES} value={abnormal} disabled={busy} onChange={setAbnormal} />
        </Field>

        {cueBreach && (
          <div className="alert alert--danger" role="alert">
            正式训练中“{intervention}”属于线索性越界介入。本题会被标记为<strong>可能影响判分有效性</strong>，保存后不可撤销。
          </div>
        )}

        <Field label="现场说明（可选）">
          <TextInput value={note} disabled={busy} onChange={(e) => setNote(e.target.value)} placeholder="现场情况说明" />
        </Field>

        <div className="drawer-actions"><Button variant="primary" disabled={busy} onClick={submit}>{busy ? "正在记录…" : "保存记录"}</Button></div>
      </section>
    </div>
  );
}
