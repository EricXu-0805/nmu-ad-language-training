"""内核与 netplan 维护窗口脚本：真跑之前，先把以前栽过的跟头钉成静态规则。

deploy/maintenance/kernel_netplan_window.sh 在 Eric 的 Mac 上跑（/bin/bash 3.2），一段段经
ssh 送到生产机执行。真跑一次要开 KiwiVM 控制台、重启两次，所以在那之前只能靠这些规则，
每条都对应一次真实的事故或评审结论：

  - 261 号上线脚本：相对路径在 Eric 的 shell 里不存在，跑了等于没跑；git diff 开了分页器
    卡住；`$OLDHEAD→` 被 bash 读成变量名，set -u 下当场崩。→ 绝对路径、--no-pager、变量后
    不紧跟非 ASCII 字符。
  - 2026-09-30 重启前六个定时器被停用，开机后 nmu-health 停在 elapsed 没人发现。→ 不许
    disable/mask 任何单元，stop 只许拆自己装的死人开关，定时器只读不碰。
  - 夜间自动装补丁 18:00 UTC 起拿着 dpkg 锁。→ 每个真装包的 apt-get 都带 DPkg::Lock::Timeout，
    而且只装点名的包，不跑 full-upgrade。
  - netplan try 经 ssh 不可靠（SIGHUP 不回滚、对 /etc/netplan 的改动不回滚）。→ 先装死人
    开关，再放新配置、再 apply。
  - MAC 与 KiwiVM 的 VM 编号挂钩，评审时一处都没打印。→ 不打印 MAC，ip link 与 networkctl
    的输出只取需要的那一行。
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
import shlex
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "maintenance" / "kernel_netplan_window.sh"
PHASES = {"check", "kernel", "reboot", "verify-kernel", "netplan", "verify-netplan", "purge",
          "restart-app"}
NMU_TIMERS = {"nmu-backup.timer", "nmu-backup-health.timer", "nmu-capacity.timer",
              "nmu-restore-drill.timer", "nmu-os-security.timer", "nmu-health.timer"}

# 与 tests/test_shell_script_hygiene.py 同一条：裸 $var 紧跟非 ASCII 字符。
BARE_VAR_BEFORE_WIDE = re.compile(r"\$\{?\\?\$?[A-Za-z_][A-Za-z0-9_]*(?![A-Za-z0-9_}])[^\x00-\x7F]")
_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_]\w*)\1")
_UNIT = re.compile(r"[\w@.-]+\.(?:timer|service|socket|target|path)\b")
_IDLE_GATE_ROOT = "${0%/deploy/maintenance/kernel_netplan_window.sh}"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _logical_lines() -> list[str]:
    """非注释行，反斜杠续行拼成一行。heredoc 正文也在内：它们在生产机上同样被执行。"""
    joined: list[str] = []
    pending = ""
    for raw in _text().splitlines():
        if not pending and raw.lstrip().startswith("#"):
            continue
        if raw.endswith("\\"):
            pending += raw[:-1] + " "
            continue
        line = pending + raw
        pending = ""
        if line.strip():
            joined.append(line)
    return joined


def _heredocs(text: str) -> list[tuple[str, str]]:
    """(定界符, 正文)，含嵌在 REMOTE 正文里的 PY、YAML。"""
    found: list[tuple[str, str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        matched = _HEREDOC.search(lines[index])
        if matched and not lines[index].lstrip().startswith("#"):
            delimiter = matched.group(2)
            end = index + 1
            while end < len(lines) and lines[end] != delimiter:
                end += 1
            assert end < len(lines), f"第 {index + 1} 行起的 heredoc {delimiter} 没有收尾"
            body = "\n".join(lines[index + 1:end]) + "\n"
            found.append((delimiter, body))
            found.extend(_heredocs(body))
            index = end
        index += 1
    return found


def _function(name: str) -> str:
    text = _text()
    start = text.index(f"\n{name}() {{")
    return text[start:text.index("\n}\n", start)]


# ---------------- 语法 ----------------

def test_the_script_itself_parses():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_every_remote_bash_body_parses_and_every_python_body_compiles():
    """heredoc 正文对本地 bash 只是字符串，bash -n 看不到它们，改坏了要到生产机上才炸。"""
    bodies = _heredocs(_text())
    remote = [body for delimiter, body in bodies if delimiter == "REMOTE"]
    python = [body for delimiter, body in bodies if delimiter == "PY"]
    assert len(remote) >= 5 and python, "一段远端正文都没解析出来，下面的断言在空转"
    for body in remote:
        result = subprocess.run(["bash", "-n"], input=body, capture_output=True, text=True)
        assert result.returncode == 0, f"{result.stderr}\n----\n{body[:400]}"
    for body in python:
        compile(body, "<PY heredoc>", "exec")


# ---------------- 261 的三条教训 ----------------

def test_the_run_line_is_absolute_and_nothing_depends_on_the_working_directory():
    run_line = re.search(r"^#\s+bash (\S+/deploy/maintenance/kernel_netplan_window\.sh) ",
                         _text(), re.M)
    assert run_line and run_line.group(1).startswith("/"), "头注释要给绝对路径的跑法"
    offenders = []
    for line in _logical_lines():
        if re.search(r"(?:^|[\s;&|(])cd(?:\s|$)", line):
            offenders.append(f"cd：{line.strip()}")
        if re.search(r"(?<![\w.])\.\.?/", line):
            offenders.append(f"相对路径：{line.strip()}")
        if re.search(r"(?<![/\w-])python3?\s", line):
            offenders.append(f"解释器不是绝对路径：{line.strip()}")
        for found in re.finditer(r"scripts/", line):
            # 空闲门按本脚本自己的绝对路径($0)找仓库里那一份,与当前目录无关。
            if not (line[:found.start()].endswith("/opt/nmu/app/")
                    or line.startswith(f'IDLE_GATE="{_IDLE_GATE_ROOT}/')):
                offenders.append(f"仓库相对路径：{line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_systemd_tools_and_git_never_open_a_pager():
    offenders = [line.strip() for line in _logical_lines()
                 if re.search(r"\b(systemctl|journalctl|networkctl|resolvectl|git)\s", line)
                 and "--no-pager" not in line]
    assert not offenders, "这些调用没带 --no-pager：\n" + "\n".join(offenders)


def test_no_bare_variable_is_immediately_followed_by_a_wide_character():
    offenders = [f"{number}: {match.group(0)!r}"
                 for number, line in enumerate(_text().splitlines(), 1)
                 for match in BARE_VAR_BEFORE_WIDE.finditer(line)]
    assert not offenders, "改成 ${var}：\n" + "\n".join(offenders)


# ---------------- 定时器与单元：只读，不碰 ----------------

def test_no_unit_is_disabled_or_masked_and_only_the_dead_man_is_ever_stopped():
    offenders = []
    for line in _logical_lines():
        if not re.search(r"\bsystemctl\b", line):
            continue
        units = _UNIT.findall(line)
        if re.search(r"\b(disable|mask|unmask|reenable|isolate)\b", line):
            offenders.append(f"停用/屏蔽：{line.strip()}")
        if re.search(r"\bstop\b", line) and units != ["nmu-netplan-revert.timer"]:
            offenders.append(f"stop 了死人开关以外的东西：{line.strip()}")
        if (re.search(r"\b(start|restart|enable|reload|try-restart)\b", line)
                and any(unit.endswith(".timer") for unit in units)):
            offenders.append(f"动了定时器：{line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_the_post_reboot_check_reads_all_six_timers():
    """开窗前与重启后共用同一段只读核对：SubState=waiting、NEXT 非空、enabled。"""
    timers = re.search(r'^TIMERS="([^"]+)"$', _text(), re.M)
    assert timers and set(timers.group(1).split()) == NMU_TIMERS
    assert "remote_health after" in _function("post_reboot_asserts")
    health = _function("remote_health")
    assert "for t in ${TIMERS}" in health
    for prop in ("SubState", "NextElapseUSecRealtime", "UnitFileState"):
        assert prop in health


def test_the_post_reboot_check_waits_for_activating_units_instead_of_failing_them():
    """11-06 重启后约 15 秒就来核对,nmu 的 ExecStartPre 还没跑完(activating),窗口当场判 FAIL;它几秒后就起来了。"""
    health = next(body for delimiter, body in _heredocs(_text())
                  if delimiter == "REMOTE" and "for t in ${TIMERS}" in body)
    loops = re.findall(r"^for unit in (.*); do\n(.*)\n(.*)\n(.*)\n", health, re.M)
    assert [units.split()[0] for units, *_lines in loops] == ["nmu", "${proxies}"], loops
    for _units, first, second, third in loops:
        assert first.strip() == ('state=$(systemctl --no-pager is-active "${unit}"); waited=0; '
                                 'restarts=$(systemctl --no-pager show -p NRestarts --value "${unit}")')
        assert second.strip().startswith(
            'while [ "${state}" = "activating" ] && [ "${waited}" -lt 90 ]; do sleep 3;'), second
        # Restart=always 的单元起来就崩时也在 activating 与 active 之间跳:重启次数涨了不许判 PASS。
        assert third.strip().startswith(
            '[ "$(systemctl --no-pager show -p NRestarts --value "${unit}")" = "${restarts}" ] || state='), third


def test_the_proxy_units_follow_what_is_enabled_on_the_host():
    """10-28 整个 IP 被墙后 sing-box 停用、改跑 sing-box-hy2:写死 sing-box 的核对会让窗口
    在开窗前就停下(2026-11-03 演练就停在这里)。开机自启的 sing-box* 都要在跑、开机后没重启过。"""
    bodies = [body for delimiter, body in _heredocs(_text()) if delimiter == "REMOTE"]
    lister = "list-unit-files --no-legend --state=enabled 'sing-box*.service'"
    health = next(body for body in bodies if "for t in ${TIMERS}" in body)
    after = next(body for body in bodies if "NRestarts" in body)
    assert lister in health and lister in after
    for body in bodies:
        for line in body.splitlines():
            assert not re.search(r"\bsing-box\b(?![-*])", line) or line.lstrip().startswith("#"), line


# ---------------- apt：等锁、只装点名的包 ----------------

def test_every_real_apt_get_waits_for_the_lock_and_never_upgrades_everything():
    # 只看调用：pgrep 的匹配式里也写着 apt-get，那不是在跑它。
    apt_lines = [line for line in _logical_lines() if re.search(r"(?:^|[\s;&(])apt-get\s", line)]
    real = [line for line in apt_lines if not re.search(r"\s-s\s", line)]
    assert len(real) >= 2, "装内核与清旧内核两处真 apt-get 都没找到，断言在空转"
    for line in real:
        assert "-o DPkg::Lock::Timeout=600" in line, line
        assert "DEBIAN_FRONTEND=noninteractive" in line and "NEEDRESTART_MODE=l" in line, line
    for line in apt_lines:
        assert not re.search(r"\b(full-upgrade|dist-upgrade|autoremove)\b", line) or " -s " in line, line
        assert not re.search(r"apt-get\s+(-\S+\s+)*upgrade\b", line), line


def test_the_install_is_bounded_to_the_nine_expected_kernel_packages():
    text = _text()
    target = re.search(r'^TARGET_ABI="(5\.15\.0-\d+)"$', text, re.M).group(1)
    metas = re.search(r'^META_PACKAGES="([^"]+)"$', text, re.M).group(1)
    expected = re.search(r'^EXPECTED_INSTALL="([^"]+)"$', text, re.M).group(1)
    assert metas.split() == ["linux-generic", "linux-image-generic", "linux-headers-generic",
                             "linux-libc-dev"]
    assert sorted(expected.replace("${TARGET_ABI}", target).split()) == sorted([
        "linux-generic", "linux-image-generic", "linux-headers-generic", "linux-libc-dev",
        f"linux-image-{target}-generic", f"linux-modules-{target}-generic",
        f"linux-modules-extra-{target}-generic", f"linux-headers-{target}",
        f"linux-headers-{target}-generic"])
    installs = [line for line in _logical_lines() if re.search(r"apt-get\b.*\binstall\b", line)]
    assert installs and all("${PINNED_META_PACKAGES}" in line for line in installs), installs
    assert 'KERNEL_PACKAGE_VERSION="5.15.0-203.213"' in text
    assert 'KERNEL_META_VERSION="5.15.0.203.175"' in text
    pinned = re.search(r'^PINNED_META_PACKAGES="([^"]+)"$', text, re.M).group(1)
    assert pinned.split() == [
        "linux-generic=${KERNEL_META_VERSION}", "linux-image-generic=${KERNEL_META_VERSION}",
        "linux-headers-generic=${KERNEL_META_VERSION}", "linux-libc-dev=${KERNEL_PACKAGE_VERSION}"]


# ---------------- netplan：先装死人开关，按 MAC 匹配，不改名 ----------------

def test_the_dead_man_is_armed_before_the_stanza_goes_live():
    lines = _logical_lines()
    armed = [i for i, line in enumerate(lines)
             if "systemd-run" in line and "nmu-netplan-revert" in line and "--on-active=" in line]
    placed = [i for i, line in enumerate(lines)
              if re.search(r"\binstall\b.*/etc/netplan/50-nmu-by-mac\.yaml", line)]
    applied = [i for i, line in enumerate(lines)
               if re.search(r"netplan apply", line) and "systemd-run" not in line]
    assert len(armed) == 1 and placed and applied
    assert armed[0] < placed[0] < applied[0]
    dead_man = lines[armed[0]]
    assert "mv /etc/netplan/50-nmu-by-mac.yaml" in dead_man and "netplan apply" in dead_man


def test_the_new_stanza_matches_by_mac_and_never_renames_the_card():
    assert not [line for line in _logical_lines() if "set-name" in line and "grep" not in line]
    [stanza] = [body for delimiter, body in _heredocs(_text()) if delimiter == "YAML"]
    assert "macaddress:" in stanza and "dhcp4: true" in stanza
    assert "set-name" not in stanza
    # networkd 取按文件名排在前面的那份：by-mac 要排在 ens18 前面才会被用上。
    assert re.search(r"^\s+by-mac:$", stanza, re.M) and "by-mac" < "ens18"


def _udev_rule_check() -> str:
    """演练那段 REMOTE 里核 .link / udev 规则的那几行，原样取出来在本机跑。"""
    [body] = [body for delimiter, body in _heredocs(_text())
              if delimiter == "REMOTE" and "netplan generate --root-dir" in body]
    lines = body.splitlines()
    start = next(i for i, line in enumerate(lines) if "-name '*.link'" in line)
    end = next(i for i in range(start, len(lines))
               if lines[i].strip() == "fi" or lines[i].rstrip().endswith("; fi"))
    return "\n".join(lines[start:end + 1]) + "\n"


# netplan 0.107 生成的原样(MAC 换成假的)。生产机开机就有这个文件,只有 ens18 那一条。
_NM_DENY_RULES = (
    "# netplan: network.ethernets.ens18 (on NetworkManager deny-list)\n"
    'SUBSYSTEM=="net", ACTION=="add|change|move", ENV{ID_NET_NAME}=="ens18", ENV{NM_UNMANAGED}="1"\n'
    "# netplan: network.ethernets.by-mac (on NetworkManager deny-list)\n"
    'SUBSYSTEM=="net", ACTION=="add|change|move", ATTR{address}=="52:54:00:12:34:56", '
    'ENV{NM_UNMANAGED}="1"\n')


@pytest.mark.parametrize(("case", "expected_bad"), [
    ("nm_deny_list_only", 0), ("rename_rule", 1), ("link_file", 1)])
def test_the_rehearsal_accepts_the_nm_deny_list_rule_but_not_a_rename(case, expected_bad, tmp_path):
    # 2026-10-03 第一次跑 netplan 阶段就停在这里:netplan 给每块它管的网卡写一条 udev 规则,
    # 只把网卡列进 NetworkManager 的不管名单,不改名。改名的规则和 .link 照旧要拦。
    rules = tmp_path / "dry" / "run" / "udev" / "rules.d" / "90-netplan.rules"
    rules.parent.mkdir(parents=True)
    extra = ('SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="52:54:00:12:34:56", NAME="eth9"\n'
             if case == "rename_rule" else "")
    rules.write_text(_NM_DENY_RULES + extra, encoding="utf-8")
    if case == "link_file":
        link = tmp_path / "dry" / "run" / "systemd" / "network" / "10-netplan-by-mac.link"
        link.parent.mkdir(parents=True)
        link.write_text("[Match]\nMACAddress=52:54:00:12:34:56\n[Link]\nName=eth9\n",
                        encoding="utf-8")

    result = subprocess.run(
        ["bash", "-c", f"set -u -o pipefail\nstage={shlex.quote(str(tmp_path))}\nbad=0\n"
         + _udev_rule_check() + 'echo "bad=${bad}"\n'],
        capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert f"bad={expected_bad}" in result.stdout, result.stdout


def test_the_mac_is_never_printed():
    lines = _logical_lines()
    readers = [line for line in lines if "/sys/class/net/eth0/address" in line]
    assert readers and all(re.match(r"\s*mac=\$\(", line) for line in readers), readers
    assert not [line for line in lines
                if re.search(r"\b(echo|printf)\b", line) and re.search(r"\$\{?mac\b", line)]
    assert all("altname" in line for line in lines if re.search(r"\bip\b.*\blink\b", line))
    assert all("Network File" in line for line in lines if "networkctl" in line)
    # 含 MAC 的两份文件（by-mac 的 yaml 与生成的 .network）不许整份打出来；`cat >` 是写，不算。
    assert not [line for line in lines if "by-mac" in line
                and re.search(r"\b(sha256sum|diff|head|tail)\b|\bcat\b(?!\s*>)", line)]


# ---------------- 阶段与重启 ----------------

def test_the_phases_are_exactly_the_documented_ones():
    text = _text()
    dispatch = text[text.index('case "${PHASE}" in'):]
    dispatch = dispatch[:dispatch.index("esac")]
    assert set(re.findall(r"^\s+([a-z-]+)\)", dispatch, re.M)) == PHASES
    header = text[:text.index("set -u")]
    for phase in PHASES:
        assert re.search(rf"^#\s+{phase}\s", header, re.M), f"头注释没写 {phase} 阶段"


def test_a_reboot_waits_for_a_new_boot_id_not_just_for_ssh():
    """258 的等待循环 ssh 一通就算回来了：重启命令刚发出去、机器还没关的那几秒也算。"""
    waiting = _function("reboot_and_wait")
    assert "/proc/sys/kernel/random/boot_id" in waiting
    assert re.search(r'"\$\{?current\}?" != "\$\{?1\}?"', waiting)


# ---------------- 各阶段对着一台假机器真跑一遍 ----------------
# bash -n 只查语法；set -u 下的未定义变量、算术、here-string、退出码的接法要真跑才炸，
# 而这个脚本在维护窗口之前没人会跑。换上假的 ssh/route/curl/sleep，本地流程就能在 Mac 的
# bash 3.2 和 CI 的 bash 5 上各走一遍。假机器只认识脚本该发的命令，远端正文一律答 PASS：
# 这里验的是本地这一侧的次序与收口，不是远端核对的内容。

_SIMULATION_203 = "".join(
    f"Inst {name} {old}({new} Ubuntu:22.04/jammy-updates [amd64])\n" for name, old, new in (
        ("linux-headers-5.15.0-203", "", "5.15.0-203.213"),
        ("linux-headers-5.15.0-203-generic", "", "5.15.0-203.213"),
        ("linux-headers-generic", "[5.15.0.202.174] ", "5.15.0.203.175"),
        ("linux-modules-5.15.0-203-generic", "", "5.15.0-203.213"),
        ("linux-image-5.15.0-203-generic", "", "5.15.0-203.213"),
        ("linux-modules-extra-5.15.0-203-generic", "", "5.15.0-203.213"),
        ("linux-image-generic", "[5.15.0.202.174] ", "5.15.0.203.175"),
        ("linux-generic", "[5.15.0.202.174] ", "5.15.0.203.175"),
        ("linux-libc-dev", "[5.15.0-202.212] ", "5.15.0-203.213"),
    ))

# 一台会随脚本的动作变化的假机器：装包后内核包在、重启换 boot_id 并起到新内核、
# 装死人开关的那段正文执行后新段就位、清包后 198 的包消失、重启过的应用单元不再映射旧库。
# 日志一行一条命令，远端正文按里面的关键动作打标签，方便核对先后次序。
_FAKE_SSH = '''\
import json
import os
import sys

args = sys.argv[1:]
index = 0
while args[index].startswith("-"):
    index += 2 if args[index] in ("-i", "-o") else 1
command = " ".join(args[index + 1:])
body = "" if "-n" in args[:index] else sys.stdin.read()
state_path = os.environ["FAKE_STATE"]
with open(state_path, encoding="utf-8") as handle:
    state = json.load(handle)
marks = [mark for mark in ("netplan generate", "nmu-netplan-revert --on-active", "netplan apply",
                           "stop nmu-netplan-revert.timer", "lsinitramfs", "os_security_check.py",
                           "127.0.0.1:8000/health/ready")
         if mark in body]
with open(os.environ["FAKE_SSH_LOG"], "a", encoding="utf-8") as log:
    log.write(command + (" :: " + " | ".join(marks) if marks else "") + "\\n")


def reply(text="", code=0):
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    sys.stdout.write(text)
    sys.exit(code)


netfile = state.get("netfile") or (
    "/run/systemd/network/10-netplan-%s.network" % ("by-mac" if state["stanza"] else "ens18"))
old_kernel = "linux-image-5.15.0-198-generic ii \\nlinux-modules-5.15.0-198-generic ii \\n"
if command == "true":
    reply()
if command.startswith("date -u"):
    reply(os.environ["FAKE_CLOCK"] + "\\n")
if command == "/usr/bin/python3 -I - --minutes 10":
    # 空闲门:送过来的就是 scripts/prod_idle_gate.py,原样跑一遍,日志换成测试给的那份。
    import subprocess
    gate = os.path.join(os.path.dirname(state_path), "idle_gate_received.py")
    with open(gate, "w", encoding="utf-8") as handle:
        handle.write(body)
    with open(os.environ["FAKE_JOURNAL"], "rb") as journal:
        done = subprocess.run([sys.executable, "-I", gate, "--minutes", "10", "--stdin"],
                              stdin=journal, capture_output=True, text=True)
    reply(done.stdout, done.returncode)
if command.startswith("/opt/nmu/venv/bin/python -I -"):
    reply("  场次运行状态: aborted 45\\n")
if command.startswith("EXPECTED_TARGET="):
    if state.get("abi_probe_empty"):
        reply()
    if state.get("abi_probe_failure"):
        reply("  FAIL 候选/回退与审核常量不符,先更新常量并重新评审\\n", 1)
    reply("  PASS 内核版本与审核常量一致\\n")
if "def stale_library_units" in command:
    reply()
if "boot_id" in command:
    reply(state["boot"] + "\\n")
if "last-deploy.state" in command:
    reply("commit = af59c28\\n")
if "list-unit-files" in command:
    reply(state.get("proxies", "sing-box-hy2.service "))
if command == "uname -r":
    reply(state["kernel"] + "\\n")
if "networkctl" in command:
    reply(netfile + "\\n")
if "then echo present" in command:
    reply("present\\n" if state["stanza"] else "absent\\n")
if "dpkg-query -W -f='${Package} ${Version}" in command:
    if not state["installed"]:
        reply("", 1)
    names = command.split("' ", 1)[1].split()
    rows = []
    for name in names:
        version = "5.15.0.203.175" if name in ("linux-generic", "linux-image-generic", "linux-headers-generic") else "5.15.0-203.213"
        if state.get("installed_version_drift") and name == "linux-libc-dev":
            version = "5.15.0-203.214"
        if state.get("installed_package_missing") and name == "linux-modules-extra-5.15.0-203-generic":
            continue
        rows.append(name + " " + version + " ii ")
    reply("\\n".join(rows) + "\\n")
if "dpkg-query -W -f='${Status}'" in command:
    reply("install ok installed" if state["installed"] else "")
if "dpkg-query -W -f='${Package}" in command:
    reply("" if state["purged"] else old_kernel)
if "apt-get -s install" in command:
    reply("" if state["installed"] else os.environ["FAKE_SIMULATION"])
if "apt-get -s purge" in command:
    reply("Purg linux-image-5.15.0-198-generic [5.15.0-198.208]\\n"
          "Purg linux-modules-5.15.0-198-generic [5.15.0-198.208]\\n")
if "systemd-run --wait" in command and "apt-get install" in command:
    state["installed"] = True
    reply()
if "systemd-run --wait" in command and "apt-get purge" in command:
    state["purged"] = True
    reply()
if command.startswith("tail -"):
    reply("  (日志尾部)\\n")
if command.startswith("test -s /boot/vmlinuz-"):
    reply()
if "preflight_check.py" in command:
    reply("[PASS] 假预检\\n")
if "systemctl --no-pager reboot" in command:
    state["boot"] += "-next"
    if state["installed"]:
        state["kernel"] = "5.15.0-203-generic"
    reply("", 255)
if command == "/usr/bin/python3 -I -B - /opt/nmu/app/scripts":
    # 扫旧库:原样跑送来的正文,os_security_check 换成按 stale 作答的替身。
    import subprocess
    if state.get("stale_scan_empty"):
        reply()
    scripts = os.path.join(os.path.dirname(state_path), "fake-scripts")
    os.makedirs(scripts, exist_ok=True)
    scan = ("raise PermissionError(13, 'Permission denied')" if state.get("stale_scan_error")
            else "return %r" % (state.get("stale", {}),))
    with open(os.path.join(scripts, "os_security_check.py"), "w", encoding="utf-8") as handle:
        handle.write("APP_UNITS = %r\\n\\n\\ndef stale_library_units():\\n    %s\\n"
                     % (tuple(state.get("app_units", ("nmu.service", "nginx.service"))), scan))
    done = subprocess.run([sys.executable, "-I", "-B", "-", scripts], input=body,
                          capture_output=True, text=True)
    reply(done.stdout, done.returncode)
if command == "/usr/bin/python3 -I - /opt/nmu/backups/os-security.state":
    # 读周检结论:原样跑送来的正文;state 照周检的样子写一份,带着不该打出来的库路径。
    import subprocess
    report = os.path.join(os.path.dirname(state_path), "os-security.state")
    with open(report, "w", encoding="utf-8") as handle:
        json.dump({"ok": state.get("os_result", "success") == "success",
                   "failures": state.get("os_failures", []),
                   "notes": ["另有 7 个非安全更新待装（不算失败）"],
                   "stale_library_units": state.get("stale", {})}, handle, ensure_ascii=False)
    done = subprocess.run([sys.executable, "-I", "-", report], input=body,
                          capture_output=True, text=True)
    reply(done.stdout, done.returncode)
if command == "nginx -t":
    if state.get("nginx_config_bad"):
        reply("nginx: configuration file /etc/nginx/nginx.conf test failed\\n", 1)
    reply("nginx: configuration file /etc/nginx/nginx.conf test is successful\\n")
if "show -p MainPID --value" in command:
    reply("4242\\n")
if "show -p NRestarts --value" in command:
    # 上次手动起来之后被自动拉起过两次(OOM 之类):重启前的计数不是 0。
    reply("2\\n")
if command.startswith("systemctl --no-pager restart "):
    # 重启过的单元换上新库;stale_after_restart:重启了却还映射着旧库。
    if not state.get("stale_after_restart"):
        state.get("stale", {}).pop(command.split()[-1], None)
    reply()
if "start nmu-os-security.service" in command:
    # os_start_rc:start 没跑成(ssh 断了之类),而 reset-failed 已把 Result 清成 success。
    reply("", state.get("os_start_rc", 0 if state.get("os_result", "success") == "success" else 1))
if "show -p Result --value nmu-os-security.service" in command:
    reply(state.get("os_result", "success") + "\\n")
if command.endswith("bash -s"):
    if "lsinitramfs" in body and state.get("boot_target_mismatch"):
        reply("  FAIL 默认项不是当前内核\\n", 1)
    if "127.0.0.1:8000/health/ready" in body and state.get("app_unhealthy"):
        reply("  FAIL http://127.0.0.1:8000/health/ready 000(应 200)\\n", 1)
    if "nmu-netplan-revert --on-active" in body:
        state["stanza"] = True
    reply("  PASS 假机器\\n")
sys.stderr.write("没预料到的远端命令：" + command + "\\n")
sys.exit(97)
'''

# 11-06 之后的生产机:跑 202,按 MAC 匹配的段已在、网络走 by-mac 那份。
_FRESH_HOST = {"boot": "boot-a", "kernel": "5.15.0-202-generic", "installed": False,
               "stanza": True, "purged": False}


def _run_phase(tmp_path: Path, phase: str, answers: str = "", clock: str = "5 03 10",
               simulation: str = _SIMULATION_203, host: dict | None = None,
               journal: str = "",
               script: Path = SCRIPT) -> tuple[subprocess.CompletedProcess, list[str]]:
    import json
    import os
    import sys

    fake_bin = tmp_path / "bin"
    state = tmp_path / "host.json"
    if not fake_bin.exists():
        fake_bin.mkdir()
        (fake_bin / "ssh").write_text(f"#!{sys.executable}\n{_FAKE_SSH}", encoding="utf-8")
        (fake_bin / "route").write_text("#!/bin/sh\necho '    interface: en0'\n", encoding="utf-8")
        (fake_bin / "curl").write_text(
            '#!/bin/sh\ncase "$*" in *"/docs"*) printf 404 ;; *) printf 200 ;; esac\n',
            encoding="utf-8")
        (fake_bin / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        for tool in fake_bin.iterdir():
            tool.chmod(0o755)
        state.write_text(json.dumps(host or _FRESH_HOST), encoding="utf-8")
    log = tmp_path / f"ssh-{phase}.log"
    log.write_text("", encoding="utf-8")
    nmu_journal = tmp_path / "nmu-journal.log"
    nmu_journal.write_text(journal, encoding="utf-8")
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "HOME": str(tmp_path),
           "FAKE_SSH_LOG": str(log), "FAKE_STATE": str(state), "FAKE_CLOCK": clock,
           "FAKE_SIMULATION": simulation, "FAKE_JOURNAL": str(nmu_journal)}
    result = subprocess.run(["bash", str(script), phase], input=answers, capture_output=True,
                            text=True, env=env, timeout=120)
    assert "没预料到的远端命令" not in result.stderr, result.stderr
    return result, log.read_text(encoding="utf-8").splitlines()


REBOOT = "systemctl --no-pager reboot"


def _first(commands: list[str], needle: str) -> int:
    return next(i for i, command in enumerate(commands) if needle in command)


def test_without_a_phase_it_prints_usage_and_never_reaches_the_host(tmp_path):
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=30,
                            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert result.returncode == 64
    assert "kernel_netplan_window.sh <check|kernel|" in result.stdout


def test_the_check_phase_passes_on_a_quiet_host_and_changes_nothing(tmp_path):
    result, commands = _run_phase(tmp_path, "check")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "演练:每一项都过" in result.stdout
    assert not [c for c in commands if re.search(r"systemd-run|reboot|netplan apply", c)]


def test_the_idle_gate_is_the_shared_script_not_an_inline_filter():
    """11-05 扫描器把内联的 grep 过滤挡死了:过滤只许有 scripts/prod_idle_gate.py 那一份,
    经 RBASH(不带 -n)的 stdin 送过去;RSH 的 -n 会把脚本丢掉。"""
    text = _text()
    assert not re.search(r"journalctl|grep -vcE|patient-heartbeat|401 Unauthorized", text)
    assert """RBASH '/usr/bin/python3 -I - --minutes 10' < "${IDLE_GATE}\"""" in _function("idle_ok")
    definition = re.search(r"^IDLE_GATE=.*$", text, re.M).group(0)
    resolved = subprocess.run(["bash", "-c", definition + '\nprintf %s "${IDLE_GATE}"', str(SCRIPT)],
                              capture_output=True, text=True, check=True).stdout
    assert Path(resolved) == ROOT / "scripts" / "prod_idle_gate.py"


