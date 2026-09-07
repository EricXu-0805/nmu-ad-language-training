from __future__ import annotations

import os
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest

from scripts import check_database_head, check_release_contract


ROOT = Path(__file__).resolve().parents[1]
VALID_IMAGE = "registry.invalid/nmu/app@sha256:" + "a" * 64


def test_caddy_large_body_limit_is_scoped_to_canonical_audio_put():
    caddyfile = (ROOT / "Caddyfile").read_text(encoding="utf-8")

    assert caddyfile.count("max_size 64MB") == 1
    assert caddyfile.count("max_size 256KB") == 1
    assert re.search(
        r"@audio_blob_upload\s*\{\s*method PUT\s*"
        r"path_regexp audio_blob_upload ([^\n]+)\s*\}",
        caddyfile,
    )
    assert re.search(
        r"@non_audio_blob_upload\s*\{\s*not\s*\{\s*method PUT\s*"
        r"path_regexp non_audio_blob_upload ([^\n]+)\s*\}\s*\}",
        caddyfile,
    )
    large_match = re.search(
        r"path_regexp audio_blob_upload ([^\n]+)", caddyfile)
    ordinary_exclusion = re.search(
        r"path_regexp non_audio_blob_upload ([^\n]+)", caddyfile)
    assert large_match is not None and ordinary_exclusion is not None
    assert large_match.group(1).strip() == ordinary_exclusion.group(1).strip()

    path_pattern = re.compile(large_match.group(1).strip())
    assert path_pattern.fullmatch("/audio/aud-123_A.B/blob")
    for unsafe in (
        "/auth/login",
        "/audio/aud-123_A.B/blob/extra",
        "/audio/../blob",
        "/audio/%2e%2e/blob",
        "/audio/aud/123/blob",
    ):
        assert path_pattern.fullmatch(unsafe) is None

    assert re.search(
        r"request_body @audio_blob_upload\s*\{\s*max_size 64MB\s*\}",
        caddyfile,
    )
    assert re.search(
        r"request_body @non_audio_blob_upload\s*\{\s*max_size 256KB\s*\}",
        caddyfile,
    )


