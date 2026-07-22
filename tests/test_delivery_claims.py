from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_readme_does_not_claim_unverified_dual_device_acceptance():
    readme = _read("README.md")

    assert "内网双设备可用" not in readme
    assert "当前 build 尚未完成真实双设备/目标浏览器验收" in readme


def test_delivery_docs_keep_research_autopilot_scope_fail_closed():
    readme = _read("README.md")
    deploy = _read("DEPLOY.md")
    regression = _read("项目综合审计_20260717/交付级回归记录_20260719.md")
    security = _read("项目综合审计_20260717/安全审查_20260719.md")

    assert "正式研究级 autopilot scope 尚未实现" in readme
    assert "不得删除 simulation guard" in deploy
    assert "当前仅 P0a 模拟 scope，不得移除 simulation guard 放行" in regression
    assert "当前唯一的 autopilot 服务范围是 P0a 模拟切片" in security


def test_delivery_docs_separate_scale_definitions_from_platform_implementation():
    readme = _read("README.md")
    deploy = _read("DEPLOY.md")
    regression = _read("项目综合审计_20260717/交付级回归记录_20260719.md")
    freeze_package = _read(
        "项目综合审计_20260717/内容协议来源与PI冻结包_20260719.md")

    required_model_terms = (
        "AssessmentInstance", "ItemResponse", "ScoringEvidence")
    for document in (readme, deploy, regression, freeze_package):
        assert all(term in document for term in required_model_terms)
        assert "approved-deferred" in document
        assert "closeout" in document
        assert "switch" in document

    assert "当前字段级协议完整" in freeze_package
    assert "20（仅字段级协议完整）" in freeze_package
    assert "当前默认整计划仍是 **0 个可启动**" in freeze_package
    assert "definitions 完整本身不得显示为“已冻结可用”" in deploy
