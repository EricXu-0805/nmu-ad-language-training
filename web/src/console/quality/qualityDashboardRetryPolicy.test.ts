import assert from "node:assert/strict";
import test from "node:test";
import { ApiError } from "../../apiResponse.ts";
import {
  qualityRetryDeadlineMs,
  qualityRetryRemainingSeconds,
} from "./qualityDashboardRetryPolicy.ts";

test("quality 429 honors Retry-After and malformed/missing values still cool down", () => {
  const now = 1_000_000;
  assert.equal(
    qualityRetryDeadlineMs(new ApiError(429, "limited", undefined, "direct", 7), now),
    now + 7_000,
  );
  assert.equal(
    qualityRetryDeadlineMs(new ApiError(429, "limited"), now),
    now + 1_000,
  );
  assert.equal(qualityRetryDeadlineMs(new ApiError(500, "failed"), now), null);
});

test("quality retry countdown rounds up and closes at the deadline", () => {
  assert.equal(qualityRetryRemainingSeconds(10_001, 10_000), 1);
  assert.equal(qualityRetryRemainingSeconds(12_001, 10_000), 3);
  assert.equal(qualityRetryRemainingSeconds(10_000, 10_000), 0);
  assert.equal(qualityRetryRemainingSeconds(null, 10_000), 0);
});
