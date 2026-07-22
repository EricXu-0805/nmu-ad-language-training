import type { AutopilotExecutionFence } from "./autopilotExecutionFence.ts";
import type { AutopilotOwnerLease } from "./autopilotOwnerLease.ts";

/**
 * A server-owned patient runner may reveal the legacy/manual plane only after
 * its exact controller shutdown has settled and the origin-wide owner lock is
 * physically released. An exact inactive response is authority to begin this
 * sequence, never authority to skip it.
 */
export async function settleAutopilotServerExit(input: {
  fence: Pick<
    AutopilotExecutionFence,
    "registerControllerShutdown" | "waitForActiveStart"
  >;
  shutdown: Promise<void> | null;
  ownerLease: Pick<AutopilotOwnerLease, "release" | "released">;
}): Promise<void> {
  if (input.shutdown) input.fence.registerControllerShutdown(input.shutdown);
  await input.fence.waitForActiveStart();
  input.ownerLease.release();
  await input.ownerLease.released;
}
