// 合成交接走查页（2026-10-23 审核报告里那份临时 harness 收进仓库）：手动准备页卸载时
// 旧录音恢复还卡在上传上，自动带练执行器接手开麦。上传、登记、audioSaved 与 ACK 都是桩，
// 麦克风换成振荡器流；跑的是真 React 卸载、Web Locks、IndexedDB、MediaRecorder。
// 只在 harness/webkit_handoff.py 起的 127.0.0.1:4179 上跑，不进 dist。
import { createRoot } from "react-dom/client";
import { useVoxRecorder } from "../src/patient/useVoxRecorder.ts";
import { api } from "../src/api.ts";
import { blobStore } from "../src/audio/blobStore.ts";
import { advanceAudioOutbox, createAudioOutboxEntry } from "../src/audio/audioOutbox.ts";
import { sha256Blob } from "../src/audio/audioUploadReceipt.ts";
import { ApiError } from "../src/apiResponse.ts";
import {
  BrowserAutopilotRecordingExecutor, browserAutopilotCaptureBootstrapPorts,
} from "../src/patient/autopilotRecordingExecutor.ts";
import { DurableAutopilotAckDelivery } from "../src/patient/autopilotAckDelivery.ts";
import type { NextCommandProjection } from "../src/patient/autopilotProtocol.ts";

const ORIGIN = "http://127.0.0.1:4179";

type HarnessWindow = Window & {
  runHandoff?: () => Promise<unknown>;
  storageProbe?: () => Promise<unknown>;
};

// 10-23 首个非持久会话里 Blob 写 IndexedDB 报 UnknownError、换持久 profile 后正常：
// 每种值各开一个新库写入再读回，只作观察记录，删库收尾。
(window as HarnessWindow).storageProbe = async () => {
  if (location.origin !== ORIGIN) throw Error("isolated origin only");
  const digest = async (value: Blob | ArrayBuffer) => Array.from(new Uint8Array(
    await crypto.subtle.digest("SHA-256", value instanceof Blob ? await value.arrayBuffer() : value),
  )).map((x) => x.toString(16).padStart(2, "0")).join("");
  const samples: [string, string | ArrayBuffer | Blob][] = [
    ["plain-string", "synthetic-only"],
    ["array-buffer", new Uint8Array([1, 2, 3, 4]).buffer],
    ["blob-webm", new Blob(["synthetic-only"], { type: "audio/webm" })],
    ["blob-no-mime", new Blob(["synthetic-only"])],
  ];
  const rows = [];
  for (const [kind, value] of samples) {
    const name = `nmu-probe-${kind}-${crypto.randomUUID()}`;
    const row: Record<string, unknown> = { kind, put: false, roundtrip: false };
    let db: IDBDatabase | undefined;
    try {
      db = await new Promise<IDBDatabase>((resolve, reject) => {
        const r = indexedDB.open(name, 1);
        r.onupgradeneeded = () => r.result.createObjectStore("p");
        r.onsuccess = () => resolve(r.result);
        r.onerror = () => reject(r.error);
      });
      const opened = db;
      await new Promise((resolve, reject) => {
        const tx = opened.transaction("p", "readwrite");
        tx.objectStore("p").put(value, "key");
        tx.oncomplete = resolve;
        tx.onabort = () => reject(tx.error);
      });
      row.put = true;
      const read = await new Promise<unknown>((resolve, reject) => {
        const r = opened.transaction("p", "readonly").objectStore("p").get("key");
        r.onsuccess = () => resolve(r.result);
        r.onerror = () => reject(r.error);
      });
      row.roundtrip = typeof value === "string"
        ? read === value
        : await digest(read as Blob | ArrayBuffer) === await digest(value);
    } catch (e) {
      row.error = (e as Error).name;
      row.message = (e as Error).message;
    } finally {
      db?.close();
      indexedDB.deleteDatabase(name);
      rows.push(row);
    }
  }
  return { origin: location.origin, secure: isSecureContext, userAgent: navigator.userAgent, indexedDB: rows };
};

