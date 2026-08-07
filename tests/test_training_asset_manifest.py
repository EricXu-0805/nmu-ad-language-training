import json
from collections import Counter
from copy import deepcopy
from pathlib import Path, PurePosixPath
import sys
from types import SimpleNamespace

import pytest

from app.image_id_contract import (
    IMAGE_ID_CONTRACT_VERSION,
    expected_training_image_ids,
    image_id_for_slot,
    is_training_image_id,
    require_training_image_id,
)
from scripts import generate_training_asset_manifest as generator


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "content" / "training_asset_manifest_v1.json"
ITEM_BANK_PATH = ROOT / "content" / "item_bank_v1.json"
PROTOCOL_PATH = ROOT / "content" / "autopilot_protocol_v1.json"

# 图片源材料有意不入库(敏感研究材料,只在本地/机构机器上)。仓库里只有
# manifest;真源树缺席的环境(CI)对这几条无从验证,只能如实跳过——
# 这不是放宽:凡有源树的机器照跑全量。
requires_real_source_tree = pytest.mark.skipif(
    not generator.DEFAULT_SOURCE_ROOT.is_dir(),
    reason="图片源树不在此机器上(不入库),无从对照 manifest")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def test_manifest_has_all_224_slots_and_stable_anonymous_ids():
    manifest = _load(MANIFEST_PATH)
    assets = manifest["assets"]
    ids = [asset["image_id"] for asset in assets]

    assert manifest["manifest_schema_version"] == "training-asset-manifest.v1"
    assert manifest["manifest_version_id"] == generator.MANIFEST_VERSION_ID
    assert manifest["image_id_contract_version"] == IMAGE_ID_CONTRACT_VERSION
    assert manifest["definition_sha256"] == generator._definition_digest(manifest)
    assert (
        generator.RELEASED_DEFINITION_DIGESTS[manifest["manifest_version_id"]]
        == manifest["definition_sha256"]
    )
    assert manifest["purpose"] == (
        "internal_inventory_only_not_a_runtime_content_definition"
    )
    assert manifest["safety_boundary"]["generator_does_not_mutate_source"] is True
    assert manifest["safety_boundary"][
        "source_filesystem_immutability_verified"
    ] is False
    assert "source_is_read_only" not in manifest["safety_boundary"]
    assert manifest["source_repository"]["asset_count"] == 224
    assert manifest["source_repository"]["unique_sha256_count"] == 149
    assert len(assets) == 224
    assert len(ids) == len(set(ids))
    assert all(is_training_image_id(image_id) for image_id in ids)
    assert set(ids) == expected_training_image_ids()

    counts = Counter(asset["week_no"] for asset in assets)
    assert counts == {week_no: 32 for week_no in range(2, 9)}
    for week_no in range(2, 9):
        week_ids = {asset["image_id"] for asset in assets
                    if asset["week_no"] == week_no}
        assert week_ids == {
            image_id_for_slot(week_no, slot_index)
            for slot_index in range(1, 33)
        }


@pytest.mark.parametrize("image_id", [
    "wk2-01", "wk8-30", "wk2-multi-01", "wk8-multi-02",
])
def test_shared_image_id_contract_accepts_numbered_and_multi_ids(image_id):
    assert is_training_image_id(image_id)
    assert require_training_image_id(image_id) == image_id


@pytest.mark.parametrize("image_id", [
    "wk2-00", "wk2-31", "wk2-32", "wk2-multi-1", "wk2-multi-03",
    "wk1-01", "wk9-01", "WK2-01", "wk2-01.webp", "wk2-01/extra",
])
def test_shared_image_id_contract_rejects_numeric_multi_remaps_and_aliases(
        image_id):
    assert not is_training_image_id(image_id)
    with pytest.raises(ValueError, match="image_id"):
        require_training_image_id(image_id)


def test_source_slot_contract_maps_31_and_32_only_to_explicit_multi_ids():
    assert image_id_for_slot(2, 30) == "wk2-30"
    assert image_id_for_slot(2, 31) == "wk2-multi-01"
    assert image_id_for_slot(2, 32) == "wk2-multi-02"
    assert "wk2-31" not in expected_training_image_ids()
    assert "wk2-32" not in expected_training_image_ids()


def test_week1_is_explicitly_no_images_and_new_weeks_remain_blocked():
    manifest = _load(MANIFEST_PATH)
    weeks = {row["week_no"]: row for row in manifest["weeks"]}

    assert weeks[1] == {
        "week_no": 1,
        "source_status": "no_images",
        "asset_count": 0,
        "runtime_content_status": "rapport_and_baseline_flow_no_image_library",
        "release_status": "not_applicable",
    }
    for week_no in range(3, 9):
        assert weeks[week_no]["source_status"] == "source_assets_present"
        assert weeks[week_no]["runtime_content_status"] == (
            "blocked_unstructured_week"
        )
        assert weeks[week_no]["release_status"] == (
            "blocked_pending_approvals"
        )
        for asset in manifest["assets"]:
            if asset["week_no"] != week_no:
                continue
            assert asset["delivery"] == {
                "public_path": None,
                "status": "blocked_not_published",
                "runtime_transport_if_approved": (
                    "session_current_asset_endpoint_only"
                ),
                "legacy_static_asset_status": "not_applicable",
                "generated_or_copied_by_this_manifest": False,
            }
            assert asset["release_status"] == "blocked_pending_approvals"


