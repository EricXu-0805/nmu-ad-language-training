#!/usr/bin/env python3
"""Exercise the actual release Caddy with the repository's proxy routes over TLS.

Uses only temporary files and loopback listeners. An explicit temporary cert is
trusted by this client alone; ACME, OCSP, redirects, admin and trust installation
are disabled. Run on Linux in CI against the exact scanned release binary.

The listener also carries box A's PROXY protocol snippet, with 127.0.0.1 standing
in for the China entry droplet; a second loopback source plays everyone else.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
from http import client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

PROXY_SNIPPET = Path("deploy/offbox/cn_entry/boxa-caddy-global.snippet")
DROPLET_ALLOW = "allow 147.182.176.237/32"
# TEST-NET-1: an address no real client or upstream log can ever contain.
HEADER_SOURCE = "192.0.2.10"
ENTRY_HOSTS = ("89-208-253-119.sslip.io", "147-182-176-237.sslip.io")
STATIC_JS = b"const releaseFixture = 'public application code';\n" * 100
STATIC_CSS = b".release-fixture { color: #123456; background: white; }\n" * 80
HTML_FIXTURE = b"<!doctype html><title>Private page fixture</title>" * 30
STATIC_RESPONSES = {
    "/assets/index-AbCd1234.js": (200, "text/javascript; charset=utf-8", STATIC_JS),
    "/assets/theme-ZyXw9876.css": (200, "text/css; charset=utf-8", STATIC_CSS),
    "/assets/missing-AbCd1234.js": (404, "text/javascript", STATIC_JS),
    "/assets/denied-AbCd1234.js": (401, "text/javascript", STATIC_JS),
    "/assets/fallback-AbCd1234.js": (200, "text/html; charset=utf-8", HTML_FIXTURE),
    "/assets/partial-AbCd1234.js": (206, "text/javascript", STATIC_JS),
    "/assets/unchanged-AbCd1234.js": (304, None, b""),
    "/assets/plain.js": (200, "text/javascript", STATIC_JS),
    "/assets/nested/index-AbCd1234.js": (200, "text/javascript", STATIC_JS),
    "/assets/source-AbCd1234.js.map": (200, "application/json", b"{}"),
    "/patient": (200, "text/html", HTML_FIXTURE),
    "/console": (200, "text/html", HTML_FIXTURE),
    "/index.html": (200, "text/html", HTML_FIXTURE),
    "/build-id.txt": (200, "text/plain", b"fixture-build-id"),
    "/patients": (200, "application/json", b'{"fixture":true}'),
    "/live/state": (401, "application/json", b'{"code":"fixture_unauthorized"}'),
    "/audio/fixture/blob": (200, "audio/webm", b"\x1a\x45\xdf\xa3" + b"fixture" * 100),
}


class SmokeError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise SmokeError(code)


class EchoServer(ThreadingHTTPServer):
    # server_close joins handlers; their reads also have a short timeout.
    daemon_threads = False

    def __init__(self):
        super().__init__(("127.0.0.1", 0), EchoHandler)
        self.completed: list[dict[str, object]] = []
        self.records_lock = threading.Lock()


class EchoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def do_GET(self):
        self.echo()

    def do_HEAD(self):
        self.echo()

    def do_POST(self):
        self.echo()

    def do_PUT(self):
        self.echo()

    def echo(self):
        self.connection.settimeout(3)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 1024 * 1024:
                self.close_connection = True
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self.close_connection = True
                return
            record = {"method": self.command, "path": self.path, "body_bytes": len(body),
                      "body_sha256": hashlib.sha256(body).hexdigest(),
                      "forwarded_for": self.headers.get("X-Forwarded-For")}
            with self.server.records_lock:
                self.server.completed.append(record)
            fixture = STATIC_RESPONSES.get(urlsplit(self.path).path)
            status, media_type, encoded = fixture or (200, "application/json", json.dumps(record).encode())
            etag = '"' + hashlib.sha256(encoded).hexdigest() + '"'
            if fixture and status == 200 and self.headers.get("If-None-Match") == etag:
                # Starlette StaticFiles' 304 omits Content-Type.
                status, media_type, encoded = 304, None, b""
            self.send_response(status)
            if media_type:
                self.send_header("Content-Type", media_type)
            if fixture:
                self.send_header("ETag", etag)
                # The gateway must enforce its policy even if upstream would
                # otherwise let browsers cache an API/error/HTML response.
                self.send_header("Cache-Control", "public, max-age=3600")
                self.send_header("Pragma", "no-cache")
                self.send_header("Vary", "X-Fixture-Variant")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(encoded)
        except (OSError, ValueError):
            # A rejected oversized upload may close a partially streamed request.
            self.close_connection = True


class LoopbackHTTPS(client.HTTPSConnection):
    """Fixed loopback transport with Host/SNI/cert verification, without DNS."""

    def __init__(self, *args, source: str | None = None, proxy_header: bytes = b"", **kwargs):
        super().__init__(*args, **kwargs)
        self.source, self.proxy_header = source, proxy_header

    def connect(self):
        target = "::1" if self.source and ":" in self.source else "127.0.0.1"
        transport = socket.create_connection((target, self.port), self.timeout,
                                             source_address=(self.source, 0) if self.source else None)
        try:
            transport.sendall(self.proxy_header)
            self.sock = self._context.wrap_socket(transport, server_hostname=self.host)
        except BaseException:
            transport.close()
            raise


def proxy_v2_header(port: int) -> bytes:
    """PROXY protocol v2, PROXY command, TCP over IPv4, no TLVs."""
    return (b"\r\n\r\n\x00\r\nQUIT\n" + bytes((0x21, 0x11)) + struct.pack("!H", 12)
            + socket.inet_aton(HEADER_SOURCE) + socket.inet_aton("127.0.0.1")
            + struct.pack("!HH", 40000, port))


def second_loopback() -> str | None:
    """A loopback source outside the fixture's PROXY allow list (127.0.0.1/32)."""
    # Linux puts all of 127/8 on lo; CI must never fall through to a skip.
    if sys.platform.startswith("linux"):
        return "127.0.0.2"
    for address, family in (("127.0.0.2", socket.AF_INET), ("::1", socket.AF_INET6)):
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((address, 0))
            except OSError:
                continue
        return address
    return None