(window as HarnessWindow).runHandoff = async () => {
  if (location.origin !== ORIGIN) throw Error("isolated origin only");
  await new Promise<void>((resolve, reject) => {
    const r = indexedDB.deleteDatabase("nmu-audio");
    r.onsuccess = () => resolve();
    r.onerror = () => reject(r.error);
    r.onblocked = () => reject(Error("blocked"));
  });
  const events: string[] = [];
  const streams: MediaStream[] = [];
  const oldBlob = new Blob(["synthetic-old-answer"], { type: "audio/webm" });
  const captured = createAudioOutboxEntry({
    rawAudioId: "raw-old-handoff", sessionId: "S-OLD", turnKey: "itm-0001#1",
    containsDirectIdentifier: false, durationSeconds: 1, blob: oldBlob,
  });
  const old = advanceAudioOutbox(captured, "registered");
  await blobStore.stageOutbox(captured, oldBlob);
  await blobStore.putOutbox(old);
  const oldMeta = JSON.stringify(await blobStore.getOutbox(old.rawAudioId));
  const oldHash = await sha256Blob(oldBlob);

  let entered!: () => void;
  const uploadEntered = new Promise<void>((r) => { entered = r; });
  let uploadCalls = 0;
  api.createAudio = async (p) => ({ raw_audio_id: p.raw_audio_id, registered: true });
  // 旧场次那条：第一次（手动准备页的恢复）一直挂到被取消；之后按服务端对旧场次的隐藏拒绝回 404。
  api.uploadAudioBlob = async (id, b, _sid, signal) => {
    if (id === old.rawAudioId) {
      uploadCalls++;
      if (uploadCalls === 1) {
        events.push("manual-recovery-waiting");
        entered();
        await new Promise((_, reject) => {
          signal?.addEventListener("abort", () => {
            events.push("manual-recovery-aborted");
            reject(new DOMException("cancel", "AbortError"));
          }, { once: true });
        });
      }
      throw new ApiError(404, "old session");
    }
    return { raw_audio_id: id, bytes: b.size, checksum: await sha256Blob(b), format: "webm" };
  };
  // 外场清理在 404 之后必须去问 audioSaved 拿确切结论；这里回 409，旧副本必须原样留下。
  api.putAudioSaved = (async (p: { rawAudioId: string }) => {
    if (p.rawAudioId === old.rawAudioId) {
      events.push("foreign-refused-kept");
      throw new ApiError(409, "same historical refusal");
    }
    return { seq: 1, audioReceipt: { rawAudioId: p.rawAudioId, serverSeq: 77, idempotent: false } };
  }) as typeof api.putAudioSaved;

  const audio = new AudioContext();
  const osc = audio.createOscillator();
  const dest = audio.createMediaStreamDestination();
  osc.connect(dest);
  osc.start();
  await audio.resume();
  const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia = async () => {
    const s = dest.stream.clone();
    streams.push(s);
    events.push("native-microphone-prepared");
    return s;
  };
  const root = createRoot(document.getElementById("root")!);
  // 走查页构建一次就跑，用不到热更新。
  // eslint-disable-next-line react/only-export-components
  function Legacy() {
    useVoxRecorder({ sessionId: "S-NEW", turnKey: "itm-0001#1", recording: "idle", connectionReady: true });
    return <p>Legacy preparation</p>;
  }
  root.render(<Legacy />);
  await uploadEntered;
  const executor = new BrowserAutopilotRecordingExecutor("S-NEW", {
    ownerGeneration: 1,
    isForeground: () => true,
    bootstrap: {
      ...browserAutopilotCaptureBootstrapPorts,
      authorize: async () => {
        events.push("exact-authorized");
        return { allowed: true, runtime_status: "active", is_simulation: true };
      },
    },
  });
  const cmd: Extract<NextCommandProjection, { kind: "record" }> = {
    schema_version: 1, command_key: "cmd-handoff", command_seq: 2, kind: "record", state: "pending",
    command_revision: 0, control_generation: 1, runner_generation: 1, item_ref: "itm-0001",
    turn_seq: 1, attempt_seq: 1, prompt_level: 0,
    payload: {
      raw_audio_id: "raw-current-handoff", turn_ref: "itm-0001#1", max_duration_seconds: 15,
      contains_direct_identifier: false, presentation_speech_key: "wk2.01.question",
      presentation_speech_text: "合成", presentation_purpose: "question",
    },
  };
  try {
    const cap = executor.start(cmd);
    const stopped = cap.stopped.catch((e: unknown) => { throw e; });
    const began = performance.now();
    root.unmount();
    await cap.started;
    const handoffMs = performance.now() - began;
    events.push("native-recording-started");
    await new Promise((r) => setTimeout(r, 150));
    cap.requestStop!();
    const facts = await stopped;
    await cap.closed;
    const delivery = await DurableAutopilotAckDelivery.create({
      capability: { capability: "x".repeat(43), sessionId: "S-NEW", expiresAt: "2026-12-31T00:00:00Z" },
      transport: {
        next: async () => null,
        ack: async (_s, _k, a) => ({
          scope_key: "p0a_sim_first_single_v1", ack_idempotency_key: a.idempotency_key,
          ack_type: a.ack_type, replayed: false, command_state: "succeeded",
          command_revision: a.command_revision + 1, status: "running", state_revision: 2, command: null,
        }),
      },
    });
    await delivery.send(
      { ...cmd, state: "started", command_revision: 1 }, delivery.initialDeviceEventSeq,
      { ack_type: "record_stopped", ...facts },
    );
    const kept = await blobStore.get(old.rawAudioId);
    const snap = await blobStore.recoverySnapshot();
    const unchanged = !!kept && await sha256Blob(kept) === oldHash
      && JSON.stringify(await blobStore.getOutbox(old.rawAudioId)) === oldMeta;
    const tracksEnded = streams.every((s) => s.getTracks().every((t) => t.readyState === "ended"));
    if (!unchanged || !tracksEnded || facts.byte_count <= 0 || snap.entries.length !== 1 || handoffMs >= 20000) {
      throw Error("handoff invariant failed");
    }
    return {
      syntheticOnly: true, browser: navigator.userAgent, handoffMs, events,
      nativeBytes: facts.byte_count, receipt: facts.receipt_server_seq,
      foreignBytesAndMetadataUnchanged: unchanged, remainingEntries: snap.entries.length, tracksEnded,
    };
  } finally {
    navigator.mediaDevices.getUserMedia = original;
    osc.stop();
    dest.stream.getTracks().forEach((t) => t.stop());
    await audio.close();
  }
};
