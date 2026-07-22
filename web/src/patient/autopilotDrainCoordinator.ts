import type { AutopilotExecutionFence } from "./autopilotExecutionFence.ts";

/**
 * The device drain fact is meaningful only after every local media continuation
 * has settled. Register the exact teardown with the same execution fence that
 * protects controller replacement, then append the proof.
 */
export async function acknowledgeDrainAfterShutdown(input: {
  fence: Pick<
    AutopilotExecutionFence,
    "registerControllerShutdown" | "waitForActiveStart"
  >;
  shutdown: Promise<void> | null;
  acknowledge(): Promise<void>;
}): Promise<void> {
  if (input.shutdown) input.fence.registerControllerShutdown(input.shutdown);
  await input.fence.waitForActiveStart();
  await input.acknowledge();
}

/** Bind the drain mutation to the exact state revision recovered just before it. */
export function assertExactDrainTransition(
  target: { state_revision: number },
  receipt: { replayed: boolean; state_revision: number },
): void {
  const expectedRevision = receipt.replayed
    ? target.state_revision : target.state_revision + 1;
  if (receipt.state_revision !== expectedRevision) {
    throw new Error("收麦回执没有精确证明目标状态转移");
  }
}
