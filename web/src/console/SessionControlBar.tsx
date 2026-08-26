import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { StatusPill } from "../components/StatusPill";
import type { SessionRuntimeStatus } from "../types";

export function SessionControlBar({ paused, loading = false, busy, resumeBlocked = false, resumeBlockedHint, recoveredLabel, error, terminalStatus, onPause, onResume, onRetry, onExit }: {
  paused: boolean;
  loading?: boolean;
  busy?: boolean;
  resumeBlocked?: boolean;
  // resumeBlocked 时给用户的真话指引:说清此刻谁挡着、去哪里继续。不给则退回
  // 通用文案。本栏不写「等待服务器确认」这类暗示等着就会有结果的话——这里没有
  // 任何在途核实(观察台的同名文案有真实的权威轮询在途,不在此列)。
  resumeBlockedHint?: string | null;
  recoveredLabel?: string | null;
  error?: string | null;
  terminalStatus?: SessionRuntimeStatus | null;
  onPause: () => void;
  onResume: () => void;
  onRetry?: () => void;
  onExit?: () => void;
}) {
  if (terminalStatus === "intervention_completed" || terminalStatus === "completed" || terminalStatus === "aborted" || terminalStatus === "failed") {
    const label = terminalStatus === "intervention_completed" ? "床旁干预已结束，等待研究复核"
      : terminalStatus === "completed" ? "场次已完成"
        : terminalStatus === "aborted" ? "场次已中止" : "场次已异常结束";
    return (
      <Alert className="recovery-note" tone={terminalStatus === "completed" ? "ok" : terminalStatus === "intervention_completed" ? "warn" : "danger"}
        title={`${label}，当前不可继续训练`}
        actions={onExit ? <Button variant="primary" onClick={onExit}>离开此场次</Button> : undefined}>
        本场已结束，所有训练操作已关闭。
      </Alert>
    );
  }
  const retryMode = Boolean(error);
  return (
    <>
      <section className={`session-control-bar${paused ? " is-paused" : ""}`} aria-label="场次运行状态">
        <div className="session-control-bar__copy">
          <StatusPill tone={loading ? "muted" : retryMode ? "danger" : paused ? "warn" : "ok"}>{loading ? "正在恢复场次" : retryMode ? "恢复失败" : paused ? "场次已暂停" : "场次进行中"}</StatusPill>
          <span>{loading ? "正在核对服务器中的位置与已保存记录。" : retryMode ? "恢复完成前，推进、线索、录音和收尾入口保持关闭。" : paused && resumeBlocked ? (resumeBlockedHint ?? "已暂停；当前不能在这里继续，请按页面上的提示处理。") : paused ? "患者端已停止推进，麦克风保持关闭。" : "当前位置会自动保存，可在刷新或换设备后继续。"}</span>
        </div>
        <Button variant={retryMode || paused ? "primary" : "secondary"} disabled={loading || busy || (retryMode && !onRetry) || (paused && resumeBlocked)} onClick={retryMode ? onRetry : paused ? onResume : onPause}>
          {loading || busy ? "正在处理…" : retryMode ? "重新同步场次" : paused && resumeBlocked ? "暂不能在此继续" : paused ? "继续本场训练" : "暂停本场训练"}
        </Button>
      </section>
      {recoveredLabel && !paused && !loading && !error && (
        <Alert className="recovery-note" tone="info" title="已恢复上次进度">
          已回到{recoveredLabel}，之前保存的记录都在。
        </Alert>
      )}
      {paused && !loading && !error && (
        <Alert className="recovery-note" tone="warn" title="当前保持安全暂停">
          研究者端仍可查看已保存记录，但题目切换、线索发送和录音入口已暂时关闭。
        </Alert>
      )}
      {error && <Alert className="recovery-note" tone="danger" title="无法同步场次状态"
        actions={onRetry ? <Button onClick={onRetry}>重新同步</Button> : undefined}>{error}</Alert>}
    </>
  );
}
