// 本地音频字节存储(IndexedDB,key=raw_audio_id)。后端无二进制口,字节只在本机。
// 删除规则:仅当服务端已过闸门放行(status==='deleted')后,才镜像删除本地 blob。
const DB = "nmu-audio";
const STORE = "blobs";

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function tx<T>(mode: IDBTransactionMode, run: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return open().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const store = db.transaction(STORE, mode).objectStore(STORE);
        const req = run(store);
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      }),
  );
}

export const blobStore = {
  put: (rawAudioId: string, blob: Blob): Promise<void> =>
    tx("readwrite", (s) => s.put(blob, rawAudioId)).then(() => undefined),
  get: (rawAudioId: string): Promise<Blob | undefined> =>
    tx<Blob | undefined>("readonly", (s) => s.get(rawAudioId) as IDBRequest<Blob | undefined>),
  del: (rawAudioId: string): Promise<void> =>
    tx("readwrite", (s) => s.delete(rawAudioId)).then(() => undefined),
};
