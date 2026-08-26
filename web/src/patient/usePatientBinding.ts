// 自动跟场循环:设备存有受试者绑定、且当前没有场次能力时,按 ATTACH_POLL_MS 静默尝试
// /device/attach。接上后交回既有 live 轮询;绑定死亡即停并清除。
// 老人端契约不变:这里永不抛错、永不改画面,只回一个"已绑定"布尔给问候页。
import { useEffect, useState } from "react";
import {
  api,
  DEVICE_CAPABILITY_UPDATED_EVENT,
  getDeviceCapability,
  getPatientBinding,
  PATIENT_BINDING_UPDATED_EVENT,
} from "../api";
import { ATTACH_POLL_MS, shouldAttemptAttach } from "./bindingAttachPolicy";

export function usePatientBinding(): { bound: boolean } {
  const [bound, setBound] = useState(() => getPatientBinding() !== null);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;

    const refreshBound = () => {
      if (!cancelled) setBound(getPatientBinding() !== null);
    };

    const tick = () => {
      if (cancelled || inFlight) return;
      if (!shouldAttemptAttach(
        getPatientBinding() !== null,
        getDeviceCapability() !== null,
      )) return;
      inFlight = true;
      // attachPatientDevice 自己负责保存能力/清绑定并广播事件;
      // 这里只保证同一时刻至多一个在途请求。
      void api.attachPatientDevice().finally(() => {
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
      window.removeEventListener(PATIENT_BINDING_UPDATED_EVENT, refreshBound);
      window.removeEventListener(DEVICE_CAPABILITY_UPDATED_EVENT, tick);
    };
  }, []);

  return { bound };
}
