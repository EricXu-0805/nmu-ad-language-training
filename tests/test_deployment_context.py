from pathlib import Path
import subprocess

from app import http_security


ROOT = Path(__file__).resolve().parents[1]


def test_docker_context_excludes_local_browser_and_visual_audit_artifacts():
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert ".playwright-cli/" in patterns
    assert "项目综合审计_*/" in patterns
    assert "data/" in patterns
    assert {
        ".env", ".env.*", "*.env", "*.env.*", "**/.env*",
        ".npmrc", "**/.npmrc",
    }.issubset(patterns)
    assert "!.env.example" in patterns
    assert "!**/.env.example" in patterns


def test_git_and_docker_context_reject_vite_env_and_npm_credentials():
    """Local build-only credentials must not become source or image inputs."""
    git_ignored = (
        ".env",
        ".env.production",
        "web/.env",
        "web/.env.development",
        "web/.env.production.local",
        ".npmrc",
        "web/.npmrc",
        "tools/private/.npmrc",
    )
    for candidate in git_ignored:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", candidate],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 0, f"Git must ignore {candidate}"

    # The checked-in, placeholder-only template remains available to operators.
    template = ROOT / ".env.example"
    assert template.is_file()
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", ".env.example"],
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 1

    # Docker's ordered ignore contract mirrors the Git boundary.  Keeping these
    # exact rules under test prevents COPY web/ from silently ingesting Vite env
    # variants or nested registry tokens while preserving the public template.
    docker_patterns = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for required in (
        ".env", ".env.*", "*.env", "*.env.*", "**/.env*",
        ".npmrc", "**/.npmrc", "!.env.example", "!**/.env.example",
    ):
        assert required in docker_patterns
    assert docker_patterns.index("**/.env*") < docker_patterns.index("!.env.example")
    assert docker_patterns.index("**/.env*") < docker_patterns.index("!**/.env.example")


def test_runtime_image_does_not_copy_host_specific_operational_scripts():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY scripts/ ./scripts/" not in dockerfile
    assert "scripts/docker-entrypoint.sh" in dockerfile
    assert "scripts/docker-migrate.sh" in dockerfile
    assert "scripts/check_release_contract.py" in dockerfile
    assert "scripts/check_database_head.py" in dockerfile
    assert "scripts/backup.sh" in dockerfile
    assert "scripts/verify_backup_snapshot.py" in dockerfile
    assert "scripts/vps-backup-pull.sh" not in dockerfile


def test_backup_entrypoints_are_executable_in_source_checkout():
    for name in ("backup.sh", "vps-backup-daily.sh", "vps-backup-pull.sh"):
        assert (ROOT / "scripts" / name).stat().st_mode & 0o111


def test_production_compose_uses_immutable_release_and_external_volume_contract():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    app_block = compose.split("\n  caddy:", 1)[0]
    volumes_block = compose.split("\nvolumes:", 1)[1]

    assert "name: nmu-platform" in compose
    assert "COMPOSE_PROJECT_NAME" in compose
    assert "build:" not in compose
    assert 'image: "${APP_IMAGE:?set immutable APP_IMAGE with @sha256 digest}"' in app_block
    assert 'NMU_RELEASE_IMAGE: "${APP_IMAGE:?' in app_block
    assert 'env_file: "${APP_ENV_FILE:-.env}"' in app_block
    assert 'name: "${APPDATA_VOLUME:?set explicit existing APPDATA_VOLUME}"' in volumes_block
    assert 'name: "${CADDY_DATA_VOLUME:?set explicit existing CADDY_DATA_VOLUME}"' in volumes_block
    assert 'name: "${CADDY_CONFIG_VOLUME:?set explicit existing CADDY_CONFIG_VOLUME}"' in volumes_block
    assert volumes_block.count("external: true") == 3


def test_host_caddy_override_is_loopback_only_and_fail_closed():
    override = (ROOT / "docker-compose.host-caddy.yml").read_text(encoding="utf-8")

    assert '127.0.0.1:${APP_HOST_PORT:?set APP_HOST_PORT}:8000' in override
    assert '${HOST_CADDY_BRIDGE_IP:?set exact Docker bridge source IP}' in override
    assert 'profiles: ["embedded-proxy-disabled-in-host-mode"]' in override
    assert "mem_limit: 384m" in override
    assert "memswap_limit: 512m" in override
    assert "0.0.0.0" not in override
    assert 'FORWARDED_ALLOW_IPS: "*"' not in override


def test_caddy_permanently_hides_legacy_answers_and_framework_docs():
    caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")

    assert "@blocked_internal path_regexp blocked_internal (?i)" in caddy
    assert "respond @blocked_internal 404" in caddy
    assert "item_bank_v1\\.json" in caddy
    assert "week1_script\\.json" in caddy
    assert "autopilot_protocol_v1\\.json" in caddy
    assert "docs(?:/.*)?" in caddy
    assert "redoc(?:/.*)?" in caddy
    assert "openapi\\.json" in caddy
    assert "\t\t-Server" in caddy


