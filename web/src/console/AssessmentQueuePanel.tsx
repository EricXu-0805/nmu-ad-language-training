import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { StatusPill } from "../components/StatusPill";
import type { AssessmentEvent, AssessmentEventsToday, ScaleProtocolReadiness } from "../types";
import { AssessmentEventCreateForm } from "./AssessmentEventCreateForm";
import { AssessmentExecutionDrawer } from "./AssessmentExecutionDrawer";
import {
  assessmentCategoryLabel,
  assessmentInstanceStatusLabel,
  assessmentQueueStatusPresentation,
  assessmentQueueView,
  assessmentTimepointLabel,
} from "./assessmentQueue";
import { DataBoundaryFilter } from "./DataBoundaryFilter";
import {
  partitionByDataClassification,
  type DataClassification,
} from "./dataClassification";

function assessmentErrorText(error: unknown): string {
  return error instanceof ApiError ? error.detail : String(error);
}

// 正式评估与周训练是两条独立工作流。这个子队列自己加载、自己失败，
// 因此评估接口异常不会清空或锁住 RunPickerScreen 中的训练安排。
export function AssessmentQueuePanel() {
  const [assessmentToday, setAssessmentToday] = useState<AssessmentEventsToday | null>(null);
  const [assessmentError, setAssessmentError] = useState<string | null>(null);
  const [assessmentLoading, setAssessmentLoading] = useState(false);
  const [classificationFilter, setClassificationFilter] = useState<DataClassification>("research");
  const [readiness, setReadiness] = useState<ScaleProtocolReadiness | null>(null);
  const [readinessError, setReadinessError] = useState<string | null>(null);
  const [readinessRetry, setReadinessRetry] = useState(0);
  const [executionEvent, setExecutionEvent] = useState<AssessmentEvent | null>(null);
  const assessmentRequest = useRef(0);

  const loadAssessmentQueue = useCallback(async () => {
    const request = ++assessmentRequest.current;
    setAssessmentToday(null);
    setAssessmentError(null);
    setAssessmentLoading(true);
    try {
      const next = await api.listTodayAssessmentEvents();
      if (assessmentRequest.current === request) setAssessmentToday(next);
    } catch (error) {
      if (assessmentRequest.current === request) {
        setAssessmentToday(null);
        setAssessmentError(assessmentErrorText(error));
      }
    } finally {
      if (assessmentRequest.current === request) setAssessmentLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadAssessmentQueue();
    return () => { assessmentRequest.current += 1; };
  }, [loadAssessmentQueue]);

  useEffect(() => {
    let active = true;
    setReadinessError(null);
    api.scaleProtocol()
      .then((next) => { if (active) setReadiness(next); })
      .catch((error) => {
        if (!active) return;
        setReadiness(null);
        setReadinessError(assessmentErrorText(error));
      });
    return () => { active = false; };
  }, [readinessRetry]);

  const executionOpen = readiness?.ready_for_research === true;

  const view = assessmentQueueView(assessmentToday, assessmentError);
  const classified = view.kind === "ready"
    ? partitionByDataClassification(view.events, (event) => event.data_classification)
    : { research: [], simulation: [], legacy_unknown: [] };
  const counts = {
    research: classified.research.length,
    simulation: classified.simulation.length,
    legacy_unknown: 0,
  };
  const visibleEvents = classified[classificationFilter];

  return (
    <section
      className="form-section"
      aria-labelledby="formal-assessment-queue-title"
      aria-busy={assessmentLoading || view.kind === "loading" || undefined}
    >
      <div className="form-section-header">
        <div>
          <p className="page-kicker">正式评估</p>
          <h3 id="formal-assessment-queue-title">今日正式评估队列</h3>
          <p className="muted">先处理未收尾的评估。</p>
        </div>
        <Button
          disabled={assessmentLoading}
          onClick={() => { void loadAssessmentQueue(); }}
        >
          {assessmentLoading ? "正在刷新评估…" : "刷新评估队列"}
        </Button>
      </div>

      {!executionOpen && readinessError && (
        <Alert tone="danger" title="就绪状态读取失败"
          actions={<Button onClick={() => setReadinessRetry((value) => value + 1)}>重新读取</Button>}>
          {readinessError}。读取成功前，这里先不开放量表执行入口。
        </Alert>
      )}
      {!executionOpen && !readinessError && readiness === null && (
        <StatusPill tone="muted">正在核对量表就绪状态…</StatusPill>
      )}
      {!executionOpen && !readinessError && readiness !== null && (
        <Alert tone="warn" title="量表流程尚未开通">
          正式量表还没开放录入，暂时只能查看待办；开放后每行会出现「打开执行」。
        </Alert>
      )}
      {executionOpen && <AssessmentEventCreateForm
        readiness={readiness}
        onCreated={() => { void loadAssessmentQueue(); }} />}
      {executionOpen && executionEvent && (
        <AssessmentExecutionDrawer
          event={executionEvent}
          readiness={readiness}
          onReceipt={(next) => {
            setExecutionEvent(next);
            void loadAssessmentQueue();
          }}
          onDismiss={() => setExecutionEvent(null)}
          onConflictRefresh={() => {
            setExecutionEvent(null);
            void loadAssessmentQueue();
          }}
        />
      )}

      {view.kind === "loading" && (
        <StatusPill tone="muted">正在读取今日正式评估待办…</StatusPill>
      )}
      {view.kind === "error" && (
        <Alert
          tone="danger"
          title="正式评估队列加载失败·训练队列仍可独立使用"
          actions={<Button onClick={() => { void loadAssessmentQueue(); }}>重试评估队列</Button>}
        >
          {view.detail}
        </Alert>
      )}
      {view.kind === "empty" && (
        <Alert tone="info" title="今天没有可见的正式评估待办">
          队列日期 {view.asOfDate}。已收尾、已取消与已撤回受试者的事件不会在此重新出现。
        </Alert>
      )}
      {view.kind === "ready" && (
        <>
          <div className="form-section-header">
            <div>
              <h3>评估工作顺序</h3>
              <p className="muted">
                队列日期 {view.asOfDate}；先收尾，再继续进行中评估，最后核对待开始安排。
              </p>
            </div>
            <StatusPill tone="primary">待办 {view.events.length} 项</StatusPill>
          </div>
          <DataBoundaryFilter
            value={classificationFilter}
            counts={counts}
            onChange={setClassificationFilter}
            label="正式评估数据分区"
          />
          {classificationFilter === "simulation" && (
            <Alert tone="danger" title="当前是专用模拟评估队列">
              模拟评估不计入研究结果。
            </Alert>
          )}
          {visibleEvents.length === 0 && (
            <p className="muted">当前数据分区没有可见的正式评估待办。</p>
          )}
          <div className="visit-plan-queue" role="list" aria-label="今日正式评估只读待办">
            {visibleEvents.map((event, index) => {
              const status = assessmentQueueStatusPresentation(event, view.asOfDate);
              return (
                <article className="run-picker-card" role="listitem" key={event.event_id}>
                  <div className="run-picker-head">
                    <div className="col" style={{ gap: 5 }}>
                      <div className="row wrap">
                        <StatusPill tone={status.tone}>{status.priorityLabel}</StatusPill>
                        <StatusPill tone={event.data_classification === "research" ? "ok" : "warn"}>
                          {event.data_classification === "research" ? "真实研究数据区" : "专用模拟数据区"}
                        </StatusPill>
                        <StatusPill tone="muted">工作顺序第 {index + 1} 位</StatusPill>
                      </div>
                      <strong className="mono" style={{ fontSize: "1.15em" }}>{event.patient_id}</strong>
                      <span>
                        {assessmentTimepointLabel(event.timepoint)}·计划日期 {event.scheduled_date}
                      </span>
                      <span className="muted">{status.nextStep}</span>
                    </div>
                    <div className="col" style={{ gap: 6, alignItems: "flex-end" }}>
                      <StatusPill tone={status.tone}>{status.statusLabel}</StatusPill>
                      {executionOpen && (
                        <Button size="sm"
                          onClick={() => setExecutionEvent(event)}>打开执行</Button>
                      )}
                    </div>
                  </div>
                  <div className="row wrap" aria-label="两类正式评估实例状态">
                    {event.instances.map((instance) => (
                      <StatusPill tone="muted" size="sm" key={instance.instance_id}>
                        {assessmentCategoryLabel(instance.category_key)}·
                        {assessmentInstanceStatusLabel(
                          instance.status,
                          instance.item_response_count,
                          instance.required_item_count,
                        )}
                      </StatusPill>
                    ))}
                  </div>
                </article>
              );
            })}
          </div>
        </>
      )}
    </section>
  );
}
