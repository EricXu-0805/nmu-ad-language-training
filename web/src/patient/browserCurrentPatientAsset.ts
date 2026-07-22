import {
  handleDeviceAuthorizationFailure,
  selectDeviceCredential,
} from "../api.ts";
import {
  decodeBrowserPatientAsset,
  type CurrentPatientAssetDependencies,
} from "./currentPatientAsset.ts";

export const browserCurrentPatientAssetDependencies: CurrentPatientAssetDependencies = {
  fetchImpl: (input, init) => fetch(input, init),
  selectCredential: (sessionId) => selectDeviceCredential(sessionId),
  handleAuthorizationFailure: handleDeviceAuthorizationFailure,
  createObjectUrl: (blob) => URL.createObjectURL(blob),
  revokeObjectUrl: (url) => URL.revokeObjectURL(url),
  decodeImage: decodeBrowserPatientAsset,
};
