"""PI 三件套检查器：它必须用真实装载器判定，且在缺席时给出可执行清单。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location(
        "check_pi_content_package", ROOT / "scripts" / "check_pi_content_package.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_absent_pieces_are_reported_as_not_delivered_rather_than_as_errors(tmp_path):
    module = _module()
    report = module.inspect(tmp_path)
    assert [entry["present"] for entry in report["files"]] == [False, False, False]
    # 缺席是"尚未交付"，不是部署缺陷——这条口径写在 scale_protocol 的装载器里
    assert any("不存在" in str(entry["detail"]) for entry in report["loaders"])
    assert report["ready_for_research"] is False


def test_the_report_names_the_exact_fields_pi_still_has_to_freeze(tmp_path):
    module = _module()
    text = module.render(module.inspect(tmp_path))
    # 两张表各自的字段清单必须点名，而不是只说"还没就绪"
    for field in ("instrument_version", "license_status", "score_direction",
                  "pretest_time_window", "copyright_approval"):
        assert field in text, field
    for category in ("untrained_standardized_naming", "functional_communication",
                     "workflow_policy"):
        assert category in text, category
    # 也要说清工程侧不代填
    assert "不代填" in text


def test_exit_code_says_not_ready_without_pretending_to_be_broken(tmp_path):
    module = _module()
    assert module.main(["--content-dir", str(tmp_path)]) == 1
