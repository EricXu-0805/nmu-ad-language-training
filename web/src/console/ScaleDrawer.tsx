import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { StatusPill } from "../components/StatusPill";
import { useDialogFocusTrap } from "../components/useDialogFocusTrap";
import type { AssessmentEvent, ScaleProtocolReadiness, ScaleResult } from "../types";
import { QuestionnairePanel } from "./QuestionnairePanel";
import { createScaleDrawerRefreshGate, requireScaleRowsForPatient } from "./scaleDrawerRefresh";

function assessmentTimepointLabel(timepoint: AssessmentEvent["timepoint"]): string {
  return { pretest: "前测", posttest: "后测", followup: "随访" }[timepoint];
}

function assessmentCategoryLabel(
  category: AssessmentEvent["instances"][number]["category_key"],
): string {
  return category === "untrained_standardized_naming"
    ? "未训练词标准化命名"
    : "功能沟通";
}

function assessmentEventTone(event: AssessmentEvent): "primary" | "warn" | "ok" | "muted" {
  if (event.status === "due") return "primary";
  if (event.status === "in_progress" || event.status === "awaiting_closeout") return "warn";
  return event.status === "closed" ? "ok" : "muted";
}

function assessmentEventStatusLabel(event: AssessmentEvent): string {
  return {
    due: "待施测",
    in_progress: "施测中",
    awaiting_closeout: "待现场收尾",
    closed: "已收尾",
    cancelled: "已取消",
  }[event.status];
}

function assessmentInstanceStatusLabel(
  instance: AssessmentEvent["instances"][number],
): string {
  if (instance.status === "due") return "待施测";
  if (instance.status === "in_progress") return "施测中";
  if (instance.status === "approved_deferred") return "已批准延期 · 无正式分数";
  return instance.scoring_evidence
    ? "已完成 · 计分证据已保存"
    : "证据异常 · 已阻止";
}

function scaleReadinessLabel(readiness: ScaleProtocolReadiness): string {
  if (readiness.status === "ready_for_research") return "正式量表流程已就绪";
  if (readiness.status === "awaiting_pi_definition") return "等待 PI 冻结两表定义";
  if (readiness.status === "awaiting_workflow_policy") return "定义已冻结 · 等待工作流政策";
  if (readiness.status === "awaiting_definition_artifacts") {
    return "定义与政策已冻结 · 等待运行时制品";
  }
  return "等待平台可信执行能力";
}

function scaleReadinessExplanation(readiness: ScaleProtocolReadiness): string {
  if (readiness.status === "awaiting_pi_definition") {
    return "须先冻结具体工具、授权版本、条目与结果结构、缺失/中止规则、计分算法及批准范围。";
  }
  if (readiness.status === "awaiting_workflow_policy") {
    return "两表定义完整不代表流程可用；排期、延期批准权限、改期、现场收尾和评估员分配规则及其批准仍未冻结。";
  }
  if (readiness.status === "awaiting_definition_artifacts") {
    return "定义与工作流政策已完整，但运行时定义制品尚未与冻结清单逐字段匹配，或未获正式研究批准。";
  }
  return "通用结果与工作流合同已存在，但尚未证明冻结定义制品、逐题录音证据和冻结工作流政策由服务器真正强制执行。可信执行验证完成前不会创建正式实例或自动计分。";
}

function scaleReadinessSummary(readiness: ScaleProtocolReadiness): string {
  if (readiness.status === "awaiting_pi_definition") {
    return "等待研究负责人确定量表工具和计分规则。";
  }
  if (readiness.status === "awaiting_workflow_policy") {
    return "量表工具已确定，施测流程规则还没批准。";
  }
  if (readiness.status === "awaiting_definition_artifacts") {
    return "规则已批准，系统配置还没通过最终核对。";
  }
  return "平台核验还没完成。";
}

