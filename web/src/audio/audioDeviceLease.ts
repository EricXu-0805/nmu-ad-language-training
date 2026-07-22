const AUDIO_DEVICE_LOCK_NAME = "nmu:audio-device-lease:v1";

export type AudioDeviceLockCallback = (lock: unknown) => Promise<void>;

/** Small structural surface so the lease can be exercised without a browser. */
export interface AudioDeviceLockManager {
  request(
    name: string,
    options: { mode: "exclusive"; signal?: AbortSignal },
    callback: AudioDeviceLockCallback,
  ): Promise<unknown>;
}

export interface AudioDeviceLease {
  release(): void;
  /** Resolves only after the browser has actually released the Web Lock. */
  released: Promise<void>;
}

export class AudioDeviceLeaseUnavailableError extends Error {
  constructor(message = "当前浏览器不支持安全的跨页面录音锁") {
    super(message);
    this.name = "AudioDeviceLeaseUnavailableError";
  }
}

export function browserAudioDeviceLockManager(): AudioDeviceLockManager | null {
  if (typeof navigator === "undefined") return null;
  const locks = navigator.locks;
  if (!locks || typeof locks.request !== "function") return null;
  return locks as unknown as AudioDeviceLockManager;
}

/**
 * Hold one origin-wide exclusive lease until release() is called. A queued tab
 * remains blocked inside Web Locks instead of falling back to a racy JS flag.
 */
export function acquireAudioDeviceLease(
  manager: AudioDeviceLockManager | null = browserAudioDeviceLockManager(),
  signal?: AbortSignal,
): Promise<AudioDeviceLease> {
  if (!manager) return Promise.reject(new AudioDeviceLeaseUnavailableError());
  if (signal?.aborted) {
    return Promise.reject(signal.reason ?? new DOMException("录音设备锁请求已取消", "AbortError"));
  }

  return new Promise<AudioDeviceLease>((resolve, reject) => {
    let granted = false;
    let releaseHold: (() => void) | null = null;
    let acknowledgeRelease: (() => void) | null = null;
    const released = new Promise<void>((done) => { acknowledgeRelease = done; });
    const options: { mode: "exclusive"; signal?: AbortSignal } = { mode: "exclusive" };
    if (signal) options.signal = signal;

    void manager.request(AUDIO_DEVICE_LOCK_NAME, options, async () => {
      granted = true;
      let releasedByOwner = false;
      const hold = new Promise<void>((done) => { releaseHold = done; });
      resolve({
        release() {
          if (releasedByOwner) return;
          releasedByOwner = true;
          releaseHold?.();
        },
        released,
      });
      await hold;
    }).catch((error: unknown) => {
      if (!granted) reject(error);
    }).then(
      () => acknowledgeRelease?.(),
      () => { if (granted) acknowledgeRelease?.(); },
    );
  });
}
