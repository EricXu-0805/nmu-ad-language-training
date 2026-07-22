import type { Plugin } from "vite";

export interface BrowserModuleGraphOptions {
  webRoot?: string;
}

export interface SensitiveDistOptions {
  distRoot?: string;
  contentRoot?: string;
  contentFiles?: string[];
}

export interface BrowserBuildEvidenceOptions {
  distRoot?: string;
  webRoot?: string;
  buildFingerprint: string;
  buildId: string;
}

export const DIST_MANIFEST_NAME: string;
export const BUILD_PROVENANCE_NAME: string;
export const DECLARED_RELEASE_IMAGES: Readonly<Record<string, string>>;
export function protectedBrowserModuleGraph(options?: BrowserModuleGraphOptions): Plugin;
export function assertNoSensitiveContentInDist(options?: SensitiveDistOptions): void;
export function assertToolchainMatchesLock(
  declared: Record<string, string>, observed: Record<string, string>,
): void;
export function writeBrowserBuildEvidence(options: BrowserBuildEvidenceOptions): {
  provenance: Record<string, unknown>;
  manifest: Record<string, unknown>;
};
