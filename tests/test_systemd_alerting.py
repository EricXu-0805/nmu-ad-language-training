"""systemd 单元的告警面：每一档故障都得有人被通知到。

2026-08-27 审查实测的三个死角，每条在这里各有一条红相：

1. `Restart=always` + `RestartSec=3`，而 systemd 默认 `StartLimitIntervalSec=10`、
   `StartLimitBurst=5`。3 秒一重启在任意 10 秒窗口最多落 4 次，**永远够不到 burst**，
   单元于是永不进 failed，`OnFailure=nmu-alert@` 永不触发。一棵只同步了一半的树
   会无限重启，而 Discord 全程安静、backup.log 照常 ok。
2. `nmu-os-security.service` 把命令包成 `sh -c '… || true'`，退出码恒 0，
   它自己那行 OnFailure 是死的——补丁积压攒到 106 个那次就是这么攒起来的。
3. 「进程活着但返 500 / Caddy 死了 / 证书过期 / 磁盘满转只读」这一整档，
   没有任何定时器去探公网，靠 backup.log 是看不出来的（备份直接读磁盘上的 db）。
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest

UNITS = Path(__file__).resolve().parents[1] / "deploy" / "systemd"


def _units() -> list[Path]:
    return sorted(p for p in UNITS.glob("*.service") if p.name != "nmu-alert@.service")


def _directives(path: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        found.setdefault(key.strip(), []).append(value.strip())
    return found


@pytest.mark.parametrize("unit", _units(), ids=lambda p: p.name)
def test_every_unit_routes_its_failure_to_the_alert_service(unit):
    assert "nmu-alert@%N.service" in _directives(unit).get("OnFailure", []), (
        f"{unit.name} 没有 OnFailure=nmu-alert@%N.service —— 它失败时没有人会知道")


@pytest.mark.parametrize("unit", _units(), ids=lambda p: p.name)
def test_always_restarting_units_can_actually_reach_failed(unit):
    """否则 OnFailure 是摆设：够不到 burst 就永远不进 failed。"""
    directives = _directives(unit)
    if "always" not in directives.get("Restart", []):
        return
    interval = directives.get("StartLimitIntervalSec")
    burst = directives.get("StartLimitBurst")
    assert interval and burst, (
        f"{unit.name} 是 Restart=always，必须显式给 StartLimitIntervalSec 与 "
        "StartLimitBurst；用默认值(10s/5 次)配 RestartSec=3 时窗口内最多落 4 次，"
        "永远进不了 failed，OnFailure 形同虚设")
    seconds = int(re.sub(r"\D", "", interval[-1]) or 0)
    restart_sec = int(re.sub(r"\D", "", (directives.get("RestartSec") or ["0"])[-1]) or 0)
    attempts_in_window = seconds // max(restart_sec, 1) + 1
    assert attempts_in_window > int(burst[-1]), (
        f"{unit.name}: 窗口 {seconds}s / 每 {restart_sec}s 一次 ≈ 最多 "
        f"{attempts_in_window} 次尝试，够不到 burst={burst[-1]}，还是进不了 failed")


@pytest.mark.parametrize("unit", _units(), ids=lambda p: p.name)
def test_no_unit_swallows_its_own_exit_code(unit):
    """`|| true` 让退出码恒 0，等于把自己的 OnFailure 关掉。"""
    text = unit.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("#"))
    assert "|| true" not in body, (
        f"{unit.name} 里有 `|| true`：退出码被吞掉，OnFailure 永不触发。"
        "要保留清理动作就写成 `rc=$?; <清理>; exit $rc`")


def test_a_timer_probes_the_public_endpoint():
    """没有这条，「进程活着但站点不服务」这一整档不会触发任何告警。"""
    service = UNITS / "nmu-health.service"
    timer = UNITS / "nmu-health.timer"
    assert service.exists() and timer.exists(), (
        "缺 nmu-health.service/.timer：备份直接读磁盘上的 db，服务死了它照样写 ok；"
        "红线与 /health 只有从外面探才知道")
    text = service.read_text(encoding="utf-8")
    assert "curl" in text and "/health" in text
    assert "nmu-alert@%N.service" in _directives(service).get("OnFailure", [])


def test_web_workers_are_never_multiplied():
    """SQLite 上 `with_for_update()` 编译成裸 SELECT，行级锁一行都不产生；

    真正在护着自动带练写路径的是进程内的 `_LIVE_WRITE_LOCK`。多开一个 worker
    会把这个前提整个掀掉。Dockerfile 里写过这句话，而运维要编辑的是 systemd 单元。
    """
    offenders = []
    for path in list(UNITS.glob("*.service")) + [UNITS.parents[1] / "Dockerfile"]:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("#"):
                continue
            if re.search(r"(?:^|\s)(--workers|-w)(?:[=\s]\d)", line):
                offenders.append(f"{path.name}: {line}")
    assert not offenders, "uvicorn 必须单 worker：\n" + "\n".join(offenders)
