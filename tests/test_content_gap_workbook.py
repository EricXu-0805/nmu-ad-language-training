"""内容缺口填报器：重点不是"能不能合并"，而是"合并会不会越权"。

这个脚本是唯一一处工程代码往临床题库里写内容的地方。它可以搬运内容组写的字，
但绝不能替他们做冻结决定——所以下面大半测试在钉"它做不到什么"。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from app import content
from scripts import content_gap_workbook as wb


BANK_PATH = content.CONTENT_DIR / "item_bank_v1.json"


@pytest.fixture
def bank():
    return content.load_item_bank(BANK_PATH)


def _read(path: Path, columns: list[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [{name: (row.get(name) or "").strip() for name in columns}
                for row in csv.DictReader(handle)]


def _write(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _fill(work_dir: Path) -> None:
    """按内容组会怎么填来填：只动该他们动的格子，占位符原样保留。"""
    rubrics = _read(work_dir / wb.RUBRIC_FILE, wb.RUBRIC_COLUMNS)
    for row in rubrics:
        row.update({
            "rubric_version": "T-1", "decision_policy": "any_acceptable_expression",
            "acceptable_expressions": "甲；乙", "cue_1": "一层提示",
            "cue_2": "二层提示", "tell_answer": "告知答案",
        })
    _write(work_dir / wb.RUBRIC_FILE, wb.RUBRIC_COLUMNS, rubrics)

    namings = _read(work_dir / wb.NAMING_FILE, wb.NAMING_COLUMNS)
    for row in namings:
        if not row["acceptable_expressions"]:
            row["acceptable_expressions"] = "甲；乙"
        if not row["difficulty_level"]:
            row["difficulty_level"] = "中"
    _write(work_dir / wb.NAMING_FILE, wb.NAMING_COLUMNS, namings)


def test_export_writes_only_position_ids_and_never_authors_content(bank, tmp_path):
    assert wb.cmd_export(bank, tmp_path) == 0
    for row in _read(tmp_path / wb.RUBRIC_FILE, wb.RUBRIC_COLUMNS):
        # 空白表里除了题位标识，每一格都必须是空的——一个字都不能是系统写的。
        for field in ("rubric_version", "decision_policy", "acceptable_expressions",
                      "required_concepts", "cue_1", "cue_2", "tell_answer"):
            assert row[field] == "", f"{row['item_id']}:{row['response_role']} 的 {field}"
    assert (tmp_path / wb.README_FILE).exists()


def test_empty_workbook_reports_every_missing_cell_by_position(bank, tmp_path, capsys):
    wb.cmd_export(bank, tmp_path)
    assert wb.cmd_check(bank, tmp_path) == 1
    out = capsys.readouterr().out
    assert "DE_烟灰缸+烟:左作用" in out
    assert "decision_policy 只能填" in out


def test_filled_workbook_passes_and_merges(bank, tmp_path):
    wb.cmd_export(bank, tmp_path)
    _fill(tmp_path)
    assert wb.cmd_check(bank, tmp_path) == 0
    out = tmp_path / "merged.json"
    assert wb.cmd_merge(bank, BANK_PATH, tmp_path, out) == 0
    merged = content.load_item_bank(out)
    assert content.unsupported_operational_rubrics(merged) == ()


def test_merge_never_freezes_the_bank_and_never_makes_it_research_ready(
        bank, tmp_path):
    """填表 ≠ 冻结。这是这个脚本最重要的一条边界。"""
    wb.cmd_export(bank, tmp_path)
    _fill(tmp_path)
    out = tmp_path / "merged.json"
    assert wb.cmd_merge(bank, BANK_PATH, tmp_path, out) == 0
    merged = content.load_item_bank(out)
    assert merged.qc_status == bank.qc_status == "draft"
    readiness = content.content_readiness(merged)
    assert readiness["ready_for_research"] is False
    raw_before = json.loads(BANK_PATH.read_text(encoding="utf-8"))
    raw_after = json.loads(out.read_text(encoding="utf-8"))
    for key in wb.FROZEN_META_KEYS:
        assert raw_after.get(key) == raw_before.get(key), key


def test_merge_refuses_when_the_workbook_is_not_finished(bank, tmp_path):
    wb.cmd_export(bank, tmp_path)
    out = tmp_path / "merged.json"
    assert wb.cmd_merge(bank, BANK_PATH, tmp_path, out) == 1
    assert not out.exists(), "没填完就不该写出任何题库文件"


def test_merge_refuses_to_overwrite_an_existing_target(bank, tmp_path):
    wb.cmd_export(bank, tmp_path)
    _fill(tmp_path)
    out = tmp_path / "merged.json"
    out.write_text("{}", encoding="utf-8")
    assert wb.cmd_merge(bank, BANK_PATH, tmp_path, out) == 2
    assert out.read_text(encoding="utf-8") == "{}"


def test_merge_never_writes_acceptable_expressions_onto_non_single_element_items(
        bank, tmp_path):
    """双要素题的模式里没有这一格；写进去整份题库会结构不合法。"""
    wb.cmd_export(bank, tmp_path)
    _fill(tmp_path)
    out = tmp_path / "merged.json"
    assert wb.cmd_merge(bank, BANK_PATH, tmp_path, out) == 0
    raw = json.loads(out.read_text(encoding="utf-8"))
    for item in raw["double_element"]:
        assert "acceptable_expressions" not in item, item["item_id"]


def test_placeholders_are_never_merged_as_real_values(bank, tmp_path):
    wb.cmd_export(bank, tmp_path)
    _fill(tmp_path)
    out = tmp_path / "merged.json"
    assert wb.cmd_merge(bank, BANK_PATH, tmp_path, out) == 0
    text = out.read_text(encoding="utf-8")
    for placeholder in wb.PLACEHOLDERS:
        assert placeholder not in text, placeholder


def test_a_broken_merge_leaves_no_half_written_bank_behind(bank, tmp_path, monkeypatch):
    wb.cmd_export(bank, tmp_path)
    _fill(tmp_path)
    monkeypatch.setattr(wb, "merge_into_bank",
                        lambda *args, **kwargs: {"single_element": "不是数组"})
    out = tmp_path / "merged.json"
    assert wb.cmd_merge(bank, BANK_PATH, tmp_path, out) == 1
    assert not out.exists()
    assert not list(tmp_path.glob("*.staged"))


def test_renaming_a_position_is_rejected_rather_than_silently_dropped(bank, tmp_path):
    wb.cmd_export(bank, tmp_path)
    _fill(tmp_path)
    rows = _read(tmp_path / wb.RUBRIC_FILE, wb.RUBRIC_COLUMNS)
    rows[0]["item_id"] = "DE_不存在的题"
    _write(tmp_path / wb.RUBRIC_FILE, wb.RUBRIC_COLUMNS, rows)
    problems = wb.check_rubric_rows(rows, wb.rubric_gaps(bank))
    assert any("对应不到题库里的题位" in problem for problem in problems)
    assert any("整行不见了" in problem for problem in problems)


def test_multi_value_cells_split_on_all_three_separators():
    assert wb._split_list("甲；乙;丙|丁") == ["甲", "乙", "丙", "丁"]
    assert wb._split_list("带 空格 的词组") == ["带 空格 的词组"]
    assert wb._split_list("") == []
    for placeholder in wb.PLACEHOLDERS:
        assert wb._split_list(placeholder) == []
