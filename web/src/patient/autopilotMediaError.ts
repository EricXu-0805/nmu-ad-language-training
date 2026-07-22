import type { AutopilotErrorCode } from "./autopilotProtocol.ts";

export class AutopilotMediaError extends Error {
  readonly errorCode: AutopilotErrorCode;

  constructor(errorCode: AutopilotErrorCode, message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "AutopilotMediaError";
    this.errorCode = errorCode;
  }
}

export function autopilotMediaErrorCode(
  error: unknown,
  fallback: AutopilotErrorCode,
): AutopilotErrorCode {
  return error instanceof AutopilotMediaError ? error.errorCode : fallback;
}