def _upgrade_to_head(database: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite:///{database}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    script = ScriptDirectory.from_config(config)
    heads = tuple(script.get_heads())
    assert len(heads) == 1
    return str(heads[0])


@pytest.mark.parametrize(
    "value",
    (
        None,
        "",
        "registry.invalid/nmu/app:latest",
        "registry.invalid/nmu/app@sha256:" + "a" * 63,
        "registry.invalid/nmu/app@sha256:" + "A" * 64,
        "registry.invalid/nmu/app@sha512:" + "a" * 64,
        "registry.invalid/nmu/app@sha256:" + "a" * 64 + " extra",
    ),
)
def test_release_image_contract_rejects_every_mutable_or_ambiguous_reference(value):
    with pytest.raises(
            check_release_contract.ReleaseContractError,
            match="release_image_not_immutable"):
        check_release_contract.validate_image_reference(value)


def test_release_image_contract_accepts_digest_without_echoing_private_reference(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    private_reference = "private.registry.invalid/study/app:v1@sha256:" + "b" * 64
    monkeypatch.setenv("NMU_RELEASE_IMAGE", private_reference)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "172.28.0.10")
    monkeypatch.setenv("NMU_EDGE_MODE", "embedded")
    monkeypatch.setenv("NMU_EDGE_IMAGE", "private.registry.invalid/edge@sha256:" + "c" * 64)

    assert check_release_contract.main() == 0
    output = capsys.readouterr()
    assert private_reference not in output.out + output.err
    assert output.out.strip() == "OK release_image_immutable"


@pytest.mark.parametrize("mode,image,code", (
    (None, None, "edge_mode_not_explicit"),
    ("", None, "edge_mode_not_explicit"),
    ("embedded", None, "edge_image_not_immutable"),
    ("embedded", "", "edge_image_not_immutable"),
    ("embedded", "caddy:2.11.4", "edge_image_not_immutable"),
    ("embedded", "private.invalid/renamed@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648", "edge_image_known_vulnerable"),
    ("host", VALID_IMAGE, "host_edge_image_must_be_unset"),
))
def test_edge_contract_rejects_missing_mutable_or_known_bad_images(mode, image, code):
    with pytest.raises(check_release_contract.ReleaseContractError, match=code):
        check_release_contract.validate_edge_contract(mode, image)


@pytest.mark.parametrize("mode,image", (("host", None), ("host", ""), ("embedded", VALID_IMAGE)))
def test_edge_topology_contract_accepts_only_explicit_modes(mode, image):
    check_release_contract.validate_edge_contract(mode, image)


@pytest.mark.parametrize("go_lines,version,accepted", (
    ("go\tgo1.26.8\n", "v2.11.4", True),
    ("go\tgo1.26.3\n", "v2.11.4", False),
    ("go\tgo1.26.8\ngo\tgo1.26.3\n", "v2.11.4", False),
    ("go\tgo1.26.8\n", "v2.11.3", False),
    ("mod\tprivate-provider\n", "v2.11.4", False),
    ("go\tgo1.26.8 extra\n", "v2.11.4", False),
))
def test_edge_process_never_runs_listener_before_actual_build_check(tmp_path, go_lines, version, accepted):
    stub = tmp_path / "caddy"
    marker = tmp_path / "listener-started"
    stub.write_text(
        "#!/bin/sh\ncase \"$1\" in\n"
        "version) printf '%s\\n' \"$TEST_CADDY_VERSION\" ;;\n"
        "build-info) printf '%s' \"$TEST_CADDY_BUILD\" ;;\n"
        "run) printf '%s\\n' \"$*\" > \"$TEST_CADDY_RUN_MARKER\" ;;\n"
        "*) exit 2 ;;\nesac\n", encoding="utf-8")
    stub.chmod(0o700)
    result = subprocess.run(["sh", str(ROOT / "scripts/caddy-entrypoint.sh")],
                            env={"PATH": f"{tmp_path}:/usr/bin:/bin", "TEST_CADDY_VERSION": version,
                                 "TEST_CADDY_BUILD": go_lines, "TEST_CADDY_RUN_MARKER": str(marker)},
                            capture_output=True, text=True, check=False)
    assert (result.returncode == 0) is accepted
    assert marker.exists() is accepted
    if accepted:
        assert marker.read_text().strip() == "run --config /etc/caddy/Caddyfile --adapter caddyfile"
    else:
        assert result.returncode == 78
        assert result.stderr.strip() == "REJECTED code=edge_binary_build_not_approved"
        assert "private-provider" not in result.stdout + result.stderr


def test_edge_build_gate_matches_independent_source_contract():
    pin = json.loads((ROOT / "deploy/caddy-build.json").read_text(encoding="utf-8"))
    entrypoint = (ROOT / "scripts/caddy-entrypoint.sh").read_text(encoding="utf-8")
    provenance = (ROOT / "web/scripts/build-integrity.mjs").read_text(encoding="utf-8")
    assert pin["schema_version"] == "nmu.caddy-build.v1"
    assert repr(pin["caddy"]["version"]) in entrypoint
    assert '"go' + pin["go"]["version"] + '"' in entrypoint
    assert 'caddy_version: "' + pin["caddy"]["version"] + '"' in provenance
    assert 'go_version: "' + pin["go"]["version"] + '"' in provenance


@pytest.mark.parametrize(
    "value",
    (
        None,
        "",
        " ",
        "*",
        "0.0.0.0",
        "127.0.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "8.8.8.8",
        "203.0.113.10",
        "172.28.0.0/24",
        "172.28.0.10,172.28.0.11",
        "172.28.0.10 ",
        ["172.28.0.10"],
    ),
)
def test_forwarded_proxy_contract_rejects_broad_or_non_private_trust(value):
    with pytest.raises(
            check_release_contract.ReleaseContractError,
            match="forwarded_allow_ips_not_exact_private_ip"):
        check_release_contract.validate_forwarded_allow_ips(value)


@pytest.mark.parametrize(
    "value",
    ("10.0.0.1", "172.28.0.10", "192.168.50.1", "fd00::1"),
)
def test_forwarded_proxy_contract_accepts_one_exact_private_address(value):
    assert check_release_contract.validate_forwarded_allow_ips(value) == value


def test_forwarded_proxy_contract_rejects_without_echoing_bad_value(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    unsafe_value = "172.28.0.0/24"
    monkeypatch.setenv("NMU_RELEASE_IMAGE", VALID_IMAGE)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", unsafe_value)

    assert check_release_contract.main() == 78
    output = capsys.readouterr()
    assert unsafe_value not in output.out + output.err
    assert (
        "REJECTED code=forwarded_allow_ips_not_exact_private_ip"
        in output.err
    )


def test_database_head_contract_accepts_only_the_images_single_head(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "app.db"
    head = _upgrade_to_head(database, monkeypatch)

    assert check_database_head.assert_database_at_head(
        f"sqlite:///{database}", root=ROOT) == head

    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    parent = script.get_revision(head).down_revision
    assert isinstance(parent, str)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE alembic_version SET version_num = ?", (parent,))
        connection.commit()

    with pytest.raises(
            check_database_head.DatabaseHeadError,
            match="database_revision_not_head"):
        check_database_head.assert_database_at_head(
            f"sqlite:///{database}", root=ROOT)


def test_database_head_contract_rejects_missing_database_without_path_leak(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]):
    database = tmp_path / "private-subject-database.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")

    assert check_database_head.main() == 78
    output = capsys.readouterr()
    assert str(database) not in output.out + output.err
    assert "REJECTED code=database_missing" in output.err


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker missing")
@pytest.mark.parametrize("host_mode", (False, True))
@pytest.mark.parametrize("edge_image", ("", "caddy:latest", VALID_IMAGE))
def test_compose_contract_parses_without_reading_production_env(host_mode, edge_image):
    environment = os.environ.copy()
    environment.update({
        "APP_IMAGE": VALID_IMAGE,
        "APPDATA_VOLUME": "nmu-test-appdata",
        "CADDY_DATA_VOLUME": "nmu-test-caddydata",
        "CADDY_CONFIG_VOLUME": "nmu-test-caddyconfig",
        "APP_ENV_FILE": ".env.example",
        "SITE_ADDRESS": "training.invalid",
        "TRUSTED_HOSTS": "training.invalid",
        "CADDY_IMAGE": edge_image,
        "APP_HOST_PORT": "18000",
        "HOST_CADDY_BRIDGE_IP": "172.28.0.1",
    })
    version = subprocess.run(
        ["docker", "compose", "version"], cwd=ROOT, env=environment,
        check=False, capture_output=True, text=True)
    if version.returncode != 0:
        pytest.skip("docker compose missing")

    command = ["docker", "compose", "--env-file", ".env.example", "--profile", "maintenance",
               "-f", "docker-compose.yml"]
    if host_mode:
        command.extend(("-f", "docker-compose.host-caddy.yml"))
    result = subprocess.run(
        command + ["config", "--format", "json"],
        cwd=ROOT, env=environment, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    for service in ("app", "migrate"):
        configured = services[service]["environment"]
        assert configured["NMU_EDGE_MODE"] == ("host" if host_mode else "embedded")
        assert configured["NMU_EDGE_IMAGE"] == ("" if host_mode else edge_image)
        check_release_contract.validate_forwarded_allow_ips(configured["FORWARDED_ALLOW_IPS"])
        if host_mode or edge_image == VALID_IMAGE:
            check_release_contract.validate_edge_contract(configured["NMU_EDGE_MODE"], configured["NMU_EDGE_IMAGE"])
        else:
            with pytest.raises(check_release_contract.ReleaseContractError, match="edge_image_not_immutable"):
                check_release_contract.validate_edge_contract(configured["NMU_EDGE_MODE"], configured["NMU_EDGE_IMAGE"])
    if host_mode:
        assert "caddy" not in services
    else:
        assert services["caddy"]["entrypoint"] == ["sh", "/usr/local/libexec/nmu-caddy-entrypoint.sh"]
        assert services["caddy"]["command"] == []
        mounted = next(row for row in services["caddy"]["volumes"]
                       if row["target"] == "/usr/local/libexec/nmu-caddy-entrypoint.sh")
        assert mounted["read_only"] is True
