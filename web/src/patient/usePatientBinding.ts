// 自动跟场循环:设备存有受试者绑定、且当前没有场次能力时,按 ATTACH_POLL_MS 静默尝试
// /device/attach。接上后交回既有 live 轮询;绑定死亡即停并清除。
// 老人端契约不变:这里永不抛错、永不改画面,只回"已绑定"布尔和一句给工作人员
// 看的提示(别的设备连着 / 这位受试者没有场次 / 本机另一个页签连着)给问候页。
import { useEffect, useState } from "react";
import {
  api,
  DEVICE_CAPABILITY_UPDATED_EVENT,
  getDeviceCapability,
  getPatientBinding,
  PATIENT_BINDING_UPDATED_EVENT,
} from "../api";
import { bus } from "../sync/bus";
import {
  ATTACH_POLL_MS, OTHER_TAB_PROBE_MS, probeOtherTabs, shouldAttemptAttach,
  type AttachHint,
} from "./bindingAttachPolicy";

function probeNonce(): string {
  return crypto.randomUUID().replaceAll("-", "").toLowerCase();
}

export function usePatientBinding(): { bound: boolean; hint: AttachHint } {
  const [bound, setBound] = useState(() => getPatientBinding() !== null);
  const [hint, setHint] = useState<AttachHint>(null);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;

    const refreshBound = () => {
      if (!cancelled) setBound(getPatientBinding() !== null);
    };

    // 本页连着场次时替别的页签作答:它们据此不去抢。
    const unsubscribeResponder = bus.subscribe((msg) => {
      if (msg.type !== "capabilityProbe") return;
      const held = getDeviceCapability();
      if (held) bus.post({ type: "capabilityHeld", nonce: msg.nonce, sessionId: held.sessionId });
    });

    const tick = () => {
      if (cancelled || inFlight) return;
      if (!shouldAttemptAttach(
        getPatientBinding() !== null,
        getDeviceCapability() !== null,
      )) return;
      inFlight = true;
      void probeOtherTabs(
        (msg) => bus.post(msg), (handler) => bus.subscribe(handler),
        OTHER_TAB_PROBE_MS, probeNonce(),
      ).then((heldByOtherTab) => {
        if (cancelled) return null;
        if (heldByOtherTab) {
          setHint("other_tab");
          return null;
        }
        // attachPatientDevice 自己负责保存能力/清绑定并广播事件;
        // 这里只保证同一时刻至多一个在途请求。
        return api.attachPatientDevice();
      }).then((result) => {
        if (result && !cancelled) setHint(result.hint);
      }).finally(() => {
        inFlight = false;
        refreshBound();
      });
    };

    tick();
    const timer = window.setInterval(tick, ATTACH_POLL_MS);
    window.addEventListener(PATIENT_BINDING_UPDATED_EVENT, refreshBound);
    window.addEventListener(DEVICE_CAPABILITY_UPDATED_EVENT, tick);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      unsubscribeResponder();
      window.removeEventListener(PATIENT_BINDING_UPDATED_EVENT, refreshBound);
      window.removeEventListener(DEVICE_CAPABILITY_UPDATED_EVENT, tick);
    };
  }, []);

  return { bound, hint };
}
