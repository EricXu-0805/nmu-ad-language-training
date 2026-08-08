"""训练周题库索引(收据 148 D2):每周一份数据包,未登记周 fail-closed。"""
import json
import shutil

import pytest

from app import content


def _stage(tmp_path, *, index: dict | None = None) -> str:
    staged = tmp_path / "content"
    staged.mkdir()
    shutil.copy(content.CONTENT_DIR / "item_bank_v1.json", staged)
    if index is not None:
        (staged / content.ITEM_BANK_INDEX_FILE).write_text(
            json.dumps(index, ensure_ascii=False), encoding="utf-8")
    return staged


def _index(banks) -> dict:
    return {"schema_version": "item-bank-index.v1", "banks": banks}


def test_shipped_index_resolves_week_two_and_refuses_the_rest():
    assert sorted(content.load_item_bank_index()) == [2]
    bank = content.load_item_bank_for_week(2)
    assert bank.version_id == "wk2-v1-20260707"
    for week in (3, 4, 5, 6, 7, 8):
        with pytest.raises(content.TrainingWeekContentUnavailable) as exc:
            content.load_item_bank_for_week(week)
        assert f"第{week}周材料尚未结构化" in str(exc.value)


def test_missing_or_malformed_index_fails_closed(tmp_path):
    staged = _stage(tmp_path)  # 无索引文件
    with pytest.raises(content.FrozenContentUnavailable):
        content.load_item_bank_index(staged)

    (staged / content.ITEM_BANK_INDEX_FILE).write_text(
        json.dumps(_index([
            {"week_no": 2, "file": "item_bank_v1.json"},
            {"week_no": 2, "file": "item_bank_v1.json"},
        ]), ensure_ascii=False), encoding="utf-8")
    with pytest.raises(content.FrozenContentUnavailable):
        content.load_item_bank_index(staged)

    (staged / content.ITEM_BANK_INDEX_FILE).write_text(
        json.dumps(_index([
            {"week_no": 2, "file": "../item_bank_v1.json"},
        ]), ensure_ascii=False), encoding="utf-8")
    with pytest.raises(content.FrozenContentUnavailable):
        content.load_item_bank_index(staged)


def test_index_week_must_match_the_bank_declaration(tmp_path):
    staged = _stage(tmp_path, index=_index([
        {"week_no": 3, "file": "item_bank_v1.json"},
    ]))
    with pytest.raises(content.FrozenContentUnavailable) as exc:
        content.load_item_bank_for_week(3, staged)
    assert "声明周次不一致" in str(exc.value)


def test_registering_a_week_file_unlocks_that_week(tmp_path):
    staged = _stage(tmp_path, index=_index([
        {"week_no": 2, "file": "item_bank_v1.json"},
        {"week_no": 3, "file": "item_bank_wk3.json"},
    ]))
    data = json.loads(
        (staged / "item_bank_v1.json").read_text(encoding="utf-8"))
    data["item_bank_version_id"] = "wk3-synth-v1"
    data["training_week_no"] = 3
    data["supported_training_weeks"] = [3]
    (staged / "item_bank_wk3.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    bank = content.load_item_bank_for_week(3, staged)
    assert bank.version_id == "wk3-synth-v1"
    assert bank.supported_training_weeks == (3,)
    # 周 2 解析不受影响。
    assert content.load_item_bank_for_week(2, staged).version_id == (
        "wk2-v1-20260707")
