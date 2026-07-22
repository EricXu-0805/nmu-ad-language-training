import copy
import json

import pytest

from app import content
from app.content import (
    CONTENT_DIR, autopilot_protocol_definition_digest,
    item_bank_definition_digest, load_autopilot_protocol,
    load_item_bank, load_week1_script,
    content_readiness, operational_rubric_for, unsupported_operational_rubrics,
    validate_autopilot_protocol, validate_item_bank, validate_week1_script, ItemBank,
)


def test_shipped_item_bank_no_errors():
    # 随包题库须【无勘误级错误】；源稿排版缺陷必须以可审计勘误恢复，
    # 不能继续让解析器把完整话术或词项静默丢掉。
    bank = load_item_bank(CONTENT_DIR / "item_bank_v1.json")
    assert bank.version_id == "wk2-v1-20260707"
    assert len(bank.single_element) == 20
    assert len(bank.double_element) == 10
    assert len(bank.multi_element) == 0
    assert bank.supported_training_weeks == (2,)
    assert bank.qc_status == "draft"
    result = validate_item_bank(bank)
    assert result["errors"] == [], f"题库不应有勘误级错误：{result['errors']}"
    assert not any("SE_花" in w and "第1级线索" in w for w in result["warnings"])
    assert bank.meta["source_document_sha256"] == (
        "b3310b61bdc6afb437cbc05785bd6f4e1f6c30dd53ad0999eb2c0fea10c3891a"
    )
    assert bank.meta["source_normalized_text_sha256"] == (
        "b7f2ad1d4389ee6193721402b1d39d9c3cc7a15d2341807471a5fc4627d06c55"
    )
    assert bank.meta["draft_revision"] == "2026-07-20.4"
    assert bank.meta["source_protocol_position_count"] == 80
    assert len(bank.meta["source_unstructured_positions"]) == 10


def test_item_bank_loader_accepts_explicit_multi_image_id(tmp_path):
    definition = json.loads(
        (CONTENT_DIR / "item_bank_v1.json").read_text(encoding="utf-8")
    )
    definition["single_element"][0]["image_id"] = "wk2-multi-01"
    path = tmp_path / "multi-image-id.json"
    path.write_text(json.dumps(definition, ensure_ascii=False), encoding="utf-8")

    bank = load_item_bank(path)

    assert bank.single_element[0]["image_id"] == "wk2-multi-01"


