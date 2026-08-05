from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from app import content

_SPEC = importlib.util.spec_from_file_location(
    "content_freeze_report",
    Path(__file__).resolve().parents[1] / "scripts" / "content_freeze_report.py")
assert _SPEC is not None and _SPEC.loader is not None
report_script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report_script)


def _bank():
    return content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")


def test_the_shipped_bank_is_reported_as_not_deliverable():
    report = report_script.collect(_bank())

    assert report["ready_for_research"] is False
    assert report["blocking_groups"]


def test_every_clinical_blocker_is_attributed_to_the_content_team():
    # 工程侧代填临床话术或判分规则是明令禁止的。清单必须把这些项挂在内容组
    # 名下,否则下一个读它的人会顺手"补齐"。
    report = report_script.collect(_bank())

    clinical = [g for g in report["blocking_groups"]
                if "rubric" in str(g["title"]) or "结构化" in str(g["title"])
                or "冻结" in str(g["title"])]
    assert clinical
    assert all(g["owner"] == report_script.CONTENT_TEAM for g in clinical)


def test_counts_match_the_runtime_readiness_contract():
    bank = _bank()
    readiness = content.content_readiness(bank)
    report = report_script.collect(bank)

    by_title = {str(g["title"]): g for g in report["blocking_groups"]}
    rubric_group = next(g for t, g in by_title.items() if "rubric" in t)
    unstructured_group = next(g for t, g in by_title.items() if "结构化" in t)

    assert rubric_group["count"] == len(readiness["unsupported_operational_rubrics"])
    assert unstructured_group["count"] == readiness["source_unstructured_position_count"]
    assert len(rubric_group["items"]) == rubric_group["count"]


def test_cli_exits_nonzero_while_blocked_and_json_carries_every_item(capsys):
    code = report_script.main(["--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    for group in payload["blocking_groups"]:
        assert len(group["items"]) == group["count"]


def test_cli_text_mode_truncates_but_says_so(capsys):
    report_script.main(["--max-items", "2"])

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
