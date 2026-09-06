#!/usr/bin/env python3
"""比对"生产上跑的到底是哪个版本"——只读，一个字节都不改。

**为什么需要它。** 2026-08-17 那次只读复核发现，机器上唯一记录版本的
`/opt/nmu/last-deploy.state` 写着 `9c34dcb @ 08-08`，而部署树实际是
`167273f`（08-15）——旧六天、跨了至少一次上线没更新。而回滚流程恰恰要
"旧代码树 + 旧校验器一起放回"，判断"旧的是哪一个"时人就会去查那个文件。

**一个会过期的记录比没有记录更危险。** 所以真判据只能是逐文件指纹：把部署树
每个文件的 sha256 拿来，跟某个提交生成的同样清单逐行比。

**这个脚本有意不碰网络、不碰生产。** 它只做本地比对，因为
`DEPLOY.md` §9.3 已经取消了源码目录的 rsync/覆盖发布路径，而任何"能连上生产
并且知道怎么同步文件"的新脚本，都会被后来的人当成发布入口复用。清单怎么取，
用 `--print-remote-command` 打印那条只读命令，人自己去目标机上跑。

用法::

    # 1) 用受审查的本脚本在目标机只读采集（不会包含 data 或根目录 .env）
    python -I -B scripts/verify_deployed_tree.py --capture --tree-root /opt/nmu/app

    # 2) 输出存为 manifest.json，与候选提交及独立保留的构建产物清单共同核对
    python -I -B scripts/verify_deployed_tree.py --manifest manifest.json \\
        --revision <commit> --expected-dist-manifest <trusted-build>/browser-dist-sha256.json

范围是源码部署树：app/scripts/content/alembic/deploy/web（排除依赖与缓存）和
显式构建/依赖配置；web/dist 必须额外与受控构建机保留的原始清单逐字节闭合。
这不是 OCI 镜像签名、构建机可信度、配置秘密或生产数据的证明。
旧的两列 Python 清单仅保留解析器用于历史阅读，CLI 不再接受为完整发布证明。

退出码：0 = 完全一致；1 = 有差异；2 = 清单或提交本身不可用（判定无效）。
**退出码 1 和 2 必须分开看**：1 是"量到了，不一样"，2 是"根本没量成"。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: 空串的 sha256。清单里出现它，说明那一行量的是"什么都没有"——通常是某条
#: 命令静默失败、输出为空，而调用方把空输出的摘要当成了文件指纹。
#: 这个常量存在是因为我 2026-08-17 真的这么错过一次，并据此得出了
#: "生产代码不对应任何提交"的错误结论。
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_ALLOWED_GIT_SUBCOMMANDS = ("cat-file", "show", "rev-parse", "ls-tree")
_HEX = frozenset("0123456789abcdef")
RELEASE_SCHEMA = "nmu.source-release-tree.v1"
MANAGED_TREES = frozenset({"app", "scripts", "content", "alembic", "deploy", "web"})
MANAGED_FILES = frozenset({
    "Dockerfile", "Caddyfile", ".dockerignore", "alembic.ini", "pyproject.toml",
    "requirements.txt", "requirements-dev.txt",
    "requirements-deploy.txt", "requirements-deploy.lock.txt",
    "docker-compose.yml", "docker-compose.host-caddy.yml",
})
IGNORED_PARTS = frozenset({"__pycache__", "node_modules", ".pytest_cache", ".ruff_cache", ".DS_Store"})
DIST_METADATA = ("browser-dist-sha256.json", "build-provenance.json", "build-fingerprint.sha256")


def _managed(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return bool(parts) and not (set(parts) & IGNORED_PARTS) and (
        name in MANAGED_FILES or parts[0] in MANAGED_TREES
    ) and not name.endswith((".pyc", ".pyo"))


def _source(name: str) -> bool:
    return _managed(name) and not name.startswith("web/dist/")


class VerifyError(RuntimeError):
    """判定无法进行。稳定 code，不含路径以外的环境信息。"""

    def __init__(self, code: str, detail: str):
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Report:
    identical: int = 0
    differing: list[str] = field(default_factory=list)
    absent_in_revision: list[str] = field(default_factory=list)
    missing_on_deployment: list[str] = field(default_factory=list)

    @property
    def matches(self) -> bool:
        return not (self.differing or self.absent_in_revision or self.missing_on_deployment)


def parse_manifest(text: str) -> dict[str, str]:
    """把 ``<相对路径> <sha256>`` 的清单读成字典。宁可拒绝，也不放过可疑行。"""
    entries: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) != 2:
            raise VerifyError(
                "manifest_line_malformed", f"第 {number} 行不是「路径 摘要」两列")
        name, digest = parts
        digest = digest.lower()
        if len(digest) != 64 or not set(digest) <= _HEX:
            raise VerifyError(
                "manifest_hash_malformed", f"第 {number} 行的摘要不是 64 位十六进制")
        if digest == EMPTY_SHA256:
            raise VerifyError(
                "manifest_hash_of_nothing",
                f"第 {number} 行的摘要是空内容的 sha256——那一行量到的是"
                "「什么都没有」，不是一个空文件。先查取清单的命令是不是失败了。")
        if (name.startswith("/") or name.startswith("~")
                or ".." in name.split("/") or "\\" in name):
            raise VerifyError(
                "manifest_path_unsafe", f"第 {number} 行的路径不是安全的相对路径")
        if name in entries:
            raise VerifyError(
                "manifest_duplicate_path", f"路径重复：{name}")
        entries[name] = digest
    if not entries:
        # 空清单最危险：逐行比对零条，读起来就是"零差异"。
        raise VerifyError("manifest_empty", "清单是空的，判定无效")
    return entries


def _git(repo_root, *args: str) -> bytes:
    if args[0] not in _ALLOWED_GIT_SUBCOMMANDS:
        raise VerifyError("git_subcommand_not_allowed", f"不允许的 git 子命令：{args[0]}")
    return subprocess.run(
        ["git", *args], cwd=repo_root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30).stdout


def compare(manifest: dict[str, str], *, revision: str, repo_root) -> Report:
    """逐文件比对。提交本身取不到就抛错，绝不降级成"每个文件都不存在"。"""
    try:
        _git(repo_root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    except subprocess.SubprocessError as exc:
        raise VerifyError("revision_unknown", f"取不到提交 {revision}") from exc

    expected = _revision_sources(revision, repo_root)
    identical = 0
    differing: list[str] = []
    absent: list[str] = []
    for name in sorted(manifest):
        if name not in expected:
            absent.append(name)
            continue
        blob = _git(repo_root, "show", f"{revision}:{name}")
        if hashlib.sha256(blob).hexdigest() == manifest[name]:
            identical += 1
        else:
            differing.append(name)
    return Report(identical=identical, differing=differing,
                  absent_in_revision=absent,
                  missing_on_deployment=sorted(set(expected) - set(manifest)))


def _revision_sources(revision: str, repo_root) -> set[str]:
    output = _git(repo_root, "ls-tree", "-r", "-z", "--full-tree", revision)
    files = set()
    for entry in output.split(b"\0"):
        if not entry:
            continue
        meta, raw_name = entry.split(b"\t", 1)
        name = raw_name.decode("utf-8")
        if _source(name):
            if meta.split()[0] not in {b"100644", b"100755"}:
                raise VerifyError("revision_non_regular_file", name)
            files.add(name)
    if not files:
        raise VerifyError("revision_scope_empty", "候选版本没有受管发布文件")
    return files


def _regular(path: Path) -> bytes:
    if not stat.S_ISREG(path.lstat().st_mode) or path.is_symlink():
        raise VerifyError("tree_non_regular_file", "发布文件必须是普通文件")
    return path.read_bytes()


def capture(tree_root: Path) -> dict:
    """Observe all managed source/build files; never traverse data or secrets."""
    if tree_root.is_symlink() or not tree_root.is_dir():
        raise VerifyError("tree_root_invalid", "发布根不是普通目录")
    files = {}
    metadata = {}
    directories = set()
    def fail_walk(_error):
        raise VerifyError("tree_unreadable", "无法完整枚举发布目录")
    for top in sorted(MANAGED_FILES | MANAGED_TREES):
        start = tree_root / top
        if not start.exists() and not start.is_symlink():
            continue
        if start.is_symlink():
            raise VerifyError("tree_symlink", top)
        paths = [start] if start.is_file() else []
        if start.is_dir():
            for directory, dirs, names in os.walk(start, followlinks=False, onerror=fail_walk):
                base = Path(directory)
                directories.add(base.relative_to(tree_root).as_posix())
                dirs[:] = sorted(d for d in dirs if d not in IGNORED_PARTS)
                if any((base / d).is_symlink() for d in dirs):
                    raise VerifyError("tree_symlink", "发布目录包含符号链接")
                paths.extend(base / name for name in sorted(names))
        for path in paths:
            relative = path.relative_to(tree_root).as_posix()
            if not _managed(relative):
                continue
            if path.name == ".env" or path.name.startswith(".env."):
                raise VerifyError("secret_in_release_scope", "受管代码目录中发现环境秘密文件")
            value = _regular(path)
            files[relative] = hashlib.sha256(value).hexdigest()
            if relative in {f"web/dist/{name}" for name in DIST_METADATA}:
                metadata[path.name] = value.decode("utf-8")
    return {"schema_version": RELEASE_SCHEMA, "files": files,
            "directories": sorted(directories), "browser_metadata": metadata}


def _json_object(raw: str) -> dict:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("not_object")
    return value


def _release_document(raw: str) -> dict:
    try:
        document = _json_object(raw)
        if set(document) != {"schema_version", "files", "directories", "browser_metadata"} or document["schema_version"] != RELEASE_SCHEMA:
            raise ValueError("schema")
        files = document["files"]
        if not isinstance(files, dict) or not files or not isinstance(document["browser_metadata"], dict):
            raise ValueError("files")
        for name, digest in files.items():
            path = PurePosixPath(name)
            if (not _managed(name) or path.is_absolute() or path.as_posix() != name
                    or ".." in path.parts or "\\" in name or "\0" in name
                    or not isinstance(digest, str) or len(digest) != 64 or not set(digest) <= _HEX):
                raise ValueError("entry")
        directories = document["directories"]
        if not isinstance(directories, list) or directories != sorted(set(directories)):
            raise ValueError("directories")
        # Git cannot contain empty directories. Every observed directory must
        # therefore be a parent of an observed release file; otherwise a blank
        # namespace or unreadable/partially captured subtree could hide here.
        expected_dirs = {str(parent) for name in files for parent in PurePosixPath(name).parents
                         if str(parent) != "."}
        if set(directories) != expected_dirs:
            raise VerifyError("release_directory_closure", "发布目录集合不能包含空项或遗漏文件的父目录")
    except (ValueError, TypeError, AttributeError, RecursionError) as exc:
        raise VerifyError("release_manifest_invalid", "需要完整 v1 发布清单，旧 Python 子集清单不能作为发布证明") from exc
    return document


def _revision_browser_fingerprint(revision: str, repo_root) -> str:
    from scripts.verify_browser_dist import REQUIRED_WEB_FILES, FROZEN_CONTENT_FILES
    names = {f"web/{name}" for name in REQUIRED_WEB_FILES}
    names.update(f"content/{name}" for name in FROZEN_CONTENT_FILES)
    names.update(name for name in _revision_sources(revision, repo_root) if name.startswith("web/src/"))
    digest = hashlib.sha256()
    for name in sorted(names, key=lambda name: name.encode("utf-16-be")):
        value = _git(repo_root, "show", f"{revision}:{name}")
        digest.update(f"{name}\0{len(value)}\0".encode())
        digest.update(value)
        digest.update(b"\0")
    return digest.hexdigest()


def verify_browser_release(document: dict, *, expected_dist_manifest: Path,
                           revision: str, repo_root) -> int:
    """Bind observed output to separately retained build bytes AND git inputs."""
    try:
        files = document["files"]
        metadata = document["browser_metadata"]
        if set(metadata) != set(DIST_METADATA):
            raise ValueError("missing_metadata")
        for name, raw in metadata.items():
            if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != files.get(f"web/dist/{name}"):
                raise ValueError("metadata_hash")
        expected_bytes = _regular(expected_dist_manifest)
        if hashlib.sha256(expected_bytes).hexdigest() != files.get("web/dist/browser-dist-sha256.json"):
            raise ValueError("untrusted_build_manifest")
        manifest = _json_object(metadata["browser-dist-sha256.json"])
        if (manifest.get("schema_version") != "nmu.browser-dist-sha256.v1"
                or manifest.get("algorithm") != "SHA-256" or manifest.get("root") != "dist/"
                or manifest.get("excluded_paths") != ["browser-dist-sha256.json"]):
            raise ValueError("dist_schema")
        expected = {"web/dist/browser-dist-sha256.json": hashlib.sha256(expected_bytes).hexdigest()}
        for row in manifest["files"]:
            name = row["path"]
            path = PurePosixPath(name)
            key = f"web/dist/{name}"
            if (key in expected or not name or path.is_absolute() or path.as_posix() != name
                    or ".." in path.parts or "\\" in name or type(row["size"]) is not int or row["size"] < 0
                    or not isinstance(row["sha256"], str) or len(row["sha256"]) != 64 or not set(row["sha256"]) <= _HEX):
                raise ValueError("dist_entry")
            expected[key] = row["sha256"]
        actual = {name: digest for name, digest in files.items() if name.startswith("web/dist/")}
        if expected != actual or "web/dist/index.html" not in actual:
            raise ValueError("dist_file_set_or_bytes")
        fingerprint = _revision_browser_fingerprint(revision, repo_root)
        provenance = _json_object(metadata["build-provenance.json"])
        if (metadata["build-fingerprint.sha256"].strip() != fingerprint
                or provenance.get("schema_version") != "nmu.browser-build-provenance.v1"
                or provenance.get("build_input_fingerprint_sha256") != fingerprint):
            raise ValueError("build_source_mismatch")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        raise VerifyError("browser_release_unverified", "前端构件未与独立构建清单及候选源码绑定") from exc
    return len(expected)


def _remote_command(tree_root: str) -> str:
    # Invoke the audited read-only collector directly: no pipeline can swallow
    # a traversal failure, and shell metacharacters in either path are quoted.
    root = shlex.quote(tree_root)
    script = shlex.quote(str(Path(tree_root) / "scripts/verify_deployed_tree.py"))
    return f"python3 -I -B {script} --capture --tree-root {root}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", help="目标机上取到的清单文件；- 表示标准输入")
    parser.add_argument("--revision", help="拿哪个提交来比")
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--print-remote-command", action="store_true")
    parser.add_argument("--tree-root", default="/opt/nmu/app")
    parser.add_argument("--capture", action="store_true", help="只读输出完整源码部署树与前端构件清单")
    parser.add_argument("--expected-dist-manifest", type=Path,
                        help="从受控构建机独立保存的 browser-dist-sha256.json；不得从被验部署取来代替")
    args = parser.parse_args(argv)

    if args.print_remote_command:
        print(_remote_command(args.tree_root))
        return 0
    if args.capture:
        try:
            print(json.dumps(capture(Path(args.tree_root)), ensure_ascii=False, sort_keys=True))
        except (OSError, UnicodeError, VerifyError) as exc:
            print(f"INVALID code={getattr(exc, 'code', 'capture_failed')}", file=sys.stderr)
            return 2
        return 0
    if not args.manifest or not args.revision or args.expected_dist_manifest is None:
        parser.error("需要 --manifest、--revision 和独立的 --expected-dist-manifest")

    root = Path(args.repo_root) if args.repo_root else Path(__file__).resolve().parents[1]
    try:
        text = (sys.stdin.read() if args.manifest == "-"
                else Path(args.manifest).read_text(encoding="utf-8"))
        document = _release_document(text)
        manifest = {name: digest for name, digest in document["files"].items() if _source(name)}
        report = compare(manifest, revision=args.revision, repo_root=root)
        browser_files = verify_browser_release(document, expected_dist_manifest=args.expected_dist_manifest,
                                               revision=args.revision, repo_root=root)
    except VerifyError as exc:
        print(f"INVALID code={exc.code}", file=sys.stderr)
        print(exc.detail, file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"INVALID code=manifest_unreadable\n{exc}", file=sys.stderr)
        return 2

    total = len(manifest)
    if report.matches:
        print(f"MATCH revision={args.revision} source_files={total} browser_files={browser_files} scope=source-release-v1")
        return 0
    print(f"DRIFT revision={args.revision} files={total} "
          f"identical={report.identical} differing={len(report.differing)} "
          f"absent_in_revision={len(report.absent_in_revision)}")
    for name in report.differing:
        print(f"  differs   {name}")
    for name in report.absent_in_revision:
        print(f"  not-in-rev {name}")
    for name in report.missing_on_deployment:
        print(f"  missing    {name}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
