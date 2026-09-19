// 自动跟场循环:设备存有受试者绑定、且当前没有场次能力时,静默尝试 /device/attach。
// 间隔从 ATTACH_POLL_MS 起,连续「没有场次」翻倍退避到 ATTACH_POLL_MAX_MS;接上(200)、
// 回到前台、手动配对/能力更新即归零并立刻再试。接上后交回既有 live 轮询;绑定死亡即停并清除。
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
  ATTACH_POLL_MS, OTHER_TAB_PROBE_MS, attachPollDelayMs, nextAttachNoSessionStreak,
  probeOtherTabs, shouldAttemptAttach,
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

    let timer: number | null = null;
    let noSessionStreak = 0;

    // 单条 setTimeout 链而不是 setInterval:下一次间隔要看上一次结果。
    const schedule = (delayMs: number) => {
      if (cancelled) return;
      if (timer !== null) window.clearTimeout(timer);
      timer = window.setTimeout(tick, delayMs);
    };

    const tick = () => {
      if (cancelled || inFlight) return;
      if (!shouldAttemptAttach(
        getPatientBinding() !== null,
        getDeviceCapability() !== null,
      )) {
        schedule(ATTACH_POLL_MS);
        return;
      }
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
        if (result && !cancelled) {
          setHint(result.hint);
          noSessionStreak = nextAttachNoSessionStreak(noSessionStreak, result);
        }
      }).finally(() => {
        inFlight = false;
        refreshBound();
        schedule(attachPollDelayMs(noSessionStreak));
      });
    };

    // 手动配对/能力更新、回到前台:退避归零,立刻再试一次。
    const wake = () => {
      noSessionStreak = 0;
      tick();
    };
    const onBindingUpdated = () => {
      refreshBound();
      wake();
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") wake();
    };

    tick();
    window.addEventListener(PATIENT_BINDING_UPDATED_EVENT, onBindingUpdated);
    window.addEventListener(DEVICE_CAPABILITY_UPDATED_EVENT, wake);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
      unsubscribeResponder();
      window.removeEventListener(PATIENT_BINDING_UPDATED_EVENT, onBindingUpdated);
      window.removeEventListener(DEVICE_CAPABILITY_UPDATED_EVENT, wake);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, []);

  return { bound, hint };
}