def request(port: int, context: ssl.SSLContext, method: str, path: str,
            body: bytes = b"", *, forged_forwarded_for: bool = False,
            source: str | None = None, proxy_header: bytes = b"",
            host: str = ENTRY_HOSTS[0], extra_headers: dict[str, str] | None = None):
    connection = LoopbackHTTPS(host, port, timeout=5, context=context,
                               source=source, proxy_header=proxy_header)
    headers = {"Host": f"{host}:{port}", "Connection": "close",
               "Content-Type": "application/octet-stream"}
    headers.update(extra_headers or {})
    if forged_forwarded_for:
        headers["X-Forwarded-For"] = "198.51.100.77"
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read(8193)
        require(len(response_body) <= 8192, "response_unexpectedly_large")
        response_headers = {}
        for key, value in response.getheaders():
            name = key.lower()
            response_headers[name] = ", ".join(filter(None, [response_headers.get(name), value]))
        return response.status, response_headers, response_body
    finally:
        connection.close()


def check_static_delivery(port: int, context: ssl.SSLContext) -> list[str]:
    """Exercise response status/MIME gates and both Host policies over real TLS."""
    results = []
    assets = ("/assets/index-AbCd1234.js", "/assets/theme-ZyXw9876.css")
    immutable = "public, max-age=31536000, immutable"
    for host in ENTRY_HOSTS:
        for path in assets:
            identity = request(port, context, "GET", path, host=host,
                               extra_headers={"Accept-Encoding": "identity"})
            compressed = request(port, context, "GET", path, host=host,
                                 extra_headers={"Accept-Encoding": "gzip"})
            expected = STATIC_RESPONSES[path][2]
            require(identity[0] == compressed[0] == 200, "public_asset_status")
            require(identity[2] == expected and gzip.decompress(compressed[2]) == expected,
                    "gzip_changed_public_asset_bytes")
            require(compressed[1].get("content-encoding") == "gzip"
                    and "content-encoding" not in identity[1]
                    and len(compressed[2]) < len(identity[2]), "public_asset_not_compressed")
            for response in (identity, compressed):
                require(response[1].get("cache-control") == immutable
                        and "pragma" not in response[1], "public_asset_cache_policy")
                require("accept-encoding" in response[1].get("vary", "").lower(),
                        "public_asset_encoding_not_varied")
                require("x-fixture-variant" in response[1].get("vary", "").lower(),
                        "public_asset_upstream_vary_lost")
            head = request(port, context, "HEAD", path, host=host)
            require(head[0] == 200 and head[1].get("cache-control") == immutable
                    and head[2] == b"", "public_asset_head_policy")
            for encoding, response in (("identity", identity), ("gzip", compressed)):
                conditional = request(port, context, "GET", path, host=host, extra_headers={
                    "Accept-Encoding": encoding, "If-None-Match": response[1]["etag"]})
                # 304 has no MIME proof; deliberately retain no-store. A force
                # reload can drop a cached object; fresh immutable hits need no
                # revalidation. Never widen the gate to HTML or error responses.
                require(conditional[0] == 304 and conditional[2] == b""
                        and conditional[1].get("cache-control") == "private, no-store",
                        "conditional_asset_policy")
        results.extend(("gzip_byte_identity:" + host, "public_200_cache_and_vary:" + host,
                        "head_and_conditional_304_policy:" + host))
        for path, (status, _media_type, expected) in STATIC_RESPONSES.items():
            if path in assets:
                continue
            response = request(port, context, "GET", path, host=host,
                               extra_headers={"Accept-Encoding": "gzip"})
            require(response[0] == status and response[2] == expected, "private_fixture_response:" + path)
            require(response[1].get("cache-control") == "private, no-store"
                    and response[1].get("pragma") == "no-cache"
                    and "content-encoding" not in response[1], "private_fixture_cached_or_encoded:" + path)
        post = request(port, context, "POST", assets[0], host=host,
                       extra_headers={"Accept-Encoding": "gzip"})
        require(post[1].get("cache-control") == "private, no-store"
                and "content-encoding" not in post[1], "non_get_asset_cached")
        blocked = request(port, context, "GET", "/docs", host=host)
        require(blocked[0] == 404 and blocked[1].get("cache-control") == "private, no-store",
                "gateway_404_cached")
        results.append("html_api_audio_errors_and_unknown_assets_no_store:" + host)
        health = request(port, context, "GET", "/health", host=host)
        alt = health[1].get("alt-svc", "")
        require(alt == "clear" if host == ENTRY_HOSTS[1] else "h3=" in alt,
                "entry_alt_svc_policy:" + host)
    results.append("tcp_relay_clears_h3_direct_entry_retains_h3")
    return results