_SCANNER = "".join(f'INFO:     198.51.100.23:0 - "GET {path} HTTP/1.1" 200 OK\n' for path in (
    "/.env", "/.git/config", "/actuator/env", "/server-status", "/v2/_catalog", "/%40vite/env"))
_POLLING = ('INFO:     127.0.0.1:0 - "GET /health/ready HTTP/1.1" 200 OK\n'
            'INFO:     203.0.113.9:0 - "GET /live/state HTTP/1.1" 200 OK\n')
_ONE_ANSWER = 'INFO:     203.0.113.9:0 - "POST /items/IE-1/turns HTTP/1.1" 200 OK\n'


@pytest.mark.parametrize(("journal", "idle", "summary"), [
    (_SCANNER + _POLLING, True, "  近 10 分钟真实使用 0 条(不算:轮询 2、"),
    (_SCANNER + _POLLING + _ONE_ANSWER, False, ",有人在用"),
    (_POLLING + 'INFO:     203.0.113.9:0 - "POST /items/IE-1/turns HTTP/1.1\n', False,
     "像访问日志却解析不了,按不空闲算"),
], ids=["scanner-noise", "one-answer", "unparsable"])
def test_the_check_phase_counts_only_real_use_as_busy(journal, idle, summary, tmp_path):
    result, _ = _run_phase(tmp_path, "check", journal=journal)

    assert summary in result.stdout, result.stdout + result.stderr
    assert ("这一项不过:生产空闲" in result.stdout) is not idle
    assert result.returncode == (0 if idle else 1)


