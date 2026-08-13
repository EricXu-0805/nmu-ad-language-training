"""Caregiver demo20 harness isolation and exact clean-database contract.

Module-level imports deliberately stay inside stdlib/pytest/harness.  Any
``app.*`` import belongs to an isolated child process whose DATABASE_URL and
all writable paths have already been pinned under a chmod-700 temporary root.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from harness import caregiver_demo_harness as caregiver
from harness import tts_ack_harness as shared

PIN = "24681357"
ADMIN_PASSWORD = "caregiver-admin-password-1"
CAREGIVER_PASSWORD = "caregiver-operator-password-2"
INSTANCE_MARKER = "caregiver-demo-instance-marker-000001"


def _private_root(tmp_path: Path, name: str) -> Path:
    root = (tmp_path / name).resolve()
    root.mkdir(mode=0o700)
    return root


def _mapping(root: Path, **overrides: str | None) -> dict[str, str]:
    values: dict[str, str | None] = {
        shared.ROOT_ENV: str(root),
        "DATABASE_URL": f"sqlite:///{root}/caregiver.db",
        "AUDIO_DIR": f"{root}/audio",
        shared.TTS_CACHE_ENV: f"{root}/tts-cache",
        shared.TTS_MODE_ENV: "synthetic",
        shared.ACTOR_ENV: "demo20-admin",
        shared.PASSWORD_ENV: ADMIN_PASSWORD,
        caregiver.CAREGIVER_USERNAME_ENV: "demo20-caregiver",
        caregiver.CAREGIVER_PASSWORD_ENV: CAREGIVER_PASSWORD,
        caregiver.INSTANCE_MARKER_ENV: INSTANCE_MARKER,
        "ALLOW_SIMULATION_DATA": "1",
        "ENABLE_AUTOPILOT_P0A_SIMULATION": "1",
        "REQUIRE_AUTH": "1",
        "CONSOLE_PIN": PIN,
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


def _subprocess_env(root: Path) -> dict[str, str]:
    """Minimal synthetic environment: no provider credential is inherited."""
    values = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(root),
        "TMPDIR": str(root),
        "PYTHONPATH": str(shared.PLATFORM_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        **_mapping(root),
        "TTS_ENGINE": "null",
        "ASR_ENGINE": "null",
        "LLM_JUDGE": "off",
    }
    for name in (
        "DASHSCOPE_API_KEY",
        shared.FINGERPRINT_KEY_ENV,
        "DEIDENTIFICATION_KEY",
    ):
        values.pop(name, None)
    return {key: value for key, value in values.items() if value}


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({caregiver.CAREGIVER_PASSWORD_ENV: None}, caregiver.CAREGIVER_PASSWORD_ENV),
        ({caregiver.CAREGIVER_PASSWORD_ENV: "short"}, caregiver.CAREGIVER_PASSWORD_ENV),
        ({caregiver.CAREGIVER_USERNAME_ENV: " bad"}, caregiver.CAREGIVER_USERNAME_ENV),
        ({caregiver.CAREGIVER_USERNAME_ENV: "bad/name"}, caregiver.CAREGIVER_USERNAME_ENV),
        ({caregiver.CAREGIVER_USERNAME_ENV: "demo20-admin"}, "admin"),
        ({caregiver.CAREGIVER_PASSWORD_ENV: ADMIN_PASSWORD}, "共用口令"),
        ({caregiver.INSTANCE_MARKER_ENV: None}, caregiver.INSTANCE_MARKER_ENV),
        ({caregiver.INSTANCE_MARKER_ENV: "too-short"}, caregiver.INSTANCE_MARKER_ENV),
    ],
)
def test_caregiver_credentials_fail_closed(tmp_path, overrides, fragment):
    root = _private_root(tmp_path, "root")
    with pytest.raises(shared.HarnessConfigError) as excinfo:
        caregiver.resolve_caregiver_config(_mapping(root, **overrides))
    assert fragment in str(excinfo.value)


def test_default_caregiver_username_is_safe_and_secrets_are_scrubbed(tmp_path):
    root = _private_root(tmp_path, "root")
    config = caregiver.resolve_caregiver_config(_mapping(
        root, **{caregiver.CAREGIVER_USERNAME_ENV: None}))
    assert config.caregiver_username == caregiver.DEFAULT_CAREGIVER_USERNAME
    rendered = json.dumps(config.scrubbed(), ensure_ascii=False)
    assert ADMIN_PASSWORD not in rendered
    assert CAREGIVER_PASSWORD not in rendered
    assert PIN not in rendered
    assert "demo20-admin" not in rendered
    assert caregiver.DEFAULT_CAREGIVER_USERNAME not in rendered
    assert INSTANCE_MARKER not in rendered


def test_instance_marker_middleware_binds_each_response_to_one_launcher():
    import asyncio

    import httpx
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/health", health)])
    app.add_middleware(
        caregiver.caregiver_instance_marker_middleware_class(INSTANCE_MARKER))

    async def call_health():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://harness.local") as client:
            return await client.get("/health")

    response = asyncio.run(call_health())
    assert response.status_code == 200
    assert response.headers[caregiver.INSTANCE_MARKER_HEADER] == INSTANCE_MARKER


def test_clean_migrate_seed_and_exact_idempotent_reseed(tmp_path):
    """Real migrations, production VisitPlan ledger, probe and exact re-seed."""
    root = _private_root(tmp_path, "isolated")
    script = r"""
        import json
        from sqlmodel import Session, select
        from harness import caregiver_demo_harness as h

        config = h.resolve_caregiver_config()
        h.migrate(config)
        h.install(config)
        first = h.seed(config)
        second = h.seed(config)
        served = h.validate_seeded_state(config)

        from app import auth, db, provider_readiness
        from app.models import Patient, ProviderReadinessProbe, ResearchUser, VisitPlan
        from app.models import Session as TrainSession
        from app.models import VisitPlanCommand

        with Session(db.engine) as session:
            users = list(session.exec(select(ResearchUser).order_by(ResearchUser.username)))
            patients = list(session.exec(select(Patient)))
            plans = list(session.exec(select(VisitPlan)))
            commands = list(session.exec(
                select(VisitPlanCommand).order_by(VisitPlanCommand.event_seq)))
            sessions = list(session.exec(select(TrainSession)))
            probes = list(session.exec(select(ProviderReadinessProbe)))
            ready = provider_readiness.readiness_projection(session)
            payload = {
                "first": first,
                "second": second,
                "served": served,
                "roles": {row.username: row.role for row in users},
                "passwords_match": {
                    row.username: auth.verify_password(
                        config.base.actor_password
                        if row.role == "admin" else config.caregiver_password,
                        row.password_hash,
                    )
                    for row in users
                },
                "patient_count": len(patients),
                "patient_simulation": patients[0].is_simulation_subject,
                "plan_count": len(plans),
                "plan": {
                    "status": plans[0].status,
                    "revision": plans[0].revision,
                    "profile": plans[0].autopilot_profile_version_id,
                    "created_by": plans[0].created_by,
                    "approved_by": plans[0].approved_by,
                    "started_by": plans[0].started_by,
                },
                "commands": [row.command_type for row in commands],
                "session_count": len(sessions),
                "probe_count": len(probes),
                "ready": ready.model_dump(mode="json"),
            }
        print(json.dumps(payload, ensure_ascii=False))
    """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=str(shared.PLATFORM_ROOT),
        env=_subprocess_env(root),
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stderr[-8000:]
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["first"]["reused"] == "0"
    assert payload["second"]["reused"] == "1"
    assert payload["first"]["status"] == payload["second"]["status"] == "approved"
    assert payload["served"]["status"] == "approved"
    assert payload["roles"] == {
        "demo20-admin": "admin",
        "demo20-caregiver": "caregiver_operator",
    }
    assert all(payload["passwords_match"].values())
    assert payload["patient_count"] == 1
    assert payload["patient_simulation"] is True
    assert payload["plan_count"] == 1
    assert payload["plan"] == {
        "status": "approved",
        "revision": 2,
        "profile": shared.DEMO_PROFILE_VERSION,
        "created_by": "demo20-admin",
        "approved_by": "demo20-admin",
        "started_by": None,
    }
    assert payload["commands"] == ["create", "approve"]
    assert payload["session_count"] == 0
    # First seed and idempotent re-seed each run and persist an actual probe.
    assert payload["probe_count"] == 2
    assert payload["ready"]["status"] == "ready"
    assert payload["ready"]["start_allowed"] is True
    assert payload["ready"]["tts"]["success"] is True
    assert payload["ready"]["asr"]["success"] is True


def test_seed_uses_the_production_research_date_across_host_timezones(tmp_path):
    """The host clock must not create an approved plan that is still 'future'."""
    root = _private_root(tmp_path, "timezone-isolated")
    script = r"""
        import json
        from datetime import date
        from sqlmodel import Session, select
        from harness import caregiver_demo_harness as h

        config = h.resolve_caregiver_config()
        h.migrate(config)
        h.install(config)
        h.seed(config)

        from app import db, visit_plan_service
        from app.models import VisitPlan
        from app.visit_plan_contract import VisitPlanMutationIn

        with Session(db.engine) as session:
            plan = session.exec(select(VisitPlan)).one()
            started = visit_plan_service.start_plan(
                session,
                plan_id=plan.plan_id,
                body=VisitPlanMutationIn(
                    idempotency_key="caregiver-timezone-start-000001",
                    expected_revision=plan.revision,
                ),
                actor_id=config.caregiver_username,
                require_caregiver_operational_demo20=True,
            )
            session.commit()
            print(json.dumps({
                "host_today": date.today().isoformat(),
                "research_today": visit_plan_service._research_today().isoformat(),
                "scheduled_date": plan.scheduled_date.isoformat(),
                "status": started.status,
            }))
    """
    env = _subprocess_env(root)
    # These clocks are exactly one calendar day apart.  The old date.today()
    # harness logic therefore fails deterministically on every wall-clock hour.
    env.update({
        "TZ": "Pacific/Kiritimati",
        "RESEARCH_TIMEZONE": "Pacific/Honolulu",
    })
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=str(shared.PLATFORM_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stderr[-8000:]
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["host_today"] != payload["research_today"]
    assert payload["scheduled_date"] == payload["research_today"]
    assert payload["status"] == "started"


def test_shell_help_is_read_only(tmp_path):
    script = shared.PLATFORM_ROOT / "scripts" / "run-caregiver-demo20.sh"
    scratch = _private_root(tmp_path, "help-scratch")
    before = list(scratch.iterdir())
    result = subprocess.run(
        ["/bin/bash", str(script), "--help"],
        cwd=str(scratch),
        env={"PATH": "/usr/bin:/bin", "TMPDIR": str(scratch)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "照护员 20 题本机演练" in result.stdout
    assert list(scratch.iterdir()) == before


def test_shell_instructions_match_the_real_console_and_pairing_order():
    guide = (shared.PLATFORM_ROOT / "docs" / "照护员本机操作指南.md").read_text(
        encoding="utf-8",
    )
    assert "以 `/console` 结尾" in guide
    assert "以 `/patient` 结尾" in guide
    start_index = guide.index("### 3. 开始本次")
    pair_index = guide.index("输入\n这次启动显示的临时配对 PIN")
    practice_index = guide.index("### 5. 开始练习")
    assert start_index < pair_index < practice_index
    assert "未开始本次前不能提前配对" in guide


def test_shell_missing_dist_stops_before_creating_temp_root(tmp_path):
    source = shared.PLATFORM_ROOT / "scripts" / "run-caregiver-demo20.sh"
    fake_repo = _private_root(tmp_path, "fake-repo")
    scripts = fake_repo / "scripts"
    scripts.mkdir()
    copied = scripts / source.name
    shutil.copy2(source, copied)
    python_dir = fake_repo / ".venv" / "bin"
    python_dir.mkdir(parents=True)
    (python_dir / "python").symlink_to(sys.executable)
    scratch = _private_root(tmp_path, "missing-dist-scratch")

    result = subprocess.run(
        ["/bin/bash", str(copied)],
        cwd=str(fake_repo),
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMPDIR": str(scratch),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "web/dist/index.html" in result.stderr
    assert list(scratch.iterdir()) == []


def test_shell_cleanup_and_minimal_environment_are_structurally_pinned():
    script = shared.PLATFORM_ROOT / "scripts" / "run-caregiver-demo20.sh"
    text = script.read_text(encoding="utf-8")
    assert text.index('case "${1:-}"') < text.index('REPO="$(cd')
    assert text.index('REPO="$(cd') < text.index('mktemp -d')
    assert 'web/dist/index.html' in text
    assert text.index('scripts/verify_browser_dist.py') < text.index('mktemp -d')
    assert 'env -i "${HARNESS_ENV[@]}"' in text
    assert 'nmu-caregiver-demo20.*)' in text
    assert 'rm -rf -- "$HARNESS_ROOT"' in text
    assert 'kill -KILL "$SERVER_PID"' in text
    assert 'X-NMU-Test-Harness'.lower() in text.lower()
    assert 'x-nmu-caregiver-harness-instance' in text.lower()
    assert '"NMU_CAREGIVER_HARNESS_INSTANCE=$INSTANCE_MARKER"' in text
    assert '"$PYTHON" -I -c' in text
    assert "trap '' INT TERM HUP" in text
    assert 'http://127.0.0.1:$PORT/health"' in text
    assert '/healthz' not in text
    assert 'application/json' in text
    assert 'http://127.0.0.1:$PORT/console' in text
    assert 'http://127.0.0.1:$PORT/patient' in text
    assert 'http://127.0.0.1:$PORT/caregiver' not in text
    assert text.count('= "text/html" ]') == 2
    assert text.index('READY=1') < text.index('照护员本机演练已就绪')
    assert "DASHSCOPE_API_KEY" not in text
    assert "PROVIDER_READINESS_FINGERPRINT_KEY" not in text
    assert "scripts/serve.sh" not in text

    syntax = subprocess.run(
        ["/bin/bash", "-n", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert syntax.returncode == 0, syntax.stderr
