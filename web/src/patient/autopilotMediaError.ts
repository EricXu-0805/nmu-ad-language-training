import type { AutopilotErrorCode, AutopilotFailureStage } from "./autopilotProtocol.ts";

export class AutopilotMediaError extends Error {
  readonly errorCode: AutopilotErrorCode;
  // 可选的精确失败阶段：只在构造时定死一次，重试/恢复路径不得重算。
  readonly failureStage?: AutopilotFailureStage;

  constructor(
    errorCode: AutopilotErrorCode,
    message: string,
    options?: ErrorOptions & { failureStage?: AutopilotFailureStage },
  ) {
    super(message, options);
    this.name = "AutopilotMediaError";
    this.errorCode = errorCode;
    this.failureStage = options?.failureStage;
  }
}

export function autopilotMediaErrorCode(
  error: unknown,
  fallback: AutopilotErrorCode,
): AutopilotErrorCode {
  return error instanceof AutopilotMediaError ? error.errorCode : fallback;
}

export function autopilotMediaFailureStage(
  error: unknown,
): AutopilotFailureStage | undefined {
  return error instanceof AutopilotMediaError ? error.failureStage : undefined;
}
