from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from scripts import verify_browser_dist


def _write_source(root: Path) -> Path:
    for relative in verify_browser_dist.REQUIRED_WEB_FILES:
        path = root / "web" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture:{relative}\n", encoding="utf-8")
    source = root / "web" / "src"
    source.mkdir(parents=True)
    (source / "base.ts").write_text("export const base = true;\n", encoding="utf-8")
    # These two labels sort differently under Unicode code-point ordering and
    # JavaScript UTF-16 ordering.  Keeping both proves byte-for-byte parity.
    (source / "\ue000.ts").write_text("private-use\n", encoding="utf-8")
    (source / "😀.ts").write_text("surrogate-pair\n", encoding="utf-8")
    for relative in verify_browser_dist.FROZEN_CONTENT_FILES:
        path = root / "content" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    return root


def _write_dist(root: Path) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<script type="module" src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (dist / "assets" / "app.js").write_text("export const ready = true;\n", encoding="utf-8")
    (dist / "build-fingerprint.sha256").write_text("a" * 64 + "\n", encoding="ascii")
    rows = []
    for path in (
        dist / "assets" / "app.js",
        dist / "build-fingerprint.sha256",
        dist / "index.html",
    ):
        value = path.read_bytes()
        rows.append({
            "path": path.relative_to(dist).as_posix(),
            "size": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        })
    (dist / verify_browser_dist.MANIFEST_NAME).write_text(
        json.dumps({
            "schema_version": verify_browser_dist.SCHEMA,
            "algorithm": "SHA-256",
            "root": "dist/",
            "excluded_paths": [verify_browser_dist.MANIFEST_NAME],
            "files": rows,
        }),
        encoding="utf-8",
    )
    return dist


def test_browser_dist_verifier_accepts_the_exact_manifest(tmp_path):
    assert verify_browser_dist.verify(_write_dist(tmp_path)) == 3


def test_browser_dist_verifier_rejects_a_self_consistent_stale_build(
        tmp_path, monkeypatch):
    dist = _write_dist(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(
        verify_browser_dist,
        "_current_source_fingerprint",
        lambda _root: "b" * 64,
    )

    with pytest.raises(
        verify_browser_dist.DistVerificationError,
        match="不是由当前源码生成",
    ):
        verify_browser_dist.verify(dist, source_root=source)


def test_python_source_fingerprint_matches_the_node_release_builder(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is only needed for cross-runtime parity testing")
    source = _write_source(tmp_path / "source")
    module = (
        Path(__file__).resolve().parents[1]
        / "web" / "scripts" / "build-fingerprint.mjs"
    )
    program = (
        f"import {{ computeBuildIdentity }} from {json.dumps(module.as_uri())};"
        "process.stdout.write(computeBuildIdentity({"
        f"webRoot:{json.dumps(str(source / 'web'))},"
        f"contentRoot:{json.dumps(str(source / 'content'))}"
        "}).fingerprint);"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert verify_browser_dist._current_source_fingerprint(source) == result.stdout


@pytest.mark.parametrize("scope", ["source", "dist"])
def test_browser_dist_verifier_fails_closed_when_a_tree_cannot_be_walked(
        tmp_path, monkeypatch, scope):
    dist = _write_dist(tmp_path)
    source = _write_source(tmp_path / "source")

    def unreadable_walk(_root, *, followlinks, onerror):
        assert followlinks is False
        onerror(PermissionError("unreadable directory"))
        return iter(())

    monkeypatch.setattr(os, "walk", unreadable_walk)
    with pytest.raises(
        verify_browser_dist.DistVerificationError,
        match="无法完整遍历受管目录",
    ):
        if scope == "source":
            verify_browser_dist._current_source_fingerprint(source)
        else:
            verify_browser_dist.verify(dist)


@pytest.mark.parametrize("mutation, fragment", [
    ("missing", "缺少文件"),
    ("tampered", "摘要不符"),
    ("extra", "文件集与清单不一致"),
])
def test_browser_dist_verifier_rejects_incomplete_or_changed_bytes(
        tmp_path, mutation, fragment):
    dist = _write_dist(tmp_path)
    asset = dist / "assets" / "app.js"
    if mutation == "missing":
        asset.unlink()
    elif mutation == "tampered":
        # Keep the byte count unchanged so this case proves the digest check,
        # independently of the earlier size check.
        asset.write_text("export const ready = null;\n", encoding="utf-8")
    else:
        (dist / "assets" / "unlisted.js").write_text("extra\n", encoding="utf-8")

    with pytest.raises(verify_browser_dist.DistVerificationError) as excinfo:
        verify_browser_dist.verify(dist)
    assert fragment in str(excinfo.value)


def test_browser_dist_verifier_rejects_symlinked_assets(tmp_path):
    dist = _write_dist(tmp_path)
    asset = dist / "assets" / "app.js"
    target = tmp_path / "outside.js"
    target.write_text(asset.read_text(encoding="utf-8"), encoding="utf-8")
    asset.unlink()
    asset.symlink_to(target)

    with pytest.raises(verify_browser_dist.DistVerificationError, match="非普通文件"):
        verify_browser_dist.verify(dist)


def test_browser_dist_verifier_rejects_a_symlinked_dist_root(tmp_path):
    actual = _write_dist(tmp_path / "actual")
    linked = tmp_path / "linked-dist"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(
        verify_browser_dist.DistVerificationError,
        match="构建根.*不得为符号链接",
    ):
        verify_browser_dist.verify(linked)
