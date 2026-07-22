export interface ExportIntent {
  sessionId: string;
  idempotencyKey: string;
}

/** Keep one export intent stable across network ambiguity and UI retries. */
export function ensureExportIntent(
  current: ExportIntent | null,
  sessionId: string,
  randomUuid: () => string = () => crypto.randomUUID(),
): ExportIntent {
  if (current?.sessionId === sessionId) return current;
  return { sessionId, idempotencyKey: `export-${randomUuid()}` };
}
