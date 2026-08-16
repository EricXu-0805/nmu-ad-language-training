from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
GATE = ROOT / "scripts/ci_gate.sh"
GATE_TEXT = GATE.read_text(encoding="utf-8")


def test_workflow_calls_the_shared_gate_for_every_core_suite() -> None:
    assert 'python: ["3.12", "3.14"]' in WORKFLOW
    assert 'node-version: "25"' in WORKFLOW
    assert "pytest==9.1.1 ruff==0.15.15" in WORKFLOW
    assert "scripts/ci_gate.sh --only backend" in WORKFLOW
    assert "scripts/ci_gate.sh --only frontend" in WORKFLOW
    assert "scripts/ci_gate.sh --only supply-chain" in WORKFLOW

    # 核心判据不得再在 workflow 里复制一份，否则本地与云端会再次漂移。
    assert "run: ruff check ." not in WORKFLOW
    assert "run: python -m pytest -q" not in WORKFLOW
    assert "run: npm run lint" not in WORKFLOW
    assert "run: python scripts/vuln_scan.py" not in WORKFLOW


def test_cloud_only_image_scan_remains_an_extra_gate() -> None:
    assert "docker build -t nmu-platform:ci ." in WORKFLOW
    assert "aquasecurity/trivy-action@" in WORKFLOW
    assert "severity: HIGH,CRITICAL" in WORKFLOW


def test_python_312_archives_a_source_bound_full_scale_receipt() -> None:
    assert "python -m harness.quality_release_scale --profile full" in WORKFLOW
    assert '--receipt "$receipt_dir/quality-release-scale.json"' in WORKFLOW
    assert "if: matrix.python == '3.12'" in WORKFLOW
    assert "name: quality-release-scale-python-3.12" in WORKFLOW
    assert "if-no-files-found: error" in WORKFLOW


def test_shared_frontend_gate_checks_the_same_node_major_without_installing() -> None:
    assert 'EXPECTED_NODE_MAJOR="25"' in GATE_TEXT
    assert "actual_node_major" in GATE_TEXT
    assert "npm run lint && npm run pretest && npm test && npm run build" in GATE_TEXT
    assert "npm install" not in GATE_TEXT
    assert "npm ci" not in GATE_TEXT


def test_shared_backend_gate_pins_the_same_ruff_version() -> None:
    assert 'EXPECTED_RUFF_VERSION="0.15.15"' in GATE_TEXT
    assert 'reported_version" != "ruff $EXPECTED_RUFF_VERSION' in GATE_TEXT


def test_backend_gate_falls_back_to_the_exact_pinned_path_ruff(tmp_path: Path) -> None:
    fake_python = tmp_path / "python"
    fake_ruff = tmp_path / "ruff"
    ruff_log = tmp_path / "ruff.log"
    ruff_log.write_text("", encoding="utf-8")
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"ruff\" ]; then exit 1; fi\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pytest\" ]; then exit 0; fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    fake_ruff.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'ruff 0.15.15'; exit 0; fi\n"
        "printf '%s\\n' \"$*\" >> \"$RUFF_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_ruff.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "NMU_GATE_PYTHON": str(fake_python),
            "PATH": f"{tmp_path}:{env['PATH']}",
            "RUFF_LOG": str(ruff_log),
        }
    )

    completed = subprocess.run(
        [str(GATE), "--only", "backend"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert ruff_log.read_text(encoding="utf-8") == "check .\n"


def test_backend_gate_rejects_a_different_path_ruff_version(tmp_path: Path) -> None:
    fake_python = tmp_path / "python"
    fake_ruff = tmp_path / "ruff"
    ruff_log = tmp_path / "ruff.log"
    ruff_log.write_text("", encoding="utf-8")
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"ruff\" ]; then exit 1; fi\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pytest\" ]; then exit 0; fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    fake_ruff.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'ruff 0.15.14'; exit 0; fi\n"
        "printf '%s\\n' \"$*\" >> \"$RUFF_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_ruff.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "NMU_GATE_PYTHON": str(fake_python),
            "PATH": f"{tmp_path}:{env['PATH']}",
            "RUFF_LOG": str(ruff_log),
        }
    )

    completed = subprocess.run(
        [str(GATE), "--only", "backend"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 1
    assert "CI 要求 ruff 0.15.15" in completed.stdout
    assert ruff_log.read_text(encoding="utf-8") == ""


def test_ambiguous_offline_flag_is_rejected_with_the_honest_replacement() -> None:
    completed = subprocess.run(
        [str(GATE), "--offline"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 64
    assert "--offline-osv" in completed.stderr


def test_help_does_not_promise_a_fully_offline_gate() -> None:
    completed = subprocess.run(
        [str(GATE), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--offline-osv" in completed.stdout
    assert "不是“全离线”模式" in completed.stdout
