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
    <section className="card col training-observer-console" aria-label="服务端托管观察台">
      <div className="row wrap" style={{ justifyContent: "space-between" }}>
        <div>
          <div className="page-kicker">服务端托管观察台</div>
          <strong>{view.label}</strong>
        </div>
        <StatusPill tone={view.tone}>
          {view.proven ? "服务器已证实持有控制权" : "为安全暂缓人工控制 · 尚未证实"}
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
          <span className="muted">服务器计划位置待同步，不显示估计进度。</span>
        )}
      </div>
      <p className="muted">进度取自服务器实时游标，仅供运营观察，不代表已完成的研究评分或完成率。暂停、中止与显式接管请使用本页上方既有控制。</p>
      {resyncStatus === "failed" && (
        <div className="row wrap">
          {resyncError && <span className="muted">{resyncError}</span>}
          <Button variant="primary" onClick={onRetryResync}>重试恢复人工控制</Button>
        </div>
      )}
    </section>
  );
}
