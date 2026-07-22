const MAX_PATIENT_ASSET_BYTES = 16 * 1024 * 1024;
const REQUIRED_CONTENT_TYPE = "image/webp";
const DEFAULT_PATIENT_ASSET_TIMEOUT_MS = 12_000;

export type PatientAssetReadiness = "loading" | "ready" | "failed";

export interface PatientAssetReadinessEvent {
  requestKey: string;
  readiness: PatientAssetReadiness;
}

export interface LoadedPatientAsset {
  objectUrl: string;
  naturalWidth: number;
  naturalHeight: number;
  dispose(): void;
}

export interface PatientAssetCredentialSelection {
  headers: Record<string, string>;
  source: "active" | "recovery" | null;
  record: {
    capability: string;
    sessionId: string;
    expiresAt: string;
  } | null;
}

export interface CurrentPatientAssetDependencies {
  fetchImpl(input: string, init: RequestInit): Promise<Response>;
  selectCredential(sessionId: string): PatientAssetCredentialSelection;
  handleAuthorizationFailure(
    status: number,
    body: string,
    credential: PatientAssetCredentialSelection,
  ): boolean;
  createObjectUrl(blob: Blob): string;
  revokeObjectUrl(url: string): void;
  decodeImage(url: string, signal: AbortSignal): Promise<{
    naturalWidth: number;
    naturalHeight: number;
  }>;
  /** Test-only override; production has one 12-second fetch+decode deadline. */
  requestTimeoutMs?: number;
}

export class PatientAssetLoadError extends Error {
  readonly code:
    | "authorization_required"
    | "not_available"
    | "invalid_response"
    | "decode_failed";

  constructor(
    code: PatientAssetLoadError["code"],
    message = "题目图片暂时无法安全显示",
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = "PatientAssetLoadError";
    this.code = code;
  }
}

function abortError(signal: AbortSignal): Error {
  return signal.reason instanceof Error
    ? signal.reason
    : new DOMException("题目图片加载已取消", "AbortError");
}

function throwIfAborted(signal: AbortSignal): void {
  if (signal.aborted) throw abortError(signal);
}

function awaitWithAbort<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(abortError(signal));
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(abortError(signal));
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(error);
      },
    );
  });
}

function contentType(response: Response): string {
  return (response.headers.get("Content-Type") ?? "")
    .split(";", 1)[0]?.trim().toLowerCase() ?? "";
}

function cacheIsPrivateNoStore(response: Response): boolean {
  const directives = (response.headers.get("Cache-Control") ?? "")
    .toLowerCase()
    .split(",")
    .map((part) => part.trim());
  return directives.includes("private") && directives.includes("no-store");
}

async function responseBodyForError(response: Response): Promise<string> {
  try { return await response.text(); }
  catch { return ""; }
}

export async function decodeBrowserPatientAsset(
  url: string,
  signal: AbortSignal,
): Promise<{ naturalWidth: number; naturalHeight: number }> {
  if (typeof Image === "undefined") {
    throw new PatientAssetLoadError("decode_failed");
  }
  const image = new Image();
  image.decoding = "async";
  const decoded = new Promise<{ naturalWidth: number; naturalHeight: number }>((resolve, reject) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      image.onload = null;
      image.onerror = null;
      if (image.naturalWidth < 1 || image.naturalHeight < 1) {
        reject(new PatientAssetLoadError("decode_failed"));
        return;
      }
      resolve({ naturalWidth: image.naturalWidth, naturalHeight: image.naturalHeight });
    };
    const fail = (error?: unknown) => {
      if (settled) return;
      settled = true;
      image.onload = null;
      image.onerror = null;
      reject(new PatientAssetLoadError(
        "decode_failed",
        undefined,
        error === undefined ? undefined : { cause: error },
      ));
    };
    image.onload = finish;
    image.onerror = () => fail();
    image.src = url;
    if (typeof image.decode === "function") {
      void image.decode().then(finish, fail);
    }
  });
  try {
    return await awaitWithAbort(decoded, signal);
  } finally {
    if (signal.aborted) image.src = "";
  }
}

/**
 * Fetches only the server-authorized current stimulus. There is deliberately no
 * image id/item id parameter: a patient tab cannot enumerate or prefetch future
 * answer-bearing assets.
 */
