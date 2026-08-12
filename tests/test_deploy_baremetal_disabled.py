from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_baremetal.sh"


def test_historical_baremetal_entrypoint_is_only_a_fail_closed_stub():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "exit 64" in source
    assert "\nrsync " not in source
    assert "systemctl" not in source
    assert "remote()" not in source
    assert "ssh -" not in source

    result = subprocess.run(
        ["bash", str(SCRIPT), "deploy", "example.invalid"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 64
    assert result.stdout == ""
    assert "已停用" in result.stderr
    assert "不可变镜像" in result.stderr