def test_source_names_never_become_public_paths_and_approvals_are_pending():
    manifest = _load(MANIFEST_PATH)
    for asset in manifest["assets"]:
        source = asset["source"]
        delivery = asset["delivery"]
        assert source["visibility"] == "internal_only"
        assert source["filename"] == PurePosixPath(source["relative_path"]).name
        assert len(source["sha256"]) == 64
        assert source["byte_size"] > 0
        assert source["png"]["width"] > 0
        assert source["png"]["height"] > 0
        assert source["png"]["has_exif_metadata"] is False
        assert delivery["public_path"] is None
        assert delivery["runtime_transport_if_approved"] == (
            "session_current_asset_endpoint_only"
        )
        assert delivery["status"] == "blocked_not_published"
        assert delivery["legacy_static_asset_status"] == (
            "removed_and_route_blocked"
            if asset["week_no"] == 2 and asset["slot_index"] <= 30
            else "not_applicable"
        )
        assert {
            name: approval["status"]
            for name, approval in asset["approvals"].items()
        } == {
            "rights": "pending",
            "visual": "pending",
            "content": "pending",
        }

    public_images = ROOT / "web" / "public" / "img"
    assert not public_images.exists() or not any(public_images.iterdir())


def test_existing_week2_item_bank_image_ids_are_a_manifest_subset():
    manifest = _load(MANIFEST_PATH)
    bank = _load(ITEM_BANK_PATH)
    manifest_ids = {asset["image_id"] for asset in manifest["assets"]}
    bank_ids = {
        row["image_id"]
        for section in ("single_element", "double_element", "multi_element")
        for row in bank.get(section, [])
        if row.get("image_id")
    }

    assert bank_ids == {
        f"wk2-{position:02d}" for position in range(1, 31)
    }
    assert bank_ids <= manifest_ids
    assert bank["supported_training_weeks"] == [2]
    assert _load(PROTOCOL_PATH)["supported_training_weeks"] == [2]
    assert manifest["safety_boundary"]["runtime_weeks_changed"] is False


def test_known_visual_leaks_and_mapping_conflicts_are_open_findings():
    manifest = _load(MANIFEST_PATH)
    findings = manifest["review_findings"]
    assets = {asset["image_id"]: asset for asset in manifest["assets"]}

    leakage_ids = {
        finding["image_ids"][0]
        for finding in findings
        if finding["category"] == "answer_text_visible"
    }
    assert leakage_ids == {
        "wk2-multi-01",
        "wk2-multi-02",
        "wk8-multi-02",
    }

    assert {finding["finding_id"] for finding in findings} == {
        "visual-answer-text-wk2-multi-01",
        "visual-answer-text-wk2-multi-02",
        "visual-answer-text-wk8-multi-02",
        "mapping-order-wk2-21",
        "mapping-order-wk3-29",
        "mapping-order-wk5-25",
        "mapping-term-wk6-28",
        "mapping-term-wk8-26",
        "mapping-order-wk8-27",
        "mapping-order-wk3-30",
        "mapping-order-function-wk4-21",
        "mapping-term-wk4-25",
        "mapping-order-wk5-30",
        "mapping-order-wk6-23",
        "mapping-order-function-wk7-21",
        "cue-direction-wk7-23",
        "cue-direction-wk8-28",
        "duplicate-scene-wk3-wk6-multi-01",
    }
    assert all("wk2-30" not in finding["image_ids"] for finding in findings)
    for finding in findings:
        assert finding["status"] == "open"
        for image_id in finding["image_ids"]:
            assert finding["finding_id"] in assets[image_id]["review_finding_ids"]


@requires_real_source_tree
def test_manifest_covers_every_regular_source_file_exactly_once():
    manifest = _load(MANIFEST_PATH)
    source_paths = {
        path.relative_to(generator.DEFAULT_SOURCE_ROOT).as_posix()
        for path in generator.DEFAULT_SOURCE_ROOT.rglob("*")
        if path.is_file()
    }
    manifest_paths = {
        asset["source"]["relative_path"] for asset in manifest["assets"]
    }

    assert len(source_paths) == 224
    assert len(manifest_paths) == 224
    assert manifest_paths == source_paths


