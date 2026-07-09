import copy

from app.content import (
    CONTENT_DIR, load_item_bank, load_week1_script,
    validate_item_bank, validate_week1_script, ItemBank,
)


def test_shipped_item_bank_no_errors():
    # 随包题库须【无勘误级错误】；待补全项（如源缺引号的“花”L1线索）作为 warning 允许存在。
    bank = load_item_bank(CONTENT_DIR / "item_bank_v1.json")
    assert bank.version_id == "wk2-v1-20260707"
    assert len(bank.single_element) == 20
    assert len(bank.double_element) == 10
    result = validate_item_bank(bank)
    assert result["errors"] == [], f"题库不应有勘误级错误：{result['errors']}"
    # 已知待补全：源脚本“花”的第1级线索缺右引号，机器无法抽取，留待内容组补
    assert any("SE_花" in w and "第1级线索" in w for w in result["warnings"])


def test_errata_was_recorded():
    bank = load_item_bank(CONTENT_DIR / "item_bank_v1.json")
    assert any(e["item"] == "斧子+树" and e["corrected_to"] == "树" for e in bank.errata_fixed)


def test_validator_catches_errata_style_defect():
    # 造一个「告知话术写成别的词」的坏题——校验器必须逮住（这正是勘误的形态）
    bad = ItemBank(
        version_id="x",
        single_element=[{
            "item_id": "SE_螺丝刀", "target_word": "螺丝刀",
            "success_line": "对了", "tell_answer": "这个物品叫衬衫。",
            "cues": {"1": {"text": "a"}, "2": {"text": "b"}},
        }],
        double_element=[],
    )
    issues = validate_item_bank(bad)["errors"]
    assert any("螺丝刀" in i and "告知话术未含目标词" in i for i in issues)


def test_validator_catches_double_word_mismatch():
    bad = ItemBank(
        version_id="x", single_element=[],
        double_element=[{"item_id": "DE_斧子+树", "pair_title": "斧子+树",
                         "left_word": "斧子", "right_word": "衬衫"}],
    )
    issues = validate_item_bank(bad)["errors"]
    assert any("right_word" in i and "斧子+树" in i for i in issues)


def test_week1_script_loads_and_valid():
    s = load_week1_script(CONTENT_DIR / "week1_script.json")
    assert s["phase_type"] == "关系建立"
    assert len(s["zodiac_closed_list"]) == 12
    assert validate_week1_script(s) == []


def test_missing_version_id_rejected(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"single_element": []}', encoding="utf-8")
    import pytest
    with pytest.raises(ValueError):
        load_item_bank(p)
