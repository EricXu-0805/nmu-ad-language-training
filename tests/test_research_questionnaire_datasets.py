"""量表进研究取数面。

在此之前，量表只在逐场次导出包里，而且按 `patient_id` 取数不按场次——同一位受试者
的每份量表会在他**每一个**场次的包里各出现一份。30 人拿前后测要对约 240 个包逐个
导出、拼起来、按 record_code 去重；谁忘了去重 n 就放大 8 倍，而重复行的
computed_total 完全一致、看不出异常。交接文档还写着「量表数据现在为零」——
那句话对正式结局契约成立，对五份试用道量表不成立。

冻结纪元的口径：纪元是一个**场次**集合，量表挂在受试者上。取「这批场次对应的
受试者、且已锁定的记录」，是唯一不引入新治理概念的口径；冻结时行被快照成字节，
后续读回放快照，所以纪元不可变自动成立。
"""
from __future__ import annotations

from app import research_dataset


def test_both_questionnaire_datasets_are_registered():
    keys = research_dataset.dataset_keys()
    assert "questionnaire_records" in keys
    assert "questionnaire_item_values" in keys


def test_the_two_datasets_join_on_record_code():
    records = research_dataset.dataset_for("questionnaire_records")
    values = research_dataset.dataset_for("questionnaire_item_values")
    assert "record_code" in records.column_names
    assert "record_code" in values.column_names
    # 两张表都带 subject_code，才能不经 records 直接按人聚合。
    assert "subject_code" in values.column_names


def test_free_text_and_absolute_time_are_declared_forbidden():
    for key in ("questionnaire_records", "questionnaire_item_values"):
        dataset = research_dataset.dataset_for(key)
        forbidden = {c.name for c in dataset.columns if c.disclosure == "forbidden"}
        published = set(research_dataset.published_columns(dataset))
        assert forbidden & published == set(), "被声明禁止的列不许出现在公开列里"
    records = research_dataset.dataset_for("questionnaire_records")
    names = {c.name: c.disclosure for c in records.columns}
    assert names["note"] == "forbidden"
    assert names["locked_at"] == "forbidden"
    values = research_dataset.dataset_for("questionnaire_item_values")
    vnames = {c.name: c.disclosure for c in values.columns}
    assert vnames["ai_draft_rationale"] == "forbidden"
    assert vnames["updated_at"] == "forbidden"


def test_the_supersede_column_is_published_so_analysts_can_filter():
    """没有它，导出里两行同为「前测」，取 first 或 max 都可能取到作废那条。"""
    records = research_dataset.dataset_for("questionnaire_records")
    published = set(research_dataset.published_columns(records))
    assert {"phase_ordinal", "superseded_by_ordinal"} <= published


def test_the_record_pseudonym_is_the_same_function_the_export_bundle_uses():
    """两边各写一份就会有一天分岔，PI 手里的 CSV 与接口拉的数就 join 不上了。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    export_src = (root / "app/export.py").read_text(encoding="utf-8")
    read_src = (root / "app/research_read.py").read_text(encoding="utf-8")
    assert "pseudonymize_questionnaire_record" in export_src
    assert "pseudonymize_questionnaire_record" in read_src
    assert 'domain="questionnaire-record"' not in read_src, (
        "研究读面自己又拼了一次假名域——必须走公共函数")


def test_there_is_exactly_one_dispatch_table():
    """加数据集只该改一个地方。

    2026-08-27 之前这张表在三处各写一遍：HTTP 取数、冻结纪元、还有测试夹具里
    自己抄的一份。加两个数据集时漏掉测试那份，症状是**别的**数据集返回 503——
    纪元少冻了两张表，读回放时对不上。
    """
    from app import research_read, research_dataset
    assert set(research_read.READERS) == set(research_dataset.dataset_keys())

    from pathlib import Path
    import re
    root = Path(__file__).resolve().parents[1]
    for rel in ("app/main.py", "app/quality_release.py",
                "tests/test_research_read_api.py"):
        src = re.sub(r"\s+", " ", (root / rel).read_text(encoding="utf-8"))
        assert '"turns": research_read.list_turns' not in src, (
            f"{rel} 又抄了一份 readers 字典——用 research_read.READERS")
