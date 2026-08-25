"""7 周题库的内容卫生回归——每条规则对应 2026-08-19 保真度审计抓到的真缺陷。

这些缺陷的共同点是：解析器/源稿出错时产物仍是合法 JSON、能过 schema，
只有读话术本身才发现。抓到过的四种形态钉成规则：
  - 脚手架前缀泄漏（wk8 眼睛：cue 文本以“第1次提示”开头，会被 TTS 念出来）
  - 完成条件词与题面脱节（wk8 毛毛虫：acceptable=[花生] 而 target=毛毛虫）
  - 引号不配对（wk7 脚+鞋：源稿双写开引号，剥壳后残留半个）
  - rubric 关键词超出源句（判分依据不得超出冻结文本）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

CONTENT_DIR = Path(__file__).resolve().parents[1] / "content"

BANK_FILES = sorted(
    p.name for p in CONTENT_DIR.glob("item_bank*.json")
    if p.name != "item_bank_index.json"
)

SCAFFOLD_MARKERS = (
    "第1次提示", "第2次提示", "若成功命名", "若仍未成功", "完成条件",
    "若患者", "当患者", "①", "②", "③", "④", "⑤",
)

SPOKEN_KEY_HINTS = ("text", "cue", "tell", "prompt", "line")


def test_the_rule_set_scans_real_banks():
    assert len(BANK_FILES) >= 7, "题库文件没扫到，下面的断言全在空转"


def _spoken_strings(item: dict):
    def walk(value, path):
        if isinstance(value, str):
            yield path, value
        elif isinstance(value, list):
            for i, row in enumerate(value):
                yield from walk(row, f"{path}[{i}]")
        elif isinstance(value, dict):
            for key, row in value.items():
                yield from walk(row, f"{path}.{key}")
    yield from walk(item, item.get("item_id", "?"))


@pytest.mark.parametrize("bank_file", BANK_FILES)
def test_no_scaffold_markers_reach_patient_facing_text(bank_file: str):
    bank = json.loads((CONTENT_DIR / bank_file).read_text(encoding="utf-8"))
    offenders = []
    for group in ("single_element", "double_element", "multi_element"):
        for item in bank.get(group, []):
            for path, value in _spoken_strings(item):
                if any(value.startswith(marker) for marker in SCAFFOLD_MARKERS):
                    offenders.append(f"{bank_file}:{path}: {value[:24]}…")
    assert not offenders, (
        "解析脚手架/源稿结构标记进入了会被念出来的文本：\n" + "\n".join(offenders))


@pytest.mark.parametrize("bank_file", BANK_FILES)
def test_quotes_are_balanced_in_spoken_text(bank_file: str):
    bank = json.loads((CONTENT_DIR / bank_file).read_text(encoding="utf-8"))
    offenders = []
    for group in ("single_element", "double_element", "multi_element"):
        for item in bank.get(group, []):
            for path, value in _spoken_strings(item):
                if not any(hint in path for hint in SPOKEN_KEY_HINTS):
                    continue
                if value.count("“") != value.count("”"):
                    offenders.append(f"{bank_file}:{path}: {value[:24]}…")
    assert not offenders, (
        "话术引号不配对（源稿双写/漏写引号剥壳残留）：\n" + "\n".join(offenders))


@pytest.mark.parametrize("bank_file", BANK_FILES)
def test_single_acceptable_expressions_stay_tied_to_the_item(bank_file: str):
    """acceptable 词必须与目标词同源或出现在告知话术里。

    wk8 毛毛虫的源稿完成条件误贴“花生”——这类错位让说出正确答案的患者
    永远判不了达成，而 JSON 完全合法。
    """
    bank = json.loads((CONTENT_DIR / bank_file).read_text(encoding="utf-8"))
    offenders = []
    for item in bank.get("single_element", []):
        target = item["target_word"]
        tell = item["tell_answer"]
        for term in item["acceptable_expressions"]:
            if not (term in target or target in term or term in tell):
                offenders.append(
                    f"{bank_file}:{item['item_id']}: "
                    f"acceptable“{term}”与 target“{target}”/告知话术无关")
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("bank_file", BANK_FILES)
def test_double_rubric_keywords_are_substrings_of_their_cue(bank_file: str):
    """双要素判分关键词只能来自冻结 cue/tell 文本——判分依据不得超出源文。"""
    bank = json.loads((CONTENT_DIR / bank_file).read_text(encoding="utf-8"))
    offenders = []
    for item in bank.get("double_element", []):
        rubrics = item.get("operational_rubrics") or {}
        for role, rubric in rubrics.items():
            source = rubric["cues"]["1"] + rubric["tell_answer"]
            for term in rubric.get("acceptable_expressions", []):
                if term not in source:
                    offenders.append(
                        f"{bank_file}:{item['item_id']}:{role}: “{term}”")
    assert not offenders, (
        "rubric 关键词不是其 cue/tell 源句的子串：\n" + "\n".join(offenders))


@pytest.mark.parametrize("week", [2, 3, 4, 5, 6, 7, 8])
def test_every_position_of_every_week_is_engine_selectable(week: int):
    """2~8 周全部 78 题位必须能被自动带练引擎选中——任何一周任何题位坏了当场红。

    2026-08-26 生产实测(墙六):wk4 SE_花瓶 因登记勘误后 unknown/silence 借用
    同一源句,被「cue1 三分支异源」规则拒 → 全周启动 409,而周就绪面报绿。
    引擎已为「errata_fixed 登记过 cues.1.* 的题」开精确豁免;未登记的塌缩仍拒。
    """
    from app import autopilot_positions, autopilot_service, content, repeat_intent
    from app.models import Session as TrainSession

    bank = content.load_item_bank_for_week(week)
    proto = content.load_autopilot_protocol(
        content.CONTENT_DIR / "autopilot_protocol_v1.json")
    pkg = content.load_autopilot_interaction_package(week, protocol=proto)
    rp = repeat_intent.active_protocol()
    sess = TrainSession(
        session_id=f"hyg-wk{week}", patient_id=f"hyg-wk{week}", week_no=week,
        phase_type="正式训练", event_line="正式训练",
        item_bank_version_id=bank.version_id,
        item_bank_definition_digest=content.item_bank_definition_digest(bank),
        autopilot_protocol_version_id=str(proto.get("protocol_version_id")),
        autopilot_protocol_definition_digest=(
            content.autopilot_protocol_definition_digest(proto)),
        repeat_protocol_version_id=rp.version_id,
        repeat_protocol_definition_digest=rp.definition_digest,
        is_simulation=True, data_classification="simulation")
    positions = autopilot_positions.build_positions(
        bank, week_no=week, event_line="正式训练")
    assert len(positions) == 78, week
    failures = []
    for p in positions:
        try:
            autopilot_service._select_p0a_content(
                sess, bank, proto, item_id=p.item_id, turn_seq=p.turn_seq,
                interaction_package=pkg)
        except autopilot_service.AutopilotServiceError as exc:
            failures.append(f"wk{week} {p.item_id}#{p.turn_seq}: {exc.code} {exc.message}")
    assert not failures, "\n".join(failures)


def test_same_source_cue1_collapse_without_errata_still_fails_closed():
    """豁免只认 errata_fixed 显式记录:抹掉登记,同一塌缩必须立刻被拒。"""
    from dataclasses import replace

    from app import autopilot_positions, autopilot_service, content, repeat_intent
    from app.models import Session as TrainSession

    bank = content.load_item_bank_for_week(4)
    stripped = replace(bank, errata_fixed=[])
    proto = content.load_autopilot_protocol(
        content.CONTENT_DIR / "autopilot_protocol_v1.json")
    pkg = content.load_autopilot_interaction_package(4, protocol=proto)
    rp = repeat_intent.active_protocol()
    sess = TrainSession(
        session_id="hyg-neg", patient_id="hyg-neg", week_no=4,
        phase_type="正式训练", event_line="正式训练",
        item_bank_version_id=stripped.version_id,
        item_bank_definition_digest=content.item_bank_definition_digest(stripped),
        autopilot_protocol_version_id=str(proto.get("protocol_version_id")),
        autopilot_protocol_definition_digest=(
            content.autopilot_protocol_definition_digest(proto)),
        repeat_protocol_version_id=rp.version_id,
        repeat_protocol_definition_digest=rp.definition_digest,
        is_simulation=True, data_classification="simulation")
    position = autopilot_positions.find_position(
        autopilot_positions.build_positions(
            stripped, week_no=4, event_line="正式训练"),
        item_id="SE_花瓶", turn_seq=1)
    with pytest.raises(autopilot_service.AutopilotServiceError) as caught:
        autopilot_service._select_p0a_content(
            sess, stripped, proto,
            item_id=position.item_id, turn_seq=position.turn_seq,
            interaction_package=pkg)
    assert caught.value.code == "autopilot_content_incomplete"
    assert "三分支" in caught.value.message