def make_config(source: str, upstream_port: int, certificate: Path, key: Path,
                snippet: str, https_port: int, binds: list[str]) -> str:
    # Keep every production route, matcher, body limit and header unchanged.
    # Only substitute the upstream and add transport-only fixture directives.
    require(source.count("reverse_proxy app:8000") == 1, "repository_upstream_shape_changed")
    require(source.count("{$SITE_ADDRESS} {") == 1, "repository_site_shape_changed")
    require(snippet.count("servers :443 {") == 1 and snippet.count(DROPLET_ALLOW) == 1,
            "proxy_snippet_shape_changed")
    snippet = snippet.replace("servers :443 {", f"servers 127.0.0.1:{https_port} {{")
    snippet = snippet.replace(DROPLET_ALLOW, "allow 127.0.0.1/32")
    source = source.replace("reverse_proxy app:8000", f"reverse_proxy 127.0.0.1:{upstream_port}")
    source = source.replace("{$SITE_ADDRESS} {", "{$SITE_ADDRESS} {\n\tbind " + " ".join(binds) + "\n"
                            f"\ttls {json.dumps(str(certificate))} {json.dumps(str(key))}")
    return ("{\n\tadmin off\n\tauto_https off\n\tpersist_config off\n"
            "\tskip_install_trust\n\tocsp_stapling off\n" + snippet + "}\n" + source)


