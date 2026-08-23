import assert from "node:assert/strict";
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