def test_caddy_uses_native_spoof_resistant_forwarded_for_handling():
    caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")

    assert "reverse_proxy app:8000" in caddy
    assert "header_up X-Forwarded-For" not in caddy
    assert not any(
        line.strip().startswith("trusted_proxies ")
        for line in caddy.splitlines()
    )


def test_schema_migration_is_explicit_and_never_runs_in_normal_entrypoint():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    entrypoint = (ROOT / "scripts" / "docker-entrypoint.sh").read_text(
        encoding="utf-8")
    migrate = (ROOT / "scripts" / "docker-migrate.sh").read_text(
        encoding="utf-8")

    assert 'profiles: ["maintenance"]' in compose
    assert 'entrypoint: ["bash", "scripts/docker-migrate.sh"]' in compose
    assert "alembic upgrade head" not in entrypoint
    assert "scripts/check_release_contract.py" in entrypoint
    assert "scripts/check_database_head.py" in entrypoint
    assert migrate.count("alembic upgrade head") == 1
    assert migrate.index("check_release_contract.py") < migrate.index(
        "alembic upgrade head")
    assert migrate.index("alembic upgrade head") < migrate.index(
        "check_database_head.py")


def test_deploy_runbook_never_renders_secrets_or_builds_mutable_source():
    deploy = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    compose_commands = [
        line.strip() for line in deploy.splitlines()
        if line.strip().startswith("docker compose ")
    ]
    config_commands = [
        line for line in compose_commands if " config" in f" {line}"
    ]
    up_commands = [line for line in compose_commands if " up " in f" {line} "]

    assert config_commands
    assert all("config --quiet" in line for line in config_commands)
    assert up_commands
    assert all("--no-build" in line for line in up_commands)
    assert "docker compose build" not in deploy
    assert "rsync -a --delete" not in deploy
    assert "APP_IMAGE" in deploy and "APPDATA_VOLUME" in deploy
    assert "-p" in deploy and "COMPOSE_PROJECT_NAME" in deploy
    assert "APP_ENV_FILE=.env.candidate" in deploy
    assert "APP_ENV_FILE` 改回 `.env`" in deploy
    assert "原 live 卷保留不动" in deploy
    assert "常规 app 入口" in deploy and "绝不自动迁移" in deploy
    assert "get.docker.com" in deploy and "不使用 `get.docker.com`" in deploy
    assert "curl -fsSL https://get.docker.com" not in deploy
    assert "usermod -aG docker" not in deploy
    assert "https://download.docker.com/linux/ubuntu" in deploy
    assert "apt-cache madison docker-ce" in deploy
    assert "DOCKER-USER" in deploy
    assert "docker` 组" in deploy and "root 能力" in deploy


def test_restore_runbook_requires_semantic_current_head_verification():
    deploy = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    assert "verify_backup_snapshot.py verify-vps <快照目录>" in deploy
    assert "真正复制或切换前立即再运行一次" in deploy
    assert "legacy-unvalidated/" in deploy
    assert "当前已部署版本自带的历史验证器" in deploy
    assert "PostgreSQL 正式部署" in deploy and "备份 NO-GO" in deploy


def test_compose_has_bounded_logs_and_health_gated_proxy_start(monkeypatch):
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    app_block, remainder = compose.split("\n  caddy:", 1)
    caddy_block, migrate_block = remainder.split("\n  migrate:", 1)
    assert "healthcheck:" in compose
    assert "condition: service_healthy" in compose
    assert compose.count('max-size: "10m"') == 2
    assert compose.count('max-file: "5"') == 2
    assert "${TRUSTED_HOSTS:?set TRUSTED_HOSTS in .env}" in compose
    assert "${SITE_ADDRESS:?set SITE_ADDRESS in .env}" in compose
    assert "mem_limit: 512m" in app_block
    assert "memswap_limit: 640m" in app_block
    assert "mem_limit: 128m" in caddy_block
    assert "memswap_limit: 160m" in caddy_block
    assert "mem_limit: 512m" in migrate_block
    assert "memswap_limit: 640m" in migrate_block

    # The probe connects directly to Uvicorn on loopback, but the application
    # still enforces the production Host allowlist on /health.  Its Host header
    # must therefore come from the required deployment setting; hard-coding
    # 127.0.0.1 would keep app unhealthy and prevent Caddy from ever starting.
    assert "from app.http_security import configured_hosts" in compose
    assert "h=configured_hosts()[0]" in compose
    assert "headers={'Host':h}" in compose
    monkeypatch.setenv("TRUSTED_HOSTS", "training.example.com")
    probe_host = http_security.configured_hosts()[0]
    assert probe_host == "training.example.com"
    assert http_security.host_allowed(probe_host)
    assert not http_security.host_allowed("127.0.0.1:8000")


