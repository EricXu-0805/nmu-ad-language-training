"""Production launchers must not log research identifiers from URL paths."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_uvicorn_access_log_is_disabled_in_container_and_local_launcher():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    serve_script = (ROOT / "scripts" / "serve.sh").read_text(encoding="utf-8")

    assert '"--no-access-log"' in dockerfile
    assert "uvicorn --no-access-log" in serve_script


def test_local_launcher_keeps_credentials_and_tls_key_out_of_service_logs():
    serve_script = (ROOT / "scripts" / "serve.sh").read_text(encoding="utf-8")

    assert "umask 077" in serve_script
    assert "if [ -t 1 ]; then" in serve_script
    assert 'chmod 600 "$CERT_DIR/server.key"' in serve_script
