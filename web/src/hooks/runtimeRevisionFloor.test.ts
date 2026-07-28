import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { ratchetRevisionFloor, type RuntimeRevisionFloor } from "./runtimeRevisionFloor.ts";

test("ratchet only rises within the same session, never falls", () => {
  const floor: RuntimeRevisionFloor = { sessionId: "S-a", revision: 5 };
  assert.deepEqual(ratchetRevisionFloor(floor, "S-a", 8), { sessionId: "S-a", revision: 8 });
  // 相等或更低的读数不后退。
  assert.deepEqual(ratchetRevisionFloor(floor, "S-a", 5), floor);
  assert.deepEqual(ratchetRevisionFloor(floor, "S-a", 3), floor);
});

test("a candidate for a foreign session is ignored outright, never adopted or averaged", () => {
  const floor: RuntimeRevisionFloor = { sessionId: "S-a", revision: 5 };
  // 换场之后，A 场次迟到的高 revision 回执不能被当成 B 场次的下限。
  const result = ratchetRevisionFloor(floor, "S-other", 100);
  assert.deepEqual(result, floor);
});

test("ratcheting is a pure, order-independent monotonic max within one session", () => {
  let floor: RuntimeRevisionFloor = { sessionId: "S-a", revision: 0 };
  // 无论到达顺序如何（模拟 poll 与直读乱序回来），最终下限都是见过的最大值。
  for (const revision of [3, 9, 1, 7, 2]) {
    floor = ratchetRevisionFloor(floor, "S-a", revision);
  }
  assert.equal(floor.revision, 9);
});

test("both the poll/refresh receipt and the pause/resume receipt ratchet the floor inside their own continuation, before setRuntime", () => {
  const source = readFileSync(new URL("./useSessionRuntime.ts", import.meta.url), "utf8");

  function indexOfOrFail(haystack: string, needle: string, from = 0): number {
    const index = haystack.indexOf(needle, from);
    assert.notEqual(index, -1, `useSessionRuntime 缺少「${needle}」`);
    return index;
  }

  // poll/refresh 共用的 fetchRuntime continuation。
  const fetchStart = indexOfOrFail(source, "const fetchRuntime = useCallback(");
  const fetchEnd = indexOfOrFail(source, "const refresh = useCallback(", fetchStart);
  const fetchBody = source.slice(fetchStart, fetchEnd);
  const fetchReport = indexOfOrFail(fetchBody, "reportRevision(next.sessionId, next.revision);");
  const fetchSetRuntime = indexOfOrFail(fetchBody, "setRuntime(", fetchReport);
  assert.ok(fetchReport < fetchSetRuntime, "poll/refresh 的棘轮必须先于 setRuntime");

  // pause/resume 共用的 setPaused continuation。
  const pausedStart = indexOfOrFail(source, "const setPaused = useCallback(");
  const pausedEnd = indexOfOrFail(source, "const currentRuntime =", pausedStart);
  const pausedBody = source.slice(pausedStart, pausedEnd);
  const pausedReport = indexOfOrFail(pausedBody, "reportRevision(next.sessionId, next.revision);");
  const pausedSetRuntime = indexOfOrFail(pausedBody, "setRuntime(", pausedReport);
  assert.ok(pausedReport < pausedSetRuntime, "pause/resume 的棘轮必须先于 setRuntime");

  // 两条路径与外部重同步直读共用同一个 ref——不是各自另开一份。
  assert.equal((source.match(/revisionFloorRef\.current/g) ?? []).length >= 3, true,
    "poll、getRevisionFloor、reportRevision 应该共用同一个 revisionFloorRef");
  assert.match(source, /getRevisionFloor: \(\) => \(revisionFloorRef\.current\.sessionId === sessionId \? revisionFloorRef\.current\.revision : 0\)/);
});
