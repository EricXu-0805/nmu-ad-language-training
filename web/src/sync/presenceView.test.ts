import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { presenceViewFrom, relativeSeen } from "./presenceView.ts";

const NOW = Date.parse("2026-08-21T10:00:00Z");

test("P1-7 回归:设备断开后不再断言患者「正在看」任何画面", () => {
  const view = presenceViewFrom({
    sessionId: "S1", checking: false, unsupported: false, unavailable: false,
    presence: {
      session_id: "S1", screen: "thanks", online: false,
      last_seen_at: new Date(NOW - 45_000).toISOString(),
    },
    now: NOW,
  });
  assert.equal(view.state, "offline");
  // 「正在看结束提示」这类断言只属于在线态。
  assert.doesNotMatch(view.screenLabel, /正在看|正在回答|正在准备/);
  assert.equal(view.screenLabel, "已断开，画面未知");
  assert.equal(view.lastSeenLabel, "约 45 秒前有响应");
});

test("在线态仍按最近画面翻译;未知画面给中性说法", () => {
  const online = presenceViewFrom({
    sessionId: "S1", checking: false, unsupported: false, unavailable: false,
    presence: { session_id: "S1", screen: "record", online: true, last_seen_at: new Date(NOW - 2_000).toISOString() },
    now: NOW,
  });
  assert.equal(online.state, "online");
  assert.equal(online.screenLabel, "正在回答");
  assert.equal(online.lastSeenLabel, "刚刚有响应");
  const unknownScreen = presenceViewFrom({
    sessionId: "S1", checking: false, unsupported: false, unavailable: false,
    presence: { session_id: "S1", screen: "future_screen", online: true },
    now: NOW,
  });
  assert.equal(unknownScreen.screenLabel, "受试者端已打开");
});

test("无场次/核对中/不支持/暂不可用各自有明确态,从未连接是 unseen", () => {
  assert.equal(presenceViewFrom({
    sessionId: null, checking: false, unsupported: false, unavailable: false, presence: null,
  }).state, "unseen");
  assert.equal(presenceViewFrom({
    sessionId: "S1", checking: true, unsupported: false, unavailable: false, presence: null,
  }).state, "checking");
  assert.equal(presenceViewFrom({
    sessionId: "S1", checking: false, unsupported: true, unavailable: false, presence: null,
  }).state, "unsupported");
  assert.equal(presenceViewFrom({
    sessionId: "S1", checking: false, unsupported: false, unavailable: true, presence: null,
  }).state, "unavailable");
  const unseen = presenceViewFrom({
    sessionId: "S1", checking: false, unsupported: false, unavailable: false, presence: null,
  });
  assert.equal(unseen.state, "unseen");
  assert.match(unseen.screenLabel, /等待受试者端/);
});

test("relativeSeen 阶梯:刚刚/秒/分钟;未来时间戳按刚刚处理", () => {
  assert.equal(relativeSeen(null, NOW), null);
  assert.equal(relativeSeen(new Date(NOW + 5_000).toISOString(), NOW), "刚刚有响应");
  assert.equal(relativeSeen(new Date(NOW - 30_000).toISOString(), NOW), "约 30 秒前有响应");
  assert.equal(relativeSeen(new Date(NOW - 180_000).toISOString(), NOW), "约 3 分钟前有响应");
});

test("D4②:presence 视图带原始 screen 事实——只在在线态断言,其余态一律 null", () => {
  const paused = presenceViewFrom({
    sessionId: "S1", checking: false, unsupported: false, unavailable: false,
    presence: { session_id: "S1", screen: "paused", online: true, last_seen_at: new Date(NOW - 2_000).toISOString() },
    now: NOW,
  });
  assert.equal(paused.state, "online");
  assert.equal(paused.screen, "paused");
  assert.equal(paused.screenLabel, "已显示暂停提示");
  const offline = presenceViewFrom({
    sessionId: "S1", checking: false, unsupported: false, unavailable: false,
    presence: { session_id: "S1", screen: "paused", online: false },
    now: NOW,
  });
  // 断开后没人知道屏上是什么:raw screen 同样不许再断言。
  assert.equal(offline.screen, null);
  assert.equal(presenceViewFrom({
    sessionId: "S1", checking: false, unsupported: false, unavailable: false, presence: null,
  }).screen, null);
  assert.equal(presenceViewFrom({
    sessionId: "S1", checking: true, unsupported: false, unavailable: false, presence: null,
  }).screen, null);
});

test("老人端页面旧版本:只在在线且服务端判 stale 时提醒", () => {
  const seen = new Date(NOW - 2_000).toISOString();
  const view = (online: boolean, pageBuild?: string) => presenceViewFrom({
    sessionId: "S1", checking: false, unsupported: false, unavailable: false, now: NOW,
    presence: { session_id: "S1", screen: "waiting", online, last_seen_at: seen,
      ...(pageBuild === undefined ? {} : { page_build: pageBuild }) },
  });
  assert.equal(view(true, "stale").pageStale, true);
  // 断开后谁也不知道屏上跑的是哪一版:不提醒。
  assert.equal(view(false, "stale").pageStale, false);
  // 旧后端没有这个字段、服务端下不了结论或给了不认识的值:不提醒。
  assert.equal(view(true).pageStale, false);
  for (const pageBuild of ["current", "unknown", "future_value"]) {
    assert.equal(view(true, pageBuild).pageStale, false, pageBuild);
  }
  for (const flags of [{ checking: true }, { unsupported: true }, { unavailable: true }]) {
    assert.equal(presenceViewFrom({
      sessionId: "S1", checking: false, unsupported: false, unavailable: false, now: NOW,
      presence: { session_id: "S1", online: true, page_build: "stale" }, ...flags,
    }).pageStale, false);
  }
  assert.equal(presenceViewFrom({
    sessionId: null, checking: false, unsupported: false, unavailable: false, presence: null,
  }).pageStale, false);
});

test("SessionContextBar 把旧页面提醒和刷新时机写在在场状态旁边", () => {
  const shell = readFileSync(new URL("../console/ConsoleShell.tsx", import.meta.url), "utf8");
  const bar = shell.slice(shell.indexOf("function SessionContextBar("));
  assert.match(bar, /presence\.pageStale && /);
  assert.match(bar, /老人端页面是旧版本，请刷新老人端/);
  assert.match(bar, /请在暂停时或本场结束后刷新；刷新后需再点一次『点一下，开始』/);
});