def test_manifest_records_exact_duplicate_identity_without_merging_slots():
    manifest = _load(MANIFEST_PATH)
    assets = {asset["image_id"]: asset for asset in manifest["assets"]}

    week3 = assets["wk3-multi-01"]["source"]
    week6 = assets["wk6-multi-01"]["source"]
    assert week3["sha256"] == week6["sha256"]
    assert week3["content_digest_occurrences"] >= 2
    assert week6["canonical_image_id"] == week3["canonical_image_id"]
    assert len({asset["source"]["sha256"] for asset in assets.values()}) == 149


@requires_real_source_tree
def test_manifest_json_has_no_duplicate_keys_and_matches_generator_bytes():
    raw = MANIFEST_PATH.read_text(encoding="utf-8")
    parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    generator._validate_generated_manifest(parsed)
    assert MANIFEST_PATH.read_bytes() == generator._json_bytes(
        generator.build_manifest(generator.DEFAULT_SOURCE_ROOT)
    )


def test_released_digest_fails_closed_on_unreviewed_definition_change():
    manifest = _load(MANIFEST_PATH)
    tampered = deepcopy(manifest)
    tampered["purpose"] = "runtime_content_definition"
    tampered["definition_sha256"] = generator._definition_digest(tampered)

    with pytest.raises(ValueError, match="必须升级 MANIFEST_VERSION_ID"):
        generator._validate_generated_manifest(tampered)


def test_manifest_never_publishes_answer_bearing_source_paths():
    manifest = _load(MANIFEST_PATH)
    for asset in manifest["assets"]:
        source = asset["source"]
        assert asset["delivery"]["public_path"] is None
        assert PurePosixPath(source["relative_path"]).name == source["filename"]
        assert ".." not in PurePosixPath(source["relative_path"]).parts
        assert source["filename"] not in json.dumps(
            asset["delivery"], ensure_ascii=False
        )


def test_source_reader_rejects_symlinks(tmp_path: Path):
    target = tmp_path / "target.png"
    target.write_bytes(b"not relevant")
    link = tmp_path / "linked.png"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("filesystem does not support symlinks")

    with pytest.raises(ValueError, match="符号链接|不可访问"):
        generator._read_regular_file_no_follow(link)


@requires_real_source_tree
def test_png_validator_rejects_truncation_after_validating_real_source():
    manifest = _load(MANIFEST_PATH)
    first = manifest["assets"][0]["source"]
    data = (generator.DEFAULT_SOURCE_ROOT / first["relative_path"]).read_bytes()
    metadata = generator._png_metadata(
        data, relative_path=first["relative_path"]
    )
    assert metadata["verification_level"] == (
        "structure_crc_ihdr_limits_and_idat_zlib"
    )

    with pytest.raises(ValueError):
        generator._png_metadata(
            data[:-1], relative_path=first["relative_path"]
        )


def test_optional_pillow_decode_reuses_the_exact_hashed_bytes(monkeypatch):
    opened_payloads: list[bytes] = []

    class FakeImage:
        size = (7, 9)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def verify(self):
            return None

        def load(self):
            return None

    class FakeImageModule:
        @staticmethod
        def open(source):
            # BytesIO proves the verifier is decoding the already-read bytes,
            # not reopening an owner-writable path after it was hashed.
            opened_payloads.append(source.read())
            return FakeImage()

    monkeypatch.setitem(
        sys.modules, "PIL", SimpleNamespace(Image=FakeImageModule),
    )
    payload = b"exactly-the-hashed-png-bytes"
    generator._pillow_decode_verify(
        payload, {"width": 7, "height": 9}, Path("display-only.png"),
    )
    assert opened_payloads == [payload, payload]


def test_manifest_output_cannot_be_written_inside_source_tree(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="输出不得写入"):
        generator._assert_output_outside_source(source, source / "manifest.json")


def _make_empty_week_tree(root: Path) -> None:
    root.mkdir(parents=True)
    for directory in generator.WEEK_DIRECTORIES.values():
        (root / directory).mkdir()


@pytest.mark.parametrize("unexpected_name", ["第一周", "README.txt"])
def test_source_root_rejects_any_unregistered_top_level_entry(
        tmp_path: Path, unexpected_name: str):
    source = tmp_path / "每周图片材料"
    _make_empty_week_tree(source)
    unexpected = source / unexpected_name
    if "." in unexpected_name:
        unexpected.write_text("unregistered", encoding="utf-8")
    else:
        unexpected.mkdir()

    with pytest.raises(ValueError, match="未登记"):
        generator._assert_source_tree_is_real(source)


def test_source_root_rejects_a_symlink_in_any_ancestor(tmp_path: Path):
    real_parent = tmp_path / "real-parent"
    source = real_parent / "每周图片材料"
    _make_empty_week_tree(source)
    alias = tmp_path / "alias-parent"
    try:
        alias.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem does not support directory symlinks")

    with pytest.raises(ValueError, match="任一级均不得为符号链接"):
        generator._assert_source_tree_is_real(alias / "每周图片材料")
