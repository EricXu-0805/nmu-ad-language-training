from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app import content

_SPEC = importlib.util.spec_from_file_location(
    "content_freeze_report",
    Path(__file__).resolve().parents[1] / "scripts" / "content_freeze_report.py")
assert _SPEC is not None and _SPEC.loader is not None
report_script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report_script)


def _bank():
    return content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")


@pytest.fixture
def staged_draft_bank_path(tmp_path) -> Path:
    """staged 降级副本：2026-08-19 内容交付后随包题库已无阻断项。

    「有阻断时报告怎么说、CLI 怎么退出」这组守门行为只能在它上面重现：
    qc 回 draft、双要素 rubric 摘掉（30 个判分缺口）、多要素退回未结构化
    （8 个计分题位记回 source_unstructured_positions，保持 78 的总数守恒）。
    """
    definition = json.loads(
        (content.CONTENT_DIR / "item_bank_v1.json").read_text(encoding="utf-8"))
    definition["qc_status"] = "draft"
    for item in definition["double_element"]:
        item.pop("operational_rubrics", None)
    unstructured = []
    for item in definition["multi_element"]:
        for element in item["key_elements"]:
            role = element.get("key") or element.get("id")
            unstructured.append({
                "source_position_key": f"{item['item_id']}:{role}",
                "response_role": role,
                "source_paragraphs": [0, 1],
                "status": "awaiting_content_decision",
            })
    definition["multi_element"] = []
    definition["source_unstructured_positions"] = unstructured
    path = tmp_path / "item_bank_draft.json"
    path.write_text(
        json.dumps(definition, ensure_ascii=False), encoding="utf-8")
    return path


def test_the_shipped_bank_is_reported_as_deliverable_and_cli_exits_zero(capsys):
    # 2026-08-19 内容交付钉：冻结、rubric 全齐、零未结构化题位，报告放行。
    report = report_script.collect(_bank())

    assert report["ready_for_research"] is True
    assert report["blocking_groups"] == []
    assert report["qc_status"] == "frozen"
    assert report["delivery_gap"]["unstructured_source_positions"] == 0

    assert report_script.main([]) == 0
    assert "没有阻断项" in capsys.readouterr().out


def test_a_staged_draft_bank_is_reported_as_not_deliverable(
        staged_draft_bank_path):
    report = report_script.collect(
        content.load_item_bank(staged_draft_bank_path))

    assert report["ready_for_research"] is False
    assert report["blocking_groups"]


def test_every_clinical_blocker_is_attributed_to_the_content_team(
        staged_draft_bank_path):
    # 工程侧代填临床话术或判分规则是明令禁止的。清单必须把这些项挂在内容组
    # 名下,否则下一个读它的人会顺手"补齐"。
    report = report_script.collect(
        content.load_item_bank(staged_draft_bank_path))

    clinical = [g for g in report["blocking_groups"]
                if "rubric" in str(g["title"]) or "结构化" in str(g["title"])
                or "冻结" in str(g["title"])]
    assert clinical
    assert all(g["owner"] == report_script.CONTENT_TEAM for g in clinical)


def test_counts_match_the_runtime_readiness_contract(staged_draft_bank_path):
    bank = content.load_item_bank(staged_draft_bank_path)
    readiness = content.content_readiness(bank)
    report = report_script.collect(bank)

    assert readiness["unsupported_operational_rubrics"]
    assert readiness["source_unstructured_position_count"]
    by_title = {str(g["title"]): g for g in report["blocking_groups"]}
    rubric_group = next(g for t, g in by_title.items() if "rubric" in t)
    unstructured_group = next(g for t, g in by_title.items() if "结构化" in t)

    assert rubric_group["count"] == len(readiness["unsupported_operational_rubrics"])
    assert unstructured_group["count"] == readiness["source_unstructured_position_count"]
    assert len(rubric_group["items"]) == rubric_group["count"]


def test_cli_exits_nonzero_while_blocked_and_json_carries_every_item(
        staged_draft_bank_path, capsys):
    code = report_script.main(
        ["--bank", str(staged_draft_bank_path), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocking_groups"], "staged draft 副本必须报出阻断组"
    for group in payload["blocking_groups"]:
        assert len(group["items"]) == group["count"]


def test_cli_text_mode_truncates_but_says_so(staged_draft_bank_path, capsys):
    report_script.main(
        ["--bank", str(staged_draft_bank_path), "--max-items", "2"])

    out = capsys.readouterr().out
    assert "另有" in out
    assert "用 --json 取全量" in out


def test_a_ready_bank_reports_no_blockers_and_exits_zero(monkeypatch, capsys):
    real = content.content_readiness

    def ready(bank):
        result = dict(real(bank))
        result.update({
            "ready_for_research": True, "operational_autopilot_ready": True,
            "unsupported_operational_rubrics": [],
            "source_unstructured_position_count": 0,
            "source_unstructured_positions": [], "errors": [], "warnings": [],
        })
        return result

    monkeypatch.setattr(content, "content_readiness", ready)
    monkeypatch.setattr(
        content.ItemBank, "qc_status", property(lambda self: "frozen"))

    bank = _bank()
    bank.meta["source_unstructured_blockers"] = []
    report = report_script.collect(bank)

    assert report["blocking_groups"] == []
    assert report["ready_for_research"] is True
    assert "没有阻断项" in report_script.render(report, max_items=5)
