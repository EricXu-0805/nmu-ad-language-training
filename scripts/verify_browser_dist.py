#!/usr/bin/env python3
"""Fail closed unless web/dist exactly matches its SHA-256 manifest."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys


SCHEMA = "nmu.browser-dist-sha256.v1"
MANIFEST_NAME = "browser-dist-sha256.json"
HEX64 = frozenset("0123456789abcdef")
REQUIRED_WEB_FILES = (
    "index.html",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "tsconfig.app.json",
    "tsconfig.node.json",
    "vite.config.ts",
    "scripts/build-integrity.d.mts",
    "scripts/build-integrity.mjs",
    "scripts/build-fingerprint.d.mts",
    "scripts/build-fingerprint.mjs",
)
FROZEN_CONTENT_FILES = (
    "item_bank_v1.json",
    "week1_script.json",
    "autopilot_protocol_v1.json",
)


class DistVerificationError(RuntimeError):
    pass


def _walk_error(exc: OSError) -> None:
    raise DistVerificationError("无法完整遍历受管目录") from exc


def _regular_bytes(path: Path) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise DistVerificationError(f"缺少文件 {path.name}") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise DistVerificationError(f"非普通文件 {path.name}")
    return path.read_bytes()


def _safe_relative(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise DistVerificationError("构建清单含非规范路径")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(
            part in {"", ".", ".."} for part in path.parts):
        raise DistVerificationError("构建清单含越界或非规范路径")
    return path


def _current_source_fingerprint(source_root: Path) -> str:
    try:
        root_info = source_root.lstat()
    except OSError as exc:
        raise DistVerificationError("源码根目录不存在") from exc
    if source_root.is_symlink() or not stat.S_ISDIR(root_info.st_mode):
        raise DistVerificationError("源码根必须是普通目录且不得为符号链接")
    root = source_root.resolve()
    web_root = root / "web"
    content_root = root / "content"
    entries: list[tuple[str, bytes]] = []

    def add_file(base: Path, relative: str, namespace: str) -> None:
        entries.append((f"{namespace}/{relative}", _regular_bytes(base / relative)))

    for relative in REQUIRED_WEB_FILES:
        add_file(web_root, relative, "web")

    source_dir = web_root / "src"
    try:
        source_info = source_dir.lstat()
    except OSError as exc:
        raise DistVerificationError("缺少网页源码目录 web/src") from exc
    if source_dir.is_symlink() or not stat.S_ISDIR(source_info.st_mode):
        raise DistVerificationError("网页源码目录必须是普通目录且不得为符号链接")
    for directory, dirs, files in os.walk(
            source_dir, followlinks=False, onerror=_walk_error):
        base = Path(directory)
        dirs.sort()
        files.sort()
        for dirname in dirs:
            child = base / dirname
            if child.is_symlink():
                raise DistVerificationError("网页源码目录含符号链接")
        for filename in files:
            child = base / filename
            relative = child.relative_to(web_root).as_posix()
            entries.append((f"web/{relative}", _regular_bytes(child)))

    for relative in FROZEN_CONTENT_FILES:
        add_file(content_root, relative, "content")

    labels = [label for label, _value in entries]
    if len(labels) != len(set(labels)):
        raise DistVerificationError("当前网页源码指纹含重复输入")
    digest = hashlib.sha256()
    # JavaScript's `<` compares strings by UTF-16 code units.  Encode the
    # portable labels the same way so non-ASCII file names cannot make the
    # runtime verifier disagree with the release builder.
    for label, value in sorted(entries, key=lambda row: row[0].encode("utf-16-be")):
        if not label or "\x00" in label or label.startswith("/") or "\\" in label:
            raise DistVerificationError("当前网页源码指纹含非规范输入")
        digest.update(f"{label}\x00{len(value)}\x00".encode())
        digest.update(value)
        digest.update(b"\x00")
    return digest.hexdigest()


def verify(dist_root: Path, *, source_root: Path | None = None) -> int:
    try:
        root_info = dist_root.lstat()
    except OSError as exc:
        raise DistVerificationError("网页构建目录不存在") from exc
    if dist_root.is_symlink() or not stat.S_ISDIR(root_info.st_mode):
        raise DistVerificationError("网页构建根必须是普通目录且不得为符号链接")
    root = dist_root.resolve()
    manifest_path = root / MANIFEST_NAME
    try:
        manifest = json.loads(_regular_bytes(manifest_path))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DistVerificationError("网页构建清单不是有效 JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version", "algorithm", "root", "excluded_paths", "files",
    }:
        raise DistVerificationError("网页构建清单字段不完整")
    if (manifest["schema_version"] != SCHEMA
            or manifest["algorithm"] != "SHA-256"
            or manifest["root"] != "dist/"
            or manifest["excluded_paths"] != [MANIFEST_NAME]
            or not isinstance(manifest["files"], list)
            or not manifest["files"]):
        raise DistVerificationError("网页构建清单版本或范围无效")

    declared: set[str] = set()
    for row in manifest["files"]:
        if not isinstance(row, dict) or set(row) != {"path", "size", "sha256"}:
            raise DistVerificationError("网页构建清单条目无效")
        relative = _safe_relative(row["path"])
        name = relative.as_posix()
        if name == MANIFEST_NAME or name in declared:
            raise DistVerificationError("网页构建清单含重复或自引用条目")
        declared.add(name)
        if (type(row["size"]) is not int or row["size"] < 0
                or not isinstance(row["sha256"], str)
                or len(row["sha256"]) != 64
                or not set(row["sha256"]).issubset(HEX64)):
            raise DistVerificationError(f"网页构建清单元数据无效：{name}")
        candidate = root.joinpath(*relative.parts)
        bytes_value = _regular_bytes(candidate)
        if len(bytes_value) != row["size"]:
            raise DistVerificationError(f"网页文件大小不符：{name}")
        if hashlib.sha256(bytes_value).hexdigest() != row["sha256"]:
            raise DistVerificationError(f"网页文件摘要不符：{name}")

    actual: set[str] = set()
    for directory, dirs, files in os.walk(
            root, followlinks=False, onerror=_walk_error):
        base = Path(directory)
        for dirname in dirs:
            child = base / dirname
            if child.is_symlink():
                raise DistVerificationError("网页构建目录含符号链接")
        for filename in files:
            child = base / filename
            relative = child.relative_to(root).as_posix()
            _regular_bytes(child)
            if relative != MANIFEST_NAME:
                actual.add(relative)
    if actual != declared:
        missing = sorted(declared - actual)
        extra = sorted(actual - declared)
        raise DistVerificationError(
            f"网页文件集与清单不一致（缺少={missing}，额外={extra}）")
    if "index.html" not in declared:
        raise DistVerificationError("网页构建清单缺少 index.html")
    if source_root is not None:
        try:
            recorded = _regular_bytes(root / "build-fingerprint.sha256").decode(
                "ascii").strip()
        except UnicodeDecodeError as exc:
            raise DistVerificationError("网页包源码指纹不是 ASCII") from exc
        if (len(recorded) != 64 or not set(recorded).issubset(HEX64)
                or recorded != _current_source_fingerprint(source_root)):
            raise DistVerificationError(
                "网页包不是由当前源码生成，请重新执行网页构建")
    return len(declared)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    source_root: Path | None = None
    if len(args) == 1:
        dist_root = Path(args[0])
    elif len(args) == 3 and args[0] == "--source-root":
        source_root = Path(args[1])
        dist_root = Path(args[2])
    else:
        print(
            "用法: verify_browser_dist.py [--source-root PLATFORM_ROOT] WEB_DIST_DIR",
            file=sys.stderr,
        )
        return 64
    try:
        count = verify(dist_root, source_root=source_root)
    except (DistVerificationError, OSError) as exc:
        print(f"网页构建完整性检查失败：{exc}", file=sys.stderr)
        return 1
    print(f"网页构建完整性通过：{count} 个受管文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
