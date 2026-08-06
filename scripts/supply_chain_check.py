#!/usr/bin/env python3
"""把一台机器上真实装着的包，和那份哈希锁逐个对账。

写这个脚本的直接原因：2026-08-06 查出生产 venv 装了 71 个包，而
requirements-deploy.lock.txt 只审计过 59 条。多出来的 18 个（pytest、
onnxruntime、protobuf、numpy…）没有人看过，也没有任何哈希在安装时被校验过。
整套"钉 digest + --require-hashes"只覆盖了容器那条路，而线上跑的是裸机。
锁本身拦不住这件事——只有把它和真实环境比一次才知道。

判据：
  unlocked   装了、锁里根本没有这个名字 → FAIL。这是没人审过的攻击面。
  drift      装了、锁里有这个名字，但版本不在锁允许的集合里 → FAIL。
  absent     锁里有、机器上没有 → 看 marker：marker 为假是应该的（win32、
             低版本 Python 垫片），marker 为真却缺席才是 FAIL。
  floor      机器的 Python 低于锁编译时的下限 → FAIL，锁的解析对它不成立。
  hashed     锁里任何一条没带 --hash → FAIL。少了它前面全部免谈。

诚实边界：本检查证明的是**集合一致**，不是"这些文件当初真的过了哈希校验"。
pip 装完不留哪个哈希被用过的记录，事后无法反推。要那个保证，只能靠安装那一刻
就走 --require-hashes（Dockerfile 与 DEPLOY.md 的裸机步骤都是这么写的）。

同名多版本是正常的：universal 锁里 websockets 有 16.1.1（<3.11）和 17.0.1
（>=3.11）两条。按"集合包含"判，别按单值等号判。

用法：
  supply_chain_check.py --python /opt/nmu/venv/bin/python
  supply_chain_check.py --python python3 --lock requirements-deploy.lock.txt --json
  supply_chain_check.py --probe-json saved.json      # 离线复核已抓好的快照
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

DEFAULT_LOCK = Path(__file__).resolve().parents[1] / "requirements-deploy.lock.txt"

# 只认锁里真正出现过的 marker 变量。多一个不认识的就抛，不猜。
MARKER_VARIABLES = frozenset({
    "implementation_name",
    "os_name",
    "platform_machine",
    "platform_python_implementation",
    "platform_system",
    "python_full_version",
    "python_version",
    "sys_platform",
})
_VERSION_VARIABLES = frozenset({"python_full_version", "python_version"})

_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s;\\]+)"
    r"(?:\s*;\s*(?P<marker>.*?))?\s*\\?\s*$")
_CLAUSE = re.compile(
    r"^(?P<var>[a-z_]+)\s*(?P<op>[=!<>]=|<|>)\s*(?P<quote>['\"])(?P<value>[^'\"]*)(?P=quote)$")
_PYTHON_FLOOR = re.compile(r"--python-version\s+(\d+)\.(\d+)")


class MarkerError(ValueError):
    """marker 里出现了这个求值器没有把握的东西。宁可炸，不要猜错。"""


def _version_key(text: str) -> tuple[tuple[int, ...], int]:
    """把 '3.10.20' / '3.15.0b1' 变成可比较的键。预发布排在同号正式版前面。

    只处理 CPython 版本号这一档形状。碰到真解析不了的直接抛——一个悄悄算错的
    marker 会让 absent 判成"本来就该缺"，正好把该报的问题吞掉。
    """
    numbers: list[int] = []
    prerelease = 1                                          # 1 = 正式版，排在预发布后
    for part in text.split("."):
        digits = re.match(r"^(\d+)", part)
        if digits is None:
            raise MarkerError(f"看不懂的版本号：{text!r}")
        numbers.append(int(digits.group(1)))
        if digits.end() != len(part):
            prerelease = 0
    return tuple(numbers), prerelease


def _compare(left: str, op: str, right: str, numeric: bool) -> bool:
    if numeric:
        a, b = _version_key(left), _version_key(right)
    else:
        if op not in ("==", "!="):
            raise MarkerError(f"字符串变量不支持 {op}")
        a, b = left, right
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    raise MarkerError(f"看不懂的运算符：{op}")


def evaluate_marker(marker: str, environment: dict[str, str]) -> bool:
    """求值 PEP 508 marker 的一个子集：形如 `变量 op '字面量'`，用 and/or 连接。

    锁里现存的八种表达式都在这个子集内，且都没有括号。真出现括号、in/not in、
    或者未知变量，抛 MarkerError 由调用方报成一条硬失败——不要默默当真或当假。
    """
    if "(" in marker or ")" in marker:
        raise MarkerError(f"这个求值器不处理括号：{marker!r}")

    # 先按 or 切，再按 and 切。子集里没有括号，所以 or 的优先级最低这一点足够。
    for conjunction in marker.split(" or "):
        if all(_evaluate_clause(part.strip(), environment)
               for part in conjunction.split(" and ")):
            return True
    return False


def _evaluate_clause(clause: str, environment: dict[str, str]) -> bool:
    matched = _CLAUSE.match(clause)
    if matched is None:
        raise MarkerError(f"看不懂的 marker 子句：{clause!r}")
    variable = matched.group("var")
    if variable not in MARKER_VARIABLES:
        raise MarkerError(f"没见过的 marker 变量：{variable}")
    if variable not in environment:
        raise MarkerError(f"环境快照里缺变量：{variable}")
    return _compare(environment[variable], matched.group("op"), matched.group("value"),
                    numeric=variable in _VERSION_VARIABLES)


def canonical(name: str) -> str:
    """PEP 503 规范化。Piper_TTS、piper-tts、piper.tts 是同一个包。"""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


class LockEntry:
    def __init__(self, name: str, version: str, marker: str | None) -> None:
        self.name = canonical(name)
        self.raw_name = name
        self.version = version
        self.marker = marker
        self.hashes: list[str] = []


def parse_lock(text: str) -> tuple[list[LockEntry], tuple[int, int] | None]:
    """读 uv 生成的锁。返回 (条目, Python 下限)。

    下限从头部那行复现命令里抠出来。它不是装饰：锁是按某个 Python 版本解析出来
    的，拿到更低的解释器上，解析结论不成立（3.10 需要的 tomli/exceptiongroup
    垫片，3.12 的锁里根本没有）。
    """
    entries: list[LockEntry] = []
    floor: tuple[int, int] | None = None
    current: LockEntry | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            found = _PYTHON_FLOOR.search(stripped)
            if found and floor is None:
                floor = (int(found.group(1)), int(found.group(2)))
            continue
        if stripped.startswith("--hash="):
            if current is None:
                raise ValueError(f"孤立的 --hash 行：{stripped}")
            current.hashes.append(stripped.split("=", 1)[1].rstrip(" \\"))
            continue
        if stripped.startswith("-"):
            continue                                        # -r / --index-url 之类
        matched = _REQUIREMENT.match(stripped)
        if matched is None:
            raise ValueError(f"看不懂的锁行：{stripped}")
        current = LockEntry(matched.group("name"), matched.group("version"),
                            (matched.group("marker") or "").strip() or None)
        entries.append(current)
    return entries, floor


# 一次子进程同时取回 marker 环境和已装分发包。用目标解释器自己回答，不靠猜；
# 不加 -I：应用平时也不是 -I 跑的，隔离会连带藏掉 user site 里的包。
_PROBE = r"""
import json, os, platform, sys
import importlib.metadata as metadata

