import { Button } from "../../components/Button";
import { StatusPill } from "../../components/StatusPill";
import {
  observerPhaseView,
  type ManualResyncStatus,
  type ObserverOwnershipPhase,
  type ObserverPlanPosition,
} from "./observerConsoleModel.ts";

// 服务端托管观察台：只读运营视图。逐步人工操作（题位切换/提示/录音/确认/锁分）
// 在锁定期间整块不挂载；这里的位置只来自服务端 runtime.cursor 的计划映射，
// 是运营进度提示，不是已锁定的科研真值或完成率。
export function ObserverConsole({ patientCode, phase, resyncStatus, resyncError, position, onRetryResync }: {
  patientCode: string;
  phase: ObserverOwnershipPhase;
  resyncStatus: ManualResyncStatus;
  resyncError: string | null;
  position: ObserverPlanPosition | null;
  onRetryResync: () => void;
}) {
  const view = observerPhaseView(phase, resyncStatus);
  return (
    <section className="card col training-observer-console" aria-label="AI 自动训练（只可观察）">
      <div className="row wrap" style={{ justifyContent: "space-between" }}>
        <div>
          <div className="page-kicker">AI 自动训练（只可观察）</div>
          <strong>{view.label}</strong>
        </div>
        <StatusPill tone={view.tone}>
          {view.proven ? "AI 控制中" : "等待服务器确认"}
        </StatusPill>
      </div>
      <p className="muted">{view.detail}</p>
      <div className="row wrap">
        <StatusPill tone="muted">受试者编号 {patientCode}</StatusPill>
        {position ? (
          <>
            <StatusPill tone="primary">{position.taskType}</StatusPill>
            <span>当前任务：{position.itemLabel} · {position.responseRole}</span>
            <span>计划位置：第 {position.itemOrdinal}/{position.itemTotal} 题 · 环节 {position.turnOrdinal}/{position.turnTotal}</span>
          </>
        ) : (
          <span className="muted">进度同步中…</span>
        )}
      </div>
      <p className="muted">此进度仅供观察，评分以事后复核为准。</p>
      {resyncStatus === "failed" && (
        <div className="row wrap">
          {resyncError && <span className="muted">{resyncError}</span>}
          <Button variant="primary" onClick={onRetryResync}>重试恢复人工控制</Button>
        </div>
      )}
    </section>
  );
}