def test_the_kernel_phase_stops_at_the_idle_gate_before_touching_anything(tmp_path):
    result, commands = _run_phase(tmp_path, "kernel", answers="yes\n" * 4,
                                  journal=_SCANNER + _ONE_ANSWER)

    assert result.returncode == 1 and "停:生产空闲(生产没有任何改动)" in result.stdout
    assert "近 10 分钟真实使用 1 条(" in result.stdout
    assert not [c for c in commands if "apt-get install" in c or REBOOT in c]


def test_without_the_gate_script_beside_it_the_host_is_not_idle(tmp_path):
    copy = tmp_path / "repo" / "deploy" / "maintenance" / SCRIPT.name
    copy.parent.mkdir(parents=True)
    copy.write_text(_text(), encoding="utf-8")

    result, commands = _run_phase(tmp_path, "check", script=copy)

    assert "本机读不到" in result.stdout and "这一项不过:生产空闲" in result.stdout
    assert "用绝对路径跑" in result.stdout
    assert result.returncode == 1
    assert not [c for c in commands if c == "/usr/bin/python3 -I - --minutes 10"]


def test_a_gate_that_never_reached_the_host_is_not_idle(tmp_path):
    """ssh -n 把 stdin 换成 /dev/null:远端 python3 读到空程序、一声不吭退 0,不能当空闲。"""
    copy = tmp_path / "repo" / "deploy" / "maintenance" / SCRIPT.name
    copy.parent.mkdir(parents=True)
    (tmp_path / "repo" / "scripts").mkdir()
    (tmp_path / "repo" / "scripts" / "prod_idle_gate.py").write_bytes(
        (ROOT / "scripts" / "prod_idle_gate.py").read_bytes())
    pipe = "RBASH '/usr/bin/python3 -I - --minutes 10'"
    assert pipe in _text()
    copy.write_text(_text().replace(pipe, "RSH '/usr/bin/python3 -I - --minutes 10'"),
                    encoding="utf-8")

    result, commands = _run_phase(tmp_path, "check", script=copy)

    assert [c for c in commands if c == "/usr/bin/python3 -I - --minutes 10"]
    assert "  查不到" in result.stdout and "这一项不过:生产空闲" in result.stdout
    assert result.returncode == 1


