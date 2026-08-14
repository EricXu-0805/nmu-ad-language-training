"""列注册表自洽性 + 去标识边界（纯函数层，不起 app、不碰数据库）。"""
from __future__ import annotations

from app import export_security, research_dataset as rd


def test_registry_is_self_consistent():
    assert rd.registry_self_check() == []
    assert rd.dataset_keys() == ("subjects", "sessions", "turns")


def test_every_forbidden_column_is_documented_but_never_published():
    for dataset in rd.DATASETS:
        published = set(rd.published_columns(dataset))
        for column in dataset.columns:
            if column.disclosure == "forbidden":
                assert column.name not in published, column.name
                assert column.description, "被排除的列必须写清为什么"
    # 字典里必须能看到这些被显式排除的列，否则读字典的人会以为是漏了
    documented = {(row["dataset"], row["column"]) for row in rd.dictionary_rows()}
    for pair in (("turns", "asr_text"), ("turns", "confirmed_response_text"),
                 ("turns", "judge_reason"), ("subjects", "patient_id"),
                 ("subjects", "consent_person"), ("sessions", "training_date")):
        assert pair in documented, pair


def test_projection_is_a_closed_set_and_drops_extra_keys():
    turns = rd.dataset_for("turns")
    assert turns is not None
    row = rd.project(turns, {
        "session_code": "SESS-x", "subject_code": "SUBJ-x",
        "item_id": "SE_胡萝卜", "turn_seq": 1,
        # 下面这些是"顺手多带的"，必须被丢掉而不是放行
        "asr_text": "我不知道", "patient_id": "P-1", "created_at": "2026-08-14T00:00:00Z",
        "session_id": "S-1",
    })
    assert set(row) == set(rd.published_columns(turns))
    for leaked in ("asr_text", "patient_id", "created_at", "session_id"):
        assert leaked not in row


def test_projected_rows_pass_the_shared_deidentification_assertion():
    """真正的兜底：投影结果必须过导出包同一道断言。"""
    sheets = {}
    for dataset in rd.DATASETS:
        row = {name: None for name in rd.published_columns(dataset)}
        row.update({
            "subject_code": "SUBJ-v1-nmu-2026-01-abcdef0123456789abcd",
            "session_code": "SESS-v1-nmu-2026-01-abcdef0123456789abcd",
            "week_no": 2, "turn_seq": 1, "prompt_level": 0,
            "judge_portrait_used": False,
        })
        sheets[dataset.key] = [row]
    export_security.assert_deidentified_sheets(sheets)


def test_csv_header_matches_the_dictionary_order_exactly():
    for dataset in rd.DATASETS:
        header, matrix = rd.csv_matrix(dataset, [rd.project(dataset, {})])
        assert header == list(rd.published_columns(dataset))
        dict_order = [row["column"] for row in rd.dictionary_rows()
                      if row["dataset"] == dataset.key and row["published"]]
        assert header == dict_order, dataset.key
        assert len(matrix[0]) == len(header)


def test_pseudonymizer_wrapper_keeps_none_as_none():
    apply = rd.make_pseudonymizer(lambda value: f"X-{value}")
    assert apply(None) is None
    assert apply("P-1") == "X-P-1"


def test_csv_rendering_neutralises_formula_injection():
    from app import research_read
    raw = research_read.render_csv(
        ["a", "b"], [["=cmd|' /c calc'!A1", "+1+1"], ["@SUM(1)", "-2"]])
    text = raw.decode("utf-8-sig")
    for line in text.splitlines()[1:]:
        for cell in line.split(","):
            stripped = cell.strip('"')
            assert (stripped[:1] not in {"=", "+", "-", "@"}
                    or stripped.startswith("'")), cell


def test_the_handover_doc_and_the_registry_cannot_drift_apart():
    """文档说什么、注册表出什么，必须对得上。

    2026-08-14 的复核发现文档 §4.2 要求"统计时按 withdrawn 过滤"，而
    sessions / turns 两张主力表里根本没有这一列——写给 PI 的指令不可执行。
    这条测试把两边钉在一起。
    """
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[1]
           / "docs/handover/研究数据接口使用说明.md").read_text(encoding="utf-8")
    assert "统计时按 `withdrawn` 过滤" in doc, "文档改了措辞，这条断言要跟着改"
    for dataset in rd.DATASETS:
        assert "withdrawn" in rd.published_columns(dataset), \
            f"文档要求按 withdrawn 过滤，但 {dataset.key} 没有这一列"
    # 文档声明的自然键，三列必须都在 turns 的发布列里，否则墓碑一置空就塌行
    turns = rd.dataset_for("turns")
    assert turns is not None
    for column in ("session_code", "item_id", "turn_seq"):
        assert column in rd.published_columns(turns), column
    assert "(`session_code`, `item_id`, `turn_seq`)" in doc or \
        "`(session_code, item_id, turn_seq)`" in doc, "文档里的自然键写法变了"
