'''Execute the generated runner with synthetic attempts and a local-only notifier.'''
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "[2026-10-22 12:42:22] "
TRANSIENT = "FAIL code=manifest_transfer_failed snapshot=20261015-220421 rc=255 reason=transport_disconnected attempts=3"
FAILED = "failed snapshots=unverified pulled=1 unchanged=5 rc=255"
OK = "ok snapshots=60 pulled=0 unchanged=14 used_mb=7611"


def executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o700)


def runner_harness(tmp_path: Path, attempts: list[tuple[int, list[str]]]):
    root = tmp_path / "nmu-backup"
    scripts = root / "runtime" / "scripts"
    scripts.mkdir(parents=True)
    (root / "offsite").mkdir()
    (root / "offsite" / "pull.log").write_text("")
    (root / "config.env").write_text("")
    (root / "ops-webhook.env").write_text("# synthetic; no external webhook\n")
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "python").symlink_to(sys.executable)
    stubs = tmp_path / "bin"
    stubs.mkdir()
    executable(stubs / "sleep", '#!/bin/bash\nprintf "%s\\n" "$1" >> "$SLEEP_LOG"\n')
    executable(stubs / "caffeinate", '#!/bin/bash\n[ "$1" = -i ] || exit 98\nshift\nexec "$@"\n')
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(attempts))
    executable(scripts / "vps-backup-pull.sh", f"#!{sys.executable}\n" + r'''
import json, os
from pathlib import Path
plan = json.loads(Path(os.environ["PLAN"]).read_text())
count_path = Path(os.environ["COUNT_FILE"])
count = int(count_path.read_text()) if count_path.exists() else 0
count_path.write_text(str(count + 1))
rc, lines = plan[min(count, len(plan)-1)]
with Path(os.environ["PULL_LOG"]).open("a") as f:
    for line in lines:
        f.write("[2026-10-22 12:42:22] " + line + "\n")
raise SystemExit(rc)
''')
    (scripts / "notify_ops.py").write_text(r'''
import json, os, sys
from pathlib import Path
with Path(os.environ["NOTIFY_LOG"]).open("a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
''')
    installer = (ROOT / "scripts" / "install-macos-offsite-pull.sh").read_text()
    start = installer.index('cat > "$ROOT/run-pull.sh"')
    end = installer.index("\nEOF\n", start) + len("\nEOF\n")
    # Exercise actual heredoc expansion, not an approximation of the installed file.
    subprocess.run(["bash", "-c", 'ROOT="$1"\n' + installer[start:end], "render", str(root)], check=True)
    env = os.environ.copy()
    env.update(PATH=str(stubs) + os.pathsep + env["PATH"], PLAN=str(plan),
               COUNT_FILE=str(tmp_path / "count"), PULL_LOG=str(root / "offsite" / "pull.log"),
               NOTIFY_LOG=str(tmp_path / "notifications"), SLEEP_LOG=str(tmp_path / "sleeps"))
    return root, env


def run_runner(root, env):
    return subprocess.run(["bash", str(root / "run-pull.sh")], env=env,
                          capture_output=True, text=True, timeout=15)


def notifications(env):
    path = Path(env["NOTIFY_LOG"])
    return path.read_text().splitlines() if path.exists() else []


def test_transient_failure_then_recovery_keeps_evidence_without_alert(tmp_path):
    root, env = runner_harness(tmp_path, [(255, [TRANSIENT, FAILED]), (0, [OK])])
    result = run_runner(root, env)
    assert result.returncode == 0, result.stderr
    assert Path(env["COUNT_FILE"]).read_text() == "2"
    assert Path(env["SLEEP_LOG"]).read_text() == "60\n"
    log = Path(env["PULL_LOG"]).read_text()
    assert TRANSIENT in log and FAILED in log
    assert "wrapper_retry" in log and "wrapper_recovered attempts=2" in log
    assert not notifications(env)
    assert not (root / ".run-pull.lock").exists()


def test_exhausted_transport_runs_twice_and_alerts_once(tmp_path):
    root, env = runner_harness(tmp_path, [(255, [TRANSIENT, FAILED])])
    result = run_runner(root, env)
    assert result.returncode == 255
    assert Path(env["COUNT_FILE"]).read_text() == "2"
    assert len(notifications(env)) == 1
    message = json.loads(notifications(env)[0])[-1]
    assert "整轮尝试 2 次" in message and FAILED in message
    assert not (root / ".run-pull.lock").exists()


