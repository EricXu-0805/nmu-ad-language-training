"""生产空闲门 scripts/prod_idle_gate.py。

11-03 国内入口开通后,扫描器的 GET /.env、/.git/config 之类全被 SPA 兜底回 200,旧的 grep
过滤把它们都当在用,11-05 晚上线脚本 277 停在空闲门。新门只认应用的真路由:这里钉住
路由表与 app.routes 一条不差、扫描噪声不算、真写请求一定算、读不了就按不空闲。
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from fastapi.routing import APIRoute
import pytest

from app.main import app

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prod_idle_gate.py"
_SPEC = importlib.util.spec_from_file_location("prod_idle_gate", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)

# 换掉之前 kernel_netplan_window.sh idle_ok 里那串过滤的原样,只用来对照,别再拿去用。
OLD_FILTER = (
    'grep -E "\\"(POST|GET|PATCH|PUT|DELETE) " | grep -vcE "/health|/device/attach|'
    '/device/heartbeat|/autopilot/next|/autopilot/voice-settings|/sessions/[^/]+/autopilot/status|'
    '/live/state|/live/patient-heartbeat|/build-id.txt|/status HTTP|/runtime HTTP|'
    '/ai/provider-readiness|401 Unauthorized|'
    '\\"GET /(patient|console|acceptance|index\\.html|assets/|favicon|robots\\.txt)"')

# 文档保留地址,不用生产机与 droplet 的真地址。
_PHRASES = {200: "OK", 204: "No Content", 401: "Unauthorized", 403: "Forbidden",
            404: "Not Found", 405: "Method Not Allowed", 422: "Unprocessable Content"}


def _line(method: str, target: str, status: int = 200, client: str = "203.0.113.9:0") -> str:
    return f'INFO:     {client} - "{method} {target} HTTP/1.1" {status} {_PHRASES[status]}'


# 11-05 晚 box A 上真实出现过的那一类成串探测(路径同类,来源换成文档地址)。
SCANNER_BURST = [
    _line("GET", "/.env", client="198.51.100.23:0"),
    _line("GET", "/.git/config", client="198.51.100.23:0"),
    _line("GET", "/actuator/env", client="198.51.100.23:0"),
    _line("GET", "/server-status", client="198.51.100.23:0"),
    _line("GET", "/v2/_catalog", client="198.51.100.23:0"),
    _line("GET", "/.DS_Store", client="198.51.100.23:0"),
    _line("GET", "/info.php", client="198.51.100.23:0"),
    _line("GET", "/%40vite/env", client="198.51.100.23:0"),
    _line("GET", "/.vscode/sftp.json", client="198.51.100.23:0"),
    _line("GET", "/debug/default/view?panel=config", client="198.51.100.23:0"),
    _line("POST", "/", 405, client="198.51.100.40:0"),
    _line("GET", "/wp-login.php?redirect_to=%2Fwp-admin", client="198.51.100.40:0"),
    _line("PROPFIND", "/", 405, client="198.51.100.40:0"),
    _line("OPTIONS", "/sessions", 405, client="198.51.100.40:0"),
    _line("HEAD", "/health", client="198.51.100.40:0"),
    _line("GET", "/sessions/.env/../../etc/passwd", 404, client="198.51.100.40:0"),
    'INFO:     198.51.100.40:0 - "WebSocket /ws" 403',
    "WARNING:  Invalid HTTP request received.",
]

POLLING_LINES = [
    _line("GET", "/health", client="127.0.0.1:0"),
    _line("GET", "/health/ready", client="127.0.0.1:0"),
    _line("HEAD", "/health/ready"),
    _line("POST", "/device/attach"),
    _line("GET", "/live/state"),
    _line("POST", "/live/patient-heartbeat", 204),
    _line("GET", "/sessions/S-1/autopilot/next?after=7"),
    _line("GET", "/sessions/S-1/autopilot/voice-settings"),
    _line("GET", "/sessions/S-1/autopilot/status"),
    _line("GET", "/caregiver/sessions/S-1/status"),
    _line("GET", "/sessions/S-1/runtime"),
    _line("GET", "/ai/provider-readiness"),
]

PAGE_LINES = [
    _line("GET", "/"),
    _line("GET", "/patient"),
    _line("GET", "/patient?pair=1"),
    _line("GET", "/console"),
    _line("GET", "/acceptance"),
    _line("GET", "/device-check"),
    _line("GET", "/assets/index-3f9a1c.js"),
    _line("GET", "/assets/index-77be02.css"),
    _line("GET", "/favicon.ico"),
    _line("GET", "/robots.txt"),
    _line("GET", "/build-id.txt"),
    _line("HEAD", "/index.html"),
]

UNAUTHENTICATED_LINES = [
    _line("GET", "/patients", 401),
    _line("GET", "/sessions/.env", 401),
    _line("GET", "/exports/x/.git/config", 401),
    _line("POST", "/sessions/S-1/attempts/process", 401),
    _line("GET", "/auth/me", 401),
]

REAL_LINES = [
    _line("POST", "/sessions/S-1/items"),
    _line("POST", "/items/IE-1/turns"),
    _line("POST", "/sessions/S-1/attempts/process"),
    _line("POST", "/sessions/S-1/autopilot/commands/C-1/acks"),
    _line("PUT", "/audio/RA-1/blob"),
    _line("PATCH", "/turns/T-1/confirm"),
    _line("GET", "/live/console-state"),
    _line("GET", "/patients"),
    _line("GET", "/sessions/S-1/journal?limit=50"),
    _line("POST", "/auth/login", 200),
    # 公开接口,控制台登录页一打开就请求它:没登录也算在用(与旧过滤相同),登录页开着多半有人要登录。
    _line("GET", "/auth/config"),
    _line("GET", "/sessions/S-1", 403),
    _line("POST", "/sessions/S-1/items", 422),
]


def _run(text: str | bytes) -> subprocess.CompletedProcess[str]:
    data = text.encode("utf-8") if isinstance(text, str) else text
    done = subprocess.run([sys.executable, "-I", str(SCRIPT), "--minutes", "10", "--stdin"],
                          input=data, capture_output=True, timeout=60, check=False)
    return subprocess.CompletedProcess(done.args, done.returncode,
                                       done.stdout.decode("utf-8"), done.stderr.decode("utf-8"))


def _journal(*lines: str) -> str:
    return "".join(line + "\n" for line in lines)


def _old_count(text: str) -> int:
    done = subprocess.run(["bash", "-c", OLD_FILTER], input=text, capture_output=True,
                          text=True, timeout=30, check=False)
    return int(done.stdout.strip())


# ---------------- 路由表与应用一条不差 ----------------

def _app_routes() -> set[tuple[str, str]]:
    found = set()
    for route in app.routes:
        assert "WebSocket" not in type(route).__name__, (
            "应用加了 WebSocket 路由:空闲门把 WebSocket 那种日志行一律当噪声,先改门再加路由")
        # 页面外壳兜底只在 web/dist 存在时注册,本来就不算在用。
        if not isinstance(route, APIRoute) or route.path == "/{full_path:path}":
            continue
        found.update((method, route.path) for method in route.methods or ())
    return found


def test_every_app_route_is_in_the_table_and_every_table_entry_is_an_app_route():
    polling = {(m, t) for m, t, _ in gate.POLLING_ROUTES}
    real = {(m, t) for m, t, _ in gate.REAL_ROUTES}
    app_routes = _app_routes()

    assert len(app_routes) > 100, "一条路由都没枚举出来,下面的断言在空转"
    assert not polling & real
    assert len(polling) == len(gate.POLLING_ROUTES) and len(real) == len(gate.REAL_ROUTES)
    missing = sorted(app_routes - polling - real)
    assert not missing, (
        "这些接口不在 scripts/prod_idle_gate.py 的表里,请求它们会被当成扫描噪声、"
        "生产照样「空闲」:加进 REAL(轮询才进 POLLING)" + str(missing))
    stale = sorted((polling | real) - app_routes)
    assert not stale, "表里这些已经不是应用路由了,删掉:" + str(stale)


def test_each_template_matches_its_own_route_and_nothing_across_a_segment():
    for method, template, pattern in gate.POLLING_ROUTES + gate.REAL_ROUTES:
        sample = template.replace("{unknown_path:path}", "a/b").replace("{", "x").replace("}", "")
        assert pattern.fullmatch(sample), (method, template)
    session_runtime = next(p for m, t, p in gate.POLLING_ROUTES
                           if t == "/sessions/{session_id}/runtime")
    assert not session_runtime.fullmatch("/sessions/a/b/runtime")


# ---------------- 各类日志 ----------------

def test_the_scanner_burst_is_idle_although_the_old_filter_counted_every_probe():
    result = _run(_journal(*SCANNER_BURST, *POLLING_LINES))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == (
        f"近 10 分钟真实使用 0 条(不算:轮询 {len(POLLING_LINES)}、页面与静态 0、"
        f"未登录被拒 0、对不上真路由的噪声 {len(SCANNER_BURST) - 1})\n")
    assert _old_count(_journal(*SCANNER_BURST)) >= 10


@pytest.mark.parametrize("line", REAL_LINES, ids=lambda line: line.split('"')[1])
def test_one_real_request_means_in_use(line):
    result = _run(_journal(*SCANNER_BURST, *POLLING_LINES, *PAGE_LINES, line))

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.startswith("近 10 分钟真实使用 1 条(")
    assert result.stdout.endswith(",有人在用\n")


@pytest.mark.parametrize("line", UNAUTHENTICATED_LINES, ids=lambda line: line.split('"')[1])
def test_a_401_on_a_real_route_is_idle(line):
    result = _run(_journal(line))

    assert result.returncode == 0, result.stdout
    assert "未登录被拒 1" in result.stdout


def test_page_loads_and_static_files_are_idle():
    result = _run(_journal(*PAGE_LINES))

    assert result.returncode == 0, result.stdout
    assert f"页面与静态 {len(PAGE_LINES)}" in result.stdout
    # 页面按整段认:扫描器的 /console.php、/patient_list 是噪声,计数才对得上。
    assert gate.classify("GET", "/console.php", 200) == "noise"
    assert gate.classify("GET", "/patient_list", 200) == "noise"


def test_an_empty_journal_is_idle():
    for text in ("", "-- No entries --\n"):
        result = _run(text)
        assert result.returncode == 0, result.stdout
        assert result.stdout == ("近 10 分钟真实使用 0 条(不算:轮询 0、页面与静态 0、"
                                 "未登录被拒 0、对不上真路由的噪声 0)\n")


def test_other_service_output_is_neither_counted_nor_an_anomaly():
    result = _run(_journal(
        "Started nmu.service - NMU language training.",
        "INFO:     Started server process [4242]",
        "INFO:     Waiting for application startup.",
        "INFO:     Application startup complete.",
        "Traceback (most recent call last):",
        '  File "/opt/nmu/app/app/main.py", line 1, in <module>',
        "  场次运行状态写入失败,稍后重试"))

    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("text", [
    'INFO:     203.0.113.9:0 - "POST /sessions/S-1/items HTTP/1.1',
    'INFO:     203.0.113.9:0 - "GET /patients HTTP/1.1" OK',
    'INFO:     203.0.113.9:0 - "\x1b[1mPOST /items/IE-1/turns HTTP/1.1\x1b[0m" \x1b[32m200 OK\x1b[0m',
    'INFO:     203.0.113.9:0 - "GET /a b HTTP/1.1" 200 OK',
    '"PUT /audio/RA-1/blob HTTP/1.1" 200 OK',
    'INFO:     203.0.113.9:0 - "GET /patients',
], ids=["truncated", "no-status", "ansi-colour", "space-in-target", "no-client",
        "cut-short-of-line-max"])
def test_a_line_that_looks_like_access_but_does_not_parse_is_not_idle(text):
    result = _run(_journal(*POLLING_LINES, text))

    assert result.returncode == 2, result.stdout
    assert "另有 1 行像访问日志却解析不了,按不空闲算" in result.stdout


# journald.conf(5):LineMax 默认 48K,stdout 上更长的一行被硬拆成几条记录,不在边界上找换行。
_LINE_MAX = 48 * 1024


def _split_by_journald(line: str) -> list[str]:
    data = line.encode("ascii")
    return [data[at:at + _LINE_MAX].decode("ascii") for at in range(0, len(data), _LINE_MAX)]


@pytest.mark.parametrize(("line", "code", "counted"), [
    (_line("GET", "/index.php?s=" + "a" * 60000, client="198.51.100.23:0"), 0, "对不上真路由的噪声 1)"),
    (_line("GET", "/console?" + "a" * 120000), 0, "页面与静态 1、"),
    # 状态码在后半截:就算其实是 401,也认不出来,按在用算。
    (_line("GET", "/sessions/S-1/journal?cursor=" + "a" * 60000, 401), 1, "真实使用 1 条"),
], ids=["scanner", "page-three-records", "real-route"])
def test_a_request_line_longer_than_line_max_is_classified_by_its_head(line, code, counted):
    records = _split_by_journald(line)
    assert len(records) >= 2 and len(records[0].encode("ascii")) == _LINE_MAX

    result = _run(_journal(*POLLING_LINES, *records))

    assert result.returncode == code, result.stdout
    assert counted in result.stdout


def test_bytes_that_are_not_utf8_are_not_idle():
    result = _run(b'INFO:     203.0.113.9:0 - "GET /health HTTP/1.1" 200 OK\n\xff\xfe\x00garbage\n')

    assert result.returncode == 2
    assert result.stdout == "近 10 分钟的日志里有不是 UTF-8 的字节,按不空闲算\n"


def test_the_line_never_carries_paths_addresses_ids_or_queries():
    markers = ["S-SECRET-1", "RA-SECRET-2", "203.0.113.77", "token=SECRET3", "/.env", "patients"]
    result = _run(_journal(
        _line("PUT", "/audio/RA-SECRET-2/blob", client="203.0.113.77:0"),
        _line("GET", "/sessions/S-SECRET-1/runtime?token=SECRET3"),
        _line("GET", "/.env?token=SECRET3", client="203.0.113.77:0"),
        _line("GET", "/patients", 401),
        'INFO:     203.0.113.77:0 - "POST /sessions/S-SECRET-1/items HTTP/1.1'))

    assert result.returncode == 2
    assert result.stdout.count("\n") == 1 and not result.stderr
    assert not [marker for marker in markers if marker in result.stdout]


# ---------------- 与旧过滤的对照 ----------------

# 旧过滤与新门有意不同的几处(扫描噪声之外)。旧 grep 只认子串:PUT /live/state(写游标、报录音)
# 与管理员点的实测被当成轮询放过;页面名单的 `"GET /patient` 没收尾,研究者读受试者名单与档案
# 的 GET /patients… 也被当成页面放过。末两条是页面外壳,旧名单漏了它们,算成了在用。
DELIBERATE_DIFFERENCES = [
    (_line("PUT", "/live/state"), 0, 1),
    (_line("POST", "/ai/provider-readiness/probe"), 0, 1),
    (_line("GET", "/patients"), 0, 1),
    (_line("GET", "/patients/P-1/sessions"), 0, 1),
    (_line("GET", "/"), 1, 0),
    (_line("GET", "/device-check"), 1, 0),
]


def test_on_ordinary_traffic_the_new_count_equals_the_old_filter_line_by_line():
    different = {line for line, _, _ in DELIBERATE_DIFFERENCES}
    lines = [line for line in POLLING_LINES + PAGE_LINES + UNAUTHENTICATED_LINES + REAL_LINES
             if line not in different]
    assert len(lines) > 30
    for line in lines:
        result = _run(_journal(line))
        assert result.returncode in (0, 1), result.stdout
        assert _old_count(_journal(line)) == result.returncode, line


@pytest.mark.parametrize(("line", "old", "new"), DELIBERATE_DIFFERENCES,
                         ids=lambda value: value.split('"')[1] if isinstance(value, str) else None)
def test_where_the_old_substring_filter_was_wrong(line, old, new):
    assert _old_count(_journal(line)) == old
    assert _run(_journal(line)).returncode == new


# ---------------- 读 journal ----------------

def _fake_journalctl(stdout: bytes = b"", *, returncode: int = 0):
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=b"")

    run.calls = calls
    return run


def test_journal_mode_reads_the_nmu_unit_for_the_given_window(capsys):
    run = _fake_journalctl(_journal(*REAL_LINES[:1]).encode("utf-8"))

    assert gate.main(["--minutes", "7"], run=run, geteuid=lambda: 0) == 1
    assert run.calls == [["journalctl", "-u", "nmu", "--since", "-7 min", "--no-pager", "-o", "cat"]]
    assert capsys.readouterr().out.startswith("近 7 分钟真实使用 1 条(")


def test_an_unreadable_journal_is_not_idle(capsys):
    def missing(argv, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "journalctl")

    def hung(argv, **_kwargs):
        raise subprocess.TimeoutExpired(argv, 60)

    assert gate.main([], run=_fake_journalctl(returncode=1), geteuid=lambda: 0) == 2
    assert gate.main([], run=missing, geteuid=lambda: 0) == 2
    assert gate.main([], run=hung, geteuid=lambda: 0) == 2
    assert capsys.readouterr().out.splitlines() == [
        "近 10 分钟的 journal 读不了(journalctl 退出码 1),按不空闲算",
        "近 10 分钟的 journal 读不了(FileNotFoundError),按不空闲算",
        "近 10 分钟的 journal 读不了(TimeoutExpired),按不空闲算",
    ]


def test_a_non_root_reader_is_not_idle_even_when_the_journal_looks_empty(capsys):
    run = _fake_journalctl(b"-- No entries --\n")

    assert gate.main([], run=run, geteuid=lambda: 1000) == 2
    assert run.calls == []
    assert "按不空闲算" in capsys.readouterr().out


def test_a_bad_window_is_a_usage_error_not_idle():
    with pytest.raises(SystemExit) as raised:
        gate.main(["--minutes", "0"], run=_fake_journalctl(), geteuid=lambda: 0)
    assert raised.value.code == 2


# ---------------- 在生产机的系统 python3 上能跑 ----------------

def test_the_script_is_stdlib_only_and_parses_as_python_3_10():
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 10))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert imported and imported <= set(sys.stdlib_module_names) | {"__future__"}, imported
