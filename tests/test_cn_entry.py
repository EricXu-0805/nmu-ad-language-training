"""NMU 国内入口:droplet 上的 haproxy 带 PROXY v2 头转给 box A,box A 的 Caddy 只认它发的头。

11-03 手装的 haproxy 把国内用户原样转给 box A,box A 看到的源地址全是 147.182.176.237:
按 IP 的限速、PIN 锁定把所有国内用户和扫 droplet 的扫描器算成一个人(扫描器能把医生锁在外面),
异地探针的 /health 计数也能被国内用户喂绿。deploy/offbox/cn_entry/ 补上这一跳:
haproxy.cfg 只多 send-proxy-v2,boxa-caddy-global.snippet 是 box A 要先加的全局选项,
install_cn_entry.sh 是 Eric 在 Mac 上跑的安装器。这里钉住三份文件的契约,以及安装器的远端
脚本在假 droplet(假 sudo/haproxy/systemctl/ufw/curl,真 TLS 的假 box A)上的行为。
snippet 在真网关上的行为由 scripts/smoke_caddy_release.py 在 CI 里跑。
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "deploy" / "offbox" / "cn_entry"
INSTALLER = ENTRY / "install_cn_entry.sh"
HAPROXY_CFG = ENTRY / "haproxy.cfg"
SNIPPET = ENTRY / "boxa-caddy-global.snippet"

# 11-03 由 Claude 手装、跑到现在的那份(当时记下的指令,注释行当时没记;安装器 PRE_PROXY 那段就是它)。
PRE_PROXY = """\
global
    log /dev/log local0
    chroot /var/lib/haproxy
    stats socket /run/haproxy/admin.sock mode 660 level admin
    user haproxy
    group haproxy
    daemon
    maxconn 512
defaults
    log global
    mode tcp
    option tcplog
    timeout connect 10s
    timeout client 10m
    timeout server 10m
frontend nmu_cn_entry
    bind 147.182.176.237:443
    default_backend nmu_box_a
backend nmu_box_a
    server box_a 89.208.253.119:443
