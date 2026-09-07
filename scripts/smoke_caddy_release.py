#!/usr/bin/env python3
"""Exercise the actual release Caddy with the repository's proxy routes over TLS.

Uses only temporary files and loopback listeners. An explicit temporary cert is
trusted by this client alone; ACME, OCSP, redirects, admin and trust installation
are disabled. Run on Linux in CI against the exact scanned release binary.
"""
from __future__ import annotations

import argparse
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
import subprocess
import tempfile
import threading
import time


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
            encoded = json.dumps(record).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)
        except (OSError, ValueError):
            # A rejected oversized upload may close a partially streamed request.
            self.close_connection = True


class LoopbackHTTPS(client.HTTPSConnection):
    """Fixed IP transport with localhost SNI/cert verification, without DNS."""

    def connect(self):
        transport = socket.create_connection(("127.0.0.1", self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(transport, server_hostname="localhost")
        except BaseException:
            transport.close()
            raise


def request(port: int, context: ssl.SSLContext, method: str, path: str,
            body: bytes = b"", *, forged_forwarded_for: bool = False):
    connection = LoopbackHTTPS("localhost", port, timeout=5, context=context)
    headers = {"Host": f"localhost:{port}", "Connection": "close",
               "Content-Type": "application/octet-stream"}
    if forged_forwarded_for:
        headers["X-Forwarded-For"] = "198.51.100.77"
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read(8193)
        require(len(response_body) <= 8192, "response_unexpectedly_large")
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response_body
    finally:
        connection.close()


def make_config(source: str, upstream_port: int, certificate: Path, key: Path) -> str:
    # Keep every production route, matcher, body limit and header unchanged.
    # Only substitute the upstream and add transport-only fixture directives.
    require(source.count("reverse_proxy app:8000") == 1, "repository_upstream_shape_changed")
    require(source.count("{$SITE_ADDRESS} {") == 1, "repository_site_shape_changed")
    source = source.replace("reverse_proxy app:8000", f"reverse_proxy 127.0.0.1:{upstream_port}")
    source = source.replace("{$SITE_ADDRESS} {", "{$SITE_ADDRESS} {\n\tbind 127.0.0.1\n"
                            f"\ttls {json.dumps(str(certificate))} {json.dumps(str(key))}")
    return ("{\n\tadmin off\n\tauto_https off\n\tpersist_config off\n"
            "\tskip_install_trust\n\tocsp_stapling off\n}\n" + source)


def validate_local_config(document: dict, https_port: int, upstream_port: int,
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
    require(server.get("listen") == [f"127.0.0.1:{https_port}"], "fixture_listener_not_loopback")
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
    results: list[str] = []
    with tempfile.TemporaryDirectory(prefix="nmu-caddy-https-smoke-") as temporary:
        work = Path(temporary)
        certificate, key = work / "localhost.crt", work / "localhost.key"
        # Certificate has no AIA/OCSP URLs. Its trust never leaves this process.
        subprocess.run([
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "1", "-nodes",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
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
                environment["SITE_ADDRESS"] = f"https://localhost:{https_port}"
                config = work / "Caddyfile"
                config.write_text(make_config(source_bytes.decode(), upstream.server_port, certificate, key))
                adapted = subprocess.run([str(binary), "adapt", "--adapter", "caddyfile", "--config", str(config)],
                                         env=environment, cwd=work, capture_output=True, text=True,
                                         check=True, timeout=15)
                document = json.loads(adapted.stdout)
                validate_local_config(document, https_port, upstream.server_port, certificate, key)
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
                response = request(https_port, context, "POST", "/smoke-oversized", large)
                require(response[0] == 413, "ordinary_large_post_not_413")
                with upstream.records_lock:
                    require(not any(row["path"] == "/smoke-oversized" for row in upstream.completed),
                            "ordinary_large_post_completed_upstream")
                results.append("ordinary_post_over_256kib_rejected")
                response = request(https_port, context, "PUT", "/audio/smoke_A.B-123/blob", large)
                require(response[0] == 200, "canonical_audio_put_rejected")
                echoed = json.loads(response[2])
                require(echoed["body_bytes"] == len(large) and echoed["body_sha256"] == hashlib.sha256(large).hexdigest(),
                        "canonical_audio_body_not_preserved")
                results.append("canonical_audio_put_over_256kib_reaches_upstream")
                require(process.poll() is None, "caddy_exited_during_smoke")
        except Exception as error:
            detail = log_path.read_text(errors="replace")[-8000:] if log_path.exists() else ""
            if isinstance(error, subprocess.CalledProcessError):
                stderr = error.stderr or ""
                detail += stderr.decode(errors="replace")[-4000:] if isinstance(stderr, bytes) else stderr[-4000:]
            raise SmokeError(f"{error}\nlocal_caddy_diagnostics={detail}") from error
        finally:
            try:
                stop_child(process)
            finally:
                upstream.shutdown()
                upstream.server_close()
                worker.join(timeout=5)
    require(hashlib.sha256(binary.read_bytes()).hexdigest() == binary_hash, "binary_changed_during_smoke")
    return {"schema_version": "nmu.caddy-https-smoke.v1", "status": "pass", "checks": results,
            "binary_sha256": binary_hash,
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
