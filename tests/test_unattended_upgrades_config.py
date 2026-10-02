"""夜间自动装安全补丁的四份配置：批准的是这份确切的配置，不只是「打开自动更新」这个想法。

生产机 2026-04-29 装机起 `Unattended-Upgrade "0"`：包列表天天刷新，补丁一个不装，
每周一告警、人手装一次。打开它会改掉 08-06 定下的「只手动装」，所以这里把形状钉死：

  - 只用 50unattended-upgrades 自带的来源（jammy、jammy-security、两条 ESM），一个都不加；
  - 内核、netplan 和引导链（grub、shim）拉黑：它们生效要重启，要配 KiwiVM 控制台的窗口；
  - 不自动重启；needrestart 只列不重启，交互式 apt 的对话框也不会默认勾上 nmu、sing-box；
  - 列表 17:30、安装 18:00 UTC（上海 02:00），错过不补跑，离 19:30 的备份至少一小时。
"""
from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
APT_SNIPPET = ROOT / "deploy" / "apt" / "52nmu-unattended-upgrades"
NEEDRESTART = ROOT / "deploy" / "needrestart" / "50-nmu-list-only.conf"
LISTS_TIMER = ROOT / "deploy" / "systemd" / "apt-daily.timer.d" / "50-nmu-window.conf"
UPGRADE_TIMER = ROOT / "deploy" / "systemd" / "apt-daily-upgrade.timer.d" / "50-nmu-window.conf"
BACKUP_TIMER = ROOT / "deploy" / "systemd" / "nmu-backup.timer"

_APT_STATEMENT = re.compile(
    r'\s*([A-Za-z0-9:_.-]+)\s*(?:"([^"]*)"\s*;|\{((?:\s*"[^"]*"\s*;)*)\s*\}\s*;)\s*')


