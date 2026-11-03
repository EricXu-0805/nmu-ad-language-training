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


# 下面三条都按 `_units()` 参数化：这个清单空掉时它们不会红，而是**一条不剩地
# 消失**，pytest 照样全绿。所以先钉住清单本身。
_EXPECTED_UNITS = frozenset({
    "nmu.service", "nmu-caddy.service", "nmu-backup.service",
    "nmu-backup-health.service", "nmu-capacity.service",
    "nmu-restore-drill.service", "nmu-os-security.service",
    "nmu-health.service",
})


def test_the_parametrized_unit_list_is_not_empty():
    names = {path.name for path in _units()}
    assert names, f"{UNITS} 下一个 .service 都没扫到——下面三条此刻全在空转"
    assert _EXPECTED_UNITS <= names, (
        "这些单元从 deploy/systemd 里消失了：{}".format(
            ", ".join(sorted(_EXPECTED_UNITS - names))))


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


def test_the_public_probe_asks_the_readiness_route_not_the_constant_health():
    """/health 返回常量,库打不开、盘满、盘转只读它照样 200;/health/ready 才查库和盘。"""
    [command] = _directives(UNITS / "nmu-health.service")["ExecStart"]
    url = command.split()[-1]

    assert url.startswith("https://") and url.endswith("/health/ready")
    assert " -f" in command  # 503 才会变成非零退出、触发 OnFailure


def test_the_boot_catch_up_probe_waits_for_the_site_instead_of_alerting():
    """Persistent=true 让开机时补跑错过的那次探针,定时器起来得比 uvicorn/Caddy 早;
    探针一次失败就告警等于每次重启都发一条「站点挂了」。要先排在站点之后,再给它几次重试。"""
    service = UNITS / "nmu-health.service"
    directives = _directives(service)
    after = " ".join(directives.get("After", []))
    assert "nmu.service" in after and "nmu-caddy.service" in after
    command = " ".join(directives.get("ExecStart", []))
    assert "--retry-connrefused" in command
    assert "--retry " in command and "--retry-max-time " in command


def _timers() -> list[Path]:
    return sorted(UNITS.glob("*.timer"))


_EXPECTED_TIMERS = frozenset({
    "nmu-backup.timer", "nmu-backup-health.timer", "nmu-capacity.timer",
    "nmu-restore-drill.timer", "nmu-os-security.timer", "nmu-health.timer",
})


def test_the_parametrized_timer_list_is_not_empty():
    names = {path.name for path in _timers()}
    assert _EXPECTED_TIMERS <= names, (
        "这些定时器从 deploy/systemd 里消失了：{}".format(
            ", ".join(sorted(_EXPECTED_TIMERS - names))))


@pytest.mark.parametrize("timer", _timers(), ids=lambda p: p.name)
def test_every_timer_fires_on_the_calendar(timer):
    """2026-09-30 重启后 nmu-health.timer 晚于开机 5 分钟才被启动：systemd 249 把已过期的
    OnBootSec 截止点直接丢掉，OnUnitActiveSec 在新一次开机里又没有基准（服务还没跑过），
    定时器停在 SubState=elapsed、NEXT 为空，ActiveState 却一直是 active——公网探针就此
    无声停摆。日历触发不依赖「这次开机里启动得够早」，任何时刻启动都会排上下一次。"""
    assert _directives(timer).get("OnCalendar"), (
        f"{timer.name} 没有 OnCalendar：只靠 OnBootSec/OnUnitActiveSec 的定时器晚启动一次"
        "就永远不再触发，而 systemctl status 仍显示 active")


@pytest.mark.parametrize("timer", _timers(), ids=lambda p: p.name)
def test_persistent_is_only_used_with_a_calendar_trigger(timer):
    """Persistent= 只作用于 OnCalendar；配单调触发时它什么也不做，只会让人以为错过的会补跑。"""
    directives = _directives(timer)
    persistent = [value.lower() for value in directives.get("Persistent", [])]
    if persistent and persistent[-1] in {"1", "yes", "true", "on"}:
        assert directives.get("OnCalendar"), (
            f"{timer.name} 写了 Persistent=true 却没有 OnCalendar：这一行是摆设，"
            "错过的触发不会补跑")


def test_web_workers_are_never_multiplied():
    """SQLite 上 `with_for_update()` 编译成裸 SELECT，行级锁一行都不产生；

    真正在护着自动带练写路径的是进程内的 `_LIVE_WRITE_LOCK`。多开一个 worker
    会把这个前提整个掀掉。Dockerfile 里写过这句话，而运维要编辑的是 systemd 单元。
    """
    dockerfile = UNITS.parents[1] / "Dockerfile"
    assert dockerfile.exists(), (
        f"{dockerfile} 不在——这条断言原本要扫它，路径写错时会静默跳过")
    offenders = []
    for path in list(UNITS.glob("*.service")) + [dockerfile]:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("#"):
                continue
            if re.search(r"(?:^|\s)(--workers|-w)(?:[=\s]\d)", line):
                offenders.append(f"{path.name}: {line}")
    assert not offenders, "uvicorn 必须单 worker：\n" + "\n".join(offenders)


def test_the_daily_health_verdict_asks_when_the_public_probe_last_succeeded():
    """探针定时器自己死掉时什么都不会失败（9/30 重启后它停在 elapsed，一次 OnFailure 都没有），
    只能由每天的健康判定去问它最近一次成功是什么时候。"""
    service = (UNITS / "nmu-backup-health.service").read_text(encoding="utf-8")

    assert "--probe-unit nmu-health.service" in service


def test_caddy_reloads_the_same_config_it_runs_without_a_restart():
    """2026-10-08 窗口里 `systemctl reload nmu-caddy` 因为单元没有 ExecReload 失败,
    只好绕去 admin 接口。reload 读的文件必须就是 run 的那份,否则载入的是另一份配置。"""
    directives = _directives(UNITS / "nmu-caddy.service")
    [start] = directives["ExecStart"]
    [reload] = directives.get("ExecReload", [""])

    config = start.split("--config ", 1)[1].split()[0]
    assert start.startswith("/usr/local/bin/caddy run ")
    assert reload == f"/usr/local/bin/caddy reload --config {config} --force"


def test_the_offbox_probe_dead_man_rides_the_daily_health_verdict_not_a_seventh_timer():
    """OpenClaw 那台死了,机外探针和它的 Heartbeat 一起没了;由每天的健康判定数它来没来过。
    维护窗口脚本与上线脚本都钉死六个 nmu 定时器,所以不另开定时器。"""
    service = (UNITS / "nmu-backup-health.service").read_text(encoding="utf-8")

    assert "--offbox-source 147.182.176.237" in service
    assert {path.name for path in UNITS.glob("nmu-*.timer")} == _EXPECTED_TIMERS
