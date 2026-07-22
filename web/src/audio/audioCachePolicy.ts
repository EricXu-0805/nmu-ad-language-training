export interface ServerAudioCacheProof {
  raw_audio_id: string;
  checksum?: string | null;
}

/** Old device cache is purgeable only when the server identifies the same asset and has its upload checksum. */
export function serverConfirmsUploadedAudio(rawAudioId: string, asset: ServerAudioCacheProof): boolean {
  return asset.raw_audio_id === rawAudioId
    && typeof asset.checksum === "string"
    && /^[a-f0-9]{64}$/i.test(asset.checksum);
}
