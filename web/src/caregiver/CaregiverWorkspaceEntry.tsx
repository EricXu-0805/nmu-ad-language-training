import type { AuthIdentity } from "../types";
import { caregiverApi } from "../caregiverApi";
import { CaregiverWorkspace } from "./CaregiverWorkspace";

export function CaregiverWorkspaceEntry({
  identity,
  onLogout,
}: {
  identity: AuthIdentity;
  onLogout: () => Promise<void>;
}) {
  return (
    <CaregiverWorkspace
      api={caregiverApi}
      identity={{
        displayId: identity.display_id,
        username: identity.username,
      }}
      onLogout={onLogout}
    />
  );
}