def test_local_deployment_build_uses_frozen_frontend_lockfile():
    launcher = (ROOT / "scripts" / "serve.sh").read_text(encoding="utf-8")
    assert "npm ci --no-audit --no-fund" in launcher
    assert "npm install --no-audit --no-fund" not in launcher


def test_local_demo20_launcher_is_explicit_and_loopback_only():
    launcher = (ROOT / "scripts" / "serve.sh").read_text(encoding="utf-8")
    assert 'if [ "${DEMO20:-0}" = "1" ]; then' in launcher
    assert 'if [ "${INTRANET:-0}" = "1" ]; then' in launcher
    assert "DEMO20 仅允许本机回环演示" in launcher
    assert "export ALLOW_SIMULATION_DATA=1" in launcher
    assert "export ENABLE_AUTOPILOT_P0A_SIMULATION=1" in launcher
    assert "app.main:app --host 127.0.0.1 --port 8000" in launcher


def test_local_launcher_help_is_read_only_and_unknown_arguments_fail_closed():
    launcher_path = ROOT / "scripts" / "serve.sh"
    launcher = launcher_path.read_text(encoding="utf-8")
    help_gate = launcher.index('if [ "$#" -gt 0 ]; then')
    assert help_gate < launcher.index("PY=./.venv/bin/python")
    assert help_gate < launcher.index("alembic upgrade head")
    assert help_gate < launcher.index("run_server()")

    help_result = subprocess.run(
        ["bash", str(launcher_path), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert help_result.returncode == 0
    assert "不迁移数据库、不启动服务" in help_result.stdout
    assert help_result.stderr == ""

    unknown_result = subprocess.run(
        ["bash", str(launcher_path), "--unknown"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert unknown_result.returncode == 64
    assert "不支持的参数" in unknown_result.stderr


def test_answer_bearing_content_is_absent_from_web_static_roots():
    package = (ROOT / "web" / "package.json").read_text(encoding="utf-8")
    vite = (ROOT / "web" / "vite.config.ts").read_text(encoding="utf-8")
    bundle = (ROOT / "web" / "src" / "content" / "bundle.ts").read_text(
        encoding="utf-8")
    assert "sync-content" not in package
    assert "publicDir: false" in vite
    assert "forbiddenStaticBundleNames" in vite
    assert "assertNoAnswerBearingStaticBundles()" in vite
    assert "protectedBrowserModuleGraph()" in vite
    assert "assertNoSensitiveContentInDist()" in vite
    assert "writeBrowserBuildEvidence({ buildFingerprint, buildId })" in vite
    assert "['content', 'img'].includes" in vite
    assert "exactContentProxyPaths" in vite
    assert "`^${path}(?:\\\\?.*)?$`" in vite
    for legacy_name in (
        "item_bank_v1.json",
        "week1_script.json",
        "autopilot_protocol_v1.json",
    ):
        assert legacy_name not in bundle
        assert not (ROOT / "web" / "public" / "content" / legacy_name).exists()
        assert not (ROOT / "web" / "dist" / "content" / legacy_name).exists()


def test_browser_build_evidence_declares_but_does_not_overclaim_release_identity():
    integrity = (ROOT / "web" / "scripts" / "build-integrity.mjs").read_text(
        encoding="utf-8")
    fingerprint = (ROOT / "web" / "scripts" / "build-fingerprint.mjs").read_text(
        encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    deploy = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    for identity, source in (
        ("node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2", dockerfile),
        ("python:3.12-alpine3.24@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df", dockerfile),
        ("caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648", compose),
    ):
        assert identity in integrity
        assert identity in source
    assert '"scripts/build-integrity.mjs"' in fingerprint
    assert '"scripts/build-integrity.d.mts"' in fingerprint
    assert "Not a cross-environment reproducibility" in integrity
    assert "excluded_paths: [DIST_MANIFEST_NAME]" in integrity
    assert "declared_lockfile_versions" in integrity
    assert "observed_runtime_versions" in integrity
    assert "assertToolchainMatchesLock" in integrity
    assert "只标识代码中明确枚举的输入字节" in deploy
    assert "任一不一致立即中止构建" in deploy
    assert "node_major_matches_declared_release_builder=true" in deploy


def test_vite_dev_proxy_covers_every_console_api_domain():
    config = (ROOT / "web" / "vite.config.ts").read_text(encoding="utf-8")
    for prefix in (
        "/ai", "/assessment-events", "/assessment-instances", "/audit",
        "/auth", "/audio", "/cloud-processing", "/device", "/exports",
        "/governance", "/health", "/items", "/live", "/patients",
        "/sessions", "/turns", "/visit-plans",
    ):
        assert repr(prefix) in config