def test_the_check_phase_flags_the_nightly_window_and_an_unexpected_package(tmp_path):
    extra = ("Inst linux-firmware [20220329] "
             "(20220329.git681281e4-0ubuntu3.40 Ubuntu:22.04/jammy-updates [all])\n")
    # 周五 18:10 UTC：正在夜间补丁的窗口里；模拟又多带进一个包。
    result, _ = _run_phase(tmp_path, "check", clock="5 18 10", simulation=_SIMULATION_203 + extra)

    assert result.returncode == 1
    assert "这一项不过:时间窗" in result.stdout
    assert "这一项不过:装包模拟只带进预期的 9 个包" in result.stdout
    assert "演练:2 项不过" in result.stdout


def test_the_kernel_phase_installs_checks_reboots_and_checks_again_in_that_order(tmp_path):
    result, commands = _run_phase(tmp_path, "kernel", answers="yes\n" * 4)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "kernel 阶段完成" in result.stdout
    installed = _first(commands, "apt-get install")
    assert (installed < _first(commands, ":: lsinitramfs") < _first(commands, REBOOT)
            < _first(commands, ":: os_security_check.py"))
    assert not [c for c in commands if re.search(r"netplan (apply|generate)|nmu-netplan-revert", c)]


@pytest.mark.parametrize(("stanza", "network"), [(True, "by-mac"), (False, "ens18")])
def test_the_kernel_phase_expects_the_network_it_found(stanza, network, tmp_path):
    """内核这一轮网络不动:重启后核对用的期望 Network File 跟着按 MAC 匹配的段在不在走。"""
    result, commands = _run_phase(tmp_path, "kernel", answers="yes\n" * 4,
                                  host={**_FRESH_HOST, "stanza": stanza})

    assert result.returncode == 0, result.stdout + result.stderr
    [after] = [c for c in commands if "EXPECT_NETWORK=" in c and "BOOT_BEFORE=boot-a" in c]
    assert f"EXPECT_NETWORK=/run/systemd/network/10-netplan-{network}.network" in after


