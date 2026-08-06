"""OS 安全补丁积压检查。

守三件事：security 源的行必须被认出来（认不出来 = 积压永远显示 0 = 门禁恒绿）、
陈旧的包列表不能产出可信结论、查不动 apt 要报失败而不是通过。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys

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
    monkeypatch.setattr(osc, "REBOOT_REQUIRED", tmp_path / "no-reboot-required")
    return osc.main(["--simulate-file", str(sim), "--lists-dir", str(lists), *extra])


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
