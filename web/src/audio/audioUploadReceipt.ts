export interface AudioUploadReceipt {
  raw_audio_id: string;
  bytes: number;
  checksum: string;
  format?: string;
}

export interface AudioCaptureReceiptAck {
  serverSeq: number;
  rawAudioId: string;
  idempotent: boolean;
}

type UnknownRecord = Record<string, unknown>;

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as UnknownRecord
    : null;
}

/** Validate that the server receipt proves it stored this exact local blob. */
export function validateAudioUploadReceipt(receipt: unknown, expected: {
  rawAudioId: string;
  bytes: number;
  checksum: string;
}): AudioUploadReceipt {
  const value = record(receipt);
  if (!value || value.raw_audio_id !== expected.rawAudioId) {
    throw new Error("音频上传回执与本次录音编号不一致");
  }
  if (!Number.isInteger(value.bytes) || value.bytes !== expected.bytes || expected.bytes <= 0) {
    throw new Error("音频上传回执的字节数与本机录音不一致");
  }
  const checksum = typeof value.checksum === "string" ? value.checksum.toLowerCase() : "";
  if (!/^[a-f0-9]{64}$/.test(checksum) || checksum !== expected.checksum.toLowerCase()) {
    throw new Error("音频上传回执的校验值与本机录音不一致");
  }
  return {
    raw_audio_id: value.raw_audio_id,
    bytes: value.bytes,
    checksum,
    format: typeof value.format === "string" ? value.format : undefined,
  };
}

/** A 2xx live write is not enough: only this server-ledger ACK permits local deletion. */
export function validateAudioCaptureReceiptAck(
  response: unknown, expectedRawAudioId: string,
): AudioCaptureReceiptAck {
  const outer = record(response);
  const ack = record(outer?.audioReceipt);
  if (!ack || ack.rawAudioId !== expectedRawAudioId) {
    throw new Error("服务端采集收据与本次录音编号不一致");
  }
  if (!Number.isSafeInteger(ack.serverSeq) || (ack.serverSeq as number) <= 0) {
    throw new Error("服务端采集收据缺少有效单调序号");
  }
  if (typeof ack.idempotent !== "boolean") {
    throw new Error("服务端采集收据缺少幂等状态");
  }
  return {
    serverSeq: ack.serverSeq as number,
    rawAudioId: ack.rawAudioId as string,
    idempotent: ack.idempotent,
  };
}

export async function completeAudioOutboxAfterServerAck(
  response: unknown,
  expectedRawAudioId: string,
  complete: () => Promise<void>,
): Promise<AudioCaptureReceiptAck> {
  const ack = validateAudioCaptureReceiptAck(response, expectedRawAudioId);
  await complete();
  return ack;
}

export async function sha256Blob(blob: Blob): Promise<string> {
  if (!globalThis.crypto?.subtle) throw new Error("当前浏览器无法校验录音上传回执");
  const digest = await globalThis.crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