def test_the_kernel_phase_stops_when_the_stanza_and_the_live_network_disagree(tmp_path):
    result, commands = _run_phase(tmp_path, "kernel", answers="yes\n" * 4, host={
        **_FRESH_HOST, "netfile": "/run/systemd/network/10-netplan-ens18.network"})

    assert result.returncode == 1 and "netplan 改到一半" in result.stdout
    assert not [c for c in commands if "apt-get install" in c or REBOOT in c]


def test_verify_kernel_reads_the_expected_network_from_the_host(tmp_path):
    result, commands = _run_phase(tmp_path, "verify-kernel", host={
        **_FRESH_HOST, "kernel": "5.15.0-203-generic", "installed": True})

    assert result.returncode == 0, result.stdout + result.stderr
    assert [c for c in commands
            if "EXPECT_NETWORK=/run/systemd/network/10-netplan-by-mac.network" in c]
    assert not [c for c in commands if re.search(r"systemd-run|reboot|apt-get", c)]


def test_the_proxies_enabled_before_the_reboot_are_what_the_post_reboot_check_expects(tmp_path):
    """列不到单元时 systemctl 也退 1:重启后拿重启前记下的清单对,才分得清「真没有」与「出错」。"""
    result, commands = _run_phase(tmp_path, "kernel", answers="yes\n" * 4)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "开机自启的代理:sing-box-hy2.service" in result.stdout
    [before] = [c for c in commands if "WHEN=before" in c]
    [after] = [c for c in commands if "WHEN=after" in c]
    assert "PROXIES_BEFORE='-'" in before
    assert "PROXIES_BEFORE='sing-box-hy2.service '" in after


def test_the_check_phase_flags_a_half_done_netplan_change(tmp_path):
    result, commands = _run_phase(tmp_path, "check", host={
        **_FRESH_HOST, "netfile": "/run/systemd/network/10-netplan-ens18.network"})

    assert result.returncode == 1
    assert "这一项不过:按 MAC 匹配的段与 eth0 的 Network File 一致" in result.stdout
    assert not [c for c in commands if re.search(r"systemd-run|reboot|netplan apply", c)]


def test_a_declined_reboot_leaves_the_kernel_installed_and_the_rerun_skips_the_install(tmp_path):
    result, commands = _run_phase(tmp_path, "kernel", answers="yes\nyes\nyes\nno\n")
    assert result.returncode == 1 and "没有确认" in result.stdout
    assert [c for c in commands if "apt-get install" in c]
    assert not [c for c in commands if REBOOT in c]

    result, commands = _run_phase(tmp_path, "kernel", answers="yes\n" * 4)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "跳过安装" in result.stdout
    assert not [c for c in commands if "apt-get install" in c]
    assert [c for c in commands if REBOOT in c]


def test_the_kernel_phase_refuses_to_run_twice(tmp_path):
    result, commands = _run_phase(tmp_path, "kernel", answers="yes\n" * 4, host={
        **_FRESH_HOST, "kernel": "5.15.0-203-generic", "installed": True})

    assert result.returncode == 1 and "这一阶段做过了" in result.stdout
    assert not [c for c in commands if "apt-get install" in c or REBOOT in c]


def test_the_netplan_phase_arms_the_dead_man_before_applying_and_disarms_before_rebooting(tmp_path):
    result, commands = _run_phase(tmp_path, "netplan", answers="yes\n" * 4, host={
        **_FRESH_HOST, "kernel": "5.15.0-203-generic", "installed": True, "stanza": False})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "netplan 阶段完成" in result.stdout
    armed = _first(commands, "nmu-netplan-revert --on-active | netplan apply")
    disarmed = _first(commands, "stop nmu-netplan-revert.timer")
    assert (_first(commands, ":: netplan generate") < armed < disarmed
            < _first(commands, REBOOT) < _first(commands, ":: os_security_check.py"))
    # 新段接管要在装上开关之后、拆开关之前，用新开的连接看
    assert [c for c in commands[armed + 1:disarmed] if "networkctl" in c]
    assert not [c for c in commands if "apt-get install" in c]


def test_the_purge_phase_simulates_first_and_never_reboots(tmp_path):
    result, commands = _run_phase(tmp_path, "purge", answers="yes\n", host={
        **_FRESH_HOST, "kernel": "5.15.0-203-generic", "installed": True})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "purge 阶段完成" in result.stdout
    assert _first(commands, "apt-get -s purge") < _first(commands, "apt-get purge -y")
    assert not [c for c in commands if REBOOT in c]


# ---------------- O03:候选与机器现状推导,常量仍需人工评审 ----------------

def _abi_checker():
    [body] = [body for delimiter, body in _heredocs(_text())
              if delimiter == "PY" and "def derive_abis(" in body]
    parsed = ast.parse(body)
    # 原样执行生产算法的定义,不运行它读本机 apt 与 /boot 的入口。
    parsed.body = [node for node in parsed.body
                   if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef))]
    namespace = {}
    exec(compile(parsed, "<kernel ABI probe>", "exec"), namespace)
    return namespace


def _images(*abis):
    return "".join(f"linux-image-{abi}-generic ii \n" for abi in abis)


_REVIEWED_ABIS = ("5.15.0-203", "5.15.0-202", "5.15.0-198")
_CANDIDATE_203 = "Package: linux-image-generic\nDepends: linux-image-5.15.0-203-generic, linux-firmware\n"


@pytest.mark.parametrize("candidate", ["5.15.0.203.176", "(none)", "5.15.0.202.174"])
def test_candidate_revision_changes_cannot_hide_behind_a_pinned_old_meta(candidate):
    policy = "linux-image-generic:\n  Candidate: " + candidate + "\n"
    with pytest.raises(ValueError, match="候选精确版本"):
        _abi_checker()["checked_candidate"](policy, "linux-image-generic", "5.15.0.203.175")