@pytest.mark.parametrize("rc,lines", [
    (255, [TRANSIENT.replace("transport_disconnected", "authentication"), FAILED]),
    (255, [TRANSIENT.replace("transport_disconnected", "host_key_verification"), FAILED]),
    (255, [TRANSIENT.replace("transport_disconnected", "ssh_error_unclassified"), FAILED]),
    (255, [TRANSIENT.replace("rc=255", "rc=95"), FAILED]),
    (255, [TRANSIENT.replace("manifest_transfer_failed", "snapshot_verify_failed"), FAILED]),
    (255, ["FAIL code=legacy_snapshot_unvalidated", TRANSIENT, FAILED]),
    (255, ["FAIL code=snapshot_conflict", TRANSIENT, FAILED]),
    (255, [TRANSIENT]),
    (255, [TRANSIENT, FAILED.replace("rc=255", "rc=12")]),
    (255, []),
    (3, ["ok snapshots=60 pulled=0 unchanged=14 used_mb=99999"]),
    (4, ["FAIL code=snapshot_unresolved_evidence", "partial snapshots=60 pulled=0 unchanged=13 held=1"]),
    (75, ["FAIL code=pull_lock_busy"]),
])
def test_non_retryable_or_incomplete_attempt_is_not_hidden(tmp_path, rc, lines):
    root, env = runner_harness(tmp_path, [(rc, lines), (0, [OK])])
    # A historical transient failure must never make the current attempt retryable.
    Path(env["PULL_LOG"]).write_text(PREFIX + TRANSIENT + "\n" + PREFIX + FAILED + "\n")
    result = run_runner(root, env)
    assert result.returncode == rc
    assert Path(env["COUNT_FILE"]).read_text() == "1"
    assert not Path(env["SLEEP_LOG"]).exists()
    assert len(notifications(env)) == 1
    assert not (root / ".run-pull.lock").exists()


def test_second_attempt_permanent_failure_is_reported(tmp_path):
    auth = TRANSIENT.replace("transport_disconnected", "authentication")
    root, env = runner_harness(tmp_path, [(255, [TRANSIENT, FAILED]), (255, [auth, FAILED])])
    result = run_runner(root, env)
    assert result.returncode == 255
    assert Path(env["COUNT_FILE"]).read_text() == "2"
    assert len(notifications(env)) == 1
    assert "authentication" in json.loads(notifications(env)[0])[-1]


def test_existing_wrapper_lock_is_preserved_and_alerts_without_log_pollution(tmp_path):
    root, env = runner_harness(tmp_path, [(0, [OK])])
    (root / ".run-pull.lock").mkdir()
    result = run_runner(root, env)
    assert result.returncode == 75
    assert (root / ".run-pull.lock").is_dir()
    assert not Path(env["COUNT_FILE"]).exists()
    assert not Path(env["PULL_LOG"]).read_text()
    assert len(notifications(env)) == 1
    assert "入口锁占用" in json.loads(notifications(env)[0])[-1]


def blocking_retry(tmp_path):
    import time

    root, env = runner_harness(tmp_path, [(255, [TRANSIENT, FAILED]), (0, [OK])])
    entered, release = tmp_path / "entered", tmp_path / "release"
    env.update(ENTERED=str(entered), RELEASE=str(release))
    executable(tmp_path / "bin" / "sleep", f"#!{sys.executable}\n" + r'''
import os, time
from pathlib import Path
Path(os.environ["ENTERED"]).touch()
for _ in range(1000):
    if Path(os.environ["RELEASE"]).exists():
        break
    time.sleep(0.01)
else:
    raise SystemExit(99)
''')
    process = subprocess.Popen(["bash", str(root / "run-pull.sh")], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(500):
        if entered.exists():
            break
        if process.poll() is not None:
            pytest.fail(process.communicate()[1])
        time.sleep(0.01)
    else:
        release.touch()
        process.communicate(timeout=15)
        pytest.fail("runner did not reach backoff")
    return root, env, process, release


def test_concurrent_runner_cannot_enter_during_backoff(tmp_path):
    root, env, first, release = blocking_retry(tmp_path)
    try:
        original = Path(env["PULL_LOG"]).read_bytes()
        second = run_runner(root, env)
        assert second.returncode == 75
        assert Path(env["PULL_LOG"]).read_bytes() == original
        assert Path(env["COUNT_FILE"]).read_text() == "1"
        assert (root / ".run-pull.lock").is_dir()
        assert len(notifications(env)) == 1  # lock attention, no duplicate transfer
    finally:
        release.touch()
        first.communicate(timeout=15)
    assert first.returncode == 0
    assert not (root / ".run-pull.lock").exists()


def test_term_during_backoff_does_not_start_another_pull(tmp_path):
    import signal

    root, env, process, release = blocking_retry(tmp_path)
    try:
        process.send_signal(signal.SIGTERM)
    finally:
        # Bash processes its pending TERM after the foreground command returns.
        release.touch()
        process.communicate(timeout=15)
    assert process.returncode == 143
    assert Path(env["COUNT_FILE"]).read_text() == "1"
    assert not (root / ".run-pull.lock").exists()
    assert not notifications(env)
