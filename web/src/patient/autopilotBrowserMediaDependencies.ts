import {
  handleDeviceAuthorizationFailure,
  selectDeviceCredential,
} from "../api.ts";
import { csrfHeader } from "../security/csrf.ts";
import type { AutopilotMediaTransportDependencies } from "./autopilotMediaTransport.ts";
import { autopilotHttpTransport } from "./autopilotHttpTransport.ts";

export const browserAutopilotMediaDependencies: AutopilotMediaTransportDependencies = {
  fetchImpl: (input, init) => fetch(input, init),
  selectCredential: (sessionId) => selectDeviceCredential(sessionId),
  handleAuthorizationFailure: handleDeviceAuthorizationFailure,
  csrf: csrfHeader,
  nextCommand: (sessionId) => autopilotHttpTransport.next(sessionId),
};