def _apt_config(text: str) -> dict[str, list[str]]:
    """够读这一份的 apt.conf 子集：注释、`键 "值";`、`键 { "值"; … };`。别的写法直接判失败。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    config: dict[str, list[str]] = {}
    position = 0
    while position < len(text):
        matched = _APT_STATEMENT.match(text, position)
        assert matched, f"读不懂这一段 apt 配置：{text[position:position + 60]!r}"
        key, scalar, block = matched.groups()
        values = [scalar] if scalar is not None else re.findall(r'"([^"]*)"', block)
        config.setdefault(key, []).extend(values)
        position = matched.end()
    return config


def _unit_sections(path: Path) -> dict[str, dict[str, list[str]]]:
    """systemd 单元/片段：{节: {键: [按出现顺序的值]}}。空值保留——那是「清空前面的」。"""
    sections: dict[str, dict[str, list[str]]] = {}
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        assert current is not None and "=" in line, f"{path.name}: 读不懂 {line!r}"
        key, _, value = line.partition("=")
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


def _daily_minutes(calendar: str) -> int:
    matched = re.fullmatch(r"\*-\*-\* (\d{1,2}):(\d{2})(?::00)?", calendar)
    assert matched, f"只认每天固定时刻的 OnCalendar，拿到的是 {calendar!r}"
    return int(matched.group(1)) * 60 + int(matched.group(2))


def _span_minutes(span: str) -> float:
    matched = re.fullmatch(r"(\d+)\s*(s|sec|m|min|h|hr)?", span)
    assert matched, f"读不懂时长 {span!r}"
    seconds = int(matched.group(1)) * {None: 1, "s": 1, "sec": 1, "m": 60, "min": 60,
                                       "h": 3600, "hr": 3600}[matched.group(2)]
    return seconds / 60


def _window(path: Path) -> tuple[int, float, dict[str, list[str]]]:
    """(每天几点触发, 最多再随机推迟几分钟, [Timer] 节)。"""
    timer = _unit_sections(path)["Timer"]
    calendars = timer.get("OnCalendar", [])
    # 片段里的 OnCalendar 是追加：不先写一行空的，发行版自带的 06:00 会照样触发。
    assert calendars[:1] == [""], f"{path.parent.name} 没有先清空自带的 OnCalendar"
    effective = [value for value in calendars[1:] if value]
    assert len(effective) == 1, f"{path.parent.name} 清空后应只剩一个触发时刻：{calendars}"
    delay = _span_minutes(timer.get("RandomizedDelaySec", ["0"])[-1])
    return _daily_minutes(effective[0]), delay, timer


# ---------------- apt：开安装、不重启、不加来源 ----------------

def test_the_snippet_turns_installs_on_and_keeps_reboots_off():
    config = _apt_config(APT_SNIPPET.read_text(encoding="utf-8"))

    assert config["APT::Periodic::Update-Package-Lists"] == ["1"]
    assert config["APT::Periodic::Unattended-Upgrade"] == ["1"]
    assert config["Unattended-Upgrade::Automatic-Reboot"] == ["false"]


def test_the_snippet_is_exactly_the_approved_set_of_keys():
    """不加来源（Allowed-Origins/Origins-Pattern 一加，-updates 甚至第三方源就进来了），
    不设全局 Dpkg::Options（它会漏进机器上每一次 apt 调用）。多一个键就要重新批准。"""
    config = _apt_config(APT_SNIPPET.read_text(encoding="utf-8"))

    assert not [key for key in config if "origin" in key.lower()]
    assert not [key for key in config if key.lower().startswith("dpkg::")]
    assert set(config) == {
        "APT::Periodic::Update-Package-Lists",
        "APT::Periodic::Unattended-Upgrade",
        "Unattended-Upgrade::Automatic-Reboot",
        "Unattended-Upgrade::Package-Blacklist",
        "Unattended-Upgrade::SyslogEnable",
    }


def test_the_blacklist_holds_back_kernel_netplan_and_the_boot_chain():
    """unattended-upgrade 用 re.match 比黑名单：从包名开头匹配的正则，不是全名。"""
    blacklist = _apt_config(APT_SNIPPET.read_text(encoding="utf-8"))[
        "Unattended-Upgrade::Package-Blacklist"]

    def held_back(package: str) -> bool:
        return any(re.match(pattern, package) for pattern in blacklist)

    for package in ("linux-image-5.15.0-198-generic", "linux-modules-extra-5.15.0-198-generic",
                    "linux-generic", "linux-headers-generic", "netplan.io", "netplan-generator",
                    "libnetplan0", "grub-pc", "grub2-common", "grub-efi-amd64-signed",
                    "shim-signed"):
        assert held_back(package), f"{package} 会在夜里被自动装上"
    for package in ("libssl3", "openssl", "openssh-server", "nginx", "nginx-core", "systemd",
                    "libc6", "util-linux", "sudo", "libsqlite3-0", "zlib1g"):
        assert not held_back(package), f"{package} 被拉黑了，安全补丁永远等人装"


def test_the_snippet_sorts_after_the_stock_files_it_overrides():
    """apt 按文件名顺序读 apt.conf.d，后读的同名键生效；带点的文件名会被整份忽略。"""
    name = APT_SNIPPET.name
    assert re.fullmatch(r"[A-Za-z0-9_-]+", name), f"{name} 会被 apt 当成无效文件名跳过"
    assert sorted(["20auto-upgrades", "50unattended-upgrades", name])[-1] == name


# ---------------- needrestart：只列，不重启 ----------------

def test_needrestart_only_lists_what_would_need_a_restart():
    """默认交互模式在有终端时会把 nmu、sing-box 都预先勾上重启。"""
    assert NEEDRESTART.suffix == ".conf", "conf.d 里只读 *.conf"
    assignments = re.findall(r"^\s*\$nrconf\{(\w+)\}\s*=\s*'([^']*)'\s*;",
                             NEEDRESTART.read_text(encoding="utf-8"), re.M)
    assert assignments == [("restart", "l")]


# ---------------- 两个定时器的窗口 ----------------

def test_the_drop_ins_live_where_systemd_reads_them():
    for path, unit in ((LISTS_TIMER, "apt-daily.timer"), (UPGRADE_TIMER, "apt-daily-upgrade.timer")):
        assert path.parent.name == f"{unit}.d" and path.suffix == ".conf"
        assert set(_unit_sections(path)) == {"Timer"}, f"{path} 只该改 [Timer]"


def test_the_install_runs_at_night_in_shanghai_and_well_before_the_backup():
    start, delay, _timer = _window(UPGRADE_TIMER)
    backup_calendar = _unit_sections(BACKUP_TIMER)["Timer"]["OnCalendar"][-1]

    assert 17 * 60 <= start <= 18 * 60 + 59, "安装要落在上海凌晨（UTC 17:00–18:59）"
    assert start + delay + 60 <= _daily_minutes(backup_calendar), (
        "安装最晚开始的时刻离 nmu-backup 不到一小时：dpkg 正在换库时拍快照")


def test_a_missed_install_is_not_caught_up_at_boot_and_barely_drifts():
    """Persistent=true 会让白天开机的那一刻补装补丁——正好撞上有人在练。"""
    _start, delay, timer = _window(UPGRADE_TIMER)

    assert timer["Persistent"][-1] == "false"
    assert delay <= 15


def test_the_lists_refresh_runs_well_before_the_install():
    lists_start, lists_delay, _ = _window(LISTS_TIMER)
    install_start, _delay, _ = _window(UPGRADE_TIMER)

    assert lists_start + lists_delay + 15 <= install_start
