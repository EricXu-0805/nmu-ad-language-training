"""单 worker 高成本端点 token bucket；外部反代配额仍是部署纵深防线。"""
from __future__ import annotations

from dataclasses import dataclass
import re
import threading
import time


@dataclass
class _Bucket:
    tokens: float
    updated: float


_LOCK = threading.Lock()
_BUCKETS: dict[tuple[str, str], _Bucket] = {}

# policy id, method, full path pattern, burst capacity, steady tokens per second
_POLICIES = (
    ("provider-readiness", "POST", re.compile(r"/ai/provider-readiness/probe"),
     1.0, 1.0 / 60.0),
    # The quality projection may verify a bounded set of physical audio blobs.
    # Keep refreshes deliberately sparse per named account/IP; the database and
    # byte preflight remain the primary, fail-closed resource boundary.
    ("quality-read", "GET", re.compile(r"/quality/ai-metrics/simulation"),
     2.0, 1.0 / 30.0),
    ("tts", "POST", re.compile(
        r"(?:/tts/speak|/sessions/[^/]+/autopilot/commands/[^/]+/tts)"),
     12.0, 1.0),
    ("attempt", "POST", re.compile(r"/sessions/[^/]+/attempts/process"), 6.0, 0.5),
    ("legacy-asr", "POST", re.compile(r"/asr/transcribe/[^/]+"), 4.0, 0.25),
    # 研究取数一页最多 1000 行，还要逐行算分域假名并过一遍去标识断言——按行数算
    # 是本服务最贵的读。桶名是 "@research-read"，一个 URL 里不可能出现的形状：
    # 第一版用的 "/research/v1/dataset" 恰好会被 `{dataset_key}` 匹配到，
    # 等于自己开了个可达入口。按数据集分桶则会让一个人换个数据集就绕开配额。
    # 放行量按"人在页面上点着看"够用、"脚本整库刮"会被拖慢来定：突发 10 页，
    # 之后每 3 秒回一页。十万行的 turns 全量拉完约 5 分钟，不影响正当取数。
    ("research-read", "GET", re.compile(r"@research-read"), 10.0, 1.0 / 3.0),
)


def consume(method: str, path: str, principal: str, *, now: float | None = None) -> float | None:
    """允许时返回 None；超限时返回建议重试秒数。"""
    matched = next((p for p in _POLICIES if p[1] == method.upper() and p[2].fullmatch(path)), None)
    if matched is None:
        return None
    policy, _method, _pattern, capacity, rate = matched
    current = time.monotonic() if now is None else now
    key = (policy, principal)
    with _LOCK:
        bucket = _BUCKETS.get(key)
        if bucket is None:
            bucket = _Bucket(capacity, current)
        else:
            bucket.tokens = min(capacity, bucket.tokens + max(0.0, current - bucket.updated) * rate)
            bucket.updated = current
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            _BUCKETS[key] = bucket
            return None
        _BUCKETS[key] = bucket
        return max(0.1, (1.0 - bucket.tokens) / rate)


def reset_for_tests() -> None:
    with _LOCK:
        _BUCKETS.clear()
