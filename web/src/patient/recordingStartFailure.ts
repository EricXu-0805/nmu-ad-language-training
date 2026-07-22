import type { PatientRecFailureCode } from "../sync/messages";

export type RecordingStartFailureSource =
  | { kind: "timeout" }
  | { kind: "authorization" }
  | { kind: "microphone"; error: unknown };

interface FailureClaimTarget {
  failureId?: string;
}

function errorName(error: unknown): string | null {
  if (error instanceof DOMException) return error.name;
  if (error !== null && typeof error === "object" && "name" in error
      && typeof (error as { name?: unknown }).name === "string") {
    return (error as { name: string }).name;
  }
  return null;
}

/** Map every microphone start failure to the closed patientRec protocol vocabulary. */
export function classifyRecordingStartFailure(source: RecordingStartFailureSource): PatientRecFailureCode {
  if (source.kind === "timeout") return "microphone_start_timeout";
  if (source.kind === "authorization") return "recording_authorization_failed";
  const name = errorName(source.error);
  if (name === "NotAllowedError" || name === "PermissionDeniedError") {
    return "microphone_permission_denied";
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return "microphone_not_found";
  }
  return "microphone_start_failed";
}

export function newPatientRecFailureId(): string {
  return crypto.randomUUID();
}

/**
 * Atomically claims the one failure report allowed for a still-current start permit.
 * JavaScript executes this mutation synchronously, so timeout and rejection races cannot
 * allocate two IDs for the same permit.
 */
export function claimRecordingStartFailure(
  permit: FailureClaimTarget,
  isCurrent: boolean,
  failureCode: PatientRecFailureCode,
  idFactory: () => string = newPatientRecFailureId,
): { failureCode: PatientRecFailureCode; failureId: string } | null {
  if (!isCurrent || permit.failureId) return null;
  const failureId = idFactory();
  permit.failureId = failureId;
  return { failureCode, failureId };
}