# caddy adapt writes 256KB / 64MB as decimal bytes: ordinary requests vs canonical audio uploads.
REQUEST_BODY_LIMITS = [256000, 64000000]


def request_body_limits(document: dict) -> list[int]:
    found = []

    def walk(value):
        if isinstance(value, dict):
            if value.get("handler") == "request_body":
                found.append(value.get("max_size"))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document)
    return sorted(found, key=lambda size: (size is None, size))


def require_request_body_limits(document: dict) -> None:
    require(request_body_limits(document) == REQUEST_BODY_LIMITS, "fixture_request_body_limits_changed")


def oversized_post_outcome(send) -> str:
    """How the gateway refused an over-limit POST: an exact 413, or a reset mid-upload.

    Caddy answers 413 from the declared Content-Length without reading the body. With
    the client's Connection: close, Go closes at once instead of draining, and the
    unread body makes the kernel send RST, so a client still sending sees a reset.
    Either way the caller still requires that the upstream never completed the request.
    A clean disconnect with no response at all is not this race and stays an error.
    """
    try:
        response = send()
    except client.RemoteDisconnected:
        raise
    except (ConnectionResetError, BrokenPipeError, ssl.SSLEOFError):
        return "connection_reset"
    require(response[0] == 413, "ordinary_large_post_not_413")
    return "status_413"


def validate_local_config(document: dict, listen: list[str], upstream_port: int,
                          certificate: Path, key: Path) -> None:
    """Fail before launching if adaptation enables an extra listener or issuer."""
    require(document.get("admin", {}).get("disabled") is True, "fixture_admin_not_disabled")
    require(document.get("admin", {}).get("config", {}).get("persist") is False,
            "fixture_persistence_not_disabled")
    apps = document.get("apps", {})
    require(set(apps) <= {"http", "tls"}, "fixture_unexpected_app")
    servers = apps.get("http", {}).get("servers", {})
    require(len(servers) == 1, "fixture_listener_count_invalid")
    server = next(iter(servers.values()))
    require(server.get("listen") == listen, "fixture_listener_not_loopback")
    wrappers = server.get("listener_wrappers", [])
    require([wrapper.get("wrapper") for wrapper in wrappers] == ["proxy_protocol", "tls"]
            and wrappers[0].get("allow") == ["127.0.0.1/32"], "fixture_proxy_protocol_not_before_tls")
    require(server.get("automatic_https", {}).get("disable") is True, "fixture_automatic_https_enabled")
    tls = apps.get("tls", {})
    require(tls.get("disable_ocsp_stapling") is True, "fixture_ocsp_not_disabled")
    # ocsp_stapling off generates a policy containing only this local flag.
    policies = tls.get("automation", {}).get("policies", [])
    require(policies in ([], [{"disable_ocsp_stapling": True}]), "fixture_issuer_present")
    certificates = tls.get("certificates", {})
    require(set(certificates) == {"load_files"}, "fixture_certificate_source_invalid")
    pairs = certificates["load_files"]
    require(len(pairs) == 1 and pairs[0].get("certificate") == str(certificate)
            and pairs[0].get("key") == str(key), "fixture_certificate_pair_invalid")
    proxies = []

    def inspect(value):
        if isinstance(value, dict):
            if value.get("handler") == "reverse_proxy":
                proxies.append(value)
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(document)
    require(len(proxies) == 1 and proxies[0].get("upstreams") == [{"dial": f"127.0.0.1:{upstream_port}"}],
            "fixture_upstream_not_loopback")
    require_request_body_limits(document)