async function loadCurrentPatientAssetWithinDeadline(
  sessionId: string,
  signal: AbortSignal,
  dependencies: CurrentPatientAssetDependencies,
): Promise<LoadedPatientAsset> {
  throwIfAborted(signal);
  const credential = dependencies.selectCredential(sessionId);
  if (credential.source !== "active" || credential.record?.sessionId !== sessionId
      || typeof credential.headers["X-Device-Capability"] !== "string") {
    throw new PatientAssetLoadError("authorization_required");
  }
  const response = await awaitWithAbort(
    dependencies.fetchImpl(
      `/sessions/${encodeURIComponent(sessionId)}/patient-asset/current`,
      {
        method: "GET",
        signal,
        credentials: "omit",
        cache: "no-store",
        headers: credential.headers,
      },
    ),
    signal,
  );
  throwIfAborted(signal);
  if (response.status === 204) {
    throw new PatientAssetLoadError("not_available");
  }
  if (!response.ok) {
    const body = await responseBodyForError(response);
    dependencies.handleAuthorizationFailure(response.status, body, credential);
    throw new PatientAssetLoadError(
      response.status === 401 ? "authorization_required" : "not_available",
    );
  }
  if (response.status !== 200
      || contentType(response) !== REQUIRED_CONTENT_TYPE
      || !cacheIsPrivateNoStore(response)
      || response.headers.get("X-Content-Type-Options")?.toLowerCase() !== "nosniff") {
    throw new PatientAssetLoadError("invalid_response");
  }
  const declaredLengthHeader = response.headers.get("Content-Length");
  const declaredLength = declaredLengthHeader === null ? null : Number(declaredLengthHeader);
  if (declaredLength !== null && Number.isFinite(declaredLength)
      && (declaredLength <= 0 || declaredLength > MAX_PATIENT_ASSET_BYTES)) {
    throw new PatientAssetLoadError("invalid_response");
  }
  const blob = await awaitWithAbort(response.blob(), signal);
  throwIfAborted(signal);
  if (blob.size < 1 || blob.size > MAX_PATIENT_ASSET_BYTES
      || (blob.type && blob.type.toLowerCase() !== REQUIRED_CONTENT_TYPE)) {
    throw new PatientAssetLoadError("invalid_response");
  }

  const objectUrl = dependencies.createObjectUrl(blob);
  let disposed = false;
  const dispose = () => {
    if (disposed) return;
    disposed = true;
    dependencies.revokeObjectUrl(objectUrl);
  };
  try {
    const decoded = await awaitWithAbort(
      dependencies.decodeImage(objectUrl, signal),
      signal,
    );
    throwIfAborted(signal);
    if (!Number.isFinite(decoded.naturalWidth) || decoded.naturalWidth < 1
        || !Number.isFinite(decoded.naturalHeight) || decoded.naturalHeight < 1) {
      throw new PatientAssetLoadError("decode_failed");
    }
    return { objectUrl, ...decoded, dispose };
  } catch (error) {
    dispose();
    if (signal.aborted) throw abortError(signal);
    if (error instanceof PatientAssetLoadError) throw error;
    throw new PatientAssetLoadError("decode_failed", undefined, { cause: error });
  }
}

export async function loadCurrentPatientAsset(
  sessionId: string,
  parentSignal: AbortSignal,
  dependencies: CurrentPatientAssetDependencies,
): Promise<LoadedPatientAsset> {
  throwIfAborted(parentSignal);
  const controller = new AbortController();
  const onParentAbort = () => controller.abort(abortError(parentSignal));
  parentSignal.addEventListener("abort", onParentAbort, { once: true });
  const configuredTimeout = dependencies.requestTimeoutMs;
  const timeoutMs = typeof configuredTimeout === "number"
      && Number.isFinite(configuredTimeout) && configuredTimeout > 0
    ? configuredTimeout
    : DEFAULT_PATIENT_ASSET_TIMEOUT_MS;
  const timer = globalThis.setTimeout(() => {
    controller.abort(new PatientAssetLoadError(
      "not_available",
      "题目图片加载超时，已停止语音和录音",
    ));
  }, timeoutMs);
  try {
    return await loadCurrentPatientAssetWithinDeadline(
      sessionId,
      controller.signal,
      dependencies,
    );
  } finally {
    globalThis.clearTimeout(timer);
    parentSignal.removeEventListener("abort", onParentAbort);
  }
}

/** Explicit no-image contracts are ready without any network request. */
export function patientAssetRequirementReady(
  required: boolean,
  state: PatientAssetReadinessEvent | null,
  requestKey: string,
): boolean {
  return !required || (state?.requestKey === requestKey && state.readiness === "ready");
}
