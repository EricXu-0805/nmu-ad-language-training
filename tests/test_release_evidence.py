from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import release_evidence


RELEASE_ID = "a" * 64
OTHER_RELEASE_ID = "b" * 64


def _json_bytes(value: object) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")) + "\n").encode("utf-8")


def _receipt(
        root: Path, name: str = "receipt.json", *,
        release_id: str = RELEASE_ID) -> tuple[Path, str]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _json_bytes({
        "schema_version": "nmu.synthetic-test-receipt.v1",
        "release_id": release_id,
        "scope": "test-only",
    })
    path.write_bytes(body)
    return path, hashlib.sha256(body).hexdigest()


def _index(
        root: Path, entries: list[dict[str, object]], *,
        release_id: str = RELEASE_ID,
        extra: dict[str, object] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    value: dict[str, object] = {
        "schema_version": release_evidence.SCHEMA_VERSION,
        "release_id": release_id,
        "entries": entries,
    }
    value.update(extra or {})
    path = root / "evidence-index.json"
    path.write_bytes(_json_bytes(value))
    return path


def _entry(kind: str, path: str, checksum: str) -> dict[str, object]:
    return {"kind": kind, "path": path, "sha256": checksum}


def test_valid_index_binds_exact_receipt_bytes(tmp_path):
    root = tmp_path / "bundle"
    _path, checksum = _receipt(root, "receipts/device.json")
    index = _index(root, [
        _entry("device-basic-check", "receipts/device.json", checksum),
    ])

    result = release_evidence.verify_evidence_index(index, RELEASE_ID)

    assert result["schema_version"] == release_evidence.SCHEMA_VERSION
    assert result["release_id"] == RELEASE_ID
    assert result["receipt_count"] == 1
    assert result["receipts"] == [{
        "kind": "device-basic-check",
        "path": "receipts/device.json",
        "sha256": checksum,
    }]
    assert result["index_sha256"] == hashlib.sha256(index.read_bytes()).hexdigest()


def test_platform_without_secure_open_primitives_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / "bundle"
    _path, checksum = _receipt(root)
    index = _index(root, [
        _entry("device-basic-check", "receipt.json", checksum),
    ])
    monkeypatch.setattr(release_evidence, "_SECURE_OPEN_SUPPORTED", False)

    with pytest.raises(
            release_evidence.ReleaseEvidenceError,
            match="secure_open_unsupported"):
        release_evidence.verify_evidence_index(index, RELEASE_ID)


@pytest.mark.parametrize("unsafe", (
    "../outside.json",
    "receipts/../../outside.json",
    "/absolute.json",
    "receipts\\device.json",
    "receipts//device.json",
    "receipts/./device.json",
    ".",
))
def test_unsafe_or_noncanonical_paths_are_rejected(tmp_path, unsafe):
    root = tmp_path / "bundle"
    _outside, checksum = _receipt(tmp_path, "outside.json")
    index = _index(root, [_entry("device-basic-check", unsafe, checksum)])

    with pytest.raises(
            release_evidence.ReleaseEvidenceError,
            match="evidence_path_invalid"):
        release_evidence.verify_evidence_index(index, RELEASE_ID)


def test_symlinked_receipt_is_rejected(tmp_path):
    root = tmp_path / "bundle"
    target, checksum = _receipt(root, "real.json")
    link = root / "receipt.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("current filesystem does not support symlinks")
    index = _index(root, [
        _entry("device-basic-check", "receipt.json", checksum),
    ])

    with pytest.raises(
            release_evidence.ReleaseEvidenceError,
            match="evidence_receipt_unreadable"):
        release_evidence.verify_evidence_index(index, RELEASE_ID)


def test_symlinked_parent_directory_is_rejected(tmp_path):
    root = tmp_path / "bundle"
    outside = tmp_path / "outside"
    _target, checksum = _receipt(outside)
    root.mkdir()
    try:
        (root / "receipts").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("current filesystem does not support symlinks")
    index = _index(root, [
        _entry("device-basic-check", "receipts/receipt.json", checksum),
    ])

    with pytest.raises(
            release_evidence.ReleaseEvidenceError,
            match="evidence_receipt_unreadable"):
        release_evidence.verify_evidence_index(index, RELEASE_ID)


def test_symlinked_index_is_rejected_before_parsing(tmp_path):
    root = tmp_path / "bundle"
    _path, checksum = _receipt(root)
    real_index = _index(root, [
        _entry("device-basic-check", "receipt.json", checksum),
    ])
    link = tmp_path / "evidence-index.json"
    try:
        link.symlink_to(real_index)
    except OSError:
        pytest.skip("current filesystem does not support symlinks")

    with pytest.raises(
            release_evidence.ReleaseEvidenceError,
            match="evidence_index_unreadable"):
        release_evidence.verify_evidence_index(link, RELEASE_ID)


@pytest.mark.parametrize("where", ("index", "entry"))
def test_extra_index_or_entry_keys_are_rejected(tmp_path, where):
    root = tmp_path / "bundle"
    _path, checksum = _receipt(root)
    entry = _entry("device-basic-check", "receipt.json", checksum)
    extra = None
    if where == "index":
        extra = {"approval": True}
    else:
        entry["approval"] = True
    index = _index(root, [entry], extra=extra)

    expected = ("evidence_index_schema_invalid" if where == "index"
                else "evidence_entry_schema_invalid")
    with pytest.raises(release_evidence.ReleaseEvidenceError, match=expected):
        release_evidence.verify_evidence_index(index, RELEASE_ID)


def test_receipt_hash_mismatch_is_rejected(tmp_path):
    root = tmp_path / "bundle"
    _receipt(root)
    index = _index(root, [
        _entry("device-basic-check", "receipt.json", "0" * 64),
    ])

    with pytest.raises(
            release_evidence.ReleaseEvidenceError,
            match="evidence_sha256_mismatch"):
        release_evidence.verify_evidence_index(index, RELEASE_ID)


def test_receipt_release_id_must_match_index_and_expected_release(tmp_path):
    root = tmp_path / "bundle"
    _path, checksum = _receipt(root, release_id=OTHER_RELEASE_ID)
    index = _index(root, [
        _entry("device-basic-check", "receipt.json", checksum),
    ])

    with pytest.raises(
            release_evidence.ReleaseEvidenceError,
            match="evidence_receipt_release_mismatch"):
        release_evidence.verify_evidence_index(index, RELEASE_ID)


def test_index_release_id_must_match_callers_expected_release(tmp_path):
    root = tmp_path / "bundle"
    _path, checksum = _receipt(root, release_id=OTHER_RELEASE_ID)
    index = _index(root, [
        _entry("device-basic-check", "receipt.json", checksum),
    ], release_id=OTHER_RELEASE_ID)

    with pytest.raises(
            release_evidence.ReleaseEvidenceError,
            match="evidence_index_release_mismatch"):
        release_evidence.verify_evidence_index(index, RELEASE_ID)


def test_duplicate_kinds_cannot_relabel_one_release_twice(tmp_path):
    root = tmp_path / "bundle"
    _first, first_checksum = _receipt(root, "first.json")
    _second, second_checksum = _receipt(root, "second.json")
    index = _index(root, [
        _entry("device-basic-check", "first.json", first_checksum),
        _entry("device-basic-check", "second.json", second_checksum),
    ])

    with pytest.raises(
            release_evidence.ReleaseEvidenceError,
            match="evidence_kind_duplicate"):
        release_evidence.verify_evidence_index(index, RELEASE_ID)


def test_referenced_directory_is_not_a_receipt(tmp_path):
    root = tmp_path / "bundle"
    (root / "receipt.json").mkdir(parents=True)
    index = _index(root, [
        _entry("device-basic-check", "receipt.json", "0" * 64),
    ])

    with pytest.raises(
            release_evidence.ReleaseEvidenceError,
            match="evidence_receipt_unreadable"):
        release_evidence.verify_evidence_index(index, RELEASE_ID)


def test_oversized_receipt_is_bounded_and_rejected(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    receipt = root / "receipt.json"
    receipt.write_bytes(b"x" * (release_evidence.MAX_RECEIPT_BYTES + 1))
    index = _index(root, [
        _entry("device-basic-check", "receipt.json", "0" * 64),
    ])

    with pytest.raises(
            release_evidence.ReleaseEvidenceError,
            match="evidence_receipt_unreadable"):
        release_evidence.verify_evidence_index(index, RELEASE_ID)
