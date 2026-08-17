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

    # 1) 在目标机上跑这条（只读，只有 find/sha256sum）
    scripts/verify_deployed_tree.py --print-remote-command --tree-root /opt/nmu/app

    # 2) 把输出存成 manifest.txt，然后在本仓库里比
    scripts/verify_deployed_tree.py --manifest manifest.txt --revision 167273f

退出码：0 = 完全一致；1 = 有差异；2 = 清单或提交本身不可用（判定无效）。
**退出码 1 和 2 必须分开看**：1 是"量到了，不一样"，2 是"根本没量成"。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import subprocess
import sys

#: 空串的 sha256。清单里出现它，说明那一行量的是"什么都没有"——通常是某条
#: 命令静默失败、输出为空，而调用方把空输出的摘要当成了文件指纹。
#: 这个常量存在是因为我 2026-08-17 真的这么错过一次，并据此得出了
#: "生产代码不对应任何提交"的错误结论。
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_ALLOWED_GIT_SUBCOMMANDS = ("cat-file", "show", "rev-parse")
_HEX = frozenset("0123456789abcdef")


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

    @property
    def matches(self) -> bool:
        return not self.differing and not self.absent_in_revision


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
    import hashlib

    try:
        _git(repo_root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    except subprocess.SubprocessError as exc:
        raise VerifyError("revision_unknown", f"取不到提交 {revision}") from exc

    identical = 0
    differing: list[str] = []
    absent: list[str] = []
    for name in sorted(manifest):
        try:
            _git(repo_root, "cat-file", "-e", f"{revision}:{name}")
        except subprocess.SubprocessError:
            absent.append(name)
            continue
        blob = _git(repo_root, "show", f"{revision}:{name}")
        if hashlib.sha256(blob).hexdigest() == manifest[name]:
            identical += 1
        else:
            differing.append(name)
    return Report(identical=identical, differing=differing,
                  absent_in_revision=absent)


def _remote_command(tree_root: str) -> str:
    # find + sha256sum，没有任何写操作。路径打印成相对于 tree_root 的形式。
    return (f"cd {tree_root} && find app scripts -type f -name '*.py' "
            "| LC_ALL=C sort | xargs sha256sum "
            "| awk '{print $2\" \"$1}' | sed 's|^\\./||'")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", help="目标机上取到的清单文件；- 表示标准输入")
    parser.add_argument("--revision", help="拿哪个提交来比")
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--print-remote-command", action="store_true")
    parser.add_argument("--tree-root", default="/opt/nmu/app")
    args = parser.parse_args(argv)

    if args.print_remote_command:
        print(_remote_command(args.tree_root))
        return 0
    if not args.manifest or not args.revision:
        parser.error("需要 --manifest 与 --revision（或用 --print-remote-command）")

    from pathlib import Path
    root = Path(args.repo_root) if args.repo_root else Path(__file__).resolve().parents[1]
    try:
        text = (sys.stdin.read() if args.manifest == "-"
                else Path(args.manifest).read_text(encoding="utf-8"))
        manifest = parse_manifest(text)
        report = compare(manifest, revision=args.revision, repo_root=root)
    except VerifyError as exc:
        print(f"INVALID code={exc.code}", file=sys.stderr)
        print(exc.detail, file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"INVALID code=manifest_unreadable\n{exc}", file=sys.stderr)
        return 2

    total = len(manifest)
    if report.matches:
        print(f"MATCH revision={args.revision} files={total} identical={total}")
        return 0
    print(f"DRIFT revision={args.revision} files={total} "
          f"identical={report.identical} differing={len(report.differing)} "
          f"absent_in_revision={len(report.absent_in_revision)}")
    for name in report.differing:
        print(f"  differs   {name}")
    for name in report.absent_in_revision:
        print(f"  not-in-rev {name}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
