import type { DeviceCredentialSelection } from "../api.ts";

/** Protected patient reads never make a headerless authority probe. */
export function livePollingHasActiveCapability(
  credential: Pick<DeviceCredentialSelection, "source" | "record">,
): boolean {
  return credential.source === "active" && credential.record !== null;
}
