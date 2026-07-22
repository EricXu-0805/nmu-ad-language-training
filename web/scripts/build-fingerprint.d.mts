export interface BuildInput {
  label: string;
  path: string;
}

export interface BuildInputOptions {
  webRoot?: string;
  contentRoot?: string;
}

export interface BuildIdentity {
  fingerprint: string;
  buildId: string;
}

export function collectBuildInputs(options?: BuildInputOptions): BuildInput[];
export function hashBuildInputs(entries: BuildInput[]): string;
export function buildIdFromFingerprint(fingerprint: string): string;
export function computeBuildIdentity(options?: BuildInputOptions): BuildIdentity;
