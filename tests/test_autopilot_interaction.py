"""交互协议数据包（autopilot-interaction.v1）契约测试。

覆盖四层：字节 sha 链（协议钉→数据包文件）、schema/语义闸、TTS 白名单闭集
纳入全部新话术、verbatim 话术逐字来自源 docx（源文件不在仓库内，CI 上跳过，
本机构建后必须跑过）。
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from app import content
from app.autopilot_plan_profiles import (
    WEEK2_SINGLE20_DEMO_DIGEST, WEEK2_SINGLE20_DEMO_VERSION,
)
from app.content import CONTENT_DIR

WEEKS = (2, 3, 4, 5, 6, 7, 8)
PROTOCOL_PATH = CONTENT_DIR / "autopilot_protocol_v1.json"
SOURCE_DIR = Path(__file__).resolve().parents[2] / "20260629"

PROTOCOL = content.load_autopilot_protocol(PROTOCOL_PATH)


def _package(week_no: int) -> dict:
    return content.load_autopilot_interaction_package(week_no, protocol=PROTOCOL)


def _bank(week_no: int) -> content.ItemBank:
    return content.load_item_bank_for_week(week_no)


def _spoken_texts(package: dict, bank: content.ItemBank) -> list[str]:
    by_id = {it["item_id"]: it
             for rows in (bank.double_element, bank.multi_element)
             for it in rows}
    texts: list[str] = []
    for item in package["items"]:
        for turn in item["turns"]:
            texts.append(turn["question"]["text"])
            branch_rows = list(turn["branches"].values())
            branch_rows += list((turn.get("after_rerecord") or {}).values())
            for branch in branch_rows:
                speech = branch.get("speech")
                if speech is None:
                    continue
                resolved = content.interaction_speech_text(
                    speech, by_id[item["item_id"]], PROTOCOL)
                assert resolved, (item["item_id"], turn["turn_seq"], speech)
                texts.append(resolved)
    return texts


@pytest.mark.parametrize("week_no", WEEKS)
def test_shipped_interaction_packages_load_and_bind_to_banks(week_no):
    bank = _bank(week_no)
    package = _package(week_no)
    assert content.validate_autopilot_interaction_package(
        package, bank, PROTOCOL) == []
    # 58 缺口位置全覆盖：10 双要素 × 5 环节 + 2 多要素 × 4 关键要素。
    assert sum(len(it["turns"]) for it in package["items"]) == 58
    assert len([it for it in package["items"]
                if it["task_type"] == "双要素"]) == 10
    assert len([it for it in package["items"]
                if it["task_type"] == "多要素"]) == 2
    assert package["qc_status"] == "draft"
    assert package["silence_seconds"] == 10
    assert package["structure_freezes"]


@pytest.mark.parametrize("week_no", WEEKS)
def test_partial_branch_exists_iff_multi_concept_policy(week_no):
    # 直接对着题库冻结 rubric 断言（不经 validate 自证）：partial 分支
    # 只在多概念判定策略下存在，否则引擎产不出该类别、分支即死代码。
    bank = _bank(week_no)
    package = _package(week_no)
    rubrics = {it["item_id"]: it.get("operational_rubrics") or {}
               for it in bank.multi_element}
    checked = 0
    for item in package["items"]:
        if item["task_type"] != "多要素":
            assert all("partial" not in turn["branches"]
                       for turn in item["turns"])
            continue
        for turn in item["turns"]:
            policy = rubrics[item["item_id"]][
                turn["response_role"]]["decision_policy"]
            multi_concept = policy in {
                "all_required_concepts", "all_concept_groups", "hybrid"}
            assert ("partial" in turn["branches"]) is multi_concept, (
                item["item_id"], turn["response_role"], policy)
            checked += 1
    assert checked == 8


def test_interaction_package_byte_sha_chain_is_fail_closed(tmp_path):
    file_name = PROTOCOL["interaction_packages"]["2"]["file"]
    shutil.copy(CONTENT_DIR / file_name, tmp_path / file_name)
    raw = (tmp_path / file_name).read_bytes()
    # 改一个字节（仍是合法 JSON）：qc_status draft -> Draft
    tampered = raw.replace(b'"draft"', b'"Draft"', 1)
    assert tampered != raw
    (tmp_path / file_name).write_bytes(tampered)
    with pytest.raises(content.FrozenContentUnavailable, match="字节摘要"):
        content.load_autopilot_interaction_package(
            2, content_dir=tmp_path, protocol=PROTOCOL)
    # 未登记周次同样 fail-closed。
    protocol_without = copy.deepcopy(PROTOCOL)
    del protocol_without["interaction_packages"]["2"]
    (tmp_path / file_name).write_bytes(raw)
    with pytest.raises(content.FrozenContentUnavailable, match="未在协议登记"):
        content.load_autopilot_interaction_package(
            2, content_dir=tmp_path, protocol=protocol_without)


def _reload_with_edit(tmp_path, week_no: int, mutate) -> dict:
    """字节钉重算后装载被改动的数据包：专门用来证明内层闸不是被 sha 闸遮住。"""
    file_name = PROTOCOL["interaction_packages"][str(week_no)]["file"]
    package = json.loads((CONTENT_DIR / file_name).read_text(encoding="utf-8"))
    mutate(package)
    raw = (json.dumps(package, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    (tmp_path / file_name).write_bytes(raw)
    protocol = copy.deepcopy(PROTOCOL)
    protocol["interaction_packages"][str(week_no)]["sha256"] = (
        hashlib.sha256(raw).hexdigest())
    return content.load_autopilot_interaction_package(
        week_no, content_dir=tmp_path, protocol=protocol)


def test_schema_rejects_branch_set_and_ladder_tampering(tmp_path):
    def drop_silence(package):
        del package["items"][0]["turns"][0]["branches"]["silence"]

    def rerecord_without_after(package):
        turn = package["items"][0]["turns"][0]
        turn["branches"]["wrong"]["kind"] = "speak_then_rerecord"

    for mutate, expected in (
        (drop_silence, "branches must be full/wrong/silence"),
        (rerecord_without_after, "after_rerecord must exist"),
    ):
        # 装载链整体 fail-closed（字节钉重算后仍被 schema 闸拒收）……
        with pytest.raises(content.FrozenContentUnavailable):
            _reload_with_edit(tmp_path, 2, mutate)
        # ……并且拒收的正是这条结构规则（loader 的错误摘要不携带规则文本）。
        file_name = PROTOCOL["interaction_packages"]["2"]["file"]
        package = json.loads(
            (CONTENT_DIR / file_name).read_text(encoding="utf-8"))
        mutate(package)
        with pytest.raises(ValueError, match=expected):
            content._AutopilotInteractionSchema.model_validate(  # noqa: SLF001
                package)


def test_semantic_gate_rejects_partial_on_single_concept_rubric(tmp_path):
    def add_partial_to_double(package):
        turn = package["items"][0]["turns"][0]
        turn["branches"]["partial"] = copy.deepcopy(turn["branches"]["wrong"])

    loaded = _reload_with_edit(tmp_path, 2, add_partial_to_double)
    issues = content.validate_autopilot_interaction_package(
        loaded, _bank(2), PROTOCOL)
    assert any("partial" in issue for issue in issues)


@pytest.mark.parametrize("week_no", WEEKS)
def test_every_interaction_line_is_in_the_tts_allowlist(week_no):
    bank = _bank(week_no)
    package = _package(week_no)
    with_package = content.tts_allowlist(
        bank, autopilot_protocol=PROTOCOL, interaction_package=package)
    without_package = content.tts_allowlist(bank, autopilot_protocol=PROTOCOL)
    texts = _spoken_texts(package, bank)
    for text in texts:
        assert text.strip() in with_package, (week_no, text)
    # 阴性对照：问句只存在于数据包里——不传数据包时白名单必须缺它们，
    # 证明上面的断言不是在别的来源上空转。
    questions = {it_turn["question"]["text"].strip()
                 for item in package["items"] for it_turn in item["turns"]}
    assert questions - without_package, "数据包问句不应已在旧白名单里"
    assert with_package > without_package


@pytest.mark.skipif(not SOURCE_DIR.exists(),
                    reason="源 docx 不在仓库内（CI 环境无 20260629/）")
@pytest.mark.parametrize("week_no", WEEKS)
def test_verbatim_lines_are_substrings_of_source_docx(week_no):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from build_week_item_banks import (  # noqa: E402
        WEEK_SOURCES, normalized_text_sha256, paragraphs,
    )
    package = _package(week_no)
    source_path = SOURCE_DIR / WEEK_SOURCES[week_no]
    assert package["source_document_sha256"] == hashlib.sha256(
        source_path.read_bytes()).hexdigest()
    paras = paragraphs(source_path)
    assert package["source_normalized_text_sha256"] == (
        normalized_text_sha256(paras))
    joined = "\n".join(paras)
    checked = 0
    for item in package["items"]:
        for turn in item["turns"]:
            verbatims = [turn["question"]]
            branch_rows = list(turn["branches"].values())
            branch_rows += list((turn.get("after_rerecord") or {}).values())
            verbatims += [b["speech"] for b in branch_rows
                          if b.get("speech", {}).get("source") == "verbatim"]
            for speech in verbatims:
                text = speech["text"]
                assert text in joined, (item["item_id"], turn["turn_seq"], text)
                # 段落索引不是装饰：句子必须真在它声明的那一段里。
                assert text in paras[speech["source_paragraph_index"]]
                checked += 1
    assert checked > 58


def _load_builder():
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import build_autopilot_interaction_packages as builder
    return builder


def _source_dedicated_namefix_sentences(week_no: int) -> list[dict[str, tuple[int, str]]]:
    """源稿专属命名纠正句，独立于构建器新字段重述一遍（NAMEFIX_RE 抽取范式）。

    独立重述而不是引用构建器内部状态：数据包是磁盘上的冻结工件，这里对照的
    是「包里的字节 vs 源 docx」，构建器自身的缺陷不能靠对照自己被遮住。
    返回按双要素题面顺序的 [{side: (paragraph_index, 句子)}]。
    """
    import re

    from build_week_item_banks import (  # noqa: E402
        DOUBLE_GROUP_RE, DOUBLE_TITLE_RE, MULTI_GROUP_RE, NAMEFIX_RE,
        WEEK_SOURCES, first_quote, paragraphs, _strip_outer_quotes,
    )
    paras = paragraphs(SOURCE_DIR / WEEK_SOURCES[week_no])
    double_start = [i for i, line in enumerate(paras)
                    if DOUBLE_GROUP_RE.match(line)]
    multi_start = [i for i, line in enumerate(paras)
                   if MULTI_GROUP_RE.match(line)]
    titles = [i for i in range(double_start[0], multi_start[0])
              if DOUBLE_TITLE_RE.match(paras[i])]
    out: list[dict[str, tuple[int, str]]] = []
    for n, i in enumerate(titles):
        j = titles[n + 1] if n + 1 < len(titles) else multi_start[0]
        side: str | None = None
        fixes: dict[str, tuple[int, str]] = {}
        for k in range(i, j):
            line = paras[k]
            if re.search(r"左边的\S{0,4}是什么", line):
                side = "left"
            elif re.search(r"右边的\S{0,4}是什么", line):
                side = "right"
            elif "有什么关系" in line:
                side = None
            quote = first_quote(line)
            if (quote and side is not None and side not in fixes
                    and NAMEFIX_RE.search(quote)
                    and ("回答错误" in line or "刚刚" in line)):
                fixes[side] = (k, _strip_outer_quotes(quote))
        out.append(fixes)
    return out


@pytest.mark.skipif(not SOURCE_DIR.exists(),
                    reason="源 docx 不在仓库内（CI 环境无 20260629/）")
def test_wk7_bird_left_namefix_is_the_source_dedicated_sentence():
    """wk7 DE_鸟+毛毛虫 左命名纠正句必须是源稿专属句「…它是一只鸟。」整句。

    协议模板展开只能给出「它叫鸟」；源稿这题写的是「它是一只鸟」。
    构建器必须先找源稿专属句，找到就 verbatim 入包。
    """
    builder = _load_builder()
    bank = _bank(7)
    package, problems = builder.build_package(7, builder.load_curation(), bank)
    assert problems == []
    item = next(it for it in package["items"]
                if it["item_id"] == "DE_鸟+毛毛虫")
    turn = next(t for t in item["turns"] if t["response_role"] == "左命名")
    bank_item = next(it for it in bank.double_element
                     if it["item_id"] == "DE_鸟+毛毛虫")
    for key in ("wrong", "silence"):
        spoken = content.interaction_speech_text(
            turn["branches"][key]["speech"], bank_item, PROTOCOL)
        assert spoken == "这个我们刚刚见过，它是一只鸟。", (key, spoken)


@pytest.mark.skipif(not SOURCE_DIR.exists(),
                    reason="源 docx 不在仓库内（CI 环境无 20260629/）")
@pytest.mark.parametrize("week_no", WEEKS)
def test_every_double_namefix_line_matches_source_dedicated_sentence(week_no):
    """已出厂数据包的每个双要素命名纠正句逐字对照源稿。

    三种许可形态，逐处判定，别的都不行：
      1. 实际播报句 == 源稿该题专属纠正句（verbatim 或恰好逐字同形的模板展开）；
      2. 源稿该题确无专属句 → 才允许模板展开，且必须列入 structure_freezes；
      3. 播报句与源稿专属句的差异恰为题库登记的 {side}_word 勘误（模板展开
         携带勘误词）→ 允许，且必须逐处列入 structure_freezes（勘误本身仍
         待书面批准，不得写成已批准）。
    """
    bank = _bank(week_no)
    package = _package(week_no)
    fixes_by_slot = _source_dedicated_namefix_sentences(week_no)
    doubles = [it for it in package["items"] if it["task_type"] == "双要素"]
    assert len(doubles) == len(fixes_by_slot) == 10
    freezes = "\n".join(package["structure_freezes"])
    checked = 0
    for slot, item in enumerate(doubles):
        bank_item = next(b for b in bank.double_element
                         if b["item_id"] == item["item_id"])
        for side, role in (("left", "左命名"), ("right", "右命名")):
            turn = next(t for t in item["turns"]
                        if t["response_role"] == role)
            speeches = [turn["branches"][k]["speech"]
                        for k in ("wrong", "silence")]
            assert speeches[0] == speeches[1], (item["item_id"], role)
            spoken = content.interaction_speech_text(
                speeches[0], bank_item, PROTOCOL)
            assert spoken, (item["item_id"], role)
            where = f"{item['item_id']}#{turn['turn_seq']}"
            dedicated = fixes_by_slot[slot].get(side)
            if dedicated is None:
                # 形态 2：源稿确无专属句，模板要挂号才许用。
                assert speeches[0]["source"] == "protocol_namefix", where
                assert where in freezes, where
                continue
            idx, sentence = dedicated
            if spoken == sentence:
                if speeches[0]["source"] == "verbatim":
                    assert speeches[0]["source_paragraph_index"] == idx
                checked += 1
                continue
            # 形态 3：仅题库登记的 {side}_word 勘误可解释的偏离。
            entries = [e for e in bank.errata_fixed
                       if e.get("field") == f"{side}_word"
                       and e.get("item") in (
                           item["item_id"],
                           item["item_id"].removeprefix("DE_"))]
            assert entries, (where, spoken, sentence)
            entry = entries[0]
            assert sentence.replace(
                entry["source_value"], entry["corrected_to"]) == spoken, where
            assert speeches[0]["source"] == "protocol_namefix", where
            assert where in freezes, where
            checked += 1
    # 本套源稿每题左右两个专属句都在（缺句走形态 2 不计数）；若未来源稿
    # 真缺句，这条下限逼着补形态 2 的挂号断言而不是静默放过。
    assert checked >= 18


def test_protocol_v2_identity_registration_and_pin_chain():
    assert PROTOCOL["protocol_version_id"] == "autopilot-v2-20260821"
    assert PROTOCOL["supported_training_weeks"] == [2, 3, 4, 5, 6, 7, 8]
    packages = PROTOCOL["interaction_packages"]
    assert sorted(packages) == [str(w) for w in WEEKS]
    for week_key, entry in packages.items():
        raw = (CONTENT_DIR / entry["file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"], week_key
    # v1 冻结话术字段原样保留（字节钉）。
    assert PROTOCOL["silence_seconds"] == 10
    assert PROTOCOL["double"]["namefix_left"] == "这个我们刚刚见过，它叫【物品名】。"
    assert PROTOCOL["naming"]["success_after_cue2"] == (
        "很好，经过提示您说出来了，这就是【物品名】。我们继续下一个。")
    # 现协议钉链：协议 digest → 演示 profile parent → registry/前端字面量。
    # （test_visit_plans 的 LEGACY_PROTOCOL_* 是历史账本收据，不随现协议动。）
    protocol_digest = content.autopilot_protocol_definition_digest(PROTOCOL)
    profile = json.loads(
        (CONTENT_DIR / "autopilot_demo_profiles_v1.json").read_text(
            encoding="utf-8"))
    assert profile["parent_autopilot_protocol_version_id"] == (
        PROTOCOL["protocol_version_id"])
    assert profile["parent_autopilot_protocol_definition_digest"] == (
        protocol_digest)
    assert profile["profile_version_id"] == WEEK2_SINGLE20_DEMO_VERSION
    assert profile["profile_definition_digest"] == WEEK2_SINGLE20_DEMO_DIGEST
