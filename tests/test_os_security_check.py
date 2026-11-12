"""OS 安全补丁积压检查。

守三件事：security 源的行必须被认出来（认不出来 = 积压永远显示 0 = 门禁恒绿）、
陈旧的包列表不能产出可信结论、查不动 apt 要报失败而不是通过。
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys
import subprocess
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import os_security_check as osc                             # noqa: E402

# 逐字取自生产机 2026-08-06 的 apt-get -s upgrade 输出。
REAL_OUTPUT = """\
NOTE: This is only a simulation!
Reading package lists...
Building dependency tree...
Calculating upgrade...
The following packages will be upgraded:
  bsdutils gzip tar distro-info-data
Inst bsdutils [1:2.37.2-4ubuntu3.4] (1:2.37.2-4ubuntu3.5 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Inst gzip [1.10-4ubuntu4.1] (1.10-4ubuntu4.2 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Inst tar [1.34+dfsg-1ubuntu0.1.22.04.2] (1.34+dfsg-1ubuntu0.1.22.04.6 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Inst distro-info-data [0.52ubuntu0.9] (0.52ubuntu0.10 Ubuntu:22.04/jammy-updates [all])
Conf bsdutils (1:2.37.2-4ubuntu3.5 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Conf gzip (1.10-4ubuntu4.2 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
"""

NOW = datetime(2026, 8, 6, 21, 30)
FRESH = timedelta(hours=10)
MAX_AGE = timedelta(days=3)


# ---------------- 解析 ----------------

def test_parse_splits_security_from_plain_updates():
    pending = osc.parse_simulation(REAL_OUTPUT)
    assert [(p.name, p.security) for p in pending] == [
        ("bsdutils", True), ("gzip", True), ("tar", True),
        ("distro-info-data", False)]
    tar = next(p for p in pending if p.name == "tar")
    assert tar.old == "1.34+dfsg-1ubuntu0.1.22.04.2"
    assert tar.new == "1.34+dfsg-1ubuntu0.1.22.04.6"


def test_conf_lines_and_prose_are_not_counted():
    """Conf 行和"The following packages"叙述行混进计数会让积压翻倍。"""
    assert len(osc.parse_simulation(REAL_OUTPUT)) == 4


def test_a_clean_system_parses_to_nothing():
    assert osc.parse_simulation(
        "NOTE: This is only a simulation!\nCalculating upgrade...\n"
        "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n") == []


def _apt_cache(monkeypatch, versions, *, installed="1.0-1", name="example:amd64"):
    # 顺序故意不依赖缓存排序；版本比较必须由 apt 处理 epoch、~ 与数字段。
    ordered = ["0:0.9-99", "1.0-1", "1.0-2~rc1", "1.0-2", "1.0-10", "1:0.1-1"]
    package = SimpleNamespace(
        current_ver=SimpleNamespace(ver_str=installed),
        version_list=[SimpleNamespace(
            ver_str=version,
            file_list=[(SimpleNamespace(archive=archive), 0) for archive in archives],
        ) for version, archives in versions],
    )
    monkeypatch.setitem(sys.modules, "apt_pkg", SimpleNamespace(
        init=lambda: None, Cache=lambda progress: {name: package},
        version_compare=lambda left, right: ordered.index(left) - ordered.index(right),
    ))
    return [{"name": name, "old": installed, "new": "1.0-10"}]


def test_a_newer_updates_candidate_includes_an_intermediate_security_fix(monkeypatch):
    upgrades = _apt_cache(monkeypatch, [
        ("1.0-10", ["jammy-updates"]),
        ("1.0-2", ["jammy-updates", "jammy-security"]),
        ("1.0-1", ["jammy"]),
    ])
    assert osc.security_flags_from_apt(upgrades) == {"example:amd64": True}


@pytest.mark.parametrize("version", ["0:0.9-99", "1.0-1", "1:0.1-1"])
def test_security_versions_outside_the_upgrade_interval_do_not_count(monkeypatch, version):
    upgrades = _apt_cache(monkeypatch, [
        (version, ["jammy-security"]), ("1.0-10", ["jammy-updates"]),
    ])
    assert osc.security_flags_from_apt(upgrades) == {"example:amd64": False}


@pytest.mark.parametrize("version", ["1.0-2~rc1", "1.0-10"])
def test_the_security_interval_includes_intermediate_and_target_versions(monkeypatch, version):
    upgrades = _apt_cache(monkeypatch, [
        (version, ["jammy-security"]), ("1.0-10", ["jammy-updates"]),
    ])
    assert osc.security_flags_from_apt(upgrades) == {"example:amd64": True}


@pytest.mark.parametrize("archive", [None, "jammy-updates", "jammy-security-testing"])
def test_only_a_security_archive_suffix_counts(monkeypatch, archive):
    upgrades = _apt_cache(monkeypatch, [("1.0-10", [archive])])
    assert osc.security_flags_from_apt(upgrades) == {"example:amd64": False}


@pytest.mark.parametrize("problem", ["missing-package", "changed-installed", "missing-target"])
def test_cache_drift_or_missing_package_cannot_produce_a_clean_result(monkeypatch, problem):
    upgrades = _apt_cache(monkeypatch, [("1.0-10", ["jammy-updates"])])
    if problem == "missing-package":
        upgrades[0]["name"] = "missing"
    elif problem == "changed-installed":
        upgrades[0]["old"] = "1.0-2"
    else:
        upgrades[0]["new"] = "1:0.1-1"
    with pytest.raises(RuntimeError):
        osc.security_flags_from_apt(upgrades)


def test_version_origin_probe_uses_system_python_and_only_unclassified_packages(monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"ordinary": true}', stderr="")

    monkeypatch.setattr(osc.subprocess, "run", run)
    pending = [osc.PendingUpgrade("known-security", "1", "2", True),
               osc.PendingUpgrade("ordinary", "1", "3", False)]
    assert osc.read_security_flags(pending) == {"ordinary": True}
    [(argv, kwargs)] = seen
    assert argv[0] == "/usr/bin/python3"
    assert argv[-1] == str(Path(osc.__file__).resolve())
    assert json.loads(kwargs["input"]) == [{"name": "ordinary", "old": "1", "new": "3"}]
    assert kwargs["env"]["LC_ALL"] == "C"
    assert kwargs["timeout"] == 60


@pytest.mark.parametrize("stdout", ["", "not-json", "{}", '{"ordinary": 1}',
                                    '{"ordinary": null}', '{"other": false}', "[]"])
def test_version_origin_probe_rejects_missing_or_malformed_classification(monkeypatch, stdout):
    monkeypatch.setattr(osc.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout=stdout, stderr=""))
    with pytest.raises(RuntimeError):
        osc.read_security_flags([osc.PendingUpgrade("ordinary", "1", "3", False)])


def test_failed_system_apt_probe_does_not_return_a_clean_classification(monkeypatch):
    monkeypatch.setattr(osc.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=1, stdout="{}", stderr="cannot open apt cache"))
    with pytest.raises(RuntimeError, match="cannot open apt cache"):
        osc.read_security_flags([osc.PendingUpgrade("ordinary", "1", "3", False)])


# ---------------- 判定 ----------------

def _evaluate(pending, age=FRESH, reboot=False):
    return osc.evaluate(pending, age, MAX_AGE, reboot)


def test_pending_security_updates_are_a_hard_failure():
    failures, notes = _evaluate(osc.parse_simulation(REAL_OUTPUT))
    assert len(failures) == 1
    assert "3 个安全更新待安装" in failures[0]
    assert "bsdutils" in failures[0]
    assert any("1 个非安全更新" in n for n in notes)


def test_only_plain_updates_pending_is_a_note_not_a_failure():
    pending = [osc.PendingUpgrade("distro-info-data", "1", "2", security=False)]
    failures, notes = _evaluate(pending)
    assert failures == []
    assert any("非安全更新" in n for n in notes)


def test_stale_package_lists_fail_even_with_zero_pending():
    """列表 10 天没刷新时,"积压 0"是拿旧账本算的,不可信。"""
    failures, _ = _evaluate([], age=timedelta(days=10))
    assert len(failures) == 1 and "陈旧" in failures[0]


def test_unreadable_package_lists_fail(tmp_path):
    assert osc.lists_age(tmp_path / "nope", NOW) is None
    failures, _ = _evaluate([], age=None)
    assert len(failures) == 1 and "读不到" in failures[0]


def test_reboot_required_is_reported_but_does_not_fail_alone():
    failures, notes = _evaluate([], reboot=True)
    assert failures == []
    assert any("未重启" in n for n in notes)


def test_lists_age_uses_the_newest_index_and_skips_lock(tmp_path):
    import os
    old = tmp_path / "a_index"
    old.write_text("x")
    os.utime(old, (0, (NOW - timedelta(days=9)).timestamp()))
    new = tmp_path / "b_index"
    new.write_text("x")
    os.utime(new, (0, (NOW - timedelta(hours=5)).timestamp()))
    lock = tmp_path / "lock"
    lock.write_text("x")
    os.utime(lock, (0, NOW.timestamp()))

    age = osc.lists_age(tmp_path, NOW)
    assert age is not None and timedelta(hours=4) < age < timedelta(hours=6)


# ---------------- CLI ----------------

def _cli(tmp_path, text, monkeypatch, extra=()):
    sim = tmp_path / "sim.txt"
    sim.write_text(text, encoding="utf-8")
    lists = tmp_path / "lists"
    lists.mkdir(exist_ok=True)
    (lists / "jammy_index").write_text("x")
    # 进程与内核两项读假的 /proc、/boot:跑测试这台机器(Mac 上根本没有 /proc)不该左右判定。
    for fake in ("proc", "boot"):
        (tmp_path / fake).mkdir(exist_ok=True)
    monkeypatch.setattr(osc, "REBOOT_REQUIRED", tmp_path / "no-reboot-required")
    monkeypatch.setattr(osc.os, "geteuid", lambda: 0)
    monkeypatch.setattr(osc, "read_security_flags",
                        lambda pending: {item.name: False for item in pending if not item.security})
    return osc.main(["--simulate-file", str(sim), "--lists-dir", str(lists),
                     "--proc-dir", str(tmp_path / "proc"), "--boot-dir", str(tmp_path / "boot"),
                     *extra])


def test_cli_exits_one_on_pending_security(tmp_path, monkeypatch, capsys):
    assert _cli(tmp_path, REAL_OUTPUT, monkeypatch) == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_cli_exits_zero_when_clean(tmp_path, monkeypatch, capsys):
    assert _cli(tmp_path, "Calculating upgrade...\n", monkeypatch) == 0
    assert "[PASS]" in capsys.readouterr().out


def test_cli_json_carries_the_counts(tmp_path, monkeypatch, capsys):
    import json
    code = _cli(tmp_path, REAL_OUTPUT, monkeypatch, extra=["--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["security_pending"] == 3 and report["other_pending"] == 1
    assert report["ok"] is False


def test_cli_unrunnable_apt_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(osc, "run_simulation", lambda: (_ for _ in ()).throw(
        RuntimeError("apt 坏了")))
    code = osc.main(["--lists-dir", str(tmp_path)])
    assert code == 2
    assert "不当作通过" in capsys.readouterr().err


def test_inventory_is_written_next_to_the_verdict(tmp_path, monkeypatch):
    def fake_inventory(path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)   # 真实现也负责建目录
        path.write_text("bash\t5.1\tamd64\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(osc, "write_inventory", fake_inventory)
    target = tmp_path / "inv" / "os-packages.txt"
    code = _cli(tmp_path, "Calculating upgrade...\n", monkeypatch,
                extra=["--inventory", str(target)])
    assert code == 0
    assert target.read_text(encoding="utf-8").startswith("bash")


# 逐字取自生产机 2026-09-14 的 apt-get -s upgrade 输出:需要装新依赖的内核/netplan
# 更新被整组"保留",不出 Inst 行——2026-09-11 那次预检 9/9 绿灯下积着 5 个安全内核包。
KEPT_BACK_OUTPUT = """\
NOTE: This is only a simulation!
Reading package lists...
Building dependency tree...
Calculating upgrade...
The following packages have been kept back:
  libnetplan0 linux-generic linux-headers-generic linux-image-generic netplan.io
0 upgraded, 0 newly installed, 0 to remove and 5 not upgraded.
"""

# 同一台机器同一时刻的 apt-get -s full-upgrade:保留的包变成 Inst 行、带来源。
FULL_UPGRADE_OUTPUT = """\
NOTE: This is only a simulation!
Calculating upgrade...
The following NEW packages will be installed:
  linux-headers-5.15.0-191 linux-image-5.15.0-191-generic netplan-generator
The following packages will be upgraded:
  libnetplan0 linux-generic linux-headers-generic linux-image-generic netplan.io
5 upgraded, 7 newly installed, 0 to remove and 0 not upgraded.
Inst netplan.io [0.106.1-7ubuntu0.22.04.4] (0.107.1-3ubuntu0.22.04.4 Ubuntu:22.04/jammy-updates [amd64]) []
Inst netplan-generator (0.107.1-3ubuntu0.22.04.4 Ubuntu:22.04/jammy-updates [amd64]) []
Inst libnetplan0 [0.106.1-7ubuntu0.22.04.4] (0.107.1-3ubuntu0.22.04.4 Ubuntu:22.04/jammy-updates [amd64])
Inst linux-image-5.15.0-191-generic (5.15.0-191.201 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Inst linux-generic [5.15.0.170.159] (5.15.0.191.169 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64]) []
Inst linux-image-generic [5.15.0.170.159] (5.15.0.191.169 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64]) []
Inst linux-headers-generic [5.15.0.170.159] (5.15.0.191.169 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
Conf netplan.io (0.107.1-3ubuntu0.22.04.4 Ubuntu:22.04/jammy-updates [amd64])
"""


def test_kept_back_block_is_parsed_and_is_a_hard_failure():
    """upgrade 模拟里被保留的包不出 Inst 行:只看 Inst 会把 5 个安全内核包算成 0。"""
    assert osc.parse_simulation(KEPT_BACK_OUTPUT) == []
    assert osc.parse_kept_back(KEPT_BACK_OUTPUT) == [
        "libnetplan0", "linux-generic", "linux-headers-generic",
        "linux-image-generic", "netplan.io"]
    failures, _ = osc.evaluate([], FRESH, MAX_AGE, False,
                               osc.parse_kept_back(KEPT_BACK_OUTPUT))
    assert len(failures) == 1 and "5 个包被保留" in failures[0] and "linux-generic" in failures[0]


def test_full_upgrade_simulation_counts_the_kernel_as_security_backlog():
    """full-upgrade 把保留的包变成带来源的 Inst 行:内核三件是 security 积压,netplan 两件是普通更新。"""
    pending = osc.parse_simulation(FULL_UPGRADE_OUTPUT)
    assert osc.parse_kept_back(FULL_UPGRADE_OUTPUT) == []
    assert sorted(p.name for p in pending if p.security) == [
        "linux-generic", "linux-headers-generic", "linux-image-generic"]
    assert sorted(p.name for p in pending if not p.security) == ["libnetplan0", "netplan.io"]
    failures, notes = osc.evaluate(pending, FRESH, MAX_AGE, False, [])
    assert len(failures) == 1 and "3 个安全更新待安装" in failures[0]
    assert any("2 个非安全更新" in n for n in notes)


# 2026-11-06 内核窗口刚重启完的生产机:xfsprogs 5.13.0-1ubuntu2.2 在 jammy-updates 分阶段推到 30%。
PHASED_DEFAULT_OUTPUT = """\
The following packages have been kept back:
  xfsprogs
The following packages will be upgraded:
  libglib2.0-0 libglib2.0-bin libglib2.0-data nano snapd
Inst nano [6.2-1ubuntu0.2] (6.2-1ubuntu0.3 Ubuntu:22.04/jammy-updates [amd64])
"""
# 同一刻加 -o APT::Get::Always-Include-Phased-Updates=true:保留块没了,xfsprogs 出带来源的 Inst 行。
PHASED_INCLUDED_OUTPUT = """\
The following packages will be upgraded:
  libglib2.0-0 libglib2.0-bin libglib2.0-data nano snapd xfsprogs
Inst nano [6.2-1ubuntu0.2] (6.2-1ubuntu0.3 Ubuntu:22.04/jammy-updates [amd64])
Inst xfsprogs [5.13.0-1ubuntu2.1] (5.13.0-1ubuntu2.2 Ubuntu:22.04/jammy-updates [amd64])
"""


def test_a_phased_update_is_counted_by_origin_instead_of_failing_as_kept_back():
    """分阶段推送没轮到的包 apt 也列成保留;不把它算进来,每推一个 -updates 包周检就误报一周。"""
    assert osc.parse_kept_back(PHASED_DEFAULT_OUTPUT) == ["xfsprogs"]
    failures, _ = osc.evaluate(osc.parse_simulation(PHASED_DEFAULT_OUTPUT), FRESH, MAX_AGE, False,
                               osc.parse_kept_back(PHASED_DEFAULT_OUTPUT))
    assert len(failures) == 1 and "xfsprogs" in failures[0]

    pending = osc.parse_simulation(PHASED_INCLUDED_OUTPUT)
    assert osc.parse_kept_back(PHASED_INCLUDED_OUTPUT) == []
    assert [(p.name, p.security) for p in pending] == [("nano", False), ("xfsprogs", False)]
    failures, notes = osc.evaluate(pending, FRESH, MAX_AGE, False, [])
    assert failures == [] and any("2 个非安全更新" in n for n in notes)


def test_the_fix_sends_kernel_packages_to_the_window_and_never_says_full_upgrade():
    """2026-11-02 的告警把内核也写成 apt-get full-upgrade,而内核按规矩只在维护窗口里装。"""
    kernel = [p for p in osc.parse_simulation(FULL_UPGRADE_OUTPUT) if p.security]
    [only_kernel], _ = osc.evaluate(kernel, FRESH, MAX_AGE, False, [])
    assert "kernel_netplan_window.sh" in only_kernel and "内核 3 个" in only_kernel
    assert "full-upgrade" not in only_kernel and "apt-get install" not in only_kernel

    [mixed], _ = osc.evaluate(kernel + osc.parse_simulation(REAL_OUTPUT), FRESH, MAX_AGE, False, [])
    assert "内核 3 个" in mixed
    assert "其余 3 个夜间补丁没装上" in mixed and "apt-get -s install" in mixed
    assert mixed.endswith("apt-get install bsdutils gzip tar")

    [plain], _ = osc.evaluate(osc.parse_simulation(REAL_OUTPUT), FRESH, MAX_AGE, False, [])
    assert "kernel_netplan_window.sh" not in plain and "full-upgrade" not in plain


def test_blacklisted_packages_the_window_cannot_install_are_hand_installed_in_a_window():
    """窗口脚本只装四个内核元包;grub、shim、netplan、linux-firmware 照告警跑 check 会对不上包。"""
    pending = [osc.PendingUpgrade(name, "1", "2", security=True)
               for name in ("grub-common", "shim-signed", "linux-firmware", "linux-generic")]
    [failure], _ = osc.evaluate(pending, FRESH, MAX_AGE, False, [])
    assert "内核 1 个走维护窗口" in failure
    assert "网络/引导类 3 个窗口脚本装不了" in failure
    assert "apt-get install grub-common shim-signed linux-firmware，装完重启" in failure


def test_a_long_hand_install_list_says_it_is_cut_short():
    pending = [osc.PendingUpgrade(f"lib{i}", "1", "2", security=True) for i in range(8)]
    [failure], _ = osc.evaluate(pending, FRESH, MAX_AGE, False, [])
    assert failure.endswith("apt-get install lib0 lib1 lib2 lib3 lib4 lib5…")


def test_the_kernel_group_is_what_the_window_script_installs():
    script = (Path(__file__).resolve().parents[1] / "deploy" / "maintenance"
              / "kernel_netplan_window.sh").read_text(encoding="utf-8")
    metas = re.search(r'^META_PACKAGES="([^"]+)"$', script, re.M).group(1).split()
    assert tuple(metas) == osc.KERNEL_WINDOW_PACKAGES


def test_the_window_only_prefixes_match_the_unattended_blacklist():
    """告警按哪些包分去维护窗口,必须和夜里拉黑不装的是同一份。"""
    snippet = (Path(__file__).resolve().parents[1] / "deploy" / "apt"
               / "52nmu-unattended-upgrades").read_text(encoding="utf-8")
    block = snippet[snippet.index("Unattended-Upgrade::Package-Blacklist {"):]
    block = block[:block.index("};")]
    assert tuple(re.findall(r'"([^"]+)"', block)) == osc.WINDOW_ONLY_PREFIXES


def test_run_simulation_asks_apt_for_a_full_upgrade_in_the_c_locale(monkeypatch):
    seen: list[tuple[list[str], dict]] = []

    class _Done:
        returncode = 0
        stdout = FULL_UPGRADE_OUTPUT
        stderr = ""

    monkeypatch.setattr(osc.subprocess, "run",
                        lambda argv, **kw: seen.append((argv, kw)) or _Done())
    assert osc.run_simulation() == FULL_UPGRADE_OUTPUT
    assert [argv for argv, _kw in seen] == [
        ["apt-get", "-s", "-o", "APT::Get::Always-Include-Phased-Updates=true", "full-upgrade"]]
    # ssh 会把 Mac 的 LANG/LC_* 带到生产机;标题一被翻译,按英文解析的保留项就失明。
    assert seen[0][1]["env"]["LC_ALL"] == "C"


def test_held_packages_are_a_note_and_only_unheld_kept_back_fail():
    """人为 apt-mark hold 的包也会出现在 kept back 里:那是运维的决定,不是统计缺口。"""
    kept = ["netplan.io", "linux-image-generic"]
    failures, notes = osc.evaluate([], FRESH, MAX_AGE, False, kept, held=["netplan.io"])
    assert len(failures) == 1 and "1 个包被保留" in failures[0] and "linux-image-generic" in failures[0]
    assert any("被人为 hold" in n and "netplan.io" in n for n in notes)
    failures, notes = osc.evaluate([], FRESH, MAX_AGE, False, ["netplan.io"], held=["netplan.io"])
    assert failures == [] and any("被人为 hold" in n for n in notes)


def test_read_holds_survives_a_missing_apt_mark(monkeypatch):
    def boom(*_a, **_kw):
        raise FileNotFoundError("apt-mark")
    monkeypatch.setattr(osc.subprocess, "run", boom)
    assert osc.read_holds() == []


# ---------------- 补丁装在盘上，内存里跑的还是旧代码 ----------------
# 自动装安全更新只换盘上的文件：nmu 不重启就一直跑着旧 libssl/libsqlite3/zlib。
# 这台机器没有 update-notifier，库更新不会立任何旗子，所以要自己去 /proc 里看。

def _maps_line(path: str, deleted: bool = True) -> str:
    return (f"7f38fcedf000-7f38fcf0a000 r-xp 00000000 08:03 131330"
            f"                     {path}{' (deleted)' if deleted else ''}")


SSL = "/usr/lib/x86_64-linux-gnu/libssl.so.3"
CRYPTO = "/usr/lib/x86_64-linux-gnu/libcrypto.so.3"
LIBC = "/usr/lib/x86_64-linux-gnu/libc.so.6"
# 都带 (deleted)，但都不是 apt 换掉的共享库：匿名内存、共享内存段、临时文件、数据文件、
# 区域数据，以及 /opt、/usr/local 下不归 apt 管的 .so。
NOT_APT_LIBRARIES = (
    "7f0000000000-7f0000001000 rw-s 00000000 00:01 1   /memfd:pulseaudio (deleted)",
    "7f0000002000-7f0000003000 rw-s 00000000 00:05 2   /SYSV00000000 (deleted)",
    _maps_line("/tmp/ffiXa81b2"),
    _maps_line("/var/lib/nmu/app.db"),
    _maps_line("/usr/lib/locale/locale-archive"),
    _maps_line("/opt/nmu/python3.14/lib/python3.14/lib-dynload/_ssl.cpython-314-x86_64-linux-gnu.so"),
    _maps_line("/usr/local/lib/libfoo.so.1"),
)


def _process(proc: Path, pid: int, cgroup: str, *maps: str) -> None:
    directory = proc / str(pid)
    directory.mkdir(parents=True)
    (directory / "cgroup").write_text(cgroup, encoding="utf-8")
    (directory / "maps").write_text("\n".join(maps) + "\n", encoding="utf-8")


def test_a_replaced_library_is_attributed_to_the_unit_that_still_maps_it(tmp_path):
    proc = tmp_path / "proc"
    _process(proc, 812, "0::/system.slice/nmu.service\n", _maps_line(SSL), _maps_line(LIBC, False))
    _process(proc, 813, "0::/system.slice/nmu.service\n", _maps_line(CRYPTO))
    _process(proc, 640, "0::/system.slice/dbus.service\n", _maps_line(LIBC, False))
    # cgroup v1/混合层级：取 name=systemd 那一行；模板实例取到实例名为止。
    _process(proc, 700, "12:pids:/system.slice/nginx.service\n"
                        "1:name=systemd:/system.slice/nginx.service\n", _maps_line(CRYPTO))
    _process(proc, 701, "0::/system.slice/system-getty.slice/getty@tty1.service\n",
             _maps_line(LIBC))
    # 登录会话和用户管理器来来去去，合成一组。
    _process(proc, 9001, "0::/user.slice/user-0.slice/session-7.scope\n", _maps_line(SSL))
    _process(proc, 9002, "0::/user.slice/user-0.slice/user@0.service/init.scope\n",
             _maps_line(LIBC))
    _process(proc, 1, "0::/init.scope\n", _maps_line(LIBC))
    _process(proc, 1500, "0::/system.slice/packagekit.service\n", *NOT_APT_LIBRARIES)
    (proc / "self").mkdir()
    (proc / "sys").mkdir()

    assert osc.stale_library_units(proc) == {
        "getty@tty1.service": [LIBC],
        "init.scope": [LIBC],
        "nginx.service": [CRYPTO],
        "nmu.service": [CRYPTO, SSL],
        "user.slice": [LIBC, SSL],
    }


def test_a_process_that_exits_mid_scan_is_skipped(tmp_path):
    proc = tmp_path / "proc"
    (proc / "4242").mkdir(parents=True)          # 读 maps 之前进程已经退出
    _process(proc, 812, "0::/system.slice/nmu.service\n", _maps_line(SSL))

    assert osc.stale_library_units(proc) == {"nmu.service": [SSL]}


def test_a_process_whose_maps_cannot_be_read_makes_the_scan_fail(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root 读得了 0000 的文件，造不出读不了的映射")
    proc = tmp_path / "proc"
    _process(proc, 812, "0::/system.slice/nmu.service\n", _maps_line(SSL))
    (proc / "812" / "maps").chmod(0)

    with pytest.raises(PermissionError):
        osc.stale_library_units(proc)


def test_a_stale_app_unit_fails_and_names_the_unit_and_the_library():
    failures, notes = osc.evaluate_running(
        {"nmu.service": [CRYPTO, SSL], "nginx.service": [CRYPTO]}, None,
        "5.15.0-194-generic", "5.15.0-194-generic")

    assert len(failures) == 2 and notes == []
    nmu = next(f for f in failures if f.startswith("nmu.service"))
    assert "libssl.so.3" in nmu and "libcrypto.so.3" in nmu
    assert "systemctl restart nmu" in nmu
    assert any(f.startswith("nginx.service") and "systemctl restart nginx" in f
               for f in failures)


def test_stale_core_units_are_a_note_for_the_next_reboot_window():
    """sing-box 一重启就断用户、dbus 重启会牵连整机：只记一笔，等重启窗口一并换新。"""
    failures, notes = osc.evaluate_running(
        {"dbus.service": [LIBC], "sing-box.service": [LIBC], "user.slice": [SSL]}, None,
        "5.15.0-194-generic", None)

    assert failures == []
    [note] = notes
    assert "dbus.service" in note and "sing-box.service" in note and "重启窗口" in note


def test_a_newer_installed_kernel_is_a_note_in_numeric_order(tmp_path):
    boot = tmp_path / "boot"
    boot.mkdir()
    for release in ("5.15.0-25-generic", "5.15.0-194-generic", "5.15.0-198-generic"):
        (boot / f"vmlinuz-{release}").write_text("x")
    (boot / "vmlinuz").symlink_to("vmlinuz-5.15.0-198-generic")
    (boot / "vmlinuz.old").symlink_to("vmlinuz-5.15.0-194-generic")

    # 这就是陷阱：按字面比，25 排在 198 后面。
    assert max(["5.15.0-198-generic", "5.15.0-25-generic"]) == "5.15.0-25-generic"
    assert osc.newest_installed_kernel(boot) == "5.15.0-198-generic"
    assert osc.newest_installed_kernel(tmp_path / "no-boot") is None

    failures, notes = osc.evaluate_running({}, None, "5.15.0-194-generic", "5.15.0-198-generic")
    assert failures == []
    [note] = notes
    assert "5.15.0-198-generic" in note and "5.15.0-194-generic" in note
    assert osc.evaluate_running({}, None, "5.15.0-198-generic", "5.15.0-198-generic") == ([], [])


def _assess(tmp_path, **overrides):
    lists = tmp_path / "lists"
    lists.mkdir(exist_ok=True)
    (lists / "jammy_index").write_text("x")
    (tmp_path / "proc").mkdir(exist_ok=True)
    kwargs = dict(lists_dir=lists, held=[], proc=tmp_path / "proc", boot=tmp_path / "boot",
                  running_kernel="5.15.0-194-generic", euid=0)
    simulation = overrides.pop("simulation", "Calculating upgrade...\n")
    kwargs.update(overrides)
    return osc.assess(simulation, MAX_AGE, **kwargs)


def test_assess_blocks_the_previously_missed_intermediate_security_fix(tmp_path, monkeypatch):
    upgrades = _apt_cache(monkeypatch, [
        ("1.0-10", ["jammy-updates"]), ("1.0-2", ["jammy-security"]),
    ])
    monkeypatch.setattr(osc, "read_security_flags",
                        lambda pending: osc.security_flags_from_apt(upgrades))
    report = _assess(tmp_path, simulation=(
        "Inst example:amd64 [1.0-1] (1.0-10 Ubuntu:22.04/jammy-updates [amd64])\n"))
    assert report["ok"] is False
    assert report["security_pending"] == 1
    assert report["other_pending"] == 0
    assert report["security_classification_complete"] is True
    assert any("example:amd64" in failure for failure in report["failures"])


@pytest.mark.parametrize("error", [FileNotFoundError("python3"), RuntimeError("bad apt cache"),
                                   subprocess.TimeoutExpired("python3", 60)])
def test_assess_fails_closed_when_intermediate_versions_cannot_be_checked(
        tmp_path, monkeypatch, error):
    def fail(_pending):
        raise error

    monkeypatch.setattr(osc, "read_security_flags", fail)
    report = _assess(tmp_path, simulation=PHASED_INCLUDED_OUTPUT)
    assert report["ok"] is False
    assert report["security_classification_complete"] is False
    assert report["other_pending"] is None
    assert any("查不全 apt 版本来源" in failure for failure in report["failures"])
    assert not any("非安全更新" in note for note in report["notes"])


def test_not_running_as_root_fails_instead_of_passing_unlooked(tmp_path):
    """非 root 读不到别的用户的进程映射：那样得出的 0 是没看，不是没有。"""
    report = _assess(tmp_path, euid=1000)

    assert report["ok"] is False
    assert report["stale_library_units"] is None
    assert any("root" in failure for failure in report["failures"])


def test_an_unreadable_proc_fails_instead_of_passing_unlooked(tmp_path):
    report = _assess(tmp_path, proc=tmp_path / "no-proc-here")

    assert report["ok"] is False
    assert report["stale_library_units"] is None
    assert any("没查过不当作通过" in failure for failure in report["failures"])


def test_a_clean_box_passes_with_the_running_code_fields_filled(tmp_path):
    report = _assess(tmp_path)

    assert report["ok"] is True, report["failures"]
    assert report["stale_library_units"] == {}
    assert report["kernel_running"] == "5.15.0-194-generic"
    assert report["kernel_newest_installed"] is None


def test_cli_json_carries_the_running_code_fields(tmp_path, monkeypatch, capsys):
    import json
    _process(tmp_path / "proc", 812, "0::/system.slice/nmu.service\n", _maps_line(SSL))
    (tmp_path / "boot").mkdir()
    (tmp_path / "boot" / "vmlinuz-5.15.0-198-generic").write_text("x")

    code = _cli(tmp_path, "Calculating upgrade...\n", monkeypatch, extra=["--json"])

    report = json.loads(capsys.readouterr().out)
    assert code == 1 and report["ok"] is False
    assert report["security_pending"] == 0
    assert report["stale_library_units"] == {"nmu.service": [SSL]}
    assert report["kernel_running"] == os.uname().release
    assert report["kernel_newest_installed"] == "5.15.0-198-generic"
    assert report["failures"][0].startswith("nmu.service")
