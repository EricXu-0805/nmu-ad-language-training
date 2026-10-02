#!/usr/bin/env python3
"""这台裸机的操作系统还欠多少安全补丁——用 apt 自己的账回答。

写这个脚本的直接原因：2026-08-06 查出生产机 `APT::Periodic::Unattended-Upgrade`
是 0——包列表天天刷新，补丁一个不装，积到了 106 个待升级、其中 93 个来自
jammy-security。没有任何东西在看这个数字，它只会越积越多。

为什么不用 OSV 扫 dpkg（实测过才这么定，2026-08-06）：
  给打满补丁的包查 Ubuntu:22.04 生态，bash/gzip/libc6/openssh 都是 0 条，
  tar 剩 2 条"Ubuntu 已知、按优先级决定不修"的记录。也就是说 OSV 能给的
  可执行信号 ≈ apt 的 security 积压数，再多出来的全是 Ubuntu 已经分诊掉的
  噪声。可执行的那个数字直接问 apt 拿，不绕远路。

判据：
  security 积压 > 0            → 退出 1。修法是装补丁，不是改这个脚本。
  包列表比 --max-age-days 旧   → 退出 1。拿陈旧列表算出的 0 不可信。
  需要重启（内核已换未生效）    → 只报告，不算失败。这台机器还跑着别的服务，
                                 重启由人挑时间，脚本不该替人做这个决定。
  被保留的包（kept back）      → 退出 1。2026-09-11 查出 `apt-get -s upgrade` 会把
                                 需要装新依赖的内核/netplan 更新整组"保留"、不出
                                 Inst 行，5 个安全内核包积着而这里报 0。所以模拟改用
                                 full-upgrade（把保留的包一起算），万一 full-upgrade
                                 仍有保留项，按名字报失败，不当作通过。
  nmu/nginx 还映射着被换掉的库 → 退出 1，给出重启命令。补丁装在盘上不等于修好：
                                 进程不重启就一直跑着旧 libssl/libsqlite3/zlib。夜间
                                 自动装安全补丁之后盘上的库天天在换，而这台机器没装
                                 update-notifier，库更新不会立任何旗子，只能自己去
                                 /proc 里看。其余单元（sing-box、dbus…）只报告，等重启窗口。
  不是 root 跑的、读不了 /proc → 退出 1。别人的进程映射读不到，那样的 0 是没看。
  已装内核比正在跑的新          → 只报告。内核类包不自动装，重启窗口由人挑。

顺手职责：--inventory 把 dpkg 全量清单落盘（供应链审计里"裸机 OS 包不在任何
清单里"那条的记录面）。清单跟着系统走、会漂，所以落在机器上按周留档，不进 git。

用法：
  os_security_check.py                          # 人读输出（要 root）
  os_security_check.py --json
  os_security_check.py --inventory /opt/nmu/backups/os-packages.txt
  os_security_check.py --simulate-file out.txt  # 测试/离线：喂一份 apt-get -s 输出
  os_security_check.py --proc-dir P --boot-dir B  # 测试/离线：读假的 /proc、/boot
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys

APT_LISTS = Path("/var/lib/apt/lists")
REBOOT_REQUIRED = Path("/var/run/reboot-required")
PROC = Path("/proc")
BOOT = Path("/boot")
# 跑在旧库上就要人去重启的单元：nmu 直接服务受试者，nginx 直面公网，重启都只要几秒。
APP_UNITS = ("nmu.service", "nginx.service")

# apt-get -s upgrade 的行形如：
#   Inst tar [1.34+dfsg-1ubuntu0.1.22.04.2] (1.34+dfsg-1ubuntu0.1.22.04.6
#        Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
_INST = re.compile(r"^Inst\s+(?P<name>\S+)\s+\[(?P<old>[^\]]+)\]\s+\((?P<new>\S+)\s+(?P<origins>[^\[]*)\[")
# apt-get 把因需要新依赖而不装的包列成一块：
#   The following packages have been kept back:
#     libnetplan0 linux-generic linux-headers-generic linux-image-generic netplan.io
_KEPT_BACK_HEADER = re.compile(r"^The following packages have been kept back:")
# /proc/<pid>/maps 的一行以路径收尾。dpkg 换文件是先写新文件再改名盖过去，旧 inode
# 被删，还映射着它的进程这一行就带 " (deleted)"。只认 apt 管的 /lib、/usr/lib 下的 .so。
_DELETED_LIBRARY = re.compile(
    r"\s(/(?:usr/)?lib(?:32|64|x32)?/\S+\.so(?:\.[0-9][0-9.]*)?) \(deleted\)$")
_KERNEL_RELEASE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-(\d+)")


class PendingUpgrade:
    def __init__(self, name: str, old: str, new: str, security: bool) -> None:
        self.name = name
        self.old = old
        self.new = new
        self.security = security


def parse_simulation(text: str) -> list[PendingUpgrade]:
    pending: list[PendingUpgrade] = []
    for line in text.splitlines():
        matched = _INST.match(line)
        if matched is None:
            continue
        pending.append(PendingUpgrade(
            matched.group("name"), matched.group("old"), matched.group("new"),
            "-security" in matched.group("origins")))
    return pending


def parse_kept_back(text: str) -> list[str]:
    """被保留的包名。紧跟标题行的缩进行都是包名，遇到不缩进的行即结束。"""
    names: list[str] = []
    collecting = False
    for line in text.splitlines():
        if _KEPT_BACK_HEADER.match(line):
            collecting = True
            continue
        if collecting:
            if line.startswith((" ", "\t")):
                names.extend(line.split())
            else:
                collecting = False
    return names


def lists_age(lists_dir: Path, now: datetime) -> timedelta | None:
    """包列表的年龄 = 最新一个索引文件距今多久。目录不存在/为空返回 None。"""
    newest: float | None = None
    if not lists_dir.is_dir():
        return None
    for entry in lists_dir.iterdir():
        if entry.is_file() and not entry.name.startswith(("lock", "partial")):
            stamp = entry.stat().st_mtime
            newest = stamp if newest is None else max(newest, stamp)
    if newest is None:
        return None
    return now - datetime.fromtimestamp(newest)


def run_simulation() -> str:
    # -s 是纯模拟：不加锁、不改系统，普通读权限即可。
    # full-upgrade 而不是 upgrade：后者把需要装新依赖的更新（新内核带 modules/headers、
    # netplan 拆包）整组"保留"、不出 Inst 行，安全内核积着这里却报 0（2026-09-11 查出）。
    # 锁定 C locale:ssh 会把 Mac 的 LANG/LC_* 带过去,apt 的"kept back"标题一被翻译,
    # 下面按英文标题解析的保留项就悄悄变成空(Inst 行不翻译,只有这一层会失明)。
    done = subprocess.run(
        ["apt-get", "-s", "full-upgrade"], capture_output=True, text=True, timeout=300,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"})
    if done.returncode != 0:
        raise RuntimeError(f"apt-get -s full-upgrade 失败：{done.stderr.strip()[:300]}")
    return done.stdout


def read_holds() -> list[str]:
    """人为 apt-mark hold 的包。查不动就当没有(hold 是例外,不该让主判据失明)。"""
    try:
        done = subprocess.run(
            ["apt-mark", "showhold"], capture_output=True, text=True, timeout=60,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"})
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def write_inventory(path: Path) -> int:
    done = subprocess.run(
        ["dpkg-query", "-W", "-f", "${Package}\\t${Version}\\t${Architecture}\\n"],
        capture_output=True, text=True, timeout=300)
    if done.returncode != 0:
        raise RuntimeError(f"dpkg-query 失败：{done.stderr.strip()[:300]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(done.stdout, encoding="utf-8")
    return len(done.stdout.splitlines())


def _unit_of(cgroup_text: str) -> str:
    """进程所属的 systemd 单元。登录会话与用户管理器来来去去，合成 user.slice 一组。"""
    path = ""
    for line in cgroup_text.splitlines():
        hierarchy, _, rest = line.partition(":")
        controllers, _, candidate = rest.partition(":")
        if hierarchy == "0" and not controllers:      # cgroup v2
            path = candidate
            break
        if controllers == "name=systemd":             # v1/混合层级
            path = candidate
    parts = [part for part in path.split("/") if part]
    if parts and parts[0] == "user.slice":
        return "user.slice"
    for part in reversed(parts):
        if part.endswith((".service", ".scope")):
            return part
    return parts[-1] if parts else "/"


def stale_library_units(proc: Path = PROC) -> dict[str, list[str]]:
    """还映射着被替换掉的共享库的进程，按单元归组：{单元: [库路径…]}。

    扫描途中退出的进程跳过。目录在、映射却读不了的进程让整次扫描抛 PermissionError：
    跳过它，就是把没看过的当成干净的。
    """
    found: dict[str, set[str]] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            maps = (entry / "maps").read_text(encoding="utf-8", errors="replace")
            if " (deleted)" not in maps:
                continue
            libraries = {matched.group(1) for matched in
                         map(_DELETED_LIBRARY.search, maps.splitlines()) if matched}
            if not libraries:
                continue
            cgroup = (entry / "cgroup").read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, ProcessLookupError):
            continue
        found.setdefault(_unit_of(cgroup), set()).update(libraries)
    return {unit: sorted(libraries) for unit, libraries in sorted(found.items())}


def kernel_key(release: str) -> tuple[int, ...]:
    """内核版本按数字比。按字面比，'5.15.0-25' 会排在 '5.15.0-198' 后面。"""
    matched = _KERNEL_RELEASE.match(release)
    return tuple(int(group) for group in matched.groups()) if matched else ()


def newest_installed_kernel(boot: Path = BOOT) -> str | None:
    """/boot 里最新的内核映像（GRUB 默认引导的就是它）。vmlinuz、vmlinuz.old 是链接，不算。"""
    releases = [entry.name.removeprefix("vmlinuz-") for entry in boot.glob("vmlinuz-*")
                if not entry.is_symlink()]
    releases = [release for release in releases if kernel_key(release)]
    return max(releases, key=kernel_key) if releases else None


def evaluate(pending: list[PendingUpgrade], age: timedelta | None,
             max_age: timedelta, reboot_required: bool,
             kept_back: list[str] | None = None,
             held: list[str] | None = None) -> tuple[list[str], list[str]]:
    """返回 (硬失败, 提示)。"""
    failures: list[str] = []
    notes: list[str] = []
    security = [p for p in pending if p.security]
    other = [p for p in pending if not p.security]
    held_set = set(held or [])
    unresolved = [name for name in (kept_back or []) if name not in held_set]
    held_back = [name for name in (kept_back or []) if name in held_set]
    if unresolved:
        failures.append(
            f"{len(unresolved)} 个包被保留未算进积压（{'、'.join(unresolved[:6])}…）；"
            "保留的包不出现在模拟安装里，这个 0 不可信")
    if held_back:
        # 人为 apt-mark hold 的是运维的决定,不是统计缺口:只报出来,不算失败。
        notes.append(f"{len(held_back)} 个包被人为 hold（{'、'.join(held_back[:6])}），不算积压")

    if age is None:
        failures.append("读不到 apt 包列表，无法判断积压——不当作通过")
    elif age > max_age:
        failures.append(
            f"包列表已 {age.days} 天没刷新（上限 {max_age.days} 天）；"
            "拿陈旧列表算出的积压数不可信，先 apt-get update")

    if security:
        sample = "、".join(p.name for p in security[:6])
        failures.append(
            f"{len(security)} 个安全更新待安装（{sample}…）；"
            "修法：apt-get full-upgrade（内核类更新装完记得挑时间重启）")
    if other:
        notes.append(f"另有 {len(other)} 个非安全更新待装（不算失败）")
    if reboot_required:
        notes.append("内核/底层库已更新但未重启，旧代码仍在内存里跑；"
                     "这台机器还有别的服务，重启由人挑时间")
    return failures, notes


def evaluate_running(stale_units: dict[str, list[str]] | None, scan_problem: str | None,
                     kernel_running: str, kernel_newest: str | None
                     ) -> tuple[list[str], list[str]]:
    """补丁装在盘上、内存里跑的还是旧代码。返回 (硬失败, 提示)。"""
    failures: list[str] = []
    notes: list[str] = []
    if scan_problem is not None:
        failures.append(f"查不了哪些进程还在用被替换的旧库：{scan_problem}；没查过不当作通过")
    stale_units = stale_units or {}
    for unit in APP_UNITS:
        libraries = stale_units.get(unit)
        if libraries:
            names = "、".join(Path(library).name for library in libraries[:4])
            failures.append(
                f"{unit} 还在用已被替换的旧库（{names}{'…' if len(libraries) > 4 else ''}）："
                "补丁装在盘上，内存里跑的还是旧代码；修法：挑没人练的时候 "
                f"systemctl restart {unit.removesuffix('.service')}（几秒），再重跑本检查")
    others = [unit for unit in stale_units if unit not in APP_UNITS]
    if others:
        notes.append(
            f"另有 {len(others)} 个单元还在用已被替换的旧库（{'、'.join(others[:6])}"
            f"{'…' if len(others) > 6 else ''}），不算失败，下一个重启窗口一并换新")
    if (kernel_newest is not None and kernel_key(kernel_running)
            and kernel_key(kernel_newest) > kernel_key(kernel_running)):
        notes.append(
            f"已装内核 {kernel_newest} 比正在跑的 {kernel_running} 新，重启才生效；"
            "内核类包不自动装也不自动重启，重启窗口由人挑")
    return failures, notes


def assess(simulation: str, max_age: timedelta, *, now: datetime | None = None,
           lists_dir: Path = APT_LISTS, held: list[str] | None = None,
           proc: Path = PROC, boot: Path = BOOT, running_kernel: str | None = None,
           euid: int | None = None) -> dict:
    """一次完整判定，周检单元与 preflight --os 共用这一份，口径不会分岔。

    apt 模拟输出由调用方给（真跑或 --simulate-file）；held 为 None 时现场读 apt-mark。
    返回值就是 --json 的主体。
    """
    pending = parse_simulation(simulation)
    kept_back = parse_kept_back(simulation)
    held = read_holds() if held is None else held
    age = lists_age(lists_dir, now or datetime.now())
    reboot_required = REBOOT_REQUIRED.exists()
    failures, notes = evaluate(pending, age, max_age, reboot_required, kept_back, held)

    stale: dict[str, list[str]] | None = None
    scan_problem: str | None = None
    if (os.geteuid() if euid is None else euid) != 0:
        scan_problem = "不是 root 在跑，读不到别的用户进程的内存映射"
    else:
        try:
            stale = stale_library_units(proc)
        except OSError as error:
            scan_problem = f"读不了 {proc} 下的进程映射（{error.strerror or error}）"
    kernel_running = running_kernel or os.uname().release
    kernel_newest = newest_installed_kernel(boot)
    running_failures, running_notes = evaluate_running(
        stale, scan_problem, kernel_running, kernel_newest)
    failures += running_failures
    notes += running_notes
    return {
        "ok": not failures,
        "security_pending": sum(1 for p in pending if p.security),
        "other_pending": sum(1 for p in pending if not p.security),
        "lists_age_days": None if age is None else age.days,
        "reboot_required": reboot_required,
        "kept_back": kept_back,
        "held": held,
        "stale_library_units": stale,
        "kernel_running": kernel_running,
        "kernel_newest_installed": kernel_newest,
        "failures": failures,
        "notes": notes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-age-days", type=int, default=3,
                        help="包列表最大可接受年龄（默认 3 天）")
    parser.add_argument("--inventory", type=Path, default=None,
                        help="顺手把 dpkg 全量清单写到这里")
    parser.add_argument("--simulate-file", type=Path, default=None,
                        help="测试/离线：读这份 apt-get -s upgrade 输出，不真跑 apt")
    parser.add_argument("--lists-dir", type=Path, default=APT_LISTS)
    parser.add_argument("--proc-dir", type=Path, default=PROC,
                        help="测试/离线：从这棵目录找还在用旧库的进程")
    parser.add_argument("--boot-dir", type=Path, default=BOOT,
                        help="测试/离线：从这个目录找已装的内核")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        text = (args.simulate_file.read_text(encoding="utf-8")
                if args.simulate_file else run_simulation())
    except (OSError, RuntimeError) as error:
        print(f"[FAIL] 查不动 apt：{error}。查不动就是没查过，不当作通过。",
              file=sys.stderr)
        return 2
    report = assess(text, timedelta(days=args.max_age_days), lists_dir=args.lists_dir,
                    held=[] if args.simulate_file else None,
                    proc=args.proc_dir, boot=args.boot_dir)
    failures, notes = report["failures"], report["notes"]

    inventory_count = None
    if args.inventory is not None:
        inventory_count = write_inventory(args.inventory)

    if args.json:
        print(json.dumps({**report, "inventory_packages": inventory_count},
                         ensure_ascii=False, indent=2))
        return 1 if failures else 0

    if inventory_count is not None:
        print(f"清单：{inventory_count} 个包 → {args.inventory}")
    for note in notes:
        print(f"  [注] {note}")
    if failures:
        for failure in failures:
            print(f"  [FAIL] {failure}")
        return 1
    print(f"[PASS] 安全更新积压为 0，nmu/nginx 没在旧库上跑"
          f"（包列表 {report['lists_age_days'] or 0} 天新）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
