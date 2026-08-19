"""跨端字面量对齐：week2 演示 profile 的 digest 在前后端各钉了一份。

2026-08-19 交付把后端 WEEK2_SINGLE20_DEMO_DIGEST 重钉而前端
web/src/autopilot/demoProfile.ts 没跟上，浏览器里快速演练开场被前端
严格契约解析器整链拒收（后端 200、屏上报"训练场次响应不符合严格契约"）。
两份测试套各自自洽，谁也抓不到跨端失步——只有这条测试同时读两边。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.autopilot_plan_profiles import (
    WEEK2_SINGLE20_DEMO_DIGEST,
    WEEK2_SINGLE20_DEMO_VERSION,
)

_FRONTEND = Path(__file__).resolve().parents[1] / "web" / "src" / "autopilot" / "demoProfile.ts"


def _frontend_literal(name: str) -> str:
    source = _FRONTEND.read_text(encoding="utf-8")
    match = re.search(rf"{name}\s*=\s*\n?\s*\"([^\"]+)\"", source)
    assert match, f"web/src/autopilot/demoProfile.ts 里找不到 {name} 字面量"
    return match.group(1)


def test_demo_profile_digest_matches_backend() -> None:
    assert _frontend_literal("WEEK2_SINGLE20_DEMO_PROFILE_DIGEST") == WEEK2_SINGLE20_DEMO_DIGEST


def test_demo_profile_version_matches_backend() -> None:
    assert _frontend_literal("WEEK2_SINGLE20_DEMO_PROFILE_VERSION") == WEEK2_SINGLE20_DEMO_VERSION
