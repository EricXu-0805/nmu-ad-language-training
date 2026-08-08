"""正式量表定义包生产装载(收据 150 S1):摘要重算/声明式计分/启动安装。"""
import json

import pytest

from app import assessment_bundles, assessment_definitions, content

_REAL_CONTENT_DIR = content.CONTENT_DIR


@pytest.fixture(autouse=True)
def _isolate_registry():
    with assessment_definitions._REGISTRY_LOCK:
        saved = dict(assessment_definitions._BUNDLES)
        saved_active = assessment_definitions._ACTIVE_BUNDLE_ID
        assessment_definitions._BUNDLES.clear()
        assessment_definitions._ACTIVE_BUNDLE_ID = None
    yield
    with assessment_definitions._REGISTRY_LOCK:
        assessment_definitions._BUNDLES.clear()
        assessment_definitions._BUNDLES.update(saved)
        assessment_definitions._ACTIVE_BUNDLE_ID = saved_active


def _definition(category: str, *, items=None, word_prefix: str | None = None):
    keys = items or [f"{category[:4]}_{i:02d}" for i in (1, 2, 3)]
    return {
        "definition_id": f"{category}-def-v1",
        "category_key": category,
        "instrument_id": f"{category}-instrument",
        "instrument_version": "v1",
        "administration_protocol": {"mode": "researcher_administered"},
        "response_schema": {
            "kind": "bounded_numeric", "minimum": 0, "maximum": 2,
            "allowed_values": [0, 1, 2],
        },
        "missingness_rule": {"kind": "all_items_required"},
        "stopping_rule": {"kind": "none"},
        "scoring": {
            "algorithm_id": "declarative_item_sum",
            "algorithm_version": "v1",
            "kind": "item_sum",
            "rounding": "integer_exact",
        },
        "score_min": 0,
        "score_max": 6,
        "score_direction": "higher_is_better",
        "permissions": {
            "automatic_scoring_permitted": True,
            "item_response_storage_permitted": True,
            "result_storage_permitted": True,
            "result_export_permitted": False,
        },
        "items": [
            {"item_key": key,
             **({"word": f"{word_prefix}{i}"} if word_prefix else {})}
            for i, key in enumerate(keys)
        ],
    }


def _package(bundle_id: str = "formal-two-outcomes-v1", *, approved=False):
    return {
        "schema_version": "assessment-definition-bundle-package.v1",
        "bundle_id": bundle_id,
        "formal_research_approved": approved,
        "definitions": [
            _definition("untrained_standardized_naming", word_prefix="词"),
            _definition("functional_communication"),
        ],
    }


