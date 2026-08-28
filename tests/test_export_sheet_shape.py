"""导出表的形状：分析者拿到的 CSV 必须能直接进统计软件。

三条都是 2026-08-27 审查坐实的，共同点是「静默」——不报错、不留痕：

1. `_flat` 明写 `keep = {k: v for k, v in d.items() if not isinstance(v, (list, dict))}`，
   于是 `prompt_level_distribution` 这个 dict 被直接丢掉。提示依赖程度的分布证据
   压根没落盘，想复原只能在 R 里重写一遍判分引擎。
2. 场次级结局七个数被压成一个 `"k=v; k=v"` 字符串列，要自己写正则拆、拆错静默出错。
3. 表头从数据推不从契约来：`cols = sorted({k for r in rows for k in r}) if rows else []`。
   没有异常事件的场次，`abnormal.csv` 是个 0 列文件；批处理里 rbind 240 个场次包时
   要么当场炸、要么被 `dplyr::bind_rows` 静默跳过——而「异常事件为零」恰是最常见的情况。
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from app import export


def test_flat_refuses_to_silently_drop_nested_values():
    with pytest.raises(ValueError, match="prompt_level_distribution"):
        export._flat({"n": 3, "prompt_level_distribution": {0: 1, 1: 2}})


def test_flat_still_handles_the_ordinary_flat_case():
    assert export._flat({"n": 3, "prompt_rate": 0.5}) == "n=3; prompt_rate=0.5"
    assert export._flat(None) is None
    assert export._flat({}) is None


def test_score_sheet_carries_the_prompt_level_distribution_as_columns():
    """七个指标各占一列，分布展成 prompt_level_0..3 —— 不再是一个字符串。"""
    row = export._score_row({"session_code": "S-1"}, "单要素", {
        "n": 4,
        "naming_accuracy": 0.75,
        "spontaneous_naming_accuracy": 0.5,
        "prompt_rate": 0.5,
        "prompt_level_distribution": {0: 2, 1: 1, 2: 1, 3: 0},
        "total_prompt_load": 3,
        "avg_time_per_item": 12.5,
        "total_task_time": 50.0,
    })
    assert row["task_type"] == "单要素"
    assert row["n"] == 4
    assert row["prompt_level_0"] == 2
    assert row["prompt_level_3"] == 0
    assert row["total_prompt_load"] == 3
    assert "summary" not in row, "结局指标不许再压成一个字符串列"


def test_every_sheet_has_a_declared_field_contract():
    missing = set(export.SHEET_FIELDS) ^ set(export.EXPECTED_SHEET_NAMES)
    assert not missing, f"表名与字段契约对不上：{sorted(missing)}"


def test_empty_sheets_still_get_their_header_row(tmp_path: Path):
    files, _ = export._write_csvs(
        {"abnormal": []}, "batch-empty-header", tmp_path, "simulation",
        staging_owner_hash="0" * 64, lease_guard=lambda: None)
    written = [Path(f) for f in files if Path(f).name == "abnormal.csv"]
    assert written, f"没写出 abnormal.csv：{files}"
    header = next(csv.reader(
        written[0].read_text(encoding="utf-8-sig").splitlines()))
    assert header == list(export.SHEET_FIELDS["abnormal"]), (
        "空表也必须写表头：0 列文件会让 rbind 静默跳过整个场次")


def test_a_sheet_without_a_declared_contract_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="没有登记列契约"):
        export._write_csvs(
            {"brand_new_sheet": []}, "batch-undeclared", tmp_path, "simulation",
            staging_owner_hash="0" * 64, lease_guard=lambda: None)


def test_an_undeclared_column_is_refused_instead_of_silently_reordered(tmp_path: Path):
    with pytest.raises(ValueError, match="未登记的列"):
        export._write_csvs(
            {"abnormal": [{"subject_code": "S", "surprise": 1}]},
            "batch-extra-col", tmp_path, "simulation",
            staging_owner_hash="0" * 64, lease_guard=lambda: None)


# ---------------------------------------------------------------------------
# 时长列与那条死列
# ---------------------------------------------------------------------------
# `research_dataset.py` 的模块契约写着「只出相对量（时长、潜伏期、序号）」，
# 而三张表一个时长列都没有。唯一带 duration_seconds 的是 attempts.csv，
# 那张表自己盖的章是 `truth_scope=operational_only`——文档定义它不是研究真值。
# 另一半：`naming_latency_ms` 全仓零写入点，恒为 None，却还在被喂给量表 AI 初评。


def test_turn_sheet_and_research_turns_both_carry_the_duration():
    from app import research_dataset
    assert "duration_seconds" in export.SHEET_FIELDS["turns"]
    turns = next(d for d in research_dataset.DATASETS if d.key == "turns")
    names = [c.name for c in turns.columns]
    assert "duration_seconds" in names, (
        "研究取数面一个时长列都没有，而模块契约承诺了「时长」")


def test_the_dead_latency_column_is_gone_everywhere():
    """留着最坏：读 schema 的人会以为平台有反应时，而每次量表初评都在把 null 当证据发出去。"""
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parents[1]
    offenders = []
    for pattern in ("app/**/*.py", "web/src/**/*.ts", "web/src/**/*.tsx"):
        for path in root.glob(pattern):
            text = path.read_text(encoding="utf-8")
            # 注释里那句「为什么删掉它」要留着，否则下一个人会照旧加回来。
            code = "\n".join(line for line in text.split("\n")
                              if not line.strip().startswith("#"))
            if "naming_latency_ms" in code:
                offenders.append(str(path.relative_to(root)))
    assert not offenders, (
        "naming_latency_ms 没有任何写入点，是死列；还留在：" + ", ".join(offenders)
        + "。真要做反应时，先和钱凯定「潜伏期从哪一刻起算」，再连同写入路径一起加。")
