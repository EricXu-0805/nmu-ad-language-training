"""M3 ASR 接口(本地、可插拔、可关闭)——决策19:本地转录,待机构 GPU。

M0 用 NullAsrEngine:恒返回 None → 操作端人工转写降级,判分链不断。
真实引擎(whisper.cpp / FunASR 等)接入时实现 AsrProvider 并注册即可,调用面不变。
热词表从 M5 冻结题库生成(目标词/可接受表达/左右词 + 第1周属相闭表),供真实引擎偏置识别。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, Sequence

from .content import ItemBank


@dataclass(frozen=True)
class AsrResult:
    asr_text: str | None            # None = 引擎不可用/未接 → 人工转写降级
    asr_confidence: float | None
    engine_version: str
    hotword_hit: bool = False


class AsrProvider(Protocol):
    version: str

    def transcribe(self, audio_bytes: bytes, hotwords: Sequence[str]) -> AsrResult: ...


class NullAsrEngine:
    """占位引擎:恒降级。保证『动态能力关闭即走降级口径』这条铁律从第一天就可跑。"""
    version = "null-0"

    def transcribe(self, audio_bytes: bytes, hotwords: Sequence[str]) -> AsrResult:
        return AsrResult(asr_text=None, asr_confidence=None, engine_version=self.version)


_ENGINES: dict[str, AsrProvider] = {"null": NullAsrEngine()}


def get_engine() -> AsrProvider:
    """按 ASR_ENGINE 环境变量选引擎,未配置/未注册一律降级 null(fail-degraded,不 fail-hard)。"""
    return _ENGINES.get(os.environ.get("ASR_ENGINE", "null"), _ENGINES["null"])


def build_hotwords(bank: ItemBank, week1_script: dict | None = None) -> list[str]:
    """M 组热词:单要素目标词+可接受表达、双要素左右词;week1 传入则附属相闭表。去重保序。"""
    out: list[str] = []
    seen: set[str] = set()

    def add(w) -> None:
        if w and isinstance(w, str) and w not in seen:
            seen.add(w)
            out.append(w)

    for it in bank.single_element:
        add(it.get("target_word"))
        for x in it.get("acceptable_expressions") or []:
            add(x)
    for it in bank.double_element:
        add(it.get("left_word"))
        add(it.get("right_word"))
    if week1_script:
        for z in week1_script.get("zodiac_closed_list") or []:
            add(z)
    return out
