import assert from "node:assert/strict";
import test from "node:test";
import {
  acquireAudioDeviceLease,
  AudioDeviceLeaseUnavailableError,
  type AudioDeviceLockCallback,
  type AudioDeviceLockManager,
} from "./audioDeviceLease.ts";

class ImmediateLockManager implements AudioDeviceLockManager {
  callbackFinished = false;

  async request(
    _name: string,
    _options: { mode: "exclusive"; signal?: AbortSignal },
    callback: AudioDeviceLockCallback,
  ): Promise<void> {
    await callback({});
    this.callbackFinished = true;
  }
}

class SerialLockManager implements AudioDeviceLockManager {
  #tail: Promise<unknown> = Promise.resolve();

  request(
    _name: string,
    _options: { mode: "exclusive"; signal?: AbortSignal },
    callback: AudioDeviceLockCallback,
  ): Promise<unknown> {
    const next = this.#tail.then(() => callback({}));
    this.#tail = next.catch(() => undefined);
    return next;
  }
}

test("audio device lease is held until its owner explicitly releases it", async () => {
  const manager = new ImmediateLockManager();
  const lease = await acquireAudioDeviceLease(manager);
  assert.equal(manager.callbackFinished, false);
  lease.release();
  await lease.released;
  assert.equal(manager.callbackFinished, true);
  lease.release(); // idempotent
});

test("missing Web Locks support fails closed", async () => {
  await assert.rejects(
    acquireAudioDeviceLease(null),
    (error: unknown) => error instanceof AudioDeviceLeaseUnavailableError,
  );
});

test("a second page cannot acquire the device lease before the first releases it", async () => {
  const manager = new SerialLockManager();
  const first = await acquireAudioDeviceLease(manager);
  let secondAcquired = false;
  const secondPromise = acquireAudioDeviceLease(manager).then((lease) => {
    secondAcquired = true;
    return lease;
  });

  await Promise.resolve();
  assert.equal(secondAcquired, false);
  first.release();
  await first.released;
  const second = await secondPromise;
  assert.equal(secondAcquired, true);
  second.release();
  await second.released;
});