// PI 尚未冻结具体工具时只展示可核查的就绪状态与历史未验证记录。
// 自由填写的量表名/分数不能伪装成正式研究结局。
export function ScaleDrawer({ patientId, open, onClose }: {
  patientId: string;
  open: boolean;
  onClose: () => void;
}) {
  const panelRef = useDialogFocusTrap<HTMLElement>({
    open,
    onCancel: onClose,
    initialFocus: "first-button",
  });
  const [readiness, setReadiness] = useState<ScaleProtocolReadiness | null>(null);
  const [assessmentEvents, setAssessmentEvents] = useState<AssessmentEvent[] | null>(null);
  const [rows, setRows] = useState<ScaleResult[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [refreshGate] = useState(() => createScaleDrawerRefreshGate());
  const activeContextRef = useRef({ patientId, open });
  activeContextRef.current = { patientId, open };

  const refresh = useCallback(async () => {
    const ticket = refreshGate.begin(patientId);
    setReadiness(null);
    setAssessmentEvents(null);
    setRows(null);
    setError(null);
    try {
      const [nextReadiness, nextAssessmentEvents, nextRows] = await Promise.all([
        api.scaleProtocol(),
        api.listPatientAssessmentEvents(patientId),
        api.listScales(patientId),
      ]);
      const activeContext = activeContextRef.current;
      if (!activeContext.open || !refreshGate.accepts(ticket, activeContext.patientId)) return;
      const verifiedRows = requireScaleRowsForPatient(nextRows, activeContext.patientId);
      setReadiness(nextReadiness);
      setAssessmentEvents(nextAssessmentEvents);
      setRows(verifiedRows);
    } catch (reason) {
      const activeContext = activeContextRef.current;
      if (!activeContext.open || !refreshGate.accepts(ticket, activeContext.patientId)) return;
      setError(reason instanceof ApiError ? reason.detail : String(reason));
    }
  }, [patientId, refreshGate]);

  useEffect(() => {
    if (!open) {
      refreshGate.cancel();
      return;
    }
    void refresh();
    return () => refreshGate.cancel();
  }, [open, refresh, refreshGate, retry]);

  if (!open) return null;

  return (
    <div className="drawer-backdrop">
      <section ref={panelRef} className="drawer-panel fade-in" role="dialog" aria-modal="true"
        aria-labelledby="scale-drawer-title">
        <div className="drawer-header">
          <div>
            <div className="page-kicker">受试者 {patientId}</div>
            <h2 id="scale-drawer-title">正式量表就绪状态</h2>
          </div>
          <Button onClick={onClose}>关闭</Button>
        </div>

        {error && (
          <Alert tone="danger" title="量表状态读取失败"
            actions={<Button onClick={() => setRetry((value) => value + 1)}>重新加载</Button>}>
            {error}。请点「重新加载」重试。
          </Alert>
        )}
        {!error && (!readiness || assessmentEvents === null || rows === null) && (
          <Alert tone="info" title="正在读取量表状态">
            读取完成后会显示就绪状态和评估记录。
          </Alert>
        )}

        {readiness && (
          <div className="card col">
            <div className="row wrap" style={{ justifyContent: "space-between" }}>
              <h3>正式量表工具</h3>
              <StatusPill tone={readiness.ready_for_research ? "ok" : "warn"}>
                {readiness.ready_for_research ? "正式量表流程已就绪" : "量表流程尚未开通"}
              </StatusPill>
            </div>
            {readiness.categories.map((category) => (
              <div className="row wrap" key={category.category_key}
                style={{ justifyContent: "space-between", borderBottom: "1px solid var(--c-line)", paddingBottom: 8 }}>
                <span>{category.label}</span>
                <StatusPill tone={category.scoring_ready ? "ok" : "muted"}>
                  {category.instrument_name && category.instrument_version
                    ? `${category.instrument_name} · ${category.instrument_version}`
                    : "工具和版本还未确定"}
                </StatusPill>
              </div>
            ))}
            {!readiness.ready_for_research && (
              <Alert tone="warn" title="现在还不能录入正式量表分数">
                {scaleReadinessSummary(readiness)}
              </Alert>
            )}
            <details>
              <summary>技术详情</summary>
              <p className="muted">
                {scaleReadinessLabel(readiness)}
                {readiness.ready_for_research ? "" : `：${scaleReadinessExplanation(readiness)}`}
              </p>
              {([
                ["定义清单", readiness.definition_ready],
                ["定义/逐题证据可信执行",
                  readiness.definition_artifact_enforcement_ready],
                ["运行时定义制品", readiness.definition_artifacts_ready],
                ["工作流政策", readiness.workflow_policy_ready],
                ["工作流政策可信执行",
                  readiness.workflow_policy_enforcement_ready],
                ["平台结果与工作流合同",
                  readiness.formal_result_contract_ready && readiness.workflow_contract_ready],
              ] as const).map(([label, ready]) => (
                <div className="row wrap" key={label} style={{ justifyContent: "space-between" }}>
                  <span>{label}</span>
                  <StatusPill tone={ready ? "ok" : "muted"}>{ready ? "已核验" : "未就绪"}</StatusPill>
                </div>
              ))}
              {!readiness.ready_for_research && (
                <p className="muted">
                  {`服务端 instance_creation_enabled=${String(readiness.instance_creation_enabled)}；训练正确率、AI 运营判分和 legacy 自由分数仍不是正式结果。`}
                </p>
              )}
              {readiness.blocking_issues.length > 0 && (
                <ul>{readiness.blocking_issues.map((issue) => <li key={issue.code}>{issue.message}</li>)}</ul>
              )}
            </details>
          </div>
        )}

        <QuestionnairePanel patientId={patientId} />

        {assessmentEvents && (
          <div className="card col">
            <div className="row wrap" style={{ justifyContent: "space-between" }}>
              <h3>正式评估记录</h3>
              <StatusPill tone={assessmentEvents.some((event) =>
                event.status === "in_progress" || event.status === "awaiting_closeout")
                ? "warn" : "muted"}>
                {assessmentEvents.length} 个事件
              </StatusPill>
            </div>
            {assessmentEvents.length === 0 && (
              <p className="muted">还没有正式评估记录。</p>
            )}
            {assessmentEvents.map((event) => (
              <div className="col" key={event.event_id}
                style={{ borderBottom: "1px solid var(--c-line)", paddingBottom: 10 }}>
                <div className="row wrap" style={{ justifyContent: "space-between" }}>
                  <span>
                    <strong>{assessmentTimepointLabel(event.timepoint)}</strong>
                    {` · ${event.scheduled_date}`}
                  </span>
                  <StatusPill tone={assessmentEventTone(event)}>
                    {assessmentEventStatusLabel(event)}
                  </StatusPill>
                </div>
                {event.instances.map((instance) => (
                  <div className="row wrap" key={instance.instance_id}
                    style={{ justifyContent: "space-between" }}>
                    <span>{assessmentCategoryLabel(instance.category_key)}</span>
                    <span className="muted">{assessmentInstanceStatusLabel(instance)}</span>
                  </div>
                ))}
                <small className="muted">
                  {event.formal_outcome_eligible
                    ? "是否已有正式分数，以上面每项的完成状态为准。"
                    : "模拟或未批准的记录，不计入研究结果。"}
                </small>
              </div>
            ))}
          </div>
        )}

        {rows && (
          <div className="card col">
            <h3>历史未验证记录 · {rows.length} 条</h3>
            <p className="muted">以下是旧系统的历史记录，仅供核查，不计入研究结果。</p>
            {rows.length === 0 && <p className="muted">没有历史未验证记录。</p>}
            {rows.map((row) => (
              <div key={row.id} className="row wrap"
                style={{ justifyContent: "space-between", borderBottom: "1px solid var(--c-line)", paddingBottom: 6 }}>
                <span><StatusPill tone="muted">历史未验证</StatusPill> {row.phase_type} · {row.scale_name}{row.subscale ? ` · ${row.subscale}` : ""}</span>
                <strong className="mono">{row.score ?? "—"}</strong>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
