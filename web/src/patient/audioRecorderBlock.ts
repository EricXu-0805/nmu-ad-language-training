export type AudioRecorderBlockReason =
  | "lease-waiting"
  | "lease-unavailable"
  | "storage-checking"
  | "legacy-audio"
  | "pending-other-session"
  | "terminal-discarded"
  | "storage-invalid"
  | "storage-error";

export interface AudioRecorderBlockCopy {
  patient: string;
  researcher: string;
}

const COPY: Record<AudioRecorderBlockReason, AudioRecorderBlockCopy> = {
  "lease-waiting": {
    patient: "正在等待本设备的录音整理完成，请稍候",
    researcher: "本设备另一个页面正持有录音锁。请等待其保存完成，或关闭多余的老人端页面。",
  },
  "lease-unavailable": {
    patient: "现在还不能录音，请找工作人员",
    researcher: "浏览器不支持 Web Locks，已按安全规则停麦。请使用支持 Web Locks 的最新版 Chrome、Edge 或 Safari。",
  },
  "storage-checking": {
    patient: "正在检查上一次的回答，请稍候",
    researcher: "正在锁内核对本机 outbox 与历史录音，完成前不会打开麦克风。",
  },
  "legacy-audio": {
    patient: "有一段旧录音需要处理，请找工作人员",
    researcher: "发现升级前留下、缺少场次/环节元数据的录音。系统已持久标记并停麦；请保留本机浏览器数据，由研究者核对服务器录音后再做受控处置，不要直接清除缓存。",
  },
  "pending-other-session": {
    patient: "上一次的回答还没有整理完，请找工作人员",
    researcher: "发现其他场次或多条未完成 outbox。若原场次恢复凭据仍在，请检查网络后刷新重试；若标签页已关闭或凭据过期，请切回原场次重新配对。未处置前本设备保持停麦。",
  },
  "terminal-discarded": {
    patient: "这段录音已经处理好了，请找工作人员",
    researcher: "服务器已确认该录音处于删除或撤回终态，本机 Blob 与 outbox 已原子清除。系统仍保持停麦；请核对场次终态与治理记录后再安排后续操作。",
  },
  "storage-invalid": {
    patient: "刚才的回答没保存好，请找工作人员",
    researcher: "本机录音字节、outbox 元数据或存储键校验失败。已停麦；请保留现场存储并进行证据核对。",
  },
  "storage-error": {
    patient: "暂时不能录音，请找工作人员",
    researcher: "IndexedDB 升级被旧页面阻塞或存储检查失败。请关闭多余页面后刷新；不要清除网站数据。",
  },
};

export function audioRecorderBlockCopy(reason: AudioRecorderBlockReason): AudioRecorderBlockCopy {
  return COPY[reason];
}
