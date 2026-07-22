const CSRF_COOKIE_NAME = "nmu_csrf";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export function readCookie(cookieHeader: string, name: string): string | null {
  for (const part of cookieHeader.split(";")) {
    const separator = part.indexOf("=");
    if (separator < 0 || part.slice(0, separator).trim() !== name) continue;
    const value = part.slice(separator + 1).trim();
    try { return decodeURIComponent(value); }
    catch { return null; }
  }
  return null;
}

/**
 * Account-cookie writes carry a session-derived proof. Device-PIN-only pages do
 * not have this cookie and remain authenticated by their non-simple custom
 * header; the server decides which principal is allowed for each route.
 */
export function csrfHeader(method: string, cookieHeader = document.cookie): Record<string, string> {
  if (SAFE_METHODS.has(method.toUpperCase())) return {};
  const token = readCookie(cookieHeader, CSRF_COOKIE_NAME);
  return token ? { "X-CSRF-Token": token } : {};
}