def check_box_a_listener(binary: Path, snippet: str, environment: dict[str, str], work: Path) -> None:
    """Box A's sites have no bind and http_port 8081: the snippet must hit their ':443'."""
    config = work / "box-a-shape.Caddyfile"
    config.write_text("{\n\thttp_port 8081\n\tauto_https disable_redirects\n" + snippet + "}\n"
                      "box-a.example, cn-entry.example {\n\treverse_proxy 127.0.0.1:8000\n}\n")
    adapted = subprocess.run([str(binary), "adapt", "--adapter", "caddyfile", "--config", str(config)],
                             env=environment, cwd=work, capture_output=True, text=True,
                             check=True, timeout=15)
    servers = json.loads(adapted.stdout).get("apps", {}).get("http", {}).get("servers", {})
    require([server.get("listen") for server in servers.values()] == [[":443"]],
            "box_a_shape_listener_changed")
    wrappers = next(iter(servers.values())).get("listener_wrappers", [])
    require([wrapper.get("wrapper") for wrapper in wrappers] == ["proxy_protocol", "tls"]
            and wrappers[0].get("allow") == ["147.182.176.237/32"], "proxy_snippet_misses_box_a_listener")


def stop_child(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def smoke(binary: Path, source_root: Path) -> dict[str, object]:
    require(binary.is_file() and not binary.is_symlink() and os.access(binary, os.X_OK),
            "binary_not_regular_executable")
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    openssl = shutil.which("openssl")
    require(openssl is not None, "openssl_missing")
    source_bytes = (source_root / "Caddyfile").read_bytes()
    snippet = (source_root / PROXY_SNIPPET).read_text()
    other = second_loopback()
    binds = ["127.0.0.1", "::1"] if other == "::1" else ["127.0.0.1"]
    results: list[str] = []
    skipped: list[str] = []
    with tempfile.TemporaryDirectory(prefix="nmu-caddy-https-smoke-") as temporary:
        work = Path(temporary)
        certificate, key = work / "localhost.crt", work / "localhost.key"
        # Certificate has no AIA/OCSP URLs. Its trust never leaves this process.
        subprocess.run([
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "1", "-nodes",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1,"
            + ",".join("DNS:" + host for host in ENTRY_HOSTS),
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,digitalSignature,keyEncipherment",
            "-addext", "extendedKeyUsage=serverAuth", "-keyout", str(key), "-out", str(certificate),
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
            env={"PATH": os.defpath, "OPENSSL_CONF": os.devnull})
        key.chmod(0o600)
        certificate.chmod(0o600)
        environment = {"PATH": os.defpath, "XDG_DATA_HOME": str(work / "data"),
                       "XDG_CONFIG_HOME": str(work / "config")}
        context = ssl.create_default_context(cafile=str(certificate))
        require(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED,
                "client_certificate_verification_disabled")
        upstream = EchoServer()
        worker = threading.Thread(target=upstream.serve_forever, kwargs={"poll_interval": 0.05})
        worker.start()
        process = None
        log_path = work / "caddy.log"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
                reservation.bind(("127.0.0.1", 0))
                https_port = reservation.getsockname()[1]
                environment["SITE_ADDRESS"] = ", ".join(
                    f"https://{host}:{https_port}" for host in ENTRY_HOSTS)
                config = work / "Caddyfile"
                config.write_text(make_config(source_bytes.decode(), upstream.server_port, certificate, key,
                                              snippet, https_port, binds))
                adapted = subprocess.run([str(binary), "adapt", "--adapter", "caddyfile", "--config", str(config)],
                                         env=environment, cwd=work, capture_output=True, text=True,
                                         check=True, timeout=15)
                document = json.loads(adapted.stdout)
                listen = [f"127.0.0.1:{https_port}"] + ([f"[::1]:{https_port}"] if other == "::1" else [])
                validate_local_config(document, listen, upstream.server_port, certificate, key)
                check_box_a_listener(binary, snippet, environment, work)
                results.append("proxy_snippet_lands_on_box_a_listener")
                runtime_config = work / "caddy.json"
                runtime_config.write_text(json.dumps(document))
            # The reserved loopback port is released immediately before startup.
            with log_path.open("wb") as log:
                process = subprocess.Popen([str(binary), "run", "--config", str(runtime_config)],
                                           env=environment, cwd=work, stdout=log, stderr=log,
                                           start_new_session=True)
                deadline = time.monotonic() + 20
                while True:
                    require(process.poll() is None, "caddy_exited_before_https_ready")
                    try:
                        health = request(https_port, context, "GET", "/health")
                        break
                    except (OSError, client.HTTPException) as error:
                        if time.monotonic() >= deadline:
                            raise SmokeError(f"https_startup_timeout:{error}") from error
                        time.sleep(0.05)
                require(health[0] == 200 and json.loads(health[2])["path"] == "/health", "https_health_failed")
                results.append("verified_certificate_https_health")
                try:
                    request(https_port, ssl.create_default_context(), "GET", "/health")
                except ssl.SSLCertVerificationError:
                    pass
                else:
                    raise SmokeError("temporary_certificate_unexpectedly_system_trusted")
                results.append("untrusted_certificate_rejected")
                expected_headers = {
                    "strict-transport-security": "max-age=31536000; includeSubDomains",
                    "x-content-type-options": "nosniff", "x-frame-options": "DENY",
                    "referrer-policy": "no-referrer",
                    "permissions-policy": "camera=(), geolocation=(), microphone=(self)",
                }
                for name, value in expected_headers.items():
                    require(health[1].get(name) == value, "security_header_mismatch:" + name)
                csp = health[1].get("content-security-policy", "")
                for directive in ("default-src 'self'", "object-src 'none'", "frame-ancestors 'none'",
                                  "script-src 'self'", "connect-src 'self'"):
                    require(directive in csp.split("; "), "content_security_policy_mismatch")
                require("server" not in health[1], "server_header_exposed")
                results.append("security_headers")
                results.extend(check_static_delivery(https_port, context))
                blocked = ("/docs", "/docs/index.html", "/%64ocs", "/ReDoC", "/openapi.json",
                           "/content/item_bank_v1.json", "/content/week1_script.json",
                           "/content/autopilot_protocol_v1.json")
                for path in blocked:
                    response = request(https_port, context, "GET", path)
                    require(response[0] == 404, "internal_route_not_404:" + path)
                with upstream.records_lock:
                    require(not any(row["path"] in blocked for row in upstream.completed), "blocked_route_reached_upstream")
                results.append("internal_paths_404")
                small = b'{"probe":"local-caddy-release"}'
                response = request(https_port, context, "POST", "/smoke-small", small, forged_forwarded_for=True)
                require(response[0] == 200, "small_post_status_invalid")
                echoed = json.loads(response[2])
                require(echoed["body_bytes"] == len(small) and echoed["body_sha256"] == hashlib.sha256(small).hexdigest(),
                        "small_post_body_not_preserved")
                require(echoed["forwarded_for"] == "127.0.0.1", "forged_forwarded_for_trusted")
                results.extend(("small_post_reaches_upstream", "untrusted_forwarded_for_replaced"))
                large = b"x" * (300 * 1024)
                refused = oversized_post_outcome(
                    lambda: request(https_port, context, "POST", "/smoke-oversized", large))
                with upstream.records_lock:
                    require(not any(row["path"] == "/smoke-oversized" for row in upstream.completed),
                            "ordinary_large_post_completed_upstream")
                results.append("ordinary_post_over_256kib_rejected:" + refused)
                response = request(https_port, context, "PUT", "/audio/smoke_A.B-123/blob", large)
                require(response[0] == 200, "canonical_audio_put_rejected")
                echoed = json.loads(response[2])
                require(echoed["body_bytes"] == len(large) and echoed["body_sha256"] == hashlib.sha256(large).hexdigest(),
                        "canonical_audio_body_not_preserved")
                results.append("canonical_audio_put_over_256kib_reaches_upstream")
                header = proxy_v2_header(https_port)
                response = request(https_port, context, "GET", "/smoke-proxy-header", proxy_header=header)
                require(response[0] == 200 and json.loads(response[2])["forwarded_for"] == HEADER_SOURCE,
                        "allowed_proxy_header_address_not_used")
                response = request(https_port, context, "GET", "/smoke-proxy-absent")
                require(response[0] == 200 and json.loads(response[2])["forwarded_for"] == "127.0.0.1",
                        "allowed_source_without_header_broken")
                results.extend(("allowed_proxy_header_address_reaches_upstream",
                                "allowed_source_without_header_uses_peer_address"))
                if other is None:
                    skipped.append("non_allowed_source_cases: no second loopback address (127.0.0.2 or ::1)")
                else:
                    try:
                        request(https_port, context, "GET", "/smoke-proxy-spoofed",
                                source=other, proxy_header=header)
                    except (OSError, client.HTTPException):
                        pass
                    else:
                        raise SmokeError("non_allowed_proxy_header_accepted")
                    response = request(https_port, context, "GET", "/smoke-proxy-direct", source=other)
                    require(response[0] == 200 and json.loads(response[2])["forwarded_for"] == other,
                            "non_allowed_source_without_header_broken")
                    results.extend(("non_allowed_proxy_header_connection_refused",
                                    "non_allowed_source_without_header_unchanged"))
                with upstream.records_lock:
                    spoofed = [row["path"] for row in upstream.completed if row["forwarded_for"] == HEADER_SOURCE]
                require(spoofed == ["/smoke-proxy-header"], "proxy_header_address_reached_upstream_unexpectedly")
                require(process.poll() is None, "caddy_exited_during_smoke")
        except Exception as error:
            detail = log_path.read_text(errors="replace")[-8000:] if log_path.exists() else ""
            if isinstance(error, subprocess.CalledProcessError):
                stderr = error.stderr or ""
                detail += stderr.decode(errors="replace")[-4000:] if isinstance(stderr, bytes) else stderr[-4000:]
            raise SmokeError(f"{error}\nchecks_passed={json.dumps(results)}\nlocal_caddy_diagnostics={detail}") from error
        finally:
            try:
                stop_child(process)
            finally:
                upstream.shutdown()
                upstream.server_close()
                worker.join(timeout=5)
    # server_close() joined every handler thread: a request the gateway let through just
    # before the client saw a reset would be recorded by now.
    with upstream.records_lock:
        require(not any(row["path"] == "/smoke-oversized" for row in upstream.completed),
                "ordinary_large_post_completed_upstream_late")
    require(hashlib.sha256(binary.read_bytes()).hexdigest() == binary_hash, "binary_changed_during_smoke")
    return {"schema_version": "nmu.caddy-https-smoke.v1", "status": "pass", "checks": results,
            "skipped": skipped, "binary_sha256": binary_hash,
            "source_caddyfile_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "scope": "temporary_loopback_only", "system_trust_modified": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()

    def interrupted(_signum, _frame):
        raise SmokeError("smoke_interrupted")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        print(json.dumps(smoke(args.binary.absolute(), args.source_root.resolve()), sort_keys=True))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, SmokeError, KeyboardInterrupt) as error:
        print(json.dumps({"schema_version": "nmu.caddy-https-smoke.v1", "status": "fail",
                          "diagnostic": str(error)[-12000:]}, sort_keys=True))
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
