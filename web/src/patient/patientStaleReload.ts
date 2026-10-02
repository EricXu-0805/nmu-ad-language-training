// 老人端「页面有更新」:只在问候页与结束页给工作人员一个安静的手动刷新入口。
// 老人端从不自己刷新——训练中(含暂停)刷新会让开着的录音记成失败,老人还得重新点一次开始。
// 点下之后也要先过安全检查,任何一项不过都不刷,只告诉工作人员稍后再点。
import { useEffect, useState } from "react";
import type { LiveConnectionState } from "../sync/useLiveCursor";

const BUILD_ID_PATTERN = /^[0-9]{10,80}$/;
const BUILD_ID_POLL_MS = 30_000;
const SHELL_PROBE_TIMEOUT_MS = 8_000;

/** 服务器 dist/build-id.txt 的内容;文件缺失时 SPA 兜底会回 200 + index.html,只认纯数字。 */
export function deployedBuildIdFrom(text: string | null | undefined): string | null {
  const value = text?.trim() ?? "";
  return BUILD_ID_PATTERN.test(value) ? value : null;
}

/** 训练中的一切画面(含暂停、关系建立、加载)都是 session。 */
export type StaleReloadScreen = "greeting" | "complete" | "session";

export interface StaleReloadEntryInput {
  /** 只有独立 /patient 页签的生产构建为 true;叠层与操作端同一页面,刷新会连操作端一起重载。 */
  enabled: boolean;
  screen: StaleReloadScreen;
  pageBuildId: string;
  deployedBuildId: string | null;
  connection: LiveConnectionState;
  hasActiveCapability: boolean;
}

export function shouldOfferStaleReload(input: StaleReloadEntryInput): boolean {
  if (!input.enabled || (input.screen !== "greeting" && input.screen !== "complete")) return false;
  if (!input.deployedBuildId || input.deployedBuildId === input.pageBuildId) return false;
  // 问候页不等于空闲:设备鉴权失败或能力轮换时,场次会在同一拍被清空并进入 reconnecting。
  // 手里还有场次能力却没连上,就当作场次可能还在进行。
  return input.connection === "connected" || !input.hasActiveCapability;
}

export interface StaleReloadPorts {
  probeShell(): Promise<{ status: number; contentType: string | null }>;
  /** 本源全部 Web Lock(持有中与排队中)的名字;查不了时返回 null。 */
  lockNames(): Promise<string[] | null>;
  /** 手动配对或自动跟场的请求是否在途。 */
  deviceTransportBusy(): boolean;
  /** 点下之后画面是否仍停在问候页或结束页。 */
  stillOffered(): boolean;
  reload(): void;
}

export type StaleReloadOutcome = "reloaded" | "busy" | "unreachable";

export async function reloadStalePatientPage(ports: StaleReloadPorts): Promise<StaleReloadOutcome> {
  // 刷新撞上服务重启会落在反代的错误页上,浏览器不会自己重试:先确认新外壳拿得到。
  try {
    const shell = await ports.probeShell();
    if (shell.status !== 200 || !/^text\/html\b/i.test(shell.contentType ?? "")) return "unreachable";
  } catch {
    return "unreachable";
  }
  // 录音租约、自动带练所有权、暂停存储都持有以 nmu 开头的 Web Lock。
  let names: string[] | null;
  try {
    names = await ports.lockNames();
  } catch {
    names = null;
  }
  if (names === null || names.some((name) => name.startsWith("nmu"))) return "busy";
  // 同步闸放在所有等待之后:服务端可能已签发能力而响应还没到,此时刷新会让下一次跟场
  // 走轮换围栏,把场次安全暂停。
  if (ports.deviceTransportBusy() || !ports.stillOffered()) return "busy";
  ports.reload();
  return "reloaded";
}

export function webLockNames(snapshot: { held?: { name?: string }[]; pending?: { name?: string }[] }): string[] {
  return [...(snapshot.held ?? []), ...(snapshot.pending ?? [])].map((lock) => lock.name ?? "");
}

export function browserStaleReloadPorts(
  checks: Pick<StaleReloadPorts, "deviceTransportBusy" | "stillOffered">,
): StaleReloadPorts {
  return {
    ...checks,
    probeShell: async () => {
      const controller = new AbortController();
      const timer = window.setTimeout(() => controller.abort(), SHELL_PROBE_TIMEOUT_MS);
      try {
        const response = await fetch("/patient", {
          cache: "no-store", credentials: "omit", signal: controller.signal,
        });
        return { status: response.status, contentType: response.headers.get("content-type") };
      } finally {
        window.clearTimeout(timer);
      }
    },
    lockNames: async () => {
      if (typeof navigator.locks?.query !== "function") return null;
      return webLockNames(await navigator.locks.query());
    },
    reload: () => window.location.reload(),
  };
}

/** 每 30 s 现读一次 /build-id.txt;关闭时不发任何请求。 */
export function useDeployedBuildId(enabled: boolean): string | null {
  const [deployed, setDeployed] = useState<string | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let stop = false;
    const check = () => {
      fetch("/build-id.txt", { cache: "no-store", credentials: "omit" })
        .then((response) => (response.ok ? response.text() : null))
        .then((text) => { if (!stop) setDeployed(deployedBuildIdFrom(text)); })
        .catch(() => { /* 网络抖动:保留上次结论,下一轮再读 */ });
    };
    check();
    const timer = window.setInterval(check, BUILD_ID_POLL_MS);
    return () => { stop = true; window.clearInterval(timer); };
  }, [enabled]);
  return enabled ? deployed : null;
}