"""
PROXY_V2_SIGNATURE = b"\r\n\r\n\x00\r\nQUIT\n"


def _without_comments(text: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True) if not line.lstrip().startswith("#"))


def _directives(text: str) -> list[list[str]]:
    return [line.split() for line in _without_comments(text).splitlines() if line.strip()]


def _installer_text() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def _heredoc(tag: str) -> str:
    text = _installer_text()
    start = text.index(f"<<'{tag}'")
    body = text[text.index("\n", start) + 1:]
    return body[:body.index(f"\n{tag}\n") + 1]


def _constant(name: str) -> str:
    match = re.search(rf'^{name}="([^"]*)"$', _installer_text(), re.M)
    assert match, name
    return match.group(1)


def _code_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


# ---------------------------------------------------------------------------
# haproxy.cfg:与 11-03 那份只差 send-proxy-v2
# ---------------------------------------------------------------------------

def test_the_repo_haproxy_cfg_is_the_installed_one_plus_send_proxy_v2_only():
    repo = _without_comments(HAPROXY_CFG.read_text(encoding="utf-8"))

    assert repo == PRE_PROXY.replace("    server box_a 89.208.253.119:443\n",
                                     "    server box_a 89.208.253.119:443 send-proxy-v2\n")


def test_haproxy_binds_only_the_droplet_public_443_in_tcp_mode():
    rows = _directives(HAPROXY_CFG.read_text(encoding="utf-8"))

    assert [row for row in rows if row[0] == "bind"] == [["bind", "147.182.176.237:443"]]
    assert [row for row in rows if row[0] == "mode"] == [["mode", "tcp"]]
    assert not any(row[0] == "listen" for row in rows)        # 没有别的 listen/stats 页
    for name in ("connect", "client", "server"):
        assert [row for row in rows if row[:2] == ["timeout", name]], name


def test_haproxy_has_one_server_box_a_with_proxy_v2_and_nothing_else():
    rows = _directives(HAPROXY_CFG.read_text(encoding="utf-8"))

    assert [row for row in rows if row[0] == "server"] == [
        ["server", "box_a", "89.208.253.119:443", "send-proxy-v2"]]
    assert [row[0] for row in rows if row[0] in {"frontend", "backend"}] == ["frontend", "backend"]
    assert "send-proxy " not in HAPROXY_CFG.read_text(encoding="utf-8")   # 不是 v1


def test_the_stats_socket_is_a_unix_path_not_a_network_address():
    [stats] = [row for row in _directives(HAPROXY_CFG.read_text(encoding="utf-8"))
               if row[:2] == ["stats", "socket"]]

    assert stats[2].startswith("/") and "@" not in stats[2] and ":" not in stats[2]
    assert stats[3:5] == ["mode", "660"]


# ---------------------------------------------------------------------------
# boxa-caddy-global.snippet:只认 droplet 的 /32,proxy_protocol 在 tls 前面
# ---------------------------------------------------------------------------

def _snippet_rows() -> list[list[str]]:
    return _directives(SNIPPET.read_text(encoding="utf-8"))


def test_the_snippet_allows_exactly_the_droplet_slash_32():
    rows = _snippet_rows()

    assert [row for row in rows if row[0] in {"allow", "deny"}] == [["allow", "147.182.176.237/32"]]
    assert ["fallback_policy", "reject"] in rows       # 别处带头直接断,不是默认的 ignore


def test_the_snippet_puts_proxy_protocol_before_tls_on_the_443_server():
    rows = _snippet_rows()
    heads = [row[0] for row in rows if row != ["}"]]

    assert rows[0] == ["servers", ":443", "{"]
    assert heads.index("proxy_protocol") < heads.index("tls")
    assert heads == ["servers", "listener_wrappers", "proxy_protocol", "allow", "fallback_policy",
                     "timeout", "tls"]


def test_the_snippet_is_global_option_lines_indented_one_tab():
    text = SNIPPET.read_text(encoding="utf-8")

    assert text.count("{") == text.count("}")
    assert all(line.startswith("\t") for line in text.splitlines())
    assert text.endswith("}\n")


# ---------------------------------------------------------------------------
# 安装器:不连机器就能测的部分
# ---------------------------------------------------------------------------

def test_the_installer_parses():
    result = subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr


def _fake_ssh(tmp_path: Path, rc: str = "99") -> tuple[Path, Path]:
    fake = tmp_path / "fake-bin"
    fake.mkdir(exist_ok=True)
    log = tmp_path / "ssh-calls"
    # 每次调用记一行参数;FAKE_SSH_RC_<mode> 决定这一次退几(默认 rc)。
    (fake / "ssh").write_text("\n".join([
        "#!/bin/sh",
        f'echo "$*" >> "{log}"',
        "cat > /dev/null",
        'mode="$(printf "%s" "$*" | sed -n "s/.*bash -s -- .\\([a-z]*\\). .*/\\1/p")"',
        f'eval "exit \\${{FAKE_SSH_RC_$mode:-{rc}}}"',
    ]) + "\n", encoding="utf-8")
    (fake / "ssh").chmod(0o755)
    return fake, log


def _plan(*flags: str, tmp_path: Path) -> subprocess.CompletedProcess:
    fake, log = _fake_ssh(tmp_path)
    result = subprocess.run(["bash", str(INSTALLER), *flags],
                            env={"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path)},
                            capture_output=True, text=True, timeout=60)
    assert not log.exists(), "--plan / 参数错误时不许连任何机器"
    return result


@pytest.mark.parametrize(("flags", "stages"), [
    ([], "0 1"),
    (["--preflight"], "0"),
    (["--verify"], "2"),
    (["--rollback"], "3"),
])
def test_plan_lists_the_stages_and_the_ordering_rule_offline(tmp_path, flags, stages):
    result = _plan("--plan", *flags, tmp_path=tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[0] == f"stages: {stages}"
    assert "先在 box A 的 Caddy 加 boxa-caddy-global.snippet 并核过,再跑这里开 send-proxy-v2" in result.stdout
    assert "先跑这里的 --rollback,再去 box A 删 snippet" in result.stdout


@pytest.mark.parametrize("flags", [["--preflight", "--rollback"], ["--verify", "--preflight"],
                                   ["--force"], ["--restart"]])
def test_unknown_flags_and_combinations_are_refused_offline(tmp_path, flags):
    result = _plan("--plan", *flags, tmp_path=tmp_path)

    assert result.returncode == 64


def test_every_ssh_goes_through_batchmode_with_a_connect_timeout():
    code = _code_lines(_installer_text())

    assert 'RSH() { ssh -o BatchMode=yes -o ConnectTimeout=20 "$HOST" "$@"; }' in code
    # ssh 当命令用的只有 RSH 里那一处(scp/rsync 也没有)。
    assert [line for line in code if re.search(r"(^|[;&|({]\s*)(ssh|scp|rsync)\s", line.strip())
            and "RSH()" not in line] == []
    assert _constant("HOST") == "eric@147.182.176.237"


def test_the_installer_reads_no_secrets_and_touches_only_haproxy():
    code = "\n".join(_code_lines(_installer_text()))

    for needle in ("secrets.env", "keychain", ".env", "OPENCLAW_", "webhook", "id_ed25519",
                   "/etc/shadow", "sing-box", "crontab"):
        assert needle not in code, needle
    assert re.search(r"\bopenclaw (cron|gateway|channels|secrets)\b", code) is None
    assert _constant("CFG") == "/etc/haproxy/haproxy.cfg"
    assert set(re.findall(r"systemctl ([a-z][a-z-]*)", code)) == {"is-active", "reload", "start"}   # 从不 restart
    # start 只出现在给人看的「手工处理」提示里,脚本自己不跑。
    assert [line for line in code.splitlines()
            if "systemctl start" in line and not line.lstrip().startswith("echo ")] == []
    assert set(re.findall(r"sudo -n ([a-z][a-z-]*)", code)) == {"true", "haproxy", "cat", "ufw", "cp",
                                                                "tee", "mv", "systemctl"}


def test_the_installer_constants_match_the_config_it_installs():
    assert f"bind {_constant('DROPLET_IP')}:443" in HAPROXY_CFG.read_text(encoding="utf-8")
    assert f"server box_a {_constant('BOXA_IP')}:443 " in HAPROXY_CFG.read_text(encoding="utf-8")
    assert _constant("SNI") == _constant("DROPLET_IP").replace(".", "-") + ".sslip.io"
    assert f"allow {_constant('DROPLET_IP')}/32" in SNIPPET.read_text(encoding="utf-8")


def test_the_rollback_config_is_the_installed_one_byte_for_byte_modulo_comments():
    assert _without_comments(_heredoc("PRE_PROXY")) == PRE_PROXY


# ---------------------------------------------------------------------------
# 安装器整体:预检没过,一个写操作都不发
# ---------------------------------------------------------------------------

def _git_copy(tmp_path: Path) -> Path:
    """安装器要求在干净的 git 仓库里跑:拷一份进临时仓库并提交。"""
    repo = tmp_path / "repo"
    target = repo / "deploy" / "offbox" / "cn_entry"
    target.mkdir(parents=True)
    for path in ENTRY.iterdir():
        shutil.copy2(path, target / path.name)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CEILING_DIRECTORIES": str(tmp_path)}
    for argv in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                  "commit", "-q", "-m", "copy"]):
        subprocess.run(["git", "-C", str(repo), *argv], env=env, check=True, capture_output=True, timeout=60)
    return target / "install_cn_entry.sh"


def _run_installer(tmp_path: Path, *flags: str, **rc: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    script = _git_copy(tmp_path)
    fake, log = _fake_ssh(tmp_path, rc="0")
    env = {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path),
           "GIT_CEILING_DIRECTORIES": str(tmp_path),
           **{f"FAKE_SSH_RC_{mode}": code for mode, code in rc.items()}}
    result = subprocess.run(["bash", str(script), *flags], env=env, capture_output=True, text=True, timeout=60)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def test_a_failed_preflight_stops_before_any_write(tmp_path):
    result, calls = _run_installer(tmp_path, preflight="1")

    assert result.returncode == 1 and "停" in result.stdout
    assert len(calls) == 1 and "bash -s -- 'preflight'" in calls[0]
    assert "-o BatchMode=yes -o ConnectTimeout=20 eric@147.182.176.237" in calls[0]


def test_an_already_installed_droplet_is_not_installed_again(tmp_path):
    result, calls = _run_installer(tmp_path, preflight="3")

    assert result.returncode == 0 and "不重复装" in result.stdout
    assert [call.split("bash -s -- ")[1].split()[0] for call in calls] == ["'preflight'"]


def test_install_runs_preflight_then_install_and_nothing_else(tmp_path):
    result, calls = _run_installer(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert [call.split("bash -s -- ")[1].split()[0] for call in calls] == ["'preflight'", "'install'"]


def test_the_installer_refuses_uncommitted_changes(tmp_path):
    script = _git_copy(tmp_path)
    script.with_name("haproxy.cfg").write_text("global\n", encoding="utf-8")
    fake, log = _fake_ssh(tmp_path)

    result = subprocess.run(["bash", str(script), "--preflight"],
                            env={"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path),
                                 "GIT_CEILING_DIRECTORIES": str(tmp_path)},
                            capture_output=True, text=True, timeout=60)

    assert result.returncode == 1 and "没提交的改动" in result.stdout
    assert not log.exists()


# ---------------------------------------------------------------------------
# 远端脚本:在本机用假 droplet 跑。box A 是一个真 TLS 的本地服务器,
# 证书只靠这一个子进程的 SSL_CERT_FILE 信任。
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def certificate(tmp_path_factory) -> Path:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("没有 openssl,做不出假 box A 的证书")
    work = tmp_path_factory.mktemp("cn-entry-cert")
    # stranger:同样是 localhost 的自签证书,只是不在 SSL_CERT_FILE 里。
    for cert, key in (("cert", "key"), ("stranger", "stranger-key")):
        subprocess.run([
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "1", "-nodes",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,digitalSignature,keyEncipherment",
            "-addext", "extendedKeyUsage=serverAuth",
            "-keyout", str(work / f"{key}.pem"), "-out", str(work / f"{cert}.pem"),
        ], check=True, capture_output=True, timeout=30, env={"PATH": os.defpath, "OPENSSL_CONF": os.devnull})
    return work


class FakeBoxA:
    """accept_proxy=True:先读一个 PROXY v2 头再走 TLS(装了 snippet 的 box A);False:直接走 TLS。
    trusted=False 时出示不受信的证书;status 是回给 /build-id.txt 的状态行。"""

    def __init__(self, certificate: Path, accept_proxy: bool, trusted: bool = True,
                 status: bytes = b"200 OK") -> None:
        self.accept_proxy = accept_proxy
        self.status = status
        self.headers: list[bytes] = []
        self.requests: list[bytes] = []
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        if trusted:
            self.context.load_cert_chain(certificate / "cert.pem", certificate / "key.pem")
        else:
            self.context.load_cert_chain(certificate / "stranger.pem", certificate / "stranger-key.pem")
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @staticmethod
    def _exact(conn: socket.socket, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                raise OSError("short read")
            data += chunk
        return data

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                try:
                    if self.accept_proxy:
                        head = self._exact(conn, 16)
                        head += self._exact(conn, struct.unpack("!H", head[14:16])[0])
                        self.headers.append(head)
                    with self.context.wrap_socket(conn, server_side=True) as tls:
                        self.requests.append(tls.recv(4096))
                        tls.sendall(b"HTTP/1.1 " + self.status
                                    + b"\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                except OSError:
                    pass

    def close(self) -> None:
        self.listener.close()
        self.thread.join(timeout=5)


UFW_ACTIVE = """\
Status: active

