import { parseSyncPayload, type PatientRecMsg } from "./messages.ts";

/** BroadcastChannel is only a wake hint; only this strict server projection is authoritative. */
export function readAuthoritativePatientRec(
  serverPayload: unknown,
  expectedSessionId: string,
): PatientRecMsg | null {
  const message = parseSyncPayload("patientRec", serverPayload);
  return message?.sessionId === expectedSessionId ? message : null;
}