def test_reviewed_candidate_revision_is_accepted():
    assert _abi_checker()["checked_candidate"](
        "linux-libc-dev:\n  Candidate: 5.15.0-203.213\n", "linux-libc-dev", "5.15.0-203.213"
    ) == "5.15.0-203.213"


@pytest.mark.parametrize(("installed", "boot", "expected_purge"), [
    (("5.15.0-202", "5.15.0-198"), ("5.15.0-202", "5.15.0-198"), "5.15.0-198"),
    (_REVIEWED_ABIS, _REVIEWED_ABIS, "5.15.0-198"),
    (_REVIEWED_ABIS[:2], _REVIEWED_ABIS[:2], None),
])
def test_abi_derivation_handles_before_install_after_install_and_after_purge(installed, boot,
                                                                         expected_purge):
    checker = _abi_checker()
    result = checker["derive_abis"](_CANDIDATE_203, _images(*installed), boot, boot)
    assert result == ("5.15.0-203", "5.15.0-202", expected_purge)
    checker["check_abis"](result, _REVIEWED_ABIS, installed[0] + "-generic")


def test_abi_derivation_still_sees_old_residual_packages_after_boot_files_are_gone():
    checker = _abi_checker()
    boot = _REVIEWED_ABIS[:2]
    packages = (_images(*boot) + "linux-image-5.15.0-198-generic rc \n"
                "linux-modules-5.15.0-198-generic rc \n"
                "linux-headers-5.15.0-190 un \n")
    assert checker["derive_abis"](_CANDIDATE_203, packages, boot, boot) == _REVIEWED_ABIS


@pytest.mark.parametrize(("metadata", "packages", "boot", "ready", "error"), [
    ("Depends: linux-image-5.15.0-203-generic | linux-image-5.15.0-205-generic\n",
     _images(*_REVIEWED_ABIS), _REVIEWED_ABIS, _REVIEWED_ABIS, "不是唯一"),
    (_CANDIDATE_203, _images("5.15.0-203", "5.15.0-202", "5.15.0-198", "5.15.0-190"),
     _REVIEWED_ABIS, _REVIEWED_ABIS, "待清内核不是唯一"),
    (_CANDIDATE_203, _images(*_REVIEWED_ABIS), _REVIEWED_ABIS, ("5.15.0-203",), "没有同时具备"),
    (_CANDIDATE_203, _images("5.15.0-202") + "linux-headers-5.15.0-203 iF \n",
     ("5.15.0-202",), ("5.15.0-202",), "没有安装/清理完"),
    (_CANDIDATE_203, _images("5.15.0-205", "5.15.0-203", "5.15.0-202"),
     ("5.15.0-203", "5.15.0-202"), ("5.15.0-203", "5.15.0-202"), "比 apt 候选更新"),
    (_CANDIDATE_203, "linux-image-5.15.0-202-generic\n", (), (), "格式不完整"),
])
def test_abi_derivation_rejects_ambiguous_or_incomplete_inventory(metadata, packages, boot, ready,
                                                              error):
    with pytest.raises(ValueError, match=error):
        _abi_checker()["derive_abis"](metadata, packages, boot, ready)


@pytest.mark.parametrize(("derived", "expected", "running", "error"), [
    (("5.15.0-205", "5.15.0-203", "5.15.0-202"), _REVIEWED_ABIS, "5.15.0-203-generic", "候选/回退"),
    (("5.15.0-203", "5.15.0-198", None), _REVIEWED_ABIS, "5.15.0-203-generic", "候选/回退"),
    (("5.15.0-203", "5.15.0-202", "5.15.0-190"), _REVIEWED_ABIS, "5.15.0-203-generic", "PURGE_ABI"),
    (_REVIEWED_ABIS, _REVIEWED_ABIS, "5.15.0-198-generic", "运行内核"),
    (_REVIEWED_ABIS, ("5.15.0-203", "5.15.0-202", "5.15.0-203"), "5.15.0-203-generic", "审核常量的顺序"),
])
def test_discovered_abis_never_automatically_replace_the_reviewed_constants(derived, expected,
                                                                       running, error):
    with pytest.raises(ValueError, match=error):
        _abi_checker()["check_abis"](derived, expected, running)


@pytest.mark.parametrize("phase", ["check", "kernel", "purge"])
def test_stale_abi_constants_block_check_and_the_real_mutating_phases(phase, tmp_path):
    host = {**_FRESH_HOST, "abi_probe_failure": True}
    if phase == "purge":
        host.update(kernel="5.15.0-203-generic", installed=True)
    result, commands = _run_phase(tmp_path, phase, answers="yes\n" * 4, host=host)
    assert result.returncode == 1 and "内核 ABI 与审核常量一致" in result.stdout
    assert not [c for c in commands if re.search(r"systemd-run|reboot|netplan apply", c)]


def test_an_empty_successful_ssh_response_is_not_an_abi_verification(tmp_path):
    result, _ = _run_phase(tmp_path, "check", host={**_FRESH_HOST, "abi_probe_empty": True})
    assert result.returncode == 1 and "没收到内核推导结果" in result.stdout


def test_check_accepts_the_completed_kernel_window_without_reinstalling_packages(tmp_path):
    result, commands = _run_phase(tmp_path, "check", host={
        **_FRESH_HOST, "kernel": "5.15.0-203-generic", "installed": True, "purged": True})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "审核目标已装好,无需再装包" in result.stdout
    assert not [c for c in commands if re.search(r"systemd-run|reboot|netplan apply", c)]


def test_an_empty_install_plan_without_the_target_installed_is_not_complete(tmp_path):
    result, _ = _run_phase(tmp_path, "check", simulation="")
    assert result.returncode == 1 and "9 个包尚未按精确版本装好" in result.stdout


@pytest.mark.parametrize("phase", ["check", "kernel"])
@pytest.mark.parametrize("old,new", [("5.15.0-203.213", "5.15.0-203.214"),
                                    ("5.15.0.203.175", "5.15.0.203.176")])
def test_same_abi_with_unreviewed_package_revision_is_rejected(phase, old, new, tmp_path):
    simulation = _SIMULATION_203.replace(old, new)
    result, commands = _run_phase(tmp_path, phase, simulation=simulation, answers="yes\n" * 4)
    assert result.returncode == 1 and "不符合精确包版本" in result.stdout
    assert not [command for command in commands if "systemd-run" in command or REBOOT in command]
    simulated = [command for command in commands if "apt-get -s install" in command]
    assert simulated and all("linux-generic=5.15.0.203.175" in command
                             and "linux-libc-dev=5.15.0-203.213" in command for command in simulated)


@pytest.mark.parametrize("change", ["installed_version_drift", "installed_package_missing"])
def test_empty_plan_requires_all_nine_exact_installed_versions(change, tmp_path):
    result, commands = _run_phase(tmp_path, "check", host={
        **_FRESH_HOST, "kernel": "5.15.0-203-generic", "installed": True, change: True})
    assert result.returncode == 1 and "9 个包尚未按精确版本装好" in result.stdout
    assert not [command for command in commands if "systemd-run" in command or REBOOT in command]


# ---------------- O05:只重启,不会顺便装内核或改网络 ----------------

def test_the_reboot_phase_keeps_the_kernel_and_network_without_installing_or_purging(tmp_path):
    result, commands = _run_phase(tmp_path, "reboot", answers="yes\n" * 4, host={
        **_FRESH_HOST, "kernel": "5.15.0-203-generic", "installed": True, "purged": True})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reboot 阶段完成:未安装包" in result.stdout
    assert (_first(commands, "--minutes 10") < _first(commands, ":: lsinitramfs")
            < _first(commands, REBOOT) < _first(commands, ":: os_security_check.py"))
    assert not [c for c in commands if re.search(r"systemd-run|apt-get|netplan (apply|generate)", c)]
    assert [c for c in commands if "EXPECT_NETWORK=/run/systemd/network/10-netplan-by-mac.network" in c
            and "BOOT_BEFORE=boot-a" in c]


@pytest.mark.parametrize(("changes", "journal", "answers", "error"), [
    ({}, _ONE_ANSWER, "yes\n" * 4, "停:生产空闲"),
    ({"boot_target_mismatch": True}, "", "yes\n" * 4, "默认启动项或启动文件"),
    ({"kernel": "5.15.0-202-generic"}, "", "yes\n" * 4, "只重启阶段要求"),
    ({"netfile": "/run/systemd/network/10-netplan-ens18.network"}, "", "yes\n" * 4, "网络段与实际"),
    ({}, "", "yes\nyes\nyes\nno\n", "没有确认"),
])
def test_the_reboot_phase_stops_before_reboot_on_unsafe_or_declined_conditions(changes, journal,
                                                                           answers, error, tmp_path):
    host = {**_FRESH_HOST, "kernel": "5.15.0-203-generic", "installed": True, "purged": True,
            **changes}
    result, commands = _run_phase(tmp_path, "reboot", answers=answers, journal=journal, host=host)
    assert result.returncode == 1 and error in result.stdout, result.stdout + result.stderr
    assert not [c for c in commands if re.search(r"systemd-run|netplan apply", c) or REBOOT in c]


