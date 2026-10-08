import { parseSyncPayload, type RapportMsg } from "./messages.ts";
import type { WarmupRequest } from "../week1Warmup.ts";
export interface WarmupReceipt { seq: number; wseq: number; runtimeRevision: number; rapportStep: RapportMsg }
export function parseWarmupReceipt(value: unknown, sessionId: string, request: WarmupRequest): WarmupReceipt {
  const row = value as Record<string, unknown> | null;
  const step = row && parseSyncPayload("rapportStep", row.rapportStep);
  if (!row || typeof row !== "object" || Array.isArray(row)
    || Object.keys(row).length !== 4 || !["seq", "wseq", "runtimeRevision", "rapportStep"].every((key) => Object.hasOwn(row, key))
    || !Number.isSafeInteger(row.seq) || Number(row.seq) < 1
    || !Number.isSafeInteger(row.wseq) || Number(row.wseq) <= request.expectedWseq
    || !Number.isSafeInteger(row.runtimeRevision) || Number(row.runtimeRevision) <= request.expectedRevision
    || !step || step.sessionId !== sessionId || step.wseq !== row.wseq || step.recording !== "idle"
    || (request.action === "continue" ? Boolean(step.warmup)
      : !step.warmup || step.warmup.seq !== row.wseq
        || (request.action === "start" && (step.warmup.topic !== request.topic || step.warmup.beat !== "ask"))
        || (request.action === "reply" && step.warmup.beat !== "reply")
        || (request.action === "end" && step.warmup.beat !== "done")
        || (request.action === "replay" && step.warmup.beat === "done"))) {
    throw new Error("训练前互动尚未获得有效服务器确认，请重新核对状态");
  }
  return { seq: Number(row.seq), wseq: Number(row.wseq), runtimeRevision: Number(row.runtimeRevision), rapportStep: step };
}
