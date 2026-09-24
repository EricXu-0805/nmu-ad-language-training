import {
  autopilotConflictCode,
  receiptAllowsAutopilotAdjudication,
  sameAutopilotStatusReceipt,
  type AutopilotStatusReceipt,
} from "../../autopilot/startControl.ts";

/** Bind the decision to the exact paused state the researcher reviewed. */
export async function submitReviewedAdjudication(options: {
  reviewed: AutopilotStatusReceipt;
  readStatus: () => Promise<AutopilotStatusReceipt>;
  write: (revision: number) => Promise<AutopilotStatusReceipt>;
  acceptReceipt: (receipt: AutopilotStatusReceipt) => void;
}): Promise<"accepted" | "changed"> {
  const latest = await options.readStatus();
  options.acceptReceipt(latest);
  if (!receiptAllowsAutopilotAdjudication(latest)
      || latest.positionItemId === null || latest.positionTurnSeq === null
      || !sameAutopilotStatusReceipt(options.reviewed, latest)) return "changed";
  try {
    options.acceptReceipt(await options.write(options.reviewed.stateRevision));
    return "accepted";
  } catch (error) {
    if (autopilotConflictCode(error) !== "autopilot_revision_conflict") throw error;
    // A new revision can represent another answer, even at the same item/turn.
    // Refresh for display, but never rebase an old human decision onto it.
    options.acceptReceipt(await options.readStatus());
    return "changed";
  }
}