# ---------------- restart-app:只重启还在旧库上的 nmu/nginx,再照单元重跑周检 ----------------
# 夜间补丁换了 libc6 这类库、按设计不重启 nmu,周检就一直判失败。这一阶段走同一套门槛,只重启
# 扫描点名的应用单元;假机器的扫描与读 state 都原样跑送过去的正文。

RESTART_APP_SCAN = "/usr/bin/python3 -I -B - /opt/nmu/app/scripts"
_LIBC = "/usr/lib/x86_64-linux-gnu/libc.so.6"
_STALE = {"nmu.service": [_LIBC, "/usr/lib/x86_64-linux-gnu/libm.so.6"], "nginx.service": [_LIBC],
          "dbus.service": [_LIBC], "ssh.service": [_LIBC]}
# 会动单元的 systemctl 动作;restart-app 只许出现点名的那几条 restart。
_UNIT_ACTION = re.compile(r"\bsystemctl\b.*\b(restart|try-restart|reload|stop|kill|reboot|disable|mask)\b")


def _stale(*app_units: str) -> dict:
    """点名的应用单元在旧库上;dbus、ssh 两个系统单元一直在,只该被报出来。"""
    return {unit: libraries for unit, libraries in _STALE.items()
            if unit in app_units or unit not in ("nmu.service", "nginx.service")}


def _restart_app(tmp_path: Path, answers: str = "yes\n", journal: str = "", **changes):
    return _run_phase(tmp_path, "restart-app", answers=answers, journal=journal,
                      host={**_FRESH_HOST, **changes})


@pytest.mark.parametrize(("clock", "journal", "error"), [
    ("5 03 10", _SCANNER + _ONE_ANSWER, "停:生产空闲(生产没有任何改动)"),
    # 周五 18:10 UTC:正在夜间补丁的窗口里,只重启应用也不行。
    ("5 18 10", "", "停:时间窗(生产没有任何改动)"),
], ids=["someone-practising", "nightly-apt-window"])
def test_restart_app_stops_at_the_gates_before_restarting_anything(clock, journal, error, tmp_path):
    result, commands = _run_phase(tmp_path, "restart-app", answers="yes\n", clock=clock,
                                  journal=journal, host={**_FRESH_HOST, "stale": _stale("nmu.service")})

    assert result.returncode == 1 and error in result.stdout, result.stdout + result.stderr
    assert not [c for c in commands if _UNIT_ACTION.search(c) or c.startswith(RESTART_APP_SCAN)
                or "nginx -t" in c or "nmu-os-security.service" in c]


def test_restart_app_opens_with_the_full_set_of_gates():
    """假机器对库、apt、备份与开窗前核对一律答 PASS,少了哪道门真跑也看不出来:钉住 local 之后头一句就是整套门槛。"""
    statements = [line.strip() for line in _function("phase_restart_app").splitlines()[2:]
                  if line.strip() and not line.lstrip().startswith("#")]
    assert statements[0].startswith("local ") and statements[1] == "common_gates 10", statements[:2]