def _stage(tmp_path, packages: dict[str, dict], active: str):
    import shutil

    # 启动安装链含「量表一与训练词表不相交」检查(收据 150 S4),该检查对
    # 训练索引 fail-closed;临时内容目录须带真实训练索引+题库。
    for name in (content.ITEM_BANK_INDEX_FILE, "item_bank_v1.json"):
        if not (tmp_path / name).exists():
            shutil.copy(_REAL_CONTENT_DIR / name, tmp_path)
    import hashlib

    base = tmp_path / assessment_bundles.ASSESSMENT_BUNDLE_DIR
    base.mkdir(parents=True)
    entries = []
    for file_name, data in packages.items():
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        (base / file_name).write_bytes(raw)
        entries.append({
            "bundle_id": data["bundle_id"],
            "file": file_name,
            "content_sha256": hashlib.sha256(raw).hexdigest(),
        })
    (base / assessment_bundles.ASSESSMENT_BUNDLE_INDEX_FILE).write_text(
        json.dumps({
            "schema_version": "assessment-bundle-index.v1",
            "active_bundle_id": active,
            "bundles": entries,
        }, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_compile_recomputes_every_digest_and_passes_registry_validation():
    bundle = assessment_bundles.compile_bundle_package(_package())
    snapshot = bundle.snapshot
    assessment_definitions.validate_registered_bundle(bundle)
    assert snapshot.formal_research_approved is False
    for definition in snapshot.definitions:
        assert definition.definition_digest.startswith("sha256:")
        assert definition.scoring_algorithm_id == "declarative_item_sum"
    # 同内容重编译=同摘要(内容寻址,与声称无关)。
    again = assessment_bundles.compile_bundle_package(_package())
    assert again.snapshot.bundle_digest == snapshot.bundle_digest


def test_expected_digest_declaration_must_match_recomputation():
    data = _package()
    good = assessment_bundles.compile_bundle_package(_package())
    data["expected_digests"] = {
        "bundle_digest": good.snapshot.bundle_digest}
    assessment_bundles.compile_bundle_package(data)
    data["expected_digests"] = {"bundle_digest": "sha256:" + "f" * 64}
    with pytest.raises(content.FrozenContentUnavailable, match="不一致"):
        assessment_bundles.compile_bundle_package(data)


def test_declarative_validator_and_scorer_fail_closed():
    bundle = assessment_bundles.compile_bundle_package(_package())
    naming = bundle.definitions["untrained_standardized_naming"]
    keys = [item.item_key for item in naming.snapshot.items]

    normalized = naming.validate_response(keys[0], {"value": 2})
    assert normalized.payload == {"value": 2.0}
    for bad in ({"value": 3}, {"value": 0.5}, {"value": "1"},
                {"value": True}, {}):
        with pytest.raises(assessment_definitions.AssessmentDefinitionError):
            naming.validate_response(keys[0], bad)
    with pytest.raises(assessment_definitions.AssessmentDefinitionError,
                       match="not in the frozen definition"):
        naming.validate_response("外来题", {"value": 1})

    responses = {key: {"value": 2} for key in keys}
    score = naming.score_responses(responses)
    assert score.score == 6 and score.result == {"total_score": 6.0}
    with pytest.raises(assessment_definitions.AssessmentDefinitionError,
                       match="all frozen items"):
        naming.score_responses({keys[0]: {"value": 1}})


def test_index_load_and_production_install_with_archived_rollover(tmp_path):
    packages = {
        "bundle_v1.json": _package("formal-v1"),
        "bundle_v2.json": _package("formal-v2"),
    }
    staged = _stage(tmp_path, packages, active="formal-v2")
    bundles, active_id, _raw = assessment_bundles.load_bundle_packages(staged)
    assert active_id == "formal-v2"
    assessment_definitions.install_production_bundles(
        bundles, active_bundle_id=active_id)
    assert assessment_definitions.current_bundle().snapshot.bundle_id == (
        "formal-v2")
    # 归档版本仍可按冻结 id 解析(在途事件 rollover 语义)。
    archived = assessment_definitions.current_bundle("formal-v1")
    assert archived.snapshot.bundle_id == "formal-v1"
    # 运行中拒绝二次安装。
    with pytest.raises(assessment_definitions.AssessmentDefinitionError,
                       match="already installed"):
        assessment_definitions.install_production_bundles(
            bundles, active_bundle_id=active_id)


def test_index_defects_fail_closed(tmp_path):
    staged = _stage(tmp_path, {"bundle_v1.json": _package("formal-v1")},
                    active="formal-v1")
    index_path = (staged / assessment_bundles.ASSESSMENT_BUNDLE_DIR
                  / assessment_bundles.ASSESSMENT_BUNDLE_INDEX_FILE)
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["bundles"][0]["bundle_id"] = "formal-renamed"
    index["active_bundle_id"] = "formal-renamed"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(content.FrozenContentUnavailable, match="不一致"):
        assessment_bundles.load_bundle_packages(staged)

    index["bundles"][0]["file"] = "../bundle_v1.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(content.FrozenContentUnavailable):
        assessment_bundles.load_bundle_packages(staged)


def test_naming_words_extraction_for_isolation_check():
    words = assessment_bundles.naming_words(_package())
    assert words == {"词0", "词1", "词2"}


def test_startup_hook_installs_or_degrades_without_bricking(tmp_path, monkeypatch):
    from app.main import _install_assessment_bundles_at_startup

    # 无索引=现状 no-op。
    monkeypatch.setattr(content, "CONTENT_DIR", tmp_path)
    _install_assessment_bundles_at_startup()
    assert assessment_definitions.registered_bundles() == ()

    staged = _stage(tmp_path, {"bundle_v1.json": _package("formal-v1")},
                    active="formal-v1")
    monkeypatch.setattr(content, "CONTENT_DIR", staged)
    _install_assessment_bundles_at_startup()
    installed = assessment_definitions.registered_bundles()
    assert [row.bundle_id for row in installed] == ["formal-v1"]
    # 再次启动(测试进程内多次建 app)不重复安装。
    _install_assessment_bundles_at_startup()

    # 坏包:评估域保持空注册表 fail-closed,不炸进程。
    with assessment_definitions._REGISTRY_LOCK:
        assessment_definitions._BUNDLES.clear()
        assessment_definitions._ACTIVE_BUNDLE_ID = None
    bundle_path = (staged / assessment_bundles.ASSESSMENT_BUNDLE_DIR
                   / "bundle_v1.json")
    data = json.loads(bundle_path.read_text(encoding="utf-8"))
    data["definitions"][0]["score_max"] = 999  # 内容漂移
    data["expected_digests"] = {"bundle_digest": "sha256:" + "0" * 64}
    bundle_path.write_text(json.dumps(data, ensure_ascii=False),
                           encoding="utf-8")
    _install_assessment_bundles_at_startup()
    assert assessment_definitions.registered_bundles() == ()


def test_word_tamper_and_wordless_naming_fail_closed(tmp_path):
    """审查 P1 双钉:量表一每题必须带词;包字节被改(如清词)即撞索引哈希。"""
    wordless = _package("formal-wordless-v1")
    for definition in wordless["definitions"]:
        if definition["category_key"] == "untrained_standardized_naming":
            for item in definition["items"]:
                item.pop("word", None)
    with pytest.raises(content.FrozenContentUnavailable, match="word"):
        assessment_bundles.compile_bundle_package(wordless)

    staged = _stage(tmp_path, {"bundle_v1.json": _package("formal-v1")},
                    active="formal-v1")
    bundle_path = (staged / assessment_bundles.ASSESSMENT_BUNDLE_DIR
                   / "bundle_v1.json")
    data = json.loads(bundle_path.read_text(encoding="utf-8"))
    for definition in data["definitions"]:
        for item in definition["items"]:
            if "word" in item:
                item["word"] = "改词"
    bundle_path.write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(content.FrozenContentUnavailable, match="登记哈希"):
        assessment_bundles.load_bundle_packages(staged)
