import type { DataClassification } from "./dataClassification";

export interface OperationalReadinessPolicy {
  operationalAutopilotAllowed: boolean;
  blocksSessionCreation: boolean;
  mode: "ready" | "research_blocked" | "supervised_simulation" | "not_applicable";
}

export function operationalReadinessPolicy(
  classification: DataClassification | null,
  declaredReady: boolean,
): OperationalReadinessPolicy {
  if (declaredReady) {
    return { operationalAutopilotAllowed: true, blocksSessionCreation: false, mode: "ready" };
  }
  if (classification === "research") {
    return { operationalAutopilotAllowed: false, blocksSessionCreation: true, mode: "research_blocked" };
  }
  if (classification === "simulation") {
    return { operationalAutopilotAllowed: false, blocksSessionCreation: false, mode: "supervised_simulation" };
  }
  return { operationalAutopilotAllowed: false, blocksSessionCreation: false, mode: "not_applicable" };
}