@pytest.mark.parametrize("image_id", ["wk2-31", "wk2-32"])
def test_item_bank_loader_rejects_numeric_multi_remaps(tmp_path, image_id):
    definition = json.loads(
        (CONTENT_DIR / "item_bank_v1.json").read_text(encoding="utf-8")
    )
    definition["single_element"][0]["image_id"] = image_id
    path = tmp_path / f"invalid-{image_id}.json"
    path.write_text(json.dumps(definition, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(content.FrozenContentUnavailable, match="image_id"):
        load_item_bank(path)


def test_complete_definition_digests_are_stable_and_mutation_sensitive():
    bank = load_item_bank(CONTENT_DIR / "item_bank_v1.json")
    protocol = load_autopilot_protocol(
        CONTENT_DIR / "autopilot_protocol_v1.json")

    assert item_bank_definition_digest(copy.deepcopy(bank)) == (
        item_bank_definition_digest(bank))
    assert autopilot_protocol_definition_digest(copy.deepcopy(protocol)) == (
        autopilot_protocol_definition_digest(protocol))

    same_version_bank = copy.deepcopy(bank)
    same_version_bank.single_element[0]["initial_prompt"] += "（同版本漂移）"
    assert same_version_bank.version_id == bank.version_id
    assert item_bank_definition_digest(same_version_bank) != (
        item_bank_definition_digest(bank))

    same_version_protocol = copy.deepcopy(protocol)
    same_version_protocol["naming"]["success_after_cue2"] += "（同版本漂移）"
    assert same_version_protocol["protocol_version_id"] == (
        protocol["protocol_version_id"])
    assert autopilot_protocol_definition_digest(same_version_protocol) != (
        autopilot_protocol_definition_digest(protocol))

    # A top-level field which would contain the definition's own digest is the
    # only excluded value; provenance hashes and nested content remain covered.
    with_self_digest = copy.deepcopy(protocol)
    with_self_digest["autopilot_protocol_definition_digest"] = "f" * 64
    assert autopilot_protocol_definition_digest(with_self_digest) == (
        autopilot_protocol_definition_digest(protocol))


@pytest.mark.parametrize("missing", [
    "protocol_schema_version",
    "protocol_version_id",
    "qc_status",
    "draft_revision",
    "supported_training_weeks",
    "source_document_sha256",
    "source_normalized_text_sha256",
])
def test_autopilot_protocol_metadata_is_a_fail_closed_contract(missing):
    protocol = load_autopilot_protocol(
        CONTENT_DIR / "autopilot_protocol_v1.json")
    protocol.pop(missing)
    assert validate_autopilot_protocol(protocol)


@pytest.mark.parametrize(
    ("field", "bad_value", "expected_issue"),
    (
        ("naming", ["truthy"], "naming 必须是对象"),
        ("naming.success_after_cue1", ["truthy"], "success_after_cue1 必须是对象"),
        ("double", ["truthy"], "double 必须是对象"),
    ),
)
def test_autopilot_protocol_validator_never_trusts_truthy_container_shapes(
        field, bad_value, expected_issue):
    protocol = load_autopilot_protocol(
        CONTENT_DIR / "autopilot_protocol_v1.json")
    if field == "naming.success_after_cue1":
        protocol["naming"]["success_after_cue1"] = bad_value
    else:
        protocol[field] = bad_value

    issues = validate_autopilot_protocol(protocol)
    assert any(expected_issue in issue for issue in issues)


def test_autopilot_protocol_validator_rejects_non_object_without_crashing():
    assert validate_autopilot_protocol(["truthy"]) == ["自动驾驶协议必须是对象"]


def test_shipped_single_terms_and_recovered_flower_cue_are_structurally_clean():
    bank = load_item_bank(CONTENT_DIR / "item_bank_v1.json")
    rows = {row["target_word"]: row for row in bank.single_element}

    assert rows["花"]["cues"]["1"]["text"] == (
        "没关系，您可以再想一想。它是植物的一部分，通常由瓣、萼、托、蕊组成，"
        "有各种颜色，有的很艳丽，也有香味。您觉得它叫什么？"
    )
    assert set(rows["花"]["cues"]["1"]["variants"]) == {
        "unknown", "close", "silence",
    }
    assert rows["花"]["cues"]["1"]["variants"]["unknown"][
        "source_paragraph_index"
    ] == 338
    assert rows["胡萝卜"]["related_but_inaccurate"] == ["蔬菜", "萝卜"]
    assert rows["纽扣"]["related_but_inaccurate"] == ["扣", "衣服", "圆球"]
    assert rows["蜜蜂"]["related_but_inaccurate"] == ["动物", "昆虫"]
    assert rows["螺丝刀"]["related_but_inaccurate"][-2:] == ["拧螺丝的", "把手"]
    assert rows["烟"]["related_but_inaccurate"][-2:] == ["点火的", "细长的"]
    assert not any(
        "“" in value or "”" in value
        for row in bank.single_element
        for value in row.get("related_but_inaccurate", [])
    )


def test_shipped_bank_is_explicitly_demo_only_until_qc_and_multi_complete():
    ready = content_readiness(load_item_bank(CONTENT_DIR / "item_bank_v1.json"))
    assert ready["qc_status"] == "draft"
    assert ready["supported_training_weeks"] == [2]
    assert ready["ready_for_research"] is False
    assert any("多要素" in w for w in ready["warnings"])
    assert ready["operational_autopilot_ready"] is False
    assert any("关系识别" in position
               for position in ready["unsupported_operational_rubrics"])
    assert any("禁止以‘有回答’自动推进" in warning
               for warning in ready["warnings"])


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


def test_item_bank_validator_rejects_pure_whitespace_operational_strings():
    bank = copy.deepcopy(load_item_bank(CONTENT_DIR / "item_bank_v1.json"))
    bank.single_element[0]["target_word"] = " "
    bank.single_element[0]["tell_answer"] = "\t\n"
    bank.single_element[0]["acceptable_expressions"] = ["  "]

    errors = validate_item_bank(bank)["errors"]
    assert any("single_element.0.target_word" in issue for issue in errors)
    assert any("single_element.0.tell_answer" in issue for issue in errors)
    assert any(
        "single_element.0.acceptable_expressions.0" in issue
        for issue in errors
    )


def test_validator_catches_double_word_mismatch():
    bad = ItemBank(
        version_id="x", single_element=[],
        double_element=[{"item_id": "DE_斧子+树", "pair_title": "斧子+树",
                         "left_word": "斧子", "right_word": "衬衫"}],
    )
    issues = validate_item_bank(bad)["errors"]
    assert any("right_word" in i and "斧子+树" in i for i in issues)


def test_validator_rejects_related_term_quote_fragments_as_extraction_errors():
    bad = ItemBank(
        version_id="x",
        single_element=[{
            "item_id": "SE_胡萝卜",
            "target_word": "胡萝卜",
            "success_line": "对了",
            "tell_answer": "这个物品叫胡萝卜。",
            "related_but_inaccurate": ["蔬菜”“萝卜"],
            "cues": {"1": {"text": "a"}, "2": {"text": "b"}},
        }],
        double_element=[],
    )
    issues = validate_item_bank(bad)["errors"]
    assert any("related_but_inaccurate 含解析引号残片" in issue for issue in issues)


def test_open_answer_roles_require_versioned_operational_rubrics():
    base = {
        "item_id": "DE_杯子+水",
        "pair_title": "杯子+水",
        "left_word": "杯子",
        "right_word": "水",
    }
    bank = ItemBank(
        version_id="rubric-test",
        single_element=[],
        double_element=[base],
        meta={"supported_training_weeks": [2], "qc_status": "frozen"},
    )
    assert unsupported_operational_rubrics(bank) == (
        "DE_杯子+水:左作用", "DE_杯子+水:右作用", "DE_杯子+水:关系识别",
    )

    complete = {
        **base,
        "operational_rubrics": {
            role: {
                "rubric_version": "clinical-v1",
                "decision_policy": "any_acceptable_expression",
                "acceptable_expressions": [answer],
                "cues": {"1": "轻提示", "2": "明确提示"},
                "tell_answer": f"参考回答：{answer}",
            }
            for role, answer in {
                "左作用": "用来喝水",
                "右作用": "可以喝",
                "关系识别": "用杯子喝水",
            }.items()
        },
    }
    ready_bank = ItemBank(
        version_id="rubric-test",
        single_element=[],
        double_element=[complete],
        meta={"supported_training_weeks": [2], "qc_status": "frozen"},
    )
    assert unsupported_operational_rubrics(ready_bank) == ()


def test_orphan_operational_rubric_cannot_invent_an_executable_response_role():
    rubric = {
        "rubric_version": "clinical-v1",
        "decision_policy": "any_acceptable_expression",
        "acceptable_expressions": ["任意回答"],
        "cues": {"1": "轻提示", "2": "明确提示"},
        "tell_answer": "参考回答",
    }
    item = {
        "item_id": "DE_杯子+水",
        "pair_title": "杯子+水",
        "left_word": "杯子",
        "right_word": "水",
        "operational_rubrics": {"后台自定义角色": rubric},
    }
    bank = ItemBank(
        version_id="orphan-rubric-test",
        single_element=[],
        double_element=[item],
        meta={"supported_training_weeks": [2], "qc_status": "draft"},
    )

    assert operational_rubric_for(
        bank, "DE_杯子+水", "后台自定义角色") is None
    errors = validate_item_bank(bank)["errors"]
    assert any(
        "operational_rubrics 含非冻结计划回答角色" in issue
        and "后台自定义角色" in issue
        for issue in errors
    )


def test_week1_script_loads_and_valid():
    s = load_week1_script(CONTENT_DIR / "week1_script.json")
    assert s["phase_type"] == "关系建立"
    assert len(s["zodiac_closed_list"]) == 12
    assert [section["key"] for section in s["sections"]] == [
        "认识机器人", "自我介绍", "介绍机构环境", "道别",
    ]
    assert validate_week1_script(s) == []


def test_week1_section_keys_are_globally_unique_in_validator_and_loader(
        tmp_path):
    script = load_week1_script(CONTENT_DIR / "week1_script.json")
    script["sections"][2]["key"] = script["sections"][1]["key"]

    assert "sections.key 必须全局唯一" in validate_week1_script(script)
    path = _write_definition(tmp_path, "week1_script.json", script)
    with pytest.raises(ValueError, match="结构不合法"):
        load_week1_script(path)


@pytest.mark.parametrize(
    ("script", "expected_issue"),
    (
        (["truthy"], "第一周脚本必须是对象"),
        ({"zodiac_closed_list": "鼠牛虎兔龙蛇马羊猴鸡狗猪", "sections": []},
         "zodiac_closed_list 必须是数组"),
        ({"zodiac_closed_list": ["鼠"] * 12, "sections": "truthy"},
         "sections 必须是非空数组"),
    ),
)
def test_week1_validator_rejects_type_confusion_without_crashing(
        script, expected_issue):
    assert any(
        expected_issue in issue for issue in validate_week1_script(script)
    )


def test_missing_version_id_rejected(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"single_element": []}', encoding="utf-8")
    import pytest
    with pytest.raises(ValueError):
        load_item_bank(p)


_FROZEN_JSON_LOADERS = (
    (load_item_bank, "item_bank_version_id"),
    (load_week1_script, "script_version_id"),
    (load_autopilot_protocol, "protocol_version_id"),
)


@pytest.mark.parametrize(
    ("loader", "version_key"),
    _FROZEN_JSON_LOADERS,
    ids=("item-bank", "week1-script", "autopilot-protocol"),
)
@pytest.mark.parametrize("duplicate_scope", ("root", "nested"))
def test_frozen_json_loaders_reject_duplicate_keys_at_any_depth(
        tmp_path, loader, version_key, duplicate_scope):
    path = tmp_path / f"{loader.__name__}-{duplicate_scope}.json"
    if duplicate_scope == "root":
        raw = f'{{"{version_key}":"v1","{version_key}":"v2"}}'
    else:
        raw = f'{{"{version_key}":"v1","nested":{{"key":1,"key":2}}}}'
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(ValueError, match="重复键"):
        loader(path)


@pytest.mark.parametrize(
    ("loader", "version_key"),
    _FROZEN_JSON_LOADERS,
    ids=("item-bank", "week1-script", "autopilot-protocol"),
)
@pytest.mark.parametrize(
    "non_finite_token",
    ("NaN", "Infinity", "-Infinity", "1e400", "-1e400"),
)
def test_frozen_json_loaders_reject_non_finite_numbers(
        tmp_path, loader, version_key, non_finite_token):
    path = tmp_path / f"{loader.__name__}-non-finite.json"
    path.write_text(
        f'{{"{version_key}":"v1","nested":{{"value":{non_finite_token}}}}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="非有限数"):
        loader(path)


@pytest.mark.parametrize(
    ("loader", "_version_key"),
    _FROZEN_JSON_LOADERS,
    ids=("item-bank", "week1-script", "autopilot-protocol"),
)
@pytest.mark.parametrize("root", ("[]", "null", '"string"', "42", "true"))
def test_frozen_json_loaders_require_object_root(
        tmp_path, loader, _version_key, root):
    path = tmp_path / f"{loader.__name__}-root.json"
    path.write_text(root, encoding="utf-8")

    with pytest.raises(ValueError, match="JSON 根必须是对象"):
        loader(path)


@pytest.mark.parametrize(
    ("loader", "version_key"),
    _FROZEN_JSON_LOADERS,
    ids=("item-bank", "week1-script", "autopilot-protocol"),
)
def test_frozen_json_loaders_normalize_excessive_nesting_to_value_error(
        tmp_path, loader, version_key):
    path = tmp_path / f"{loader.__name__}-too-deep.json"
    # Decoder depth differs by Python release (3.12 fails much earlier than
    # local 3.14), so use a depth that exercises RecursionError on both.
    depth = 200_000
    path.write_text(
        f'{{"{version_key}":"v1","nested":' + '{"level":' * depth
        + "0" + "}" * depth + "}",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="JSON 嵌套过深"):
        loader(path)


def _write_definition(tmp_path, name, definition):
    path = tmp_path / name
    path.write_text(
        json.dumps(definition, ensure_ascii=False), encoding="utf-8"
    )
    return path


@pytest.mark.parametrize(
    ("filename", "loader"),
    (
        ("item_bank_v1.json", load_item_bank),
        ("week1_script.json", load_week1_script),
        ("autopilot_protocol_v1.json", load_autopilot_protocol),
    ),
)
def test_shipped_frozen_definitions_remain_strict_schema_compatible(
        filename, loader):
    # Full schema validation is observational: it must not rewrite the frozen
    # source or replace its digest input with a normalized model dump.
    path = CONTENT_DIR / filename
    before = path.read_bytes()
    loaded = loader(path)
    assert loaded
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "digest_fields",
    (
        ("definition_digest", "item_bank_definition_digest"),
        (
            "definition_digest",
            "protocol_definition_digest",
            "autopilot_protocol_definition_digest",
        ),
    ),
    ids=("item-bank", "autopilot-protocol"),
)
def test_present_self_digest_aliases_must_all_match_runtime_identity(
        tmp_path, digest_fields):
    if "item_bank_definition_digest" in digest_fields:
        filename = "item_bank_v1.json"
        loader = load_item_bank
        original = loader(CONTENT_DIR / filename)
        expected = item_bank_definition_digest(original)
    else:
        filename = "autopilot_protocol_v1.json"
        loader = load_autopilot_protocol
        original = loader(CONTENT_DIR / filename)
        expected = autopilot_protocol_definition_digest(original)

    definition = json.loads((CONTENT_DIR / filename).read_text(encoding="utf-8"))
    for field_name in digest_fields:
        definition[field_name] = expected
    loaded = loader(_write_definition(tmp_path, filename, definition))
    stored = loaded.meta if isinstance(loaded, ItemBank) else loaded
    assert all(stored[field_name] == expected for field_name in digest_fields)


@pytest.mark.parametrize(
    ("filename", "loader", "digest_field"),
    (
        ("item_bank_v1.json", load_item_bank, "definition_digest"),
        ("item_bank_v1.json", load_item_bank, "item_bank_definition_digest"),
        ("autopilot_protocol_v1.json", load_autopilot_protocol, "definition_digest"),
        (
            "autopilot_protocol_v1.json",
            load_autopilot_protocol,
            "protocol_definition_digest",
        ),
        (
            "autopilot_protocol_v1.json",
            load_autopilot_protocol,
            "autopilot_protocol_definition_digest",
        ),
    ),
)
def test_wrong_or_null_declared_self_digest_is_rejected(
        tmp_path, filename, loader, digest_field):
    definition = json.loads((CONTENT_DIR / filename).read_text(encoding="utf-8"))
    definition[digest_field] = "0" * 64
    with pytest.raises(ValueError, match="与运行时定义摘要不一致"):
        loader(_write_definition(tmp_path, filename, definition))

    definition[digest_field] = None
    with pytest.raises(ValueError):
        loader(_write_definition(tmp_path, filename, definition))


def test_absent_and_explicit_empty_multi_element_share_runtime_identity(
        tmp_path):
    source = json.loads(
        (CONTENT_DIR / "item_bank_v1.json").read_text(encoding="utf-8")
    )
    assert "multi_element" not in source
    absent = load_item_bank(CONTENT_DIR / "item_bank_v1.json")
    expected = item_bank_definition_digest(absent)

    explicit = copy.deepcopy(source)
    explicit["multi_element"] = []
    explicit["item_bank_definition_digest"] = expected
    loaded = load_item_bank(
        _write_definition(tmp_path, "item_bank_v1.json", explicit)
    )
    assert item_bank_definition_digest(loaded) == expected


@pytest.mark.parametrize(
    ("filename", "loader", "mutate"),
    (
        (
            "item_bank_v1.json",
            load_item_bank,
            lambda value: value["single_element"][0]
            .__setitem__("target_word", " \t"),
        ),
        (
            "item_bank_v1.json",
            load_item_bank,
            lambda value: value["single_element"][0]
            .__setitem__("tell_answer", "\n"),
        ),
        (
            "item_bank_v1.json",
            load_item_bank,
            lambda value: value["single_element"][0]
            .__setitem__("acceptable_expressions", [" "]),
        ),
        (
            "week1_script.json",
            load_week1_script,
            lambda value: value["sections"][1]["questions"][0]
            .__setitem__("success", "  "),
        ),
        (
            "week1_script.json",
            load_week1_script,
            lambda value: value["zodiac_closed_list"].__setitem__(0, " "),
        ),
        (
            "autopilot_protocol_v1.json",
            load_autopilot_protocol,
            lambda value: value["naming"]
            .__setitem__("success_after_cue2", "\t"),
        ),
        (
            "autopilot_protocol_v1.json",
            load_autopilot_protocol,
            lambda value: value["notes"].__setitem__(0, "\n"),
        ),
    ),
    ids=(
        "item-scalar",
        "item-nested-answer",
        "item-string-list",
        "week1-optional-string",
        "week1-string-list",
        "protocol-nested-dict-value",
        "protocol-string-list",
    ),
)
def test_frozen_schema_rejects_whitespace_only_strings_at_every_depth(
        tmp_path, filename, loader, mutate):
    definition = json.loads((CONTENT_DIR / filename).read_text(encoding="utf-8"))
    mutate(definition)
    path = _write_definition(tmp_path, filename, definition)

    with pytest.raises(ValueError, match="结构不合法"):
        loader(path)


def test_schema_checks_whitespace_without_stripping_digest_input(tmp_path):
    definition = json.loads(
        (CONTENT_DIR / "item_bank_v1.json").read_text(encoding="utf-8")
    )
    definition["note"] = "  有效的冻结说明文本  "
    path = _write_definition(tmp_path, "item_bank_v1.json", definition)

    bank = load_item_bank(path)
    assert bank.meta["note"] == "  有效的冻结说明文本  "


@pytest.mark.parametrize(
    ("filename", "loader", "mutate"),
    (
        (
            "item_bank_v1.json",
            load_item_bank,
            lambda value: value["single_element"][0]["cues"]["1"]
            ["variants"]["unknown"].__setitem__("text", ["truthy"]),
        ),
        (
            "item_bank_v1.json",
            load_item_bank,
            lambda value: value["single_element"][0]["cues"]["2"]
            .__setitem__("unapproved", "extra"),
        ),
        (
            "week1_script.json",
            load_week1_script,
            lambda value: value.__setitem__(
                "zodiac_closed_list", "鼠牛虎兔龙蛇马羊猴鸡狗猪"
            ),
        ),
        (
            "week1_script.json",
            load_week1_script,
            lambda value: value.__setitem__("sections", "truthy sections"),
        ),
        (
            "week1_script.json",
            load_week1_script,
            lambda value: value["sections"][1]["questions"][0]
            .__setitem__("patient_name", "extra"),
        ),
        (
            "autopilot_protocol_v1.json",
            load_autopilot_protocol,
            lambda value: value.__setitem__("naming", ["truthy naming"]),
        ),
        (
            "autopilot_protocol_v1.json",
            load_autopilot_protocol,
            lambda value: value["naming"]["success_after_cue1"]
            .__setitem__("invented_branch", "extra"),
        ),
    ),
    ids=(
        "item-nested-type",
        "item-nested-extra",
        "week1-zodiac-string",
        "week1-sections-string",
        "week1-question-extra",
        "protocol-naming-list",
        "protocol-nested-extra",
    ),
)
def test_frozen_schema_rejects_nested_type_confusion_and_extra_fields(
        tmp_path, filename, loader, mutate):
    definition = json.loads((CONTENT_DIR / filename).read_text(encoding="utf-8"))
    mutate(definition)
    path = _write_definition(tmp_path, filename, definition)

    with pytest.raises(ValueError, match="结构不合法"):
        loader(path)
