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

    # 2026-08-21 真实研究场次通道落地后,README 的红线改为「双通道各自显式开启、
    # 不得放宽」;两份 2026-07-19 的审计文档是历史快照,保留当时的表述。
    assert "开启真实通道的唯一途径是显式开关加全部前置" in readme
    assert "ENABLE_AUTOPILOT_REAL_SESSIONS" in readme
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


def test_the_delivery_gap_number_in_the_docs_is_the_one_the_script_computes():
    """文档里那个「交付缺口」数必须等于题库自己算出来的数。

    它以前只写在六份文档的正文里靠人手同步，任何一次内容交付都可能让它悄悄
    失真——而这个数正是"能不能收真实受试者"的门槛之一。现在脚本会算，这条
    测试把两边钉在一起：内容组交付后数变了，文档没跟上就红。
    """
    import importlib.util
    from app import content

    spec = importlib.util.spec_from_file_location(
        "content_freeze_report",
        ROOT / "scripts" / "content_freeze_report.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    gap = module.delivery_gap(bank)
    total = gap["delivery_gap_total"]
    assert total == gap["in_plan_gaps"] + gap["unstructured_source_positions"]

    stated = f"{total} 个交付缺口"
    for name in ("README.md", "DEPLOY.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert stated in text, (
            f"{name} 里的交付缺口数与脚本算出的 {total} 对不上；"
            "内容交付后请同时更新文档，或反过来查是不是题库被改坏了")