def test_restart_app_restarts_only_the_stale_app_unit_and_reruns_the_weekly_check(tmp_path):
    result, commands = _restart_app(tmp_path, stale=_stale("nmu.service"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("== restart-app 完成:应用已换新库,OS 周检 success ==")
    assert "只重启 nmu.service?nmu 断 10–20 秒" in result.stdout
    assert "  nmu.service 还映射着被换掉的库:libc.so.6、libm.so.6" in result.stdout
    assert "另有 2 个单元也还在用旧库(dbus.service、ssh.service),这里不重启" in result.stdout
    assert "Result=success" in result.stdout and "  注 另有 7 个非安全更新待装" in result.stdout
    # 扫描与 state 里都有完整库路径,打出来的只许是文件名。
    assert "/usr/lib/x86_64-linux-gnu" not in result.stdout
    assert [c for c in commands if _UNIT_ACTION.search(c)] == ["systemctl --no-pager restart nmu.service"]
    assert not [c for c in commands
                if re.search(r"sing-box|nmu-caddy|ssh\.service|systemd-|apt-get|dpkg|nginx|--force", c)]
    scans = [i for i, c in enumerate(commands) if c.startswith(RESTART_APP_SCAN)]
    assert len(scans) == 2
    assert (scans[0] < _first(commands, "show -p MainPID --value nmu.service")
            < _first(commands, "systemctl --no-pager restart nmu.service")
            < _first(commands, ":: 127.0.0.1:8000/health/ready") < scans[1]
            < _first(commands, "reset-failed nmu-os-security.service; "
                               "systemctl --no-pager start nmu-os-security.service")
            < _first(commands, "show -p Result --value nmu-os-security.service")
            < _first(commands, "WHEN=after"))
    [after] = [c for c in commands if ":: 127.0.0.1:8000/health/ready" in c]
    assert after.startswith("BEFORE='nmu.service=4242/2 ' SITE=https://89-208-253-119.sslip.io ")


def test_a_declined_restart_restarts_nothing(tmp_path):
    result, commands = _restart_app(tmp_path, answers="no\n",
                                    stale=_stale("nmu.service", "nginx.service"))

    assert result.returncode == 1
    assert "停:没有确认:只重启 nmu.service nginx.service?" in result.stdout
    assert "nginx 断几秒" in result.stdout
    assert not [c for c in commands if _UNIT_ACTION.search(c) or "nginx -t" in c
                or "nmu-os-security.service" in c]


@pytest.mark.parametrize("os_result", ["success", "exit-code"])
def test_with_no_stale_app_unit_nothing_restarts_but_the_weekly_check_reruns(os_result, tmp_path):
    """一个要重启的都没有也照样重跑周检:过时的 failed 状态要清掉。不问问题(answers 为空)。"""
    result, commands = _restart_app(tmp_path, answers="", stale=_stale(), os_result=os_result)

    assert "  要重启:无" in result.stdout and "不用重启" in result.stdout
    assert not [c for c in commands if _UNIT_ACTION.search(c) or "nginx -t" in c]
    assert [c for c in commands if "start nmu-os-security.service" in c]
    if os_result == "success":
        assert result.returncode == 0, result.stdout + result.stderr
        assert "restart-app 完成:nmu/nginx 本来就不在旧库上,没有重启,OS 周检 success" in result.stdout
        assert [c for c in commands if "WHEN=after" in c]
    else:
        assert result.returncode == 1
        assert "没有重启;OS 周检仍不过,原因见上" in result.stdout
        assert not [c for c in commands if "WHEN=after" in c]


@pytest.mark.parametrize("change", ["app_unhealthy", "stale_after_restart"])
def test_a_bad_restart_stops_before_the_weekly_check_and_says_what_was_restarted(change, tmp_path):
    """健康接口等不到 200、或者重启了还映射着旧库:停,说清已经重启过、上机看什么。"""
    result, commands = _restart_app(tmp_path, stale=_stale("nmu.service"), **{change: True})

    assert result.returncode == 1
    assert "已经重启过:nmu.service" in result.stdout
    assert "上机看 systemctl --no-pager status -n 50 nmu.service" in result.stdout
    assert [c for c in commands if _UNIT_ACTION.search(c)] == ["systemctl --no-pager restart nmu.service"]
    assert not [c for c in commands if "nmu-os-security.service" in c or "WHEN=after" in c]


@pytest.mark.parametrize(("os_result", "start_rc", "shown"), [
    ("exit-code", 1, "  FAIL 1 个安全更新待安装（procps…）"),
    # reset-failed 会把 Result 清成 success:start 没跑成时读到的 success 不算数。
    ("success", 255, "start 退出码 255,Result=success"),
], ids=["check-failed", "start-never-ran"])
def test_a_weekly_check_that_still_fails_is_not_reported_as_a_failed_restart(os_result, start_rc,
                                                                             shown, tmp_path):
    """重启好了而周检仍不过(积压里还有包之类):原因照 state 打出来,单独一行收尾,退 1。"""
    result, commands = _restart_app(tmp_path, stale=_stale("nmu.service"), os_result=os_result,
                                    os_start_rc=start_rc,
                                    os_failures=["1 个安全更新待安装（procps…）；修法：其余 1 个夜间补丁没装上"])

    assert result.returncode == 1
    assert result.stdout.rstrip().endswith("== restart-app 完成:应用已换新库;OS 周检仍不过,原因见上 ==")
    assert f"start 退出码 {start_rc},Result={os_result}" in result.stdout and shown in result.stdout
    assert "停:" not in result.stdout and "已经重启过" not in result.stdout
    assert [c for c in commands if _UNIT_ACTION.search(c)] == ["systemctl --no-pager restart nmu.service"]
    assert not [c for c in commands if "WHEN=after" in c]


@pytest.mark.parametrize("stale_units", [("nginx.service",), ("nmu.service", "nginx.service")])
@pytest.mark.parametrize("config_ok", [True, False])
def test_nginx_config_is_tested_before_any_restart(stale_units, config_ok, tmp_path):
    """nginx -t 在任何重启之前跑;不过就一个都不重启,连同样在旧库上的 nmu 也不动。"""
    result, commands = _restart_app(tmp_path, stale=_stale(*stale_units),
                                    nginx_config_bad=not config_ok)

    restarts = [c for c in commands if _UNIT_ACTION.search(c)]
    if config_ok:
        assert result.returncode == 0, result.stdout + result.stderr
        assert restarts == ["systemctl --no-pager restart " + unit for unit in stale_units]
        assert _first(commands, "nginx -t") < _first(commands, "systemctl --no-pager restart ")
    else:
        assert result.returncode == 1 and "停:nginx -t 没过,什么都没重启" in result.stdout
        assert "nginx -t" in commands and not restarts
        assert not [c for c in commands if "nmu-os-security.service" in c]


@pytest.mark.parametrize(("change", "message"), [
    ({"stale_scan_error": True}, "  FAIL 扫不了哪些进程还在用被换掉的库(PermissionError"),
    ({"stale_scan_empty": True}, "  FAIL 没收到扫描结果"),
    ({"app_units": ["nmu.service", "sing-box-hy2.service"],
      "stale": {"nmu.service": [_LIBC], "sing-box-hy2.service": [_LIBC]}},
     "那一行不认识:nmu.service sing-box-hy2.service"),
], ids=["scan-error", "empty-answer", "unexpected-unit"])
def test_a_scan_that_is_not_a_clean_answer_restarts_nothing(change, message, tmp_path):
    """扫不了、没收到、或者答出 nmu/nginx 以外的单元:没扫清楚不当作干净,也绝不顺手重启别的。"""
    result, commands = _restart_app(tmp_path, **{"stale": _stale("nmu.service"), **change})

    assert result.returncode == 1 and message in result.stdout, result.stdout + result.stderr
    assert "停:扫不了哪些进程还在用被换掉的库" in result.stdout
    assert not [c for c in commands if _UNIT_ACTION.search(c) or "nginx -t" in c
                or "nmu-os-security.service" in c]


def test_restart_app_never_reboots_installs_or_touches_any_other_unit():
    """只重启 nmu、nginx:不重启整机、不碰 apt/dpkg、代理、ssh、systemd-*、网关和定时器,也不 --force。"""
    code = [line for name in ("stale_app_units", "post_restart_asserts", "rerun_os_security",
                              "phase_restart_app")
            for line in _function(name).splitlines() if not line.lstrip().startswith("#")]
    assert len(code) > 50, "没取到 restart-app 的几段函数,下面的断言在空转"
    for line in code:
        assert not re.search(r"reboot|apt-get|dpkg|sing-box|nmu-caddy|ssh\.service|systemd-|--force"
                             r"|\.timer\b", line), line
        assert not re.search(r"\bsystemctl\b.*\b(stop|disable|mask|kill|reload)\b", line), line
    restarts = [line.strip() for line in code if re.search(r"\bsystemctl\b.*\brestart\b", line)]
    assert restarts == ['RSH "systemctl --no-pager restart ${unit}"'], restarts


# 重启后那段远端正文要在本机原样跑一遍:假机器对它一律答 PASS,它自己的判法只有这里看得到。
# 最后一个参数是网址,各接口的状态码、单元状态由环境变量给;sleep 只记次数。
_FAKE_HOST_TOOLS = {
    "curl": ('#!/bin/sh\nfor url; do :; done\ncase "$url" in\n'
             '  *147-182-176-237*) printf %s "${CN_CODE:-200}" ;;\n'
             '  */docs) printf %s "${DOCS_CODE:-404}" ;;\n'
             '  */health/ready) printf %s "${READY_CODE:-200}" ;;\n'
             '  *) printf 200 ;;\nesac\n'),
    "systemctl": ('#!/bin/sh\ncase "$*" in\n'
                  '  *is-active*) echo "${ACTIVE:-active}" ;;\n'
                  '  *MainPID*) echo "${NEW_PID:-5151}" ;;\n'
                  '  *NRestarts*) echo "${NEW_RESTARTS:-0}" ;;\n'
                  '  *) exit 97 ;;\nesac\n'),
    "sleep": '#!/bin/sh\necho x >> "$SLEEP_LOG"\n',
}


def _post_restart_check(tmp_path: Path, env: dict,
                        preamble: str = "") -> tuple[subprocess.CompletedProcess, int]:
    """(结果, sleep 了几次)。preamble 在正文之前执行,用来把 SECONDS 拨到某个已过去的秒数。"""
    [body] = [body for delimiter, body in _heredocs(_text())
              if delimiter == "REMOTE" and "127.0.0.1:8000/health/ready" in body]
    fake = tmp_path / "bin"
    fake.mkdir()
    for name, text in _FAKE_HOST_TOOLS.items():
        (fake / name).write_text(text, encoding="utf-8")
        (fake / name).chmod(0o755)
    sleeps = tmp_path / "sleeps"
    result = subprocess.run(["bash", "-s"], input=preamble + body, capture_output=True, text=True,
                            timeout=60, env={"PATH": f"{fake}:/usr/bin:/bin", "SLEEP_LOG": str(sleeps),
                                             "BEFORE": "nmu.service=4242/0 ",
                                             "SITE": "https://89-208-253-119.sslip.io", **env})
    return result, sleeps.read_text(encoding="utf-8").count("x") if sleeps.exists() else 0


@pytest.mark.parametrize(("env", "rc", "shown"), [
    ({"CN_CODE": "000"}, 0, "注 国内入口 https://147-182-176-237.sslip.io/health 000(只看不判)"),
    ({"READY_CODE": "503"}, 1, "FAIL http://127.0.0.1:8000/health/ready 503(应 200)"),
    ({"NEW_PID": "4242"}, 1, "FAIL nmu.service MainPID 4242(重启前 4242),没换成新进程"),
    ({"NEW_RESTARTS": "1"}, 1, "FAIL nmu.service NRestarts=1(手动重启会清零,重启前 0):起来就崩过"),
    # 手动 restart 把计数清零:重启前被自动拉起过两次,干净地起来读到的是 0。
    ({"BEFORE": "nmu.service=4242/2 ", "NEW_RESTARTS": "0"}, 0, "PASS nmu.service 起来时没自动重启过"),
    # 重启前是 1,起来时又崩了一次:清零后再数到 1,和重启前一样也不算过。
    ({"BEFORE": "nmu.service=4242/1 ", "NEW_RESTARTS": "1"}, 1,
     "FAIL nmu.service NRestarts=1(手动重启会清零,重启前 1):起来就崩过"),
    ({"ACTIVE": "activating"}, 1, "FAIL nmu.service activating"),
    ({"DOCS_CODE": "200"}, 1, "FAIL https://89-208-253-119.sslip.io/docs 200(应 404)"),
], ids=["cn-entry-only-reported", "never-ready", "same-pid", "auto-restarted",
        "counter-flushed-by-restart", "crashed-again-same-count", "not-active", "docs-open"])
def test_the_post_restart_check_on_the_host(env, rc, shown, tmp_path):
    result, waited = _post_restart_check(tmp_path, env)

    assert result.returncode == rc and shown in result.stdout, result.stdout + result.stderr
    # 等不到 200 最多 90 秒:每 3 秒一次,30 次就收手;一开始就好的一次都不等。
    assert waited == (30 if "READY_CODE" in env else 0)


def test_hanging_health_probes_cannot_stretch_the_wait_past_two_minutes(tmp_path):
    """curl 每次都卡满 10 秒时,只按 sleep 计数会拖到十来分钟;SECONDS 过了 120 就收手。"""
    result, waited = _post_restart_check(tmp_path, {"READY_CODE": "000"}, preamble="SECONDS=200\n")

    assert result.returncode == 1 and waited == 0, result.stdout + result.stderr
    assert "FAIL http://127.0.0.1:8000/health/ready 000(应 200)" in result.stdout


def test_the_systemd_readme_sends_stale_library_restarts_through_restart_app():
    readme = (ROOT / "deploy" / "systemd" / "README.md").read_text(encoding="utf-8")
    assert "kernel_netplan_window.sh restart-app" in readme
    assert "systemctl restart nmu" not in readme
