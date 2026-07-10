// 本地无依赖 ID 生成(crypto.randomUUID,全离线可用)。raw_audio_id 由前端生成、贯穿 blobStore↔后端登记。
export const newAudioId = (): string => `aud-${crypto.randomUUID()}`;
export const newSessionId = (): string => `S-${crypto.randomUUID().slice(0, 8)}`;

// 环节唯一键:item_id#turn_seq —— 作业日志与音频回指共用。
export const turnKey = (itemId: string, turnSeq: number): string => `${itemId}#${turnSeq}`;
