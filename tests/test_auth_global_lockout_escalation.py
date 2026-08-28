"""全局限速：连续命中要越锁越久，不能每次锁 30 秒然后把计数器清零。

2026-08-27 审查实测：`record_failure` 在命中上限时 `_GLOBAL_FAILURES.pop(scope)`，
于是稳态吞吐 ≈ 上限次 / 锁定秒数 ≈ 63 次 / 30 秒 ≈ 7560 次/小时，与攻击者用多少个
IP 无关（这是**全局**桶）。`/device/pair` 又没有任何 `_POLICIES` 条目，
`app/main.py` 里那句 `_expensive_rate_limit` 只在成功分支上跑。

合法场景不受影响：护理员当面输错三五次，永远碰不到第二档。
"""
from __future__ import annotations

import pytest

from app import auth


@pytest.fixture(autouse=True)
def _clean():
    auth.reset_for_tests()
    yield
    auth.reset_for_tests()


_ROUND = 0


def _burn_one_round(scope: str, start: float) -> float:
    """把一个 scope 打到全局锁定，返回打完时的时刻。

    每次失败换一个 IP：全局桶就是为这种形态存在的，攻击者用多少个源地址都
    落进同一个桶。同时这样也不会顺带触发**单键**锁定，测的才是全局那一层。
    """
    global _ROUND
    _ROUND += 1
    limit = auth._global_limit_max()
    for i in range(limit):
        auth.record_failure(f"{scope}:10.{_ROUND}.{i // 256}.{i % 256}",
                            now=start + i * 0.01)
    return start + limit * 0.01


def test_consecutive_lockouts_get_longer_instead_of_resetting():
    base = auth._global_lock_seconds()
    scope = "pair"
    probe_key = "pair:203.0.113.9"

    t = _burn_one_round(scope, 0.0)
    assert auth.is_locked(probe_key, now=t) is True
    # 第一档就是基础时长。
    assert auth.is_locked(probe_key, now=t + base + 0.1) is False

    # 锁一解开就再打满：第二档必须明显更长，而不是又一个 30 秒。
    t2 = _burn_one_round(scope, t + base + 0.2)
    assert auth.is_locked(probe_key, now=t2 + base + 0.1) is True, (
        "第二次连续命中仍然只锁基础时长——稳态刷穿的洞还在")

    # 第三档还要更长。
    second_lock = None
    for probe in range(1, 200):
        if not auth.is_locked(probe_key, now=t2 + probe * base):
            second_lock = probe * base
            break
    assert second_lock is not None and second_lock > base

    t3 = _burn_one_round(scope, t2 + second_lock + 0.2)
    assert auth.is_locked(probe_key, now=t3 + second_lock + 0.1) is True


def test_a_quiet_period_forgives_the_escalation():
    """连续性靠「上一次锁定之后不久又打满」判定；安静足够久就回到第一档。"""
    base = auth._global_lock_seconds()
    scope = "pair"
    probe_key = "pair:198.51.100.7"
    t = _burn_one_round(scope, 0.0)
    # 远远超过遗忘窗口之后再打满，仍是第一档。
    quiet = t + auth._global_escalation_memory_seconds() + base + 1.0
    t2 = _burn_one_round(scope, quiet)
    assert auth.is_locked(probe_key, now=t2 + base + 0.1) is False


def test_the_pairing_pin_minimum_length_is_eight():
    """6 位纯数字空间只有 1e6。提到 8 位，空间 ×100。"""
    assert auth._PAIRING_PIN_PATTERN.fullmatch("12345678") is not None
    assert auth._PAIRING_PIN_PATTERN.fullmatch("1234567") is None
    assert auth._PAIRING_PIN_PATTERN.fullmatch("123456") is None