To                         Action      From
--                         ------      ----
OpenSSH                    ALLOW       Anywhere
443/tcp                    ALLOW       Anywhere                   # NMU 国内入口 -> box A
OpenSSH (v6)               ALLOW       Anywhere (v6)
443/tcp (v6)               ALLOW       Anywhere (v6)              # NMU 国内入口 -> box A
"""


class Droplet:
    """假 openclaw-prod:sudo 原样执行,haproxy/systemctl/ufw/curl/sleep 是记账的假货。"""

    def __init__(self, tmp_path: Path, certificate: Path) -> None:
        self.tmp = tmp_path
        self.certificate = certificate
        self.etc = tmp_path / "etc-haproxy"
        self.etc.mkdir()
        self.cfg = self.etc / "haproxy.cfg"
        self.cfg.write_text("# 11-03 Claude 手装\n" + PRE_PROXY, encoding="utf-8")
        self.log = tmp_path / "droplet.log"
        self.log.write_text("", encoding="utf-8")          # 假货里的 grep -c 要它在
        self.bin = tmp_path / "droplet-bin"
        self.bin.mkdir()
        (tmp_path / "ufw.txt").write_text(UFW_ACTIVE, encoding="utf-8")
        log = self.log
        self._tool("hostname", 'echo "${FAKE_HOSTNAME:-openclaw-prod}"')
        self._tool("sudo", '[ "$1" = -n ] && shift\nexec "$@"')
        self._tool("haproxy", "\n".join([
            'case "$1" in',
            '  -v) echo "HAProxy version 2.8.5-1ubuntu3 2024/04/01 - https://haproxy.org/" ;;',
            f'  -c) echo "check $3" >> "{log}"; if grep -q BROKEN "$3"; then echo "[ALERT] parsing" >&2; exit 1; fi',
            '      echo "Configuration file is valid" ;;',
            '  *) exit 2 ;;',
            "esac",
        ]))
        # FAKE_STATE:每次 is-active 依次取一个,取完之后一直用最后一个。上一次 is-active
        # 是 failed/inactive 时 reload 被拒,跟 systemd 对停掉的 unit 一样。
        self._tool("systemctl", "\n".join([
            'case "$1" in',
            f'  is-active) n="$(($(grep -c "^is-active " "{log}") + 1))"',
            "      state=\"$(printf '%s\\n' ${FAKE_STATE:-active} | sed -n \"${n}p\")\"",
            "      [ -n \"$state\" ] || state=\"$(printf '%s\\n' ${FAKE_STATE:-active} | tail -n 1)\"",
            f'      echo "is-active $state" >> "{log}"; echo "$state"; [ "$state" = active ] ;;',
            f'  reload) case "$(grep "^is-active " "{log}" | tail -n 1)" in',
            f'        *" failed"|*" inactive") echo "refused reload $2" >> "{log}"; exit 1 ;;',
            "      esac",
            f'      echo "reload $2" >> "{log}"; exit "${{FAKE_RELOAD_RC:-0}}" ;;',
            f'  *) echo "systemctl $*" >> "{log}"; exit 1 ;;',
            "esac",
        ]))
        # FAKE_TEE_SHORT:写进去的只有前三行(都是注释),换上去的就不是仓库那份。
        self._tool("tee", '[ -z "${FAKE_TEE_SHORT:-}" ] || { head -n 3 > "$1"; exit 0; }\nexec /usr/bin/tee "$@"')
        self._tool("ufw", f'cat "{tmp_path}/ufw.txt"')
        # FAKE_CURL_CODES:每次调用依次取一个码,取完之后一直用最后一个。
        self._tool("curl", "\n".join([
            f'echo "curl $*" >> "{log}"',
            f'n="$(grep -c "^curl " "{log}")"',
            'codes="${FAKE_CURL_CODES:-200}"',
            "code=\"$(printf '%s\\n' $codes | sed -n \"${n}p\")\"",
            "[ -n \"$code\" ] || code=\"$(printf '%s\\n' $codes | tail -n 1)\"",
            'printf "%s" "$code"',
        ]))
        self._tool("sleep", "exit 0")
        if not any((Path(d) / "sha256sum").exists() for d in os.environ.get("PATH", "").split(os.pathsep)):
            self._tool("sha256sum", 'exec shasum -a 256 "$@"')
        self.box_a: FakeBoxA | None = None

    def _tool(self, name: str, body: str) -> None:
        tool = self.bin / name
        tool.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        tool.chmod(0o755)

    def serve_box_a(self, accept_proxy: bool, **options) -> FakeBoxA:
        self.box_a = FakeBoxA(self.certificate, accept_proxy, **options)
        return self.box_a

    def entries(self, kind: str) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line.startswith(kind)]

    def others(self) -> list[str]:
        return sorted(path.name for path in self.etc.iterdir() if path.name != "haproxy.cfg")

    def run(self, mode: str, repo: bytes | None = None, **env: str) -> subprocess.CompletedProcess:
        repo_b64 = base64.b64encode(HAPROXY_CFG.read_bytes() if repo is None else repo).decode()
        pre_b64 = base64.b64encode(_heredoc("PRE_PROXY").encode()).decode()
        # 没起假 box A 时指向一个刚关掉的端口:连接被拒,等于 box A 没准备好。
        if self.box_a is not None:
            port = self.box_a.port
        else:
            with socket.create_server(("127.0.0.1", 0)) as closed:
                port = closed.getsockname()[1]
        # 远端是 /usr/bin/python3;本机/CI 上换成跑测试的这个解释器。
        script = _heredoc("REMOTE").replace("/usr/bin/python3", sys.executable)
        return subprocess.run(
            ["bash", "-s", "--", mode, str(self.cfg), "147.182.176.237", "127.0.0.1", str(port),
             "localhost", repo_b64, pre_b64],
            input=script, capture_output=True, text=True, timeout=60,
            env={"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.tmp),
                 "SSL_CERT_FILE": str(self.certificate / "cert.pem"), "LANG": "C.UTF-8",
                 "LC_ALL": "C.UTF-8", **env})


@pytest.fixture
def droplet(tmp_path, certificate):
    world = Droplet(tmp_path, certificate)
    yield world
    if world.box_a is not None:
        world.box_a.close()


ORIGINAL = "# 11-03 Claude 手装\n" + PRE_PROXY


def test_preflight_passes_when_box_a_already_takes_the_header(droplet):
    box_a = droplet.serve_box_a(accept_proxy=True)

    result = droplet.run("preflight")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "去掉注释后 = 11-03 那份" in result.stdout
    assert "ready=yes status=HTTP/1.1 200 OK" in result.stdout
    assert "ufw active,放行 443" in result.stdout
    assert droplet.cfg.read_text(encoding="utf-8") == ORIGINAL
    assert droplet.others() == [] and droplet.entries("reload") == []
    [request] = box_a.requests
    assert request.startswith(b"GET /build-id.txt HTTP/1.1\r\nHost: localhost\r\n")


def test_the_readiness_header_is_proxy_v2_from_test_net_1_to_the_droplet_443(droplet):
    box_a = droplet.serve_box_a(accept_proxy=True)

    droplet.run("preflight")

    [header] = box_a.headers
    assert header[:12] == PROXY_V2_SIGNATURE
    assert header[12:14] == bytes((0x21, 0x11))          # v2 PROXY,TCP over IPv4
    assert struct.unpack("!H", header[14:16]) == (12,)
    assert socket.inet_ntoa(header[16:20]) == "192.0.2.1"
    assert socket.inet_ntoa(header[20:24]) == "147.182.176.237"
    assert struct.unpack("!HH", header[24:28])[1] == 443


@pytest.mark.parametrize("accept_proxy", [False, None], ids=["plain-tls", "refused"])
def test_preflight_stops_before_any_write_when_box_a_is_not_ready(droplet, accept_proxy):
    if accept_proxy is not None:
        droplet.serve_box_a(accept_proxy=accept_proxy)

    result = droplet.run("preflight")

    assert result.returncode == 1
    assert "box A 还不认 droplet 发的 PROXY v2 头" in result.stdout and "ready=no" in result.stdout
    assert droplet.cfg.read_text(encoding="utf-8") == ORIGINAL
    assert droplet.others() == [] and droplet.entries("reload") == []


@pytest.mark.parametrize(("options", "seen"), [
    ({"trusted": False}, "ready=no SSLCertVerificationError"),
    ({"status": b"502 Bad Gateway"}, "ready=no status=HTTP/1.1 502 Bad Gateway"),
], ids=["untrusted-cert", "status-502"])
def test_preflight_wants_a_verified_certificate_and_a_200(droplet, options, seen):
    droplet.serve_box_a(accept_proxy=True, **options)

    result = droplet.run("preflight")

    assert result.returncode == 1 and seen in result.stdout
    assert droplet.cfg.read_text(encoding="utf-8") == ORIGINAL
    assert droplet.others() == [] and droplet.entries("reload") == []


def test_preflight_recognises_the_installed_cfg_whatever_its_comments_and_spacing(droplet):
    droplet.serve_box_a(accept_proxy=True)
    droplet.cfg.write_text("# 手写的注释\n\n" + PRE_PROXY.replace("    ", "\t")
                           .replace("maxconn 512", "maxconn  512   # 小机器"), encoding="utf-8")

    result = droplet.run("preflight")

    assert result.returncode == 0, result.stdout


def test_preflight_says_already_installed_on_the_repo_cfg(droplet):
    droplet.serve_box_a(accept_proxy=True)
    droplet.cfg.write_bytes(HAPROXY_CFG.read_bytes())

    result = droplet.run("preflight")

    assert result.returncode == 3 and "send-proxy-v2 已经开着" in result.stdout


def test_preflight_stops_with_a_masked_diff_on_any_other_cfg(droplet):
    droplet.serve_box_a(accept_proxy=True)
    droplet.cfg.write_text(PRE_PROXY + "listen stats\n    stats auth admin:sentinel-secret\n", encoding="utf-8")

    result = droplet.run("preflight")

    assert result.returncode == 1 and "既不是 11-03 那份也不是仓库那份" in result.stdout
    assert "+listen stats" in result.stdout and "stats auth <遮住>" in result.stdout
    assert "sentinel" not in result.stdout + result.stderr
    assert droplet.others() == []


def test_preflight_wants_ufw_active_with_443_open(droplet):
    droplet.serve_box_a(accept_proxy=True)
    (droplet.tmp / "ufw.txt").write_text("Status: inactive\n", encoding="utf-8")

    result = droplet.run("preflight")

    assert result.returncode == 1 and "ufw 不是 active" in result.stdout


def test_preflight_wants_haproxy_active(droplet):
    droplet.serve_box_a(accept_proxy=True)

    result = droplet.run("preflight", FAKE_STATE="failed")

    assert result.returncode == 1 and "haproxy 是 failed" in result.stdout


def test_preflight_wants_the_entry_answering_through_the_droplet_before_the_swap(droplet):
    droplet.serve_box_a(accept_proxy=True)

    result = droplet.run("preflight", FAKE_CURL_CODES="502")

    assert result.returncode == 1 and "换配置之前经 droplet 打 /build-id.txt 就是 502" in result.stdout
    assert droplet.cfg.read_text(encoding="utf-8") == ORIGINAL and droplet.entries("reload") == []


def test_preflight_wants_the_current_cfg_to_pass_haproxy_check(droplet):
    droplet.serve_box_a(accept_proxy=True)
    # BROKEN 只在注释里:去掉注释仍是 11-03 那份,只有假 haproxy -c 不过。
    droplet.cfg.write_text("# BROKEN\n" + PRE_PROXY, encoding="utf-8")

    result = droplet.run("preflight")

    assert result.returncode == 1 and "现在这份 haproxy -c 都不过" in result.stdout
    assert "去掉注释后 = 11-03 那份" in result.stdout


def test_the_remote_script_refuses_any_other_host(droplet):
    result = droplet.run("install", FAKE_HOSTNAME="nmu-prod")

    assert result.returncode == 1 and "nmu-prod" in result.stdout
    assert droplet.cfg.read_text(encoding="utf-8") == ORIGINAL and droplet.others() == []


def test_install_backs_up_swaps_in_the_repo_cfg_reloads_once_and_checks_via_the_droplet(droplet):
    result = droplet.run("install")

    assert result.returncode == 0, result.stdout + result.stderr
    assert droplet.cfg.read_bytes() == HAPROXY_CFG.read_bytes()
    [backup] = droplet.etc.glob("haproxy.cfg.before-cn-proxy-*")
    assert backup.read_text(encoding="utf-8") == ORIGINAL
    assert droplet.others() == [backup.name]                      # 没留 .new
    assert droplet.entries("reload") == ["reload haproxy"]
    [check] = droplet.entries("check")
    assert not check.endswith(str(droplet.cfg))                   # 换上之前先 -c 的是那份新的
    [curl] = droplet.entries("curl")
    assert "--resolve localhost:443:147.182.176.237 https://localhost/build-id.txt" in curl


@pytest.mark.parametrize(("env", "reason", "outcome"), [
    ({"FAKE_CURL_CODES": "502 502 502 200"}, "经 droplet 打 /build-id.txt = 502",   # 还原之后又通了
     "已还原:"),
    ({"FAKE_RELOAD_RC": "1"}, "systemctl reload haproxy 失败",                      # 还原那次也 reload 不了
     "但 reload 失败,跑着的可能还是换上去的那份;手工处理:sudo systemctl reload haproxy"),
])
def test_install_restores_the_backup_when_anything_after_the_swap_fails(droplet, env, reason, outcome):
    result = droplet.run("install", **env)

    assert result.returncode == 1
    assert reason in result.stdout and outcome in result.stdout
    assert ("已还原:" in result.stdout) == (outcome == "已还原:")
    assert droplet.cfg.read_text(encoding="utf-8") == ORIGINAL
    assert droplet.entries("reload") == ["reload haproxy", "reload haproxy"]   # 换上一次、还原一次


def test_a_dead_haproxy_after_the_reload_gets_a_start_hint_not_restored(droplet):
    result = droplet.run("install", FAKE_STATE="failed")

    assert result.returncode == 1 and "reload 之后 haproxy 不是 active(是 failed)" in result.stdout
    assert "手工处理:sudo systemctl start haproxy" in result.stdout and "已还原:" not in result.stdout
    assert droplet.cfg.read_text(encoding="utf-8") == ORIGINAL
    assert droplet.entries("reload") == ["reload haproxy"]
    assert droplet.entries("refused") == ["refused reload haproxy"]          # 停掉的 unit 不让 reload


def test_install_waits_out_a_reloading_state_instead_of_restoring(droplet):
    result = droplet.run("install", FAKE_STATE="reloading reloading active")

    assert result.returncode == 0, result.stdout + result.stderr
    assert droplet.cfg.read_bytes() == HAPROXY_CFG.read_bytes()
    assert droplet.entries("reload") == ["reload haproxy"]


def test_install_restores_when_what_landed_is_not_the_repo_cfg(droplet):
    result = droplet.run("install", FAKE_TEE_SHORT="1")

    assert result.returncode == 1 and "换上去的不是仓库那份" in result.stdout and "已还原:" in result.stdout
    assert droplet.cfg.read_text(encoding="utf-8") == ORIGINAL
    assert droplet.entries("reload") == ["reload haproxy"]                   # 只有还原那一次


def test_install_does_not_send_anyone_to_redo_a_restore_that_already_landed(droplet):
    result = droplet.run("install", FAKE_CURL_CODES="000")

    assert result.returncode == 1 and "仍是 000(预检时是 200)" in result.stdout
    assert "sudo cp" not in result.stdout and "已还原:" not in result.stdout
    assert droplet.cfg.read_text(encoding="utf-8") == ORIGINAL


def test_install_writes_nothing_when_the_cfg_moved_after_preflight(droplet):
    droplet.cfg.write_text(PRE_PROXY + "    # nothing\nlisten x\n", encoding="utf-8")
    before = droplet.cfg.read_bytes()

    result = droplet.run("install")

    assert result.returncode == 1 and "没动" in result.stdout
    assert droplet.cfg.read_bytes() == before and droplet.others() == []
    assert droplet.entries("reload") == []


def test_install_writes_nothing_when_the_new_cfg_fails_haproxy_check(droplet):
    result = droplet.run("install", repo=PRE_PROXY.encode() + b"    BROKEN directive\n")

    assert result.returncode == 1 and "haproxy -c 不过" in result.stdout
    assert droplet.cfg.read_text(encoding="utf-8") == ORIGINAL and droplet.others() == []
    assert droplet.entries("reload") == []


def test_rollback_writes_back_the_pre_proxy_cfg_reloads_and_checks(droplet):
    droplet.cfg.write_bytes(HAPROXY_CFG.read_bytes())

    result = droplet.run("rollback")

    assert result.returncode == 0, result.stdout + result.stderr
    assert droplet.cfg.read_text(encoding="utf-8") == _heredoc("PRE_PROXY")
    [backup] = droplet.etc.glob("haproxy.cfg.before-cn-rollback-*")
    assert backup.read_bytes() == HAPROXY_CFG.read_bytes()
    assert droplet.entries("reload") == ["reload haproxy"]
    assert "现在可以去 box A 删 snippet" in result.stdout


def test_rollback_keeps_the_pre_proxy_cfg_even_if_the_check_fails(droplet):
    droplet.cfg.write_bytes(HAPROXY_CFG.read_bytes())

    result = droplet.run("rollback", FAKE_CURL_CODES="000")

    assert result.returncode == 1 and "问题不在这一跳" in result.stdout
    assert droplet.cfg.read_text(encoding="utf-8") == _heredoc("PRE_PROXY")
    assert droplet.entries("reload") == ["reload haproxy"]


def test_rollback_on_the_pre_proxy_cfg_rewrites_nothing_but_still_reloads_and_checks(droplet):
    result = droplet.run("rollback")

    assert result.returncode == 0, result.stdout + result.stderr
    assert droplet.cfg.read_text(encoding="utf-8") == ORIGINAL and droplet.others() == []
    assert droplet.entries("reload") == ["reload haproxy"]
    assert "现在可以去 box A 删 snippet" in result.stdout


def test_a_rollback_whose_reload_fails_leaves_the_file_matching_what_runs(droplet):
    droplet.cfg.write_bytes(HAPROXY_CFG.read_bytes())

    result = droplet.run("rollback", FAKE_RELOAD_RC="1")

    assert result.returncode == 1 and "box A 的 snippet 先别删" in result.stdout
    assert "现在可以去 box A 删 snippet" not in result.stdout
    assert droplet.cfg.read_bytes() == HAPROXY_CFG.read_bytes()             # 跑着的还是它
    assert "send-proxy-v2 开着" in droplet.run("verify").stdout


def test_rollback_checks_haproxy_is_active_before_blaming_another_hop(droplet):
    droplet.cfg.write_bytes(HAPROXY_CFG.read_bytes())

    result = droplet.run("rollback", FAKE_STATE="failed")

    assert result.returncode == 1 and "reload 之后 haproxy 是 failed,不是 active" in result.stdout
    assert "现在可以去 box A 删 snippet" not in result.stdout and "问题不在这一跳" not in result.stdout


def test_rollback_writes_nothing_on_an_unknown_cfg(droplet):
    droplet.cfg.write_text(PRE_PROXY + "listen x\n", encoding="utf-8")

    result = droplet.run("rollback")

    assert result.returncode == 1 and "没动" in result.stdout
    assert droplet.others() == [] and droplet.entries("reload") == []


def test_verify_only_reads(droplet):
    droplet.serve_box_a(accept_proxy=True)
    droplet.cfg.write_bytes(HAPROXY_CFG.read_bytes())

    result = droplet.run("verify")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "send-proxy-v2 开着" in result.stdout and "经 droplet 打 /build-id.txt = 200" in result.stdout
    assert droplet.cfg.read_bytes() == HAPROXY_CFG.read_bytes() and droplet.others() == []
    assert droplet.entries("reload") == [] and droplet.entries("check") == []


def test_verify_fails_when_the_entry_does_not_answer_through_the_droplet(droplet):
    droplet.cfg.write_bytes(HAPROXY_CFG.read_bytes())

    result = droplet.run("verify", FAKE_CURL_CODES="502")

    assert result.returncode == 1 and "✗ 经 droplet 打 /build-id.txt = 502" in result.stdout


def test_verify_fails_with_a_masked_diff_on_an_unknown_cfg(droplet):
    droplet.cfg.write_text(HAPROXY_CFG.read_text(encoding="utf-8")
                           + "listen stats\n    stats auth admin:sentinel-secret\n", encoding="utf-8")

    result = droplet.run("verify")

    assert result.returncode == 1 and "既不是 11-03 那份也不是仓库那份" in result.stdout
    assert "+listen stats" in result.stdout and "stats auth <遮住>" in result.stdout
    assert "sentinel" not in result.stdout + result.stderr
    assert droplet.others() == []