seen = {}
for dist in metadata.distributions():
    name = dist.metadata["Name"]
    if name:
        seen.setdefault(name, dist.version)
print(json.dumps({
    "environment": {
        "implementation_name": sys.implementation.name,
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_python_implementation": platform.python_implementation(),
        "platform_system": platform.system(),
        "python_full_version": platform.python_version(),
        "python_version": ".".join(platform.python_version_tuple()[:2]),
        "sys_platform": sys.platform,
    },
    "distributions": seen,
}))
"""


def probe(python_executable: str) -> dict:
    out = subprocess.run([python_executable, "-c", _PROBE],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"探测 {python_executable} 失败：{out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


class Finding:
    def __init__(self, kind: str, name: str, detail: str) -> None:
        self.kind = kind
        self.name = name
        self.detail = detail

    def line(self) -> str:
        return f"  [{self.kind}] {self.name} — {self.detail}"


def compare(entries: list[LockEntry], floor: tuple[int, int] | None,
            snapshot: dict) -> tuple[list[Finding], list[Finding], dict]:
    """返回 (硬失败, 提示, 统计)。"""
    environment = snapshot["environment"]
    installed = {canonical(k): v for k, v in snapshot["distributions"].items()}
    failures: list[Finding] = []
    notes: list[Finding] = []

    if floor is not None:
        actual = _version_key(environment["python_version"])[0]
        if actual < floor:
            failures.append(Finding(
                "floor", "python",
                f"机器是 {environment['python_full_version']}，锁按 "
                f"{floor[0]}.{floor[1]} 解析，低于下限则锁不成立"))

    unhashed = [e.raw_name for e in entries if not e.hashes]
    if unhashed:
        failures.append(Finding(
            "hashed", "锁本身",
            f"{len(unhashed)} 条没有 --hash（{', '.join(unhashed[:5])}…）；"
            "锁必须用 --generate-hashes 重出"))

    allowed: dict[str, set[str]] = {}
    expected: dict[str, LockEntry] = {}
    for entry in entries:
        allowed.setdefault(entry.name, set()).add(entry.version)
        if entry.marker is None:
            expected[entry.name] = entry
            continue
        try:
            if evaluate_marker(entry.marker, environment):
                expected[entry.name] = entry
        except MarkerError as error:
            failures.append(Finding("marker", entry.raw_name, str(error)))

    for name in sorted(installed):
        if name not in allowed:
            failures.append(Finding(
                "unlocked", name,
                f"装着 {installed[name]}，锁里没有这个名字——没人审过，"
                "安装时也没有哈希被校验"))
        elif installed[name] not in allowed[name]:
            failures.append(Finding(
                "drift", name,
                f"装着 {installed[name]}，锁允许 {'/'.join(sorted(allowed[name]))}"))

    for name in sorted(set(allowed) - set(installed)):
        entry = expected.get(name)
        if entry is None:
            markers = sorted({e.marker for e in entries
                              if e.name == name and e.marker})
            notes.append(Finding("absent", name,
                                 f"marker 为假，本来就不该装（{'；'.join(markers)}）"))
        else:
            failures.append(Finding(
                "absent", name,
                f"锁要求 {entry.version} 且 marker 成立，机器上却没有"))

    stats = {
        "locked_names": len(allowed),
        "locked_entries": len(entries),
        "installed": len(installed),
        "expected_here": len(expected),
        "python": environment["python_full_version"],
    }
    return failures, notes, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--python", default=None,
                        help="要对账的解释器，例如 /opt/nmu/venv/bin/python")
    parser.add_argument("--probe-json", type=Path, default=None,
                        help="离线：已抓好的探测快照")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if (args.python is None) == (args.probe_json is None):
        parser.error("--python 与 --probe-json 二选一")

    entries, floor = parse_lock(args.lock.read_text(encoding="utf-8"))
    snapshot = (json.loads(args.probe_json.read_text(encoding="utf-8"))
                if args.probe_json else probe(args.python))
    failures, notes, stats = compare(entries, floor, snapshot)

    if args.json:
        print(json.dumps({
            "ok": not failures,
            "stats": stats,
            "failures": [{"kind": f.kind, "name": f.name, "detail": f.detail}
                         for f in failures],
            "notes": [{"kind": n.kind, "name": n.name, "detail": n.detail}
                      for n in notes],
        }, ensure_ascii=False, indent=2))
        return 1 if failures else 0

    print(f"锁 {args.lock}：{stats['locked_entries']} 条 / {stats['locked_names']} 个包名")
    print(f"机器 Python {stats['python']}：装着 {stats['installed']} 个，"
          f"按 marker 应有 {stats['expected_here']} 个")
    if notes:
        print("提示：")
        for note in notes:
            print(note.line())
    if failures:
        print(f"未对上 {len(failures)} 处：")
        for failure in failures:
            print(failure.line())
        print("\n[FAIL] 运行环境与哈希锁不一致。")
        return 1
    print("\n[PASS] 运行环境与哈希锁逐个对上了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
