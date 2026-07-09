"""训练内容加载与校验（M5）。

把结构化题库/脚本从 JSON 载入并校验一致性——把「双人校对四者一致」自动化：
目标词 ↔ 成功话术 ↔ 告知话术 ↔ 线索 对不上就报出来（斧子+树 那类复制粘贴勘误即由此发现）。
纯标准库，无需装依赖即可跑测试与校验。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ItemBank:
    version_id: str
    single_element: list[dict]
    double_element: list[dict]
    errata_fixed: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def load_item_bank(path: str | Path) -> ItemBank:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    vid = data.get("item_bank_version_id")
    if not vid:
        raise ValueError("题库缺少 item_bank_version_id（每场次须绑冻结版本号）")
    return ItemBank(
        version_id=vid,
        single_element=data.get("single_element", []),
        double_element=data.get("double_element", []),
        errata_fixed=data.get("errata_fixed", []),
        meta={k: v for k, v in data.items()
              if k not in ("single_element", "double_element", "errata_fixed")},
    )


def load_week1_script(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not data.get("script_version_id"):
        raise ValueError("第一周脚本缺少 script_version_id")
    return data


def validate_item_bank(bank: ItemBank) -> dict[str, list[str]]:
    """两档校验，冻结前的自动校对闸：
      errors   = 缺陷/勘误（目标词↔告知话术↔标题 对不上、重复 id）——冻结前必须清零；
      warnings = 待补全（缺线索/成功/告知话术文本，如缺引号导致抽不出的线索）——内容组填。
    返回 {"errors": [...], "warnings": [...]}。
    """
    errors: list[str] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()

    for it in bank.single_element:
        iid = it.get("item_id", "?")
        if iid in seen_ids:
            errors.append(f"{iid}: item_id 重复")
        seen_ids.add(iid)
        tw = it.get("target_word")
        if not tw:
            errors.append(f"{iid}: 缺 target_word")
            continue
        tell = it.get("tell_answer")
        if tell and tw not in tell:
            # 四者一致核心：告知话术点到别的词 = 勘误（斧子+树/螺丝刀→衬衫 那类）
            errors.append(f"{iid}: 告知话术未含目标词“{tw}”（勘误）：{tell[:24]}…")
        elif not tell:
            warnings.append(f"{iid}: 缺 tell_answer")
        if not it.get("success_line"):
            warnings.append(f"{iid}: 缺 success_line")
        cues = it.get("cues", {})
        for lv in ("1", "2"):
            if not (cues.get(lv) or {}).get("text"):
                warnings.append(f"{iid}: 缺第{lv}级线索文本")

    for it in bank.double_element:
        iid = it.get("item_id", "?")
        if iid in seen_ids:
            errors.append(f"{iid}: item_id 重复")
        seen_ids.add(iid)
        title = it.get("pair_title", "")
        parts = [p for p in title.split("+") if p]
        for side in ("left_word", "right_word"):
            w = it.get(side)
            if not w:
                warnings.append(f"{iid}: 缺 {side}")
            elif w not in parts:
                errors.append(f"{iid}: {side}“{w}”不在标题“{title}”中（勘误）")

    return {"errors": errors, "warnings": warnings}


def validate_week1_script(script: dict) -> list[str]:
    issues: list[str] = []
    zodiac = script.get("zodiac_closed_list", [])
    if len(zodiac) != 12:
        issues.append(f"属相封闭词表应为 12 个，实为 {len(zodiac)}")
    if not script.get("sections"):
        issues.append("缺 sections")
    if script.get("silence_seconds") != 10:
        issues.append("沉默触发阈值应为 10 秒（与第2–8周口径一致）")
    return issues


# 内容目录默认位置
CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"
