from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys

import pytest
from alembic import command
from alembic.config import Config

from app import models as _models  # noqa: F401 - registers recovery schema


ROOT = Path(__file__).resolve().parents[1]
GUARD_SOURCE = ROOT / "scripts" / "verify_backup_snapshot.py"
_SPEC = importlib.util.spec_from_file_location("verify_backup_snapshot_vps_test", GUARD_SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
_GUARD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GUARD)
_STRICT_STAMP = re.compile(r"^[0-9]{8}-[0-9]{6}$")


def _make_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _create_current_database(path: Path, audio: tuple[str, bytes, str] | None = None) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")
    connection = sqlite3.connect(path)
    try:
        if audio is not None:
            raw_id, payload, audio_format = audio
            connection.execute(
                "INSERT INTO audioassetrow "
                "(raw_audio_id,status,withdrawn,withdrawal_status,checksum,byte_count,"
                "audio_format,is_simulation,data_classification,patient_turn_ref_version,"
                "is_reliability_sample,contains_direct_identifier,delete_gate_passed) "
                "VALUES (?,?,?,?,?,?,?,0,'research',2,0,0,0)",
                (raw_id, "recorded", 0, None, hashlib.sha256(payload).hexdigest(),
                 len(payload), audio_format),
            )
        connection.commit()
    finally:
        connection.close()


def _write_manifest(snapshot: Path) -> None:
    rows = []
    for path in sorted(snapshot.rglob("*")):
        if path.is_file() and path.name != "MANIFEST.sha256":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append(f"{digest}  ./{path.relative_to(snapshot).as_posix()}\n")
    (snapshot / "MANIFEST.sha256").write_text("".join(rows), encoding="utf-8")
    for current, directories, files in os.walk(snapshot):
        os.chmod(current, 0o700)
        for name in directories:
            os.chmod(Path(current) / name, 0o700)
        for name in files:
            os.chmod(Path(current) / name, 0o600)


def _create_remote_snapshot(remote: Path, stamp: str, payload: bytes = b"audio") -> Path:
    snapshot = remote / "daily" / stamp
    audio = snapshot / "audio"
    audio.mkdir(parents=True)
    (audio / "aud-test.webm").write_bytes(payload)
    _create_current_database(snapshot / "app.db", ("aud-test", payload, "webm"))
    config = snapshot / "config"
    config.mkdir()
    for name in _GUARD.VPS_CONFIG_FILES:
        (config / name).write_text(f"synthetic {name}\n", encoding="utf-8")
    _write_manifest(snapshot)
    return snapshot


def _create_minimal_remote_snapshot(remote: Path, stamp: str) -> Path:
    snapshot = remote / "daily" / stamp
    snapshot.mkdir(parents=True)
    _create_current_database(snapshot / "app.db")
    config = snapshot / "config"
    config.mkdir()
    for name in _GUARD.VPS_CONFIG_FILES:
        (config / name).write_text(f"synthetic {name}\n", encoding="utf-8")
    _write_manifest(snapshot)
    return snapshot


def _pull_harness(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    project = tmp_path / "platform"
    scripts = project / "scripts"
    stubs = tmp_path / "bin"
    remote = tmp_path / "remote"
    local = tmp_path / "local"
    scripts.mkdir(parents=True)
    stubs.mkdir()
    (remote / "daily").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "vps-backup-pull.sh", scripts / "vps-backup-pull.sh")
    shutil.copy2(GUARD_SOURCE, scripts / "verify_backup_snapshot.py")
    shutil.copy2(ROOT / "scripts" / "audit_anchor_check.py",
                 scripts / "audit_anchor_check.py")
    transfer_log = tmp_path / "transfers.log"
    _make_executable(stubs / "ssh", r'''#!/usr/bin/env bash
set -euo pipefail
command_arg="${!#}"
if [[ "$command_arg" == *"NMU_SNAPSHOT_FACTS_CONTRACT=1"* ]]; then
  if [[ "$command_arg" =~ /daily/([0-9]{8}-[0-9]{6}) ]]; then
    stamp=${BASH_REMATCH[1]}
  else
    exit 2
  fi
  # 只有远端命令真的要了多链接文件的路径（%P），这里才按新合同放行顶层安全名录音。
  linked_audio_contract=0
  if [[ "$command_arg" == *"%P"* ]]; then
    linked_audio_contract=1
  fi
  "$PYTHON_BIN" -I - "$FAKE_REMOTE_ROOT/daily/$stamp" "$linked_audio_contract" <<'PY'
import os
from pathlib import Path
import re
import stat
import sys

root = Path(sys.argv[1])
linked_audio_contract = sys.argv[2] == "1"
files = 0
byte_count = 0
directory_count = 0
invalid = 0
for current, directories, filenames in os.walk(root, followlinks=False):
    current_path = Path(current)
    directory_count += 1
    for name in directories:
        meta = (current_path / name).lstat()
        if stat.S_ISLNK(meta.st_mode) or not stat.S_ISDIR(meta.st_mode):
            invalid += 1
    for name in filenames:
        meta = (current_path / name).lstat()
        if stat.S_ISLNK(meta.st_mode) or not stat.S_ISREG(meta.st_mode):
            invalid += 1
            continue
        relative = (current_path / name).relative_to(root).as_posix()
        if meta.st_nlink != 1 and not (
                linked_audio_contract
                and re.fullmatch(r"audio/[A-Za-z0-9][A-Za-z0-9._-]*", relative)):
            invalid += 1
            continue
        files += 1
        byte_count += meta.st_size
print(files, byte_count, directory_count, invalid)
PY
elif [[ "$command_arg" == *"backup.log"* ]]; then
  target="$FAKE_REMOTE_ROOT/backup.log"
  if [ -L "$target" ]; then
    printf 'l 0\n'
  elif [ -f "$target" ]; then
    printf 'f %s\n' "$(wc -c < "$target" | tr -d ' ')"
  else
    exit 1
  fi
else
  shopt -s nullglob
  for target in "$FAKE_REMOTE_ROOT"/daily/*; do
    name=${target##*/}
    [[ "$name" =~ ^[0-9]{8}-[0-9]{6}$ ]] || continue
    if [ -L "$target" ]; then kind=l
    elif [ -d "$target" ]; then kind=d
    else kind=f
    fi
    printf '%s %s\n' "$kind" "$name"
  done
fi
''')
    _make_executable(stubs / "rsync", r'''#!/usr/bin/env bash
set -euo pipefail
if [ -n "${FAIL_RSYNC_SECRET:-}" ]; then
  printf '%s\n' "$FAIL_RSYNC_SECRET" >&2
  exit 95
fi
argc=$#
src_index=$((argc - 1))
dst_index=$argc
src=${!src_index}
dst=${!dst_index}
if [[ "$src" == */ ]] && [[ " $* " != *" -x "* ]]; then
  exit 96
fi
printf '%s\n' "$src" >> "$TRANSFER_LOG"
if [ -n "${RSYNC_ARGS_LOG:-}" ]; then
  printf '%s\n' "$*" >> "$RSYNC_ARGS_LOG"
fi
remote_path=${src#*:}
relative=${remote_path#"$NMU_BACKUP_REMOTE_ROOT"}
source="$FAKE_REMOTE_ROOT$relative"
if [ "${REPLACE_MANIFEST_WITH_SYMLINK_ON_TRANSFER:-}" = 1 ] \
   && [[ "$remote_path" == */MANIFEST.sha256 ]]; then
  rm -f "$source"
  ln -s "$MANIFEST_SYMLINK_TARGET" "$source"
fi
if [[ "$remote_path" == */ ]]; then
  case "${INJECT_UNLISTED_AFTER_PREFLIGHT:-}" in
    1)
      mkdir -p "$source/unlisted-flood/nested"
      printf flood > "$source/unlisted-flood/nested/not-in-manifest.bin"
      ;;
    empty)
      mkdir -p "$source/unlisted-empty-directory"
      ;;
  esac
  files_from=""
  for argument in "$@"; do
    case "$argument" in
      --files-from=*) files_from=${argument#--files-from=} ;;
    esac
  done
  if [ -n "$files_from" ]; then
    from0=0
    [[ " $* " == *" --from0 "* ]] && from0=1
    while IFS= read -r -d '' rel; do
      [ -n "$rel" ] || continue
      rel=${rel#./}
      parent=${rel%/*}
      if [ "$parent" != "$rel" ]; then mkdir -p "$dst/$parent"; fi
      cp -p "$source/$rel" "$dst/$rel"
    done < "$files_from"
    [ "$from0" -eq 1 ] || exit 97
  else
    cp -a "$source"/. "$dst"/
  fi
else
  rm -f "$dst"
  if [ -L "$source" ]; then
    ln -s "$(readlink "$source")" "$dst"
  else
    cp -p "$source" "$dst"
  fi
fi
''')
    (remote / "backup.log").write_text("remote backup ok\n", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "PYTHON_BIN": sys.executable,
        "FAKE_REMOTE_ROOT": str(remote),
        "TRANSFER_LOG": str(transfer_log),
        "NMU_BACKUP_SSH_HOST": "backup.example.test",
        "NMU_BACKUP_SSH_USER": "nmu-backup",
        "NMU_BACKUP_REMOTE_ROOT": "/srv/nmu/backups",
        "NMU_BACKUP_LOCAL_ROOT": str(local),
    }
    return project, remote, local, env


def _run_pull(project: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/vps-backup-pull.sh"], cwd=project, env=env,
        check=False, capture_output=True, text=True,
    )


def _inject_ssh_failure(env: dict[str, str], *, phase: str, failures: int,
                        error: str, start: int = 1, rc: int = 255) -> Path:
    """Fail selected read-only requests, including partial output, before real stubs."""
    stubs = Path(env["PATH"].split(":", 1)[0])
    (stubs / "ssh").rename(stubs / "ssh-success")
    env.update({
        "SSH_FAIL_PHASE": phase, "SSH_FAIL_COUNT": str(failures),
        "SSH_FAIL_START": str(start), "SSH_FAIL_RC": str(rc),
        "SSH_FAIL_ERROR": error, "SSH_TEST_STATE": str(stubs),
    })
    _make_executable(stubs / "sleep", '''#!/bin/bash
printf '%s\\n' "$*" >> "$SSH_TEST_STATE/sleeps.log"
''')
    _make_executable(stubs / "ssh", r'''#!/bin/bash
set -euo pipefail
command_arg="${!#}"
if [[ "$command_arg" == *"NMU_SNAPSHOT_FACTS_CONTRACT=1"* ]]; then phase=facts
elif [[ "$command_arg" == *"backup.log"* ]]; then phase=log
else phase=list
fi
printf '%s\0' "$*" >> "$SSH_TEST_STATE/ssh-args.log"
count_file="$SSH_TEST_STATE/$phase.count"
count=0
[ ! -f "$count_file" ] || read -r count < "$count_file"
count=$((count + 1))
printf '%s\n' "$count" > "$count_file"
if [ "$phase" = "$SSH_FAIL_PHASE" ] && [ "$count" -ge "$SSH_FAIL_START" ] \
   && [ "$count" -lt $((SSH_FAIL_START + SSH_FAIL_COUNT)) ]; then
  printf '%s\n' 'PARTIAL-UNTRUSTED-STDOUT'
  printf '%s\n' "$SSH_FAIL_ERROR" >&2
  exit "$SSH_FAIL_RC"
fi
exec "$SSH_TEST_STATE/ssh-success" "$@"
''')
    return stubs


def _inject_rsync_failure(env: dict[str, str], *, phase: str, failures: int,
                          error: str, rc: int = 255, after_copy: bool = False) -> Path:
    stubs = Path(env["PATH"].split(":", 1)[0])
    (stubs / "rsync").rename(stubs / "rsync-success")
    env.update({
        "RSYNC_FAIL_PHASE": phase, "RSYNC_FAIL_COUNT": str(failures),
        "RSYNC_FAIL_RC": str(rc), "RSYNC_FAIL_ERROR": error,
        "RSYNC_FAIL_AFTER_COPY": "1" if after_copy else "0",
        "RSYNC_TEST_STATE": str(stubs),
    })
    _make_executable(stubs / "sleep", '''#!/bin/bash
printf '%s\\n' "$*" >> "$RSYNC_TEST_STATE/sleeps.log"
''')
    _make_executable(stubs / "rsync", r'''#!/bin/bash
set -euo pipefail
src_index=$(($# - 1))
src=${!src_index}
dst=${!#}
case "$src" in
  */MANIFEST.sha256) phase=manifest ;;
  */backup.log) phase=log ;;
  */) phase=payload ;;
  *) exit 96 ;;
esac
printf '%s\n' "$*" >> "$RSYNC_TEST_STATE/rsync-attempts.log"
count_file="$RSYNC_TEST_STATE/$phase.count"
count=0
[ ! -f "$count_file" ] || read -r count < "$count_file"
count=$((count + 1))
printf '%s\n' "$count" > "$count_file"
if [ "$phase" = "$RSYNC_FAIL_PHASE" ] && [ "$count" -le "$RSYNC_FAIL_COUNT" ]; then
  if [ "$RSYNC_FAIL_AFTER_COPY" = 1 ]; then
    "$RSYNC_TEST_STATE/rsync-success" "$@"
    if [ "${RSYNC_POISON_RESIDUE:-}" = 1 ] && [ "$phase" = payload ]; then
      "$PYTHON_BIN" -I - "$dst/audio/aud-test.webm" <<'PY'
import os
from pathlib import Path
import sys

target = Path(sys.argv[1])
meta = target.stat()
target.write_bytes(b"x" * meta.st_size)
os.utime(target, ns=(meta.st_atime_ns, meta.st_mtime_ns))
PY
    fi
  fi
  printf '%s\n' 'PARTIAL-UNTRUSTED-RSYNC-STDOUT'
  printf '%s\n' "$RSYNC_FAIL_ERROR" >&2
  exit "$RSYNC_FAIL_RC"
fi
exec "$RSYNC_TEST_STATE/rsync-success" "$@"
''')
    return stubs


_RSYNC_PHASE_CODES = [
    ("manifest", "manifest_transfer_failed"),
    ("payload", "snapshot_transfer_failed"),
    ("log", "remote_log_transfer_failed"),
]


@pytest.mark.parametrize(("phase", "code"), _RSYNC_PHASE_CODES)
def test_pull_retries_rsync_transport_then_completes_verified_snapshot(tmp_path, phase, code):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp)
    stubs = _inject_rsync_failure(env, phase=phase, failures=2,
                                 error="Connection reset by peer PRIVATE-ID", after_copy=True)

    completed = _run_pull(project, env)

    assert completed.returncode == 0, completed.stderr
    assert (stubs / f"{phase}.count").read_text().strip() == "3"
    assert (stubs / "sleeps.log").read_text().splitlines() == ["2", "5"]
    log = (local / "pull.log").read_text()
    assert log.count(f"retry code={code}") == 2
    assert f"recovered code={code}" in log and "attempts=3" in log
    assert " FAIL " not in log
    assert " ok snapshots=1 pulled=1 unchanged=0" in log.splitlines()[-1]
    assert "PRIVATE-ID" not in log + completed.stdout + completed.stderr
    assert "PARTIAL-UNTRUSTED" not in log + completed.stdout + completed.stderr
    assert stamp in (local / "audit-anchors.log").read_text()
    assert (local / "daily" / stamp / "MANIFEST.sha256").is_file()
    assert (local / "backup.log").read_bytes() == (remote / "backup.log").read_bytes()
    assert not (local / ".pull.lock").exists()
    assert not list((local / ".incoming").iterdir())
    assert not list((local / "quarantine").iterdir())
    for arguments in (stubs / "rsync-attempts.log").read_text().splitlines():
        assert "--checksum" in arguments.split()
        assert "StrictHostKeyChecking=yes" in arguments


@pytest.mark.parametrize(("phase", "code"), _RSYNC_PHASE_CODES)
def test_pull_exhausted_rsync_retries_preserve_archives_and_report_progress(tmp_path, phase, code):
    project, remote, local, env = _pull_harness(tmp_path)
    older = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d-%H%M%S")
    newer = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, older, b"retained-recording")
    assert _run_pull(project, env).returncode == 0
    old_archive = local / "daily" / older
    before = {p.relative_to(old_archive): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in old_archive.rglob("*") if p.is_file()}
    anchors_before = (local / "audit-anchors.log").read_bytes()
    old_log = (local / "backup.log").read_bytes()
    log_before = (local / "pull.log").read_text()
    _create_remote_snapshot(remote, newer)
    (remote / "backup.log").write_text("new remote log\n")
    stubs = _inject_rsync_failure(env, phase=phase, failures=20,
                                 error="Connection timed out PRIVATE-ID", after_copy=True)

    completed = _run_pull(project, env)

    assert completed.returncode == 255
    assert (stubs / f"{phase}.count").read_text().strip() == "3"
    assert (stubs / "sleeps.log").read_text().splitlines() == ["2", "5"]
    log = (local / "pull.log").read_text()[len(log_before):]
    assert log.count(" FAIL ") == 1
    assert f"FAIL code={code}" in log
    assert "reason=transport_timeout attempts=3" in log
    progress = "pulled=1 unchanged=1" if phase == "log" else "pulled=0 unchanged=0"
    assert f"failed snapshots=unverified {progress} rc=255" in log.splitlines()[-1]
    assert "PRIVATE-ID" not in log + completed.stdout + completed.stderr
    assert "PARTIAL-UNTRUSTED" not in log + completed.stdout + completed.stderr
    assert not (local / ".pull.lock").exists()
    assert not list((local / ".incoming").iterdir())
    assert list((local / "quarantine").iterdir())
    after = {p.relative_to(old_archive): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in old_archive.rglob("*") if p.is_file()}
    assert before == after
    assert (local / "backup.log").read_bytes() == old_log
    if phase == "log":
        assert (local / "daily" / newer).is_dir()
        assert (local / "audit-anchors.log").read_bytes().startswith(anchors_before)
    else:
        assert not (local / "daily" / newer).exists()
        assert (local / "audit-anchors.log").read_bytes() == anchors_before


@pytest.mark.parametrize(("phase", "code"), _RSYNC_PHASE_CODES)
@pytest.mark.parametrize(("error", "reason"), [
    ("Permission denied (publickey). Connection closed PRIVATE-ID", "authentication"),
    ("Host key verification failed. Connection closed PRIVATE-ID", "host_key_verification"),
    ("Bad configuration option: PRIVATE-ID", "ssh_configuration"),
    ("unrecognized PRIVATE-ID", "rsync_error_unclassified"),
])
def test_pull_permanent_or_unknown_rsync_failure_does_not_retry_or_leak(
        tmp_path, phase, code, error, reason):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp)
    stubs = _inject_rsync_failure(env, phase=phase, failures=20, error=error)

    completed = _run_pull(project, env)

    assert completed.returncode == 255
    assert (stubs / f"{phase}.count").read_text().strip() == "1"
    assert not (stubs / "sleeps.log").exists()
    log = (local / "pull.log").read_text()
    assert log.count(" FAIL ") == 1 and f"FAIL code={code}" in log
    assert f"reason={reason} attempts=1" in log
    assert "failed snapshots=unverified" in log.splitlines()[-1]
    assert "PRIVATE-ID" not in log + completed.stdout + completed.stderr
    assert "PARTIAL-UNTRUSTED" not in log + completed.stdout + completed.stderr
    assert not (local / ".pull.lock").exists()
    assert not list((local / ".incoming").iterdir())
    assert (local / "daily" / stamp).exists() is (phase == "log")


@pytest.mark.parametrize(("rc", "error", "reason", "retry"), [
    (10, "Connection refused PRIVATE-ID", "transport_disconnected", True),
    (12, "Broken pipe PRIVATE-ID", "transport_disconnected", True),
    (30, "PRIVATE-ID", "transport_timeout", True),
    (35, "PRIVATE-ID", "transport_timeout", True),
    (255, "Network is unreachable PRIVATE-ID", "network_unavailable", True),
    (10, "unrecognized PRIVATE-ID", "rsync_error_unclassified", False),
    (12, "connection unexpectedly closed PRIVATE-ID", "rsync_error_unclassified", False),
    (12, "rsync: command not found; Broken pipe PRIVATE-ID", "rsync_nontransport_failure", False),
    (12, "protocol version mismatch; Connection closed PRIVATE-ID", "rsync_nontransport_failure", False),
    (30, "Permission denied; Connection timed out PRIVATE-ID", "authentication", False),
    (35, "Host key verification failed PRIVATE-ID", "host_key_verification", False),
    (255, "checksum mismatch; Broken pipe PRIVATE-ID", "rsync_nontransport_failure", False),
    (23, "Connection timed out PRIVATE-ID", "rsync_error_unclassified", False),
    (3, "Error selecting input/output file PRIVATE-ID", "rsync_error_unclassified", False),
    (4, "Requested action not supported PRIVATE-ID", "rsync_error_unclassified", False),
    (5, "Error starting client-server protocol PRIVATE-ID", "rsync_error_unclassified", False),
])
def test_pull_rsync_exit_codes_require_confirmed_transport_failure(
        tmp_path, rc, error, reason, retry):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp)
    stubs = _inject_rsync_failure(env, phase="manifest", failures=1, error=error, rc=rc)

    completed = _run_pull(project, env)

    expected_rc = 0 if retry else 2 if rc in (3, 4, 5) else rc
    assert completed.returncode == expected_rc, completed.stderr
    assert (stubs / "manifest.count").read_text().strip() == ("2" if retry else "1")
    log = (local / "pull.log").read_text()
    assert f"reason={reason}" in log
    assert ("recovered code=manifest_transfer_failed" in log) is retry
    if not retry:
        assert f"rc={rc} reason={reason}" in log
        assert "failed snapshots=unverified" in log.splitlines()[-1]
    assert "PRIVATE-ID" not in log + completed.stdout + completed.stderr
    assert not (local / ".pull.lock").exists()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_pull_rsync_retry_rechecks_same_size_mtime_residue_with_real_rsync(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp, b"good-audio")
    stubs = _inject_rsync_failure(env, phase="payload", failures=1,
                                 error="Broken pipe", after_copy=True)
    # Use the actual rsync engine with local paths; no SSH/network is involved.
    env["REAL_RSYNC"] = shutil.which("rsync") or "rsync"
    env["REAL_RSYNC_PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    env["RSYNC_POISON_RESIDUE"] = "1"
    _make_executable(stubs / "rsync-success", r'''#!/bin/bash
set -euo pipefail
args=()
while [ "$#" -gt 2 ]; do
  if [ "$1" = -e ]; then shift 2
  else args+=("$1"); shift
  fi
done
remote_path=${1#*:}
relative=${remote_path#"$NMU_BACKUP_REMOTE_ROOT"}
# The local rsync receiver also resolves rsync via PATH; keep it out of the shim.
exec env PATH="$REAL_RSYNC_PATH" "$REAL_RSYNC" "${args[@]}" "$FAKE_REMOTE_ROOT$relative" "$2"
''')

    completed = _run_pull(project, env)

    assert completed.returncode == 0, completed.stderr
    assert (local / "daily" / stamp / "audio" / "aud-test.webm").read_bytes() == b"good-audio"
    assert (stubs / "payload.count").read_text().strip() == "2"
    assert "recovered code=snapshot_transfer_failed" in (local / "pull.log").read_text()
    assert not (local / ".pull.lock").exists()


@pytest.mark.parametrize(("phase", "code"), _RSYNC_PHASE_CODES[:2])
def test_pull_rsync_recovery_does_not_bypass_manifest_integrity(tmp_path, phase, code):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    snapshot = _create_remote_snapshot(remote, stamp)
    (snapshot / "audio" / "aud-test.webm").write_bytes(b"wrong")
    _inject_rsync_failure(env, phase=phase, failures=1, error="Broken pipe")

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    log = (local / "pull.log").read_text()
    assert f"recovered code={code}" in log
    assert "FAIL code=snapshot_verify_failed" in log
    assert "retry code=snapshot_verify_failed" not in log
    assert not (local / "daily" / stamp).exists()
    assert list((local / "quarantine").iterdir())


@pytest.mark.parametrize(("phase", "start", "code"), [
    ("list", 1, "remote_snapshot_list_failed"),
    ("facts", 1, "remote_snapshot_facts_failed"),
    ("log", 1, "remote_log_probe_failed"),
    ("facts", 2, "remote_snapshot_reprobe_failed"),
])
def test_pull_retries_transient_ssh_then_completes_verified_snapshot(
        tmp_path, phase, start, code):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp)
    stubs = _inject_ssh_failure(env, phase=phase, start=start, failures=2,
                               error="Connection reset by peer PRIVATE-ID")

    completed = _run_pull(project, env)

    assert completed.returncode == 0, completed.stderr
    assert (local / "daily" / stamp / "MANIFEST.sha256").is_file()
    assert stamp in (local / "audit-anchors.log").read_text()
    log = (local / "pull.log").read_text()
    assert log.count(f"retry code={code}") == 2
    assert f"recovered code={code}" in log
    assert "attempts=3" in log
    assert " FAIL " not in log
    assert " ok snapshots=1 pulled=1 unchanged=0" in log.splitlines()[-1]
    assert "PRIVATE-ID" not in log + completed.stdout + completed.stderr
    assert "PARTIAL-UNTRUSTED" not in log + completed.stdout + completed.stderr
    assert (stubs / "sleeps.log").read_text().splitlines() == ["2", "5"]
    assert not (local / ".pull.lock").exists()
    for call in (stubs / "ssh-args.log").read_text().split("\0")[:-1]:
        assert "StrictHostKeyChecking=yes" in call
        assert "ConnectTimeout=15" in call
        assert "ServerAliveInterval=15" in call
        assert "ServerAliveCountMax=2" in call


@pytest.mark.parametrize("reprobe", [False, True])
def test_pull_exhausted_ssh_retries_fail_with_footer_and_preserve_archives(tmp_path, reprobe):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp)
    assert _run_pull(project, env).returncode == 0
    before = {p.relative_to(local / "daily"): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (local / "daily").rglob("*") if p.is_file()}
    anchors_before = (local / "audit-anchors.log").read_bytes()
    log_before = (local / "pull.log").read_text()
    stubs = _inject_ssh_failure(env, phase="facts", start=2 if reprobe else 1,
                               failures=20, error="Connection timed out PRIVATE-ID")

    completed = _run_pull(project, env)

    assert completed.returncode == 255
    assert (stubs / "facts.count").read_text().strip() == ("4" if reprobe else "3")
    assert (stubs / "sleeps.log").read_text().splitlines() == ["2", "5"]
    log = (local / "pull.log").read_text()[len(log_before):]
    assert log.count(" FAIL ") == 1
    assert "reason=transport_timeout attempts=3" in log
    assert "failed snapshots=unverified pulled=0 unchanged=0 rc=255" in log.splitlines()[-1]
    assert "PRIVATE-ID" not in log + completed.stdout + completed.stderr
    assert not (local / ".pull.lock").exists()
    assert not list((local / ".incoming").iterdir())
    assert bool(list((local / "quarantine").iterdir())) is reprobe
    after = {p.relative_to(local / "daily"): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in (local / "daily").rglob("*") if p.is_file()}
    assert before == after
    assert (local / "audit-anchors.log").read_bytes() == anchors_before


@pytest.mark.parametrize(("error", "rc", "reason"), [
    ("Host key verification failed. Connection closed PRIVATE-ID", 255, "host_key_verification"),
    ("Permission denied (publickey). PRIVATE-ID", 255, "authentication"),
    ("Bad configuration option: PRIVATE-ID", 255, "ssh_configuration"),
    ("unrecognized PRIVATE-ID", 255, "ssh_error_unclassified"),
    ("Connection timed out PRIVATE-ID", 2, "remote_command_failed"),
])
def test_pull_permanent_or_unknown_ssh_failure_does_not_retry_or_leak(
        tmp_path, error, rc, reason):
    project, _remote, local, env = _pull_harness(tmp_path)
    stubs = _inject_ssh_failure(env, phase="list", failures=20, error=error, rc=rc)

    completed = _run_pull(project, env)

    assert completed.returncode == rc
    assert (stubs / "list.count").read_text().strip() == "1"
    assert not (stubs / "sleeps.log").exists()
    assert not (local / ".pull.lock").exists()
    log = (local / "pull.log").read_text()
    assert f"reason={reason} attempts=1" in log
    assert "failed snapshots=unverified" in log.splitlines()[-1]
    assert "PRIVATE-ID" not in log + completed.stdout + completed.stderr
    assert not Path(env["TRANSFER_LOG"]).exists()


def test_pull_transient_ssh_recovery_still_rejects_manifest_corruption(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    snapshot = _create_remote_snapshot(remote, stamp)
    # Same file/byte facts pass; only the real manifest content verifier catches it.
    (snapshot / "audio" / "aud-test.webm").write_bytes(b"wrong")
    _inject_ssh_failure(env, phase="facts", start=2, failures=1,
                        error="Broken pipe")

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert not (local / "daily" / stamp).exists()
    assert any((local / "quarantine").iterdir())
    log = (local / "pull.log").read_text()
    assert "recovered code=remote_snapshot_reprobe_failed" in log
    assert "FAIL code=snapshot_verify_failed" in log
    assert "failed snapshots=unverified" in log.splitlines()[-1]


def test_macos_offsite_wrapper_reports_failed_footer_without_sending(tmp_path):
    installer = (ROOT / "scripts" / "install-macos-offsite-pull.sh").read_text()
    match = re.search(r"grep -E '( [^']+ snapshots=)'", installer)
    assert match is not None
    failure = "[2026-10-08 12:57:17] failed snapshots=unverified pulled=0 unchanged=0 rc=255"
    checked = subprocess.run(["grep", "-E", match.group(1)], input=failure + "\n",
                             capture_output=True, text=True, check=False)
    assert checked.returncode == 0
    assert checked.stdout.strip() == failure


def test_pull_new_replay_and_same_name_conflict_preserves_local(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp, b"original")

    first = _run_pull(project, env)
    assert first.returncode == 0, first.stderr
    local_audio = local / "daily" / stamp / "audio" / "aud-test.webm"
    original_hash = hashlib.sha256(local_audio.read_bytes()).hexdigest()

    replay = _run_pull(project, env)
    assert replay.returncode == 0, replay.stderr
    assert hashlib.sha256(local_audio.read_bytes()).hexdigest() == original_hash

    shutil.rmtree(remote / "daily" / stamp)
    _create_remote_snapshot(remote, stamp, b"tampered-and-remanifested")
    conflict = _run_pull(project, env)
    assert conflict.returncode != 0
    assert hashlib.sha256(local_audio.read_bytes()).hexdigest() == original_hash
    assert any((local / "conflicts").iterdir())

    transfer_count = len(Path(env["TRANSFER_LOG"]).read_text().splitlines())
    unresolved = _run_pull(project, env)
    assert unresolved.returncode != 0
    # 2026-08-07 语义:未决 conflict 不再中止整个 run(其余快照照常归档、每天
    # partial+告警),但这一份一个字节都不许再拉——唯一允许的新传输是
    # backup.log 审计视图刷新。
    new_transfers = Path(env["TRANSFER_LOG"]).read_text().splitlines()[transfer_count:]
    assert all(line.endswith("/backup.log") for line in new_transfers)
    assert stamp not in "".join(new_transfers)
    assert hashlib.sha256(local_audio.read_bytes()).hexdigest() == original_hash


def test_pull_files_broken_audit_chain_snapshot_into_conflicts(tmp_path):
    """伪造审计行+重写 manifest 让传输层全绿——唯一拦得住的就是锚定层。"""
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    snapshot = _create_remote_snapshot(remote, stamp, b"payload")
    conn = sqlite3.connect(snapshot / "app.db")
    conn.execute(
        "INSERT INTO auditlog (ts,actor,action,summary,prev_hash,entry_hash)"
        " VALUES ('2026-01-01 00:00:00','forger','score_lock','forged',?,?)",
        ("f" * 64, "e" * 64))
    conn.commit()
    conn.close()
    _write_manifest(snapshot)

    completed = _run_pull(project, env)

    assert completed.returncode == 4          # partial:证据已归档并要求告警
    log = (local / "pull.log").read_text()
    assert "code=audit_anchor_check_failed" in log
    assert "problem=chain_broken" in log
    assert not list((local / "daily").iterdir())
    assert any((local / "conflicts").iterdir())
    # 锚点账不给被拒快照记账
    anchors = local / "audit-anchors.log"
    assert not anchors.exists() or stamp not in anchors.read_text()


def test_pull_rejects_empty_remote_set(tmp_path):
    project, _remote, local, env = _pull_harness(tmp_path)

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert not any((local / "daily").iterdir())


def test_pull_rejects_symlink_snapshot_and_symlink_log(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    outside = tmp_path / "outside"
    outside.mkdir()
    (remote / "daily" / stamp).symlink_to(outside, target_is_directory=True)
    snapshot_link = _run_pull(project, env)
    assert snapshot_link.returncode != 0
    assert not any((local / "daily").iterdir())

    (remote / "daily" / stamp).unlink()
    _create_remote_snapshot(remote, stamp)
    (remote / "backup.log").unlink()
    (remote / "backup.log").symlink_to(tmp_path / "untrusted-log")
    log_link = _run_pull(project, env)
    assert log_link.returncode != 0
    assert not any((local / "daily").iterdir())


def test_pull_remote_flood_never_deletes_last_known_good(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    old_stamp = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d-%H%M%S")
    old = local / "daily" / old_stamp
    old.mkdir(parents=True)
    marker = old / "immutable.marker"
    marker.write_bytes(b"last-known-good")
    old_hash = hashlib.sha256(marker.read_bytes()).hexdigest()
    for offset in range(15):
        stamp = (datetime.now() - timedelta(minutes=offset + 1)).strftime("%Y%m%d-%H%M%S")
        (remote / "daily" / stamp).mkdir()

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert hashlib.sha256(marker.read_bytes()).hexdigest() == old_hash
    assert not Path(env["TRANSFER_LOG"]).exists()


def test_pull_bad_manifest_is_never_promoted(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    snapshot = _create_remote_snapshot(remote, stamp)
    (snapshot / "MANIFEST.sha256").write_text(
        "0" * 64 + "  ./app.db\n", encoding="utf-8"
    )
    (snapshot / "MANIFEST.sha256").chmod(0o600)

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert not (local / "daily" / stamp).exists()
    assert any((local / "quarantine").iterdir())


def test_pull_suppresses_raw_transfer_error_identifiers(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp)
    secret = "SYNTHETIC-PATIENT-ID-IN-RSYNC-ERROR"
    env["FAIL_RSYNC_SECRET"] = secret

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert secret not in completed.stdout
    assert secret not in completed.stderr
    assert secret not in (local / "pull.log").read_text(encoding="utf-8")


def test_pull_rejects_oversize_snapshot_before_transfer(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp)
    env["NMU_BACKUP_MAX_SNAPSHOT_BYTES"] = "1"

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert not Path(env["TRANSFER_LOG"]).exists()
    assert not any((local / "daily").iterdir())
    assert "remote_snapshot_over_limit" in (local / "pull.log").read_text()


def test_pull_rejects_aggregate_transfer_set_before_transfer(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    for minutes in (5, 6):
        stamp = (datetime.now() - timedelta(minutes=minutes)).strftime(
            "%Y%m%d-%H%M%S"
        )
        _create_remote_snapshot(remote, stamp)
    env["NMU_BACKUP_MAX_PULL_BYTES"] = "1"

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert not Path(env["TRANSFER_LOG"]).exists()
    assert not any((local / "daily").iterdir())
    assert "remote_pull_set_over_limit" in (local / "pull.log").read_text()


def test_pull_rejects_remote_hardlink_before_transfer(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    snapshot = _create_remote_snapshot(remote, stamp)
    os.link(snapshot / "config" / "env", snapshot / "config" / "linked-env")

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert not Path(env["TRANSFER_LOG"]).exists()
    assert not any((local / "daily").iterdir())
    assert "remote_snapshot_facts_invalid" in (local / "pull.log").read_text()


def test_pull_rejects_remote_directory_flood_before_transfer(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp)
    env["NMU_BACKUP_MAX_SNAPSHOT_DIRECTORIES"] = "2"

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert not Path(env["TRANSFER_LOG"]).exists()
    assert not any((local / "daily").iterdir())
    assert "remote_snapshot_over_limit" in (local / "pull.log").read_text()


def test_pull_rejects_aggregate_directory_budget_before_transfer(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    for minutes in (5, 6):
        stamp = (datetime.now() - timedelta(minutes=minutes)).strftime(
            "%Y%m%d-%H%M%S"
        )
        _create_remote_snapshot(remote, stamp)
    env["NMU_BACKUP_MAX_PULL_DIRECTORIES"] = "5"

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert not Path(env["TRANSFER_LOG"]).exists()
    assert not any((local / "daily").iterdir())
    assert "remote_pull_set_over_limit" in (local / "pull.log").read_text()


def test_pull_reprobe_rejects_post_probe_unlisted_file_and_directory_flood(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp)
    env["INJECT_UNLISTED_AFTER_PREFLIGHT"] = "1"

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert not (local / "daily" / stamp).exists()
    assert not list(local.rglob("not-in-manifest.bin"))
    assert not list((local / ".incoming").glob(".files-from.*"))
    assert not list((local / ".incoming").glob(".data-files-from.*"))
    assert "snapshot_changed_during_transfer" in (
        local / "pull.log"
    ).read_text()


def test_pull_reprobe_rejects_post_probe_empty_directory(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp)
    env["INJECT_UNLISTED_AFTER_PREFLIGHT"] = "empty"

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert not (local / "daily" / stamp).exists()
    assert not list(local.rglob("unlisted-empty-directory"))
    assert "snapshot_changed_during_transfer" in (
        local / "pull.log"
    ).read_text()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_real_rsync_nul_allowlist_reconstructs_legal_parent_directories(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    nested = source / "exports" / "simulation" / "BATCH" / "session.csv"
    nested.parent.mkdir(parents=True)
    nested.write_text("synthetic\n", encoding="utf-8")
    destination.mkdir()
    allowlist = tmp_path / "files-from"
    allowlist.write_bytes(b"./exports/simulation/BATCH/session.csv\0")

    completed = subprocess.run(
        [
            shutil.which("rsync") or "rsync",
            "-a", "-x", "--no-links", "--no-devices", "--no-specials",
            "--no-implied-dirs", "--from0", f"--files-from={allowlist}",
            f"{source}/", f"{destination}/",
        ],
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode == 0, completed.stderr
    copied = destination / "exports" / "simulation" / "BATCH" / "session.csv"
    assert copied.read_text(encoding="utf-8") == "synthetic\n"


@pytest.mark.parametrize(
    "empty_path",
    [
        "exports/analysis/ANALYSIS-v1-ORPHAN",
        "exports/staging/STAGING-v1-ORPHAN",
        "exports/analysis",
    ],
)
def test_pull_rejects_remote_empty_directory_not_covered_by_manifest(
        empty_path, tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    snapshot = _create_remote_snapshot(remote, stamp)
    (snapshot / empty_path).mkdir(parents=True, exist_ok=True)

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert not (local / "daily" / stamp).exists()
    assert "manifest_plan_mismatch" in (local / "pull.log").read_text()


def test_pull_accepts_minimal_snapshot_without_audio_or_exports(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_minimal_remote_snapshot(remote, stamp)

    completed = _run_pull(project, env)

    assert completed.returncode == 0, completed.stderr
    snapshot = local / "daily" / stamp
    assert snapshot.is_dir()
    assert not (snapshot / "audio").exists()
    assert not (snapshot / "exports").exists()
    assert not (snapshot / "controlled-audio-exports").exists()


def test_pull_rejects_manifest_changed_to_symlink_without_touching_target(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp)
    sentinel = tmp_path / "local-mode-sentinel"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    sentinel.chmod(0o644)
    env["REPLACE_MANIFEST_WITH_SYMLINK_ON_TRANSFER"] = "1"
    env["MANIFEST_SYMLINK_TARGET"] = str(sentinel)

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o644
    assert not (local / "daily" / stamp).exists()
    assert "manifest_not_regular" in (local / "pull.log").read_text()


@pytest.mark.parametrize("unsafe_root", ["", "/", "/.."])
def test_pull_rejects_empty_or_filesystem_root_local_destination(
        unsafe_root, tmp_path):
    project, _remote, _local, env = _pull_harness(tmp_path)
    env["NMU_BACKUP_LOCAL_ROOT"] = unsafe_root

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert "本地备份" in completed.stderr
    assert not Path(env["TRANSFER_LOG"]).exists()


def test_pull_rejects_received_tree_that_changed_after_preflight(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp)
    env["MUTATE_AFTER_PREFLIGHT"] = "1"
    rsync_stub = Path(env["PATH"].split(":", 1)[0]) / "rsync"
    body = rsync_stub.read_text(encoding="utf-8")
    body = body.replace(
        'printf \'%s\\n\' "$src" >> "$TRANSFER_LOG"',
        'printf \'%s\\n\' "$src" >> "$TRANSFER_LOG"\n'
        'if [ "${MUTATE_AFTER_PREFLIGHT:-}" = 1 ] && [[ "$src" == */ ]]; then\n'
        '  printf changed >> "${FAKE_REMOTE_ROOT}/daily/' + stamp
        + '/audio/aud-test.webm"\n'
        'fi',
    )
    rsync_stub.write_text(body, encoding="utf-8")
    rsync_stub.chmod(0o755)

    completed = _run_pull(project, env)

    assert completed.returncode != 0
    assert not (local / "daily" / stamp).exists()
    assert "snapshot_changed_during_transfer" in (local / "pull.log").read_text()


def _snapshot_facts_awk() -> str:
    text = (ROOT / "scripts" / "vps-backup-pull.sh").read_text(encoding="utf-8")
    match = re.search(r"^SNAPSHOT_FACTS_AWK='([^']*)'$", text, re.M)
    assert match, "vps-backup-pull.sh 里找不到远端事实的 awk 程序 SNAPSHOT_FACTS_AWK"
    return match.group(1)


@pytest.mark.skipif(shutil.which("awk") is None, reason="awk not installed")
@pytest.mark.parametrize(("lines", "expected"), [
    (["d 4096 3", "f 100 1", "f 200 2 audio/aud-1.webm", "d 4096 2"], "2 300 2 0"),
    (["d 4096 3", "f 200 2 audio/aud-1.webm.m4a"], "1 200 1 0"),
    (["d 4096 3", "f 200 2 app.db"], "0 0 1 1"),
    (["d 4096 3", "f 200 2 MANIFEST.sha256"], "0 0 1 1"),
    (["d 4096 3", "f 200 2 config/env"], "0 0 1 1"),
    (["d 4096 3", "f 200 2 exports/research/BATCH/audio_manifest.csv"], "0 0 1 1"),
    (["d 4096 3", "f 200 2 audio/nested/aud-1.webm"], "0 0 1 1"),
    (["d 4096 3", "f 200 2 audio/.aud-1.webm.link"], "0 0 1 1"),
    (["d 4096 3", "f 200 2 audio/aud 1.webm"], "0 0 1 1"),
    (["d 4096 3", "f -200 2 audio/aud-1.webm"], "0 0 1 1"),
    (["d 4096 3", "f 200 2"], "0 0 1 1"),
    (["d 4096 3", "l 10 1"], "0 0 1 1"),
])
def test_remote_facts_allow_multi_link_files_only_as_top_level_safe_audio(lines, expected):
    completed = subprocess.run(
        ["awk", _snapshot_facts_awk()], input="\n".join(lines) + "\n",
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == expected


def _link_remote_audio(remote: Path, older_stamp: str, newer_stamp: str) -> None:
    """VPS 打开录音去重后的样子：较新快照的录音是较旧那份的硬链接。"""
    older = remote / "daily" / older_stamp / "audio" / "aud-test.webm"
    newer = remote / "daily" / newer_stamp / "audio" / "aud-test.webm"
    newer.unlink()
    os.link(older, newer)


def test_pull_lands_sibling_linked_remote_audio_as_independent_strict_files(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    older = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d-%H%M%S")
    newer = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    for stamp in (older, newer):
        _create_remote_snapshot(remote, stamp, b"unchanged-recording")
    _link_remote_audio(remote, older, newer)

    completed = _run_pull(project, env)

    assert completed.returncode == 0, completed.stderr
    landed = [local / "daily" / stamp / "audio" / "aud-test.webm" for stamp in (older, newer)]
    assert [path.stat().st_nlink for path in landed] == [1, 1]
    assert landed[0].stat().st_ino != landed[1].stat().st_ino
    for stamp in (older, newer):
        verified = subprocess.run(
            [sys.executable, "-I", str(GUARD_SOURCE), "verify-vps",
             str(local / "daily" / stamp)],
            check=False, capture_output=True, text=True,
        )
        assert verified.returncode == 0, verified.stderr


def test_pull_redownloads_and_compares_an_archived_stamp_by_default(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    env.pop("NMU_BACKUP_PULL_SKIP_ARCHIVED", None)
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp, b"archived-recording")
    assert _run_pull(project, env).returncode == 0
    transfers = Path(env["TRANSFER_LOG"])
    before = len(transfers.read_text().splitlines())

    replay = _run_pull(project, env)

    assert replay.returncode == 0, replay.stderr
    remote_prefix = (f"{env['NMU_BACKUP_SSH_USER']}@{env['NMU_BACKUP_SSH_HOST']}:"
                     f"{env['NMU_BACKUP_REMOTE_ROOT']}")
    assert f"{remote_prefix}/daily/{stamp}/" in transfers.read_text().splitlines()[before:]
    assert re.search(r" ok snapshots=1 pulled=0 unchanged=1 used_mb=[0-9]+$",
                     (local / "pull.log").read_text().splitlines()[-1])


def test_pull_skips_the_payload_of_an_already_archived_unchanged_stamp(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    env["NMU_BACKUP_PULL_SKIP_ARCHIVED"] = "1"
    stamp = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, stamp, b"archived-recording")
    assert _run_pull(project, env).returncode == 0
    transfers = Path(env["TRANSFER_LOG"])
    before = len(transfers.read_text().splitlines())

    replay = _run_pull(project, env)

    assert replay.returncode == 0, replay.stderr
    remote_prefix = (f"{env['NMU_BACKUP_SSH_USER']}@{env['NMU_BACKUP_SSH_HOST']}:"
                     f"{env['NMU_BACKUP_REMOTE_ROOT']}")
    assert transfers.read_text().splitlines()[before:] == [
        f"{remote_prefix}/daily/{stamp}/MANIFEST.sha256",
        f"{remote_prefix}/backup.log",
    ]
    assert re.search(r" ok snapshots=1 pulled=0 unchanged=1 used_mb=[0-9]+$",
                     (local / "pull.log").read_text().splitlines()[-1])


def test_offsite_installer_writes_the_skip_switch_off_unless_asked(tmp_path):
    text = (ROOT / "scripts" / "install-macos-offsite-pull.sh").read_text(encoding="utf-8")
    start = text.index('cat > "$ROOT/config.env" <<EOF')
    config_lines = text[text.index("\n", start) + 1:text.index("\nEOF\n", start)].splitlines()
    assert "NMU_BACKUP_PULL_SKIP_ARCHIVED='$SKIP_ARCHIVED'" in config_lines
    gate = text[text.index('SKIP_ARCHIVED="'):text.index("esac\n", text.index('SKIP_ARCHIVED="')) + 5]
    probe = gate + 'printf %s "$SKIP_ARCHIVED"\n'
    base = {key: value for key, value in os.environ.items()
            if key != "NMU_BACKUP_PULL_SKIP_ARCHIVED"}

    def run(**extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", "-c", probe], env={**base, **extra},
                              check=False, capture_output=True, text=True)

    assert (run().returncode, run().stdout) == (0, "0")
    assert (run(NMU_BACKUP_PULL_SKIP_ARCHIVED="1").stdout) == "1"
    assert run(NMU_BACKUP_PULL_SKIP_ARCHIVED="yes").returncode == 64
    assert run(NMU_BACKUP_PULL_SKIP_ARCHIVED="1' x='").returncode == 64


def test_pull_copies_unchanged_bytes_from_the_newest_local_archive(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    args_log = tmp_path / "rsync-args.log"
    env["RSYNC_ARGS_LOG"] = str(args_log)
    older = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d-%H%M%S")
    newer = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    _create_remote_snapshot(remote, older, b"unchanged-recording")
    assert _run_pull(project, env).returncode == 0
    first = args_log.read_text().splitlines()
    assert not any("--copy-dest" in line for line in first)

    _create_remote_snapshot(remote, newer, b"unchanged-recording")
    second = _run_pull(project, env)

    assert second.returncode == 0, second.stderr
    data_calls = [line for line in args_log.read_text().splitlines()[len(first):]
                  if f"/daily/{newer}/ " in line]
    assert len(data_calls) == 1
    arguments = data_calls[0].split()
    assert "--checksum" in arguments
    assert f"--copy-dest={(local / 'daily' / older).resolve()}" in arguments


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_real_rsync_copy_dest_with_checksum_lands_correct_independent_files(tmp_path):
    source, basis, destination = (tmp_path / name for name in ("source", "basis", "destination"))
    for directory in (source / "audio", basis / "audio", destination):
        directory.mkdir(parents=True)
    (source / "audio" / "same.webm").write_bytes(b"unchanged-recording")
    shutil.copy2(source / "audio" / "same.webm", basis / "audio" / "same.webm")
    # 同长同时间、字节不同：只比长度+时间的话会把基底的旧字节当成新快照的。
    (source / "app.db").write_bytes(b"database-v2")
    (basis / "app.db").write_bytes(b"database-v1")
    source_time = (source / "app.db").stat()
    os.utime(basis / "app.db", ns=(source_time.st_atime_ns, source_time.st_mtime_ns))
    allowlist = tmp_path / "files-from"
    allowlist.write_bytes(b"./app.db\0./audio/same.webm\0")

    completed = subprocess.run(
        [
            shutil.which("rsync") or "rsync",
            "-a", "-x", "--no-links", "--no-devices", "--no-specials", "--no-implied-dirs",
            "--checksum", f"--copy-dest={basis}",
            "--from0", f"--files-from={allowlist}", f"{source}/", f"{destination}/",
        ],
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert (destination / "app.db").read_bytes() == b"database-v2"
    assert (destination / "audio" / "same.webm").read_bytes() == b"unchanged-recording"
    for name in ("app.db", "audio/same.webm"):
        assert (destination / name).stat().st_nlink == 1
        assert (destination / name).stat().st_ino != (basis / name).stat().st_ino


def _gnu_find() -> str | None:
    for candidate in ("gfind", "find"):
        path = shutil.which(candidate)
        if path is None:
            continue
        probe = subprocess.run([path, "--version"], check=False, capture_output=True, text=True)
        if probe.returncode == 0 and "GNU findutils" in probe.stdout:
            return path
    return None


@pytest.mark.skipif(_gnu_find() is None, reason="needs GNU find -printf, as on the VPS")
def test_real_remote_facts_command_accepts_linked_audio_and_rejects_other_links(tmp_path):
    project, remote, local, env = _pull_harness(tmp_path)
    finder = tmp_path / "gnu-find"
    finder.mkdir()
    (finder / "find").symlink_to(_gnu_find())
    ssh_stub = Path(env["PATH"].split(":", 1)[0]) / "ssh"
    body = ssh_stub.read_text(encoding="utf-8")
    ssh_stub.write_text(body.replace(
        'command_arg="${!#}"\n',
        'command_arg="${!#}"\n'
        'if [[ "$command_arg" == *"NMU_SNAPSHOT_FACTS_CONTRACT=1"* ]]; then\n'
        '  PATH="$GNU_FIND_DIR:$PATH" exec bash -c '
        '"${command_arg//$NMU_BACKUP_REMOTE_ROOT/$FAKE_REMOTE_ROOT}"\n'
        'fi\n', 1), encoding="utf-8")
    env["GNU_FIND_DIR"] = str(finder)
    older = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d-%H%M%S")
    newer = (datetime.now() - timedelta(minutes=5)).strftime("%Y%m%d-%H%M%S")
    for stamp in (older, newer):
        _create_remote_snapshot(remote, stamp, b"unchanged-recording")
    _link_remote_audio(remote, older, newer)

    linked_audio = _run_pull(project, env)

    assert linked_audio.returncode == 0, linked_audio.stderr
    assert (local / "daily" / newer / "audio" / "aud-test.webm").stat().st_nlink == 1

    config = remote / "daily" / newer / "config"
    (config / "env").unlink()
    os.link(remote / "daily" / older / "config" / "env", config / "env")
    linked_config = _run_pull(project, env)

    assert linked_config.returncode != 0
    assert "remote_snapshot_facts_invalid" in (local / "pull.log").read_text()


def _daily_harness(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    app = tmp_path / "app"
    scripts = app / "scripts"
    data = app / "data"
    stubs = tmp_path / "daily-bin"
    root = tmp_path / "backups"
    scripts.mkdir(parents=True)
    data.mkdir()
    stubs.mkdir()
    for name in ("backup.sh", "vps-backup-daily.sh", "verify_backup_snapshot.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    (scripts / "backup.sh").chmod(0o755)
    (scripts / "vps-backup-daily.sh").chmod(0o755)
    _create_current_database(data / "app.db")
    (app / ".env").write_text("DUMMY_NON_SECRET=1\n", encoding="utf-8")
    configs = {}
    for name in (
        "Caddyfile", "nmu.service", "nmu-caddy.service",
        "nmu-backup.service", "nmu-backup.timer",
    ):
        path = tmp_path / "config" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"synthetic {name}\n", encoding="utf-8")
        configs[name] = path
    _make_executable(stubs / "flock", "#!/usr/bin/env bash\nexit 0\n")
    _make_executable(stubs / "install", r'''#!/usr/bin/env bash
if [ "${FAIL_COMMAND:-}" = install ]; then exit 91; fi
exec /usr/bin/install "$@"
''')
    _make_executable(stubs / "cp", r'''#!/usr/bin/env bash
if [ "${FAIL_COMMAND:-}" = cp ]; then
  /bin/cp "$@" || exit $?
  exit 94
fi
exec /bin/cp "$@"
''')
    # 真实二进制路径按当前系统解析:macOS 在 /sbin,Ubuntu(CI)在 /usr/bin,
    # 写死任一边都会在另一边 No such file or directory。
    real_sha256sum = shutil.which("sha256sum") or "/usr/bin/sha256sum"
    _make_executable(stubs / "sha256sum", f'''#!/usr/bin/env bash
if [ "${{FAIL_COMMAND:-}}" = sha256sum ]; then exit 92; fi
exec {real_sha256sum} "$@"
''')
    _make_executable(stubs / "chmod", r'''#!/usr/bin/env bash
if [ "${FAIL_COMMAND:-}" = chmod ] && [[ "$*" == *".partial."* ]]; then exit 93; fi
exec /bin/chmod "$@"
''')
    _make_executable(stubs / "df", r'''#!/usr/bin/env bash
if [ -n "${FAKE_DF_FREE_KB:-}" ]; then
  printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
  printf 'synthetic 999999 1 %s 1%% /synthetic\n' "$FAKE_DF_FREE_KB"
  exit 0
fi
exec /bin/df "$@"
''')
    env = {
        **os.environ,
        "PATH": f"{stubs}:{os.environ.get('PATH', '/usr/bin:/bin:/sbin')}",
        "PYTHON_BIN": sys.executable,
        # An ambient/poisoned parent-process DATABASE_URL must never leak into
        # the copied backup.sh; pin it to the fixture DB this harness created.
        "DATABASE_URL": f"sqlite:///{data / 'app.db'}",
        "NMU_BACKUP_APP_DIR": str(app),
        "NMU_BACKUP_ROOT": str(root),
        "NMU_BACKUP_CADDYFILE": str(configs["Caddyfile"]),
        "NMU_BACKUP_APP_SERVICE": str(configs["nmu.service"]),
        "NMU_BACKUP_CADDY_SERVICE": str(configs["nmu-caddy.service"]),
        "NMU_BACKUP_SERVICE": str(configs["nmu-backup.service"]),
        "NMU_BACKUP_TIMER": str(configs["nmu-backup.timer"]),
    }
    return app, root, env


@pytest.mark.parametrize("failure", ["install", "sha256sum", "chmod"])
def test_daily_critical_failure_never_logs_ok_or_publishes(failure, tmp_path):
    app, root, env = _daily_harness(tmp_path)
    env["FAIL_COMMAND"] = failure

    completed = subprocess.run(
        ["bash", "scripts/vps-backup-daily.sh"], cwd=app, env=env,
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode != 0
    log = (root / "backup.log").read_text(encoding="utf-8")
    assert "] ok " not in log
    completed_dirs = [
        child for child in (root / "daily").iterdir()
        if child.is_dir() and _STRICT_STAMP.fullmatch(child.name)
    ]
    assert completed_dirs == []


def test_daily_success_publishes_only_verified_snapshot_and_logs_ok(tmp_path):
    app, root, env = _daily_harness(tmp_path)

    completed = subprocess.run(
        ["bash", "scripts/vps-backup-daily.sh"], cwd=app, env=env,
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode == 0, completed.stderr
    snapshots = [
        child for child in (root / "daily").iterdir()
        if child.is_dir() and _STRICT_STAMP.fullmatch(child.name)
    ]
    assert len(snapshots) == 1
    assert "] ok " in (root / "backup.log").read_text(encoding="utf-8")
    verified = subprocess.run(
        [sys.executable, "-I", str(GUARD_SOURCE), "verify-vps", str(snapshots[0])],
        check=False, capture_output=True, text=True,
    )
    assert verified.returncode == 0, verified.stderr


def test_daily_harness_pins_database_url_despite_poisoned_parent_env(monkeypatch, tmp_path):
    # A leaked/stale DATABASE_URL in the outer pytest process (e.g. left behind
    # by another test) must not reach the copied backup.sh via {**os.environ}.
    monkeypatch.setenv("DATABASE_URL", "sqlite:///not-the-fixture-db.sqlite")
    app, root, env = _daily_harness(tmp_path)

    assert env["DATABASE_URL"] == f"sqlite:///{app / 'data' / 'app.db'}"

    completed = subprocess.run(
        ["bash", "scripts/vps-backup-daily.sh"], cwd=app, env=env,
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode == 0, completed.stderr
    snapshots = [
        child for child in (root / "daily").iterdir()
        if child.is_dir() and _STRICT_STAMP.fullmatch(child.name)
    ]
    assert len(snapshots) == 1
    assert "] ok " in (root / "backup.log").read_text(encoding="utf-8")
    verified = subprocess.run(
        [sys.executable, "-I", str(GUARD_SOURCE), "verify-vps", str(snapshots[0])],
        check=False, capture_output=True, text=True,
    )
    assert verified.returncode == 0, verified.stderr


def test_daily_omits_empty_optional_source_roots_from_published_snapshot(tmp_path):
    app, root, env = _daily_harness(tmp_path)
    for name in ("audio", "exports", "controlled-audio-exports"):
        (app / "data" / name).mkdir()

    completed = subprocess.run(
        ["bash", "scripts/vps-backup-daily.sh"], cwd=app, env=env,
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode == 0, completed.stderr
    snapshots = [
        child for child in (root / "daily").iterdir()
        if child.is_dir() and _STRICT_STAMP.fullmatch(child.name)
    ]
    assert len(snapshots) == 1
    assert not (snapshots[0] / "audio").exists()
    assert not (snapshots[0] / "exports").exists()
    assert not (snapshots[0] / "controlled-audio-exports").exists()


def test_daily_persistent_failure_never_retains_failed_snapshot_payload(tmp_path):
    app, root, env = _daily_harness(tmp_path)
    env["FAIL_COMMAND"] = "install"

    for _ in range(5):
        completed = subprocess.run(
            ["bash", "scripts/vps-backup-daily.sh"], cwd=app, env=env,
            check=False, capture_output=True, text=True,
        )
        assert completed.returncode != 0

    failed = [
        child for child in (root / "daily").iterdir()
        if child.is_dir() and child.name.endswith(".failed")
    ]
    assert failed == []
    assert "] ok " not in (root / "backup.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("unsafe_root", ["", "/", "/.."])
def test_daily_rejects_empty_or_filesystem_root_before_any_write(
        unsafe_root, tmp_path):
    app, root, env = _daily_harness(tmp_path)
    env["NMU_BACKUP_ROOT"] = unsafe_root

    completed = subprocess.run(
        ["bash", "scripts/vps-backup-daily.sh"], cwd=app, env=env,
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode != 0
    assert "code=backup_root_unsafe" in completed.stderr
    assert not root.exists()


def test_daily_capacity_gate_uses_source_bytes_and_keeps_only_small_log(tmp_path):
    app, root, env = _daily_harness(tmp_path)
    evidence_name = "SYNTHETIC-PATIENT-ID-NOT-FOR-LOG.bin"
    exports = app / "data" / "exports"
    exports.mkdir()
    (exports / evidence_name).write_bytes(b"x" * (4 * 1024 * 1024))
    env["FAKE_DF_FREE_KB"] = str(82 * 1024)
    env["NMU_BACKUP_RESERVE_BYTES"] = "1"

    completed = subprocess.run(
        ["bash", "scripts/vps-backup-daily.sh"], cwd=app, env=env,
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode != 0
    log = (root / "backup.log").read_text(encoding="utf-8")
    assert "code=disk_capacity_insufficient" in log
    assert evidence_name not in completed.stdout
    assert evidence_name not in completed.stderr
    assert evidence_name not in log
    assert (root / "backup.log").stat().st_size < 4096
    assert list((root / "daily").iterdir()) == []


def test_daily_post_copy_failure_discards_large_partial_payload(tmp_path):
    app, root, env = _daily_harness(tmp_path)
    evidence_name = "SYNTHETIC-PATIENT-ID-IN-PARTIAL"
    payload = b"x" * (2 * 1024 * 1024)
    audio = app / "data" / "audio"
    audio.mkdir()
    (audio / f"{evidence_name}.webm").write_bytes(payload)
    _create_current_database(
        app / "data" / "app.db", (evidence_name, payload, "webm"),
    )
    env["FAIL_COMMAND"] = "install"

    completed = subprocess.run(
        ["bash", "scripts/vps-backup-daily.sh"], cwd=app, env=env,
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode != 0
    log = (root / "backup.log").read_text(encoding="utf-8")
    assert "payload=discarded" in log
    assert evidence_name not in completed.stdout
    assert evidence_name not in completed.stderr
    assert evidence_name not in log
    assert list((root / "daily").iterdir()) == []


def test_daily_base_snapshot_copy_failure_discards_large_failed_payload(tmp_path):
    app, root, env = _daily_harness(tmp_path)
    evidence_name = "SYNTHETIC-PATIENT-ID-IN-FAILED-BASE"
    payload = b"x" * (2 * 1024 * 1024)
    audio = app / "data" / "audio"
    audio.mkdir()
    (audio / f"{evidence_name}.webm").write_bytes(payload)
    _create_current_database(
        app / "data" / "app.db", (evidence_name, payload, "webm"),
    )
    env["FAIL_COMMAND"] = "cp"

    completed = subprocess.run(
        ["bash", "scripts/vps-backup-daily.sh"], cwd=app, env=env,
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode != 0
    log = (root / "backup.log").read_text(encoding="utf-8")
    assert "code=base_snapshot_failed" in log
    assert "payload=not-present" in log
    assert evidence_name not in completed.stdout
    assert evidence_name not in completed.stderr
    assert evidence_name not in log
    assert (root / "backup.log").stat().st_size < 4096
    assert list((root / "daily").iterdir()) == []


def test_daily_rejects_invalid_reserve_configuration_before_copy(tmp_path):
    app, root, env = _daily_harness(tmp_path)
    env["NMU_BACKUP_RESERVE_BYTES"] = "not-a-number"

    completed = subprocess.run(
        ["bash", "scripts/vps-backup-daily.sh"], cwd=app, env=env,
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode != 0
    assert "code=disk_capacity_invalid" in (
        root / "backup.log"
    ).read_text(encoding="utf-8")
    assert list((root / "daily").iterdir()) == []


def test_daily_rejects_source_directory_flood_before_staging(tmp_path):
    app, root, env = _daily_harness(tmp_path)
    exports = app / "data" / "exports"
    for index in range(4):
        directory = exports / f"nonempty-{index}"
        directory.mkdir(parents=True)
        (directory / "small.bin").write_bytes(b"x")
    env["NMU_BACKUP_SOURCE_DIRECTORY_LIMIT"] = "5"

    completed = subprocess.run(
        ["bash", "scripts/vps-backup-daily.sh"], cwd=app, env=env,
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode != 0
    log = (root / "backup.log").read_text(encoding="utf-8")
    assert "code=source_directory_limit_exceeded" in log
    assert list((root / "daily").iterdir()) == []


def test_daily_rejects_source_file_flood_before_staging(tmp_path):
    app, root, env = _daily_harness(tmp_path)
    exports = app / "data" / "exports" / "nonempty"
    exports.mkdir(parents=True)
    for index in range(4):
        (exports / f"small-{index}.bin").write_bytes(b"x")
    # app.db + six config inputs + four export files = eleven source files;
    # the generated manifest is deliberately excluded from this source limit.
    env["NMU_BACKUP_SOURCE_FILE_LIMIT"] = "10"

    completed = subprocess.run(
        ["bash", "scripts/vps-backup-daily.sh"], cwd=app, env=env,
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode != 0
    log = (root / "backup.log").read_text(encoding="utf-8")
    assert "code=source_file_limit_exceeded" in log
    assert list((root / "daily").iterdir()) == []


def test_daily_uses_app_venv_when_python_bin_is_unset(tmp_path):
    app, root, env = _daily_harness(tmp_path)
    venv_python = app / ".venv" / "bin" / "python"
    python_trace = tmp_path / "python-trace.log"
    venv_python.parent.mkdir(parents=True)
    _make_executable(
        venv_python,
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$PYTHON_TRACE\"\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
    )
    env.pop("PYTHON_BIN")
    env["PYTHON_TRACE"] = str(python_trace)
    stub_dir = env["PATH"].split(":", 1)[0]
    env["PATH"] = f"{stub_dir}:/usr/bin:/bin:/sbin"

    completed = subprocess.run(
        ["bash", "scripts/vps-backup-daily.sh"], cwd=app, env=env,
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "] ok " in (root / "backup.log").read_text(encoding="utf-8")
    invocations = python_trace.read_text(encoding="utf-8")
    assert " verify-vps " in f" {invocations} "
    assert " publish-vps " in f" {invocations} "
    assert len(invocations.splitlines()) >= 8


# ---------------------------------------------------------------------------
# 录音硬链接去重开关（NMU_BACKUP_LINK_AUDIO，默认关）
# ---------------------------------------------------------------------------

_OK_LINE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ok snapshot=(?P<stamp>[0-9]{8}-[0-9]{6}) "
    r"size=\S+ config=ok keep=14 source_bytes=[0-9]+ source_dirs=[0-9]+ "
    r"reserve_bytes=[0-9]+(?P<tail>.*)$")
_LINK_TAIL = re.compile(
    r"^ linked=(?P<linked>[0-9]+) new_bytes=(?P<new_bytes>[0-9]+) "
    r"cmp_mismatch=(?P<mismatch>[0-9]+) outside_links=(?P<outside>[0-9]+)$")


def _add_live_audio(app: Path, raw_id: str, payload: bytes) -> Path:
    audio = app / "data" / "audio"
    audio.mkdir(exist_ok=True)
    path = audio / f"{raw_id}.webm"
    path.write_bytes(payload)
    connection = sqlite3.connect(app / "data" / "app.db")
    try:
        connection.execute(
            "INSERT INTO audioassetrow "
            "(raw_audio_id,status,withdrawn,withdrawal_status,checksum,byte_count,"
            "audio_format,is_simulation,data_classification,patient_turn_ref_version,"
            "is_reliability_sample,contains_direct_identifier,delete_gate_passed) "
            "VALUES (?,?,?,?,?,?,?,0,'research',2,0,0,0)",
            (raw_id, "recorded", 0, None, hashlib.sha256(payload).hexdigest(),
             len(payload), "webm"),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _traced_daily_harness(tmp_path: Path) -> tuple[Path, Path, dict[str, str], Path]:
    """每轮快照名由测试指定（同一秒连跑两次会撞名），解释器调用全部留痕。"""
    app, root, env = _daily_harness(tmp_path)
    stubs = Path(env["PATH"].split(":", 1)[0])
    real_date = shutil.which("date") or "/bin/date"
    _make_executable(stubs / "date", f'''#!/usr/bin/env bash
if [ "$#" -eq 1 ] && [ "$1" = "+%Y%m%d-%H%M%S" ] && [ -n "${{FAKE_STAMP:-}}" ]; then
  printf '%s\\n' "$FAKE_STAMP"
  exit 0
fi
exec {real_date} "$@"
''')
    trace = tmp_path / "python-trace.log"
    traced_python = tmp_path / "traced-python"
    _make_executable(
        traced_python,
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$PYTHON_TRACE\"\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
    )
    env["PYTHON_BIN"] = str(traced_python)
    env["PYTHON_TRACE"] = str(trace)
    env.pop("NMU_BACKUP_LINK_AUDIO", None)
    return app, root, env, trace


def _run_daily(app: Path, env: dict[str, str], stamp: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/vps-backup-daily.sh"], cwd=app,
        env={**env, "FAKE_STAMP": stamp}, check=False, capture_output=True, text=True,
    )


def _ok_lines(root: Path) -> list[re.Match[str]]:
    lines = (root / "backup.log").read_text(encoding="utf-8").splitlines()
    matches = [_OK_LINE.fullmatch(line) for line in lines if "] ok " in line]
    assert all(matches), lines
    return matches


def _verify_vps(snapshot: Path, *link_root: Path) -> subprocess.CompletedProcess[str]:
    extra = ["--link-root", str(link_root[0])] if link_root else []
    return subprocess.run(
        [sys.executable, "-I", str(GUARD_SOURCE), "verify-vps", *extra, str(snapshot)],
        check=False, capture_output=True, text=True,
    )


@pytest.mark.parametrize("switch", [None, "true"])
def test_daily_switch_off_is_the_previous_behaviour(switch, tmp_path):
    app, root, env, trace = _traced_daily_harness(tmp_path)
    if switch is not None:
        env["NMU_BACKUP_LINK_AUDIO"] = switch
    live = _add_live_audio(app, "aud-old", b"unchanged-recording")

    for stamp in ("20261001-193000", "20261002-193000"):
        completed = _run_daily(app, env, stamp)
        assert completed.returncode == 0, completed.stderr

    older, newer = (root / "daily" / stamp for stamp in ("20261001-193000", "20261002-193000"))
    inodes = set()
    for snapshot in (older, newer):
        copied = snapshot / "audio" / "aud-old.webm"
        assert copied.stat().st_nlink == 1
        inodes.add(copied.stat().st_ino)
        assert _verify_vps(snapshot).returncode == 0
        assert sorted(path.relative_to(snapshot).as_posix()
                      for path in snapshot.rglob("*") if path.is_file()) == sorted(
            ["MANIFEST.sha256", "app.db", "audio/aud-old.webm",
             *(f"config/{name}" for name in _GUARD.VPS_CONFIG_FILES)])
    assert len(inodes) == 2 and live.stat().st_ino not in inodes
    assert [match["tail"] for match in _ok_lines(root)] == ["", ""]
    calls = [line for line in trace.read_text(encoding="utf-8").splitlines()
             if " verify-vps " in f" {line} " or " publish-vps " in f" {line} "]
    assert len(calls) == 4
    assert all(re.fullmatch(
        r"-I \S+/verify_backup_snapshot\.py "
        r"(verify-vps \S+/daily/\.[0-9]{8}-[0-9]{6}\.partial\.[0-9]+"
        r"|publish-vps \S+/daily/\.[0-9]{8}-[0-9]{6}\.partial\.[0-9]+ \S+/daily/[0-9]{8}-[0-9]{6})",
        line) for line in calls), calls


def test_daily_switch_on_links_unchanged_audio_and_survives_rotation(tmp_path):
    app, root, env, trace = _traced_daily_harness(tmp_path)
    env["NMU_BACKUP_LINK_AUDIO"] = "1"
    daily = root / "daily"
    old_live = _add_live_audio(app, "aud-old", b"unchanged-recording")

    first = _run_daily(app, env, "20261001-193000")
    assert first.returncode == 0, first.stderr
    new_live = _add_live_audio(app, "aud-new", b"recorded-after-the-first-backup")
    second = _run_daily(app, env, "20261002-193000")
    assert second.returncode == 0, second.stderr

    older, newer = daily / "20261001-193000", daily / "20261002-193000"
    shared = newer / "audio" / "aud-old.webm"
    assert shared.stat().st_ino == (older / "audio" / "aud-old.webm").stat().st_ino
    assert shared.stat().st_nlink == 2
    assert (newer / "audio" / "aud-new.webm").stat().st_nlink == 1
    for live in (old_live, new_live):
        assert live.stat().st_nlink == 1
        assert live.stat().st_ino not in {
            path.stat().st_ino for path in daily.rglob("*.webm")}

    tails = [_LINK_TAIL.fullmatch(match["tail"]) for match in _ok_lines(root)]
    assert all(tails), tails
    assert [(tail["linked"], tail["mismatch"]) for tail in tails] == [("0", "0"), ("1", "0")]
    newer_bytes = sum(path.stat().st_size for path in newer.rglob("*") if path.is_file())
    assert int(tails[1]["new_bytes"]) == newer_bytes - len(b"unchanged-recording")

    strict = _verify_vps(newer)
    assert strict.returncode == 2
    assert "code=snapshot_hardlinked_file" in strict.stderr
    for snapshot in (older, newer):
        assert _verify_vps(snapshot, daily).returncode == 0
    assert any(re.fullmatch(
        r"-I \S+/verify_backup_snapshot\.py verify-vps --link-root \S+/daily "
        r"\S+/daily/\.20261002-193000\.partial\.[0-9]+", line)
        for line in trace.read_text(encoding="utf-8").splitlines())

    # 轮转删掉最旧一份后，较新的快照照样自证；下一份接着与它共享。
    shutil.rmtree(older)
    assert shared.stat().st_nlink == 1
    assert _verify_vps(newer).returncode == 0
    third = _run_daily(app, env, "20261003-193000")
    assert third.returncode == 0, third.stderr
    newest = daily / "20261003-193000"
    for name in ("aud-old.webm", "aud-new.webm"):
        assert (newest / "audio" / name).stat().st_ino == (newer / "audio" / name).stat().st_ino
    assert _LINK_TAIL.fullmatch(_ok_lines(root)[-1]["tail"])["linked"] == "2"
    assert _verify_vps(newest, daily).returncode == 0


def test_daily_switch_on_copies_afresh_when_previous_bytes_differ(tmp_path):
    app, root, env, _trace = _traced_daily_harness(tmp_path)
    env["NMU_BACKUP_LINK_AUDIO"] = "1"
    live = _add_live_audio(app, "aud-old", b"unchanged-recording")
    assert _run_daily(app, env, "20261001-193000").returncode == 0
    older = root / "daily" / "20261001-193000" / "audio" / "aud-old.webm"
    with older.open("r+b") as handle:
        handle.write(b"U")  # 同名同长，只有字节不同：上一份那里已经坏了

    completed = _run_daily(app, env, "20261002-193000")

    assert completed.returncode == 0, completed.stderr
    copied = root / "daily" / "20261002-193000" / "audio" / "aud-old.webm"
    assert copied.stat().st_ino != older.stat().st_ino
    assert copied.stat().st_nlink == 1
    assert copied.read_bytes() == live.read_bytes()
    tail = _LINK_TAIL.fullmatch(_ok_lines(root)[-1]["tail"])
    assert (tail["linked"], tail["mismatch"]) == ("0", "1")
    assert _verify_vps(copied.parents[1]).returncode == 0


def test_daily_switch_on_keeps_linking_a_recording_already_shared_by_older_snapshots(tmp_path):
    # 稳态:第三晚起上一份那个 inode 已被两份快照共享,链接数要按全部兄弟快照数。
    app, root, env, _trace = _traced_daily_harness(tmp_path)
    env["NMU_BACKUP_LINK_AUDIO"] = "1"
    daily = root / "daily"
    _add_live_audio(app, "aud-old", b"unchanged-recording")
    stamps = ("20261001-193000", "20261002-193000", "20261003-193000")

    for stamp in stamps:
        completed = _run_daily(app, env, stamp)
        assert completed.returncode == 0, completed.stderr

    copies = [daily / stamp / "audio" / "aud-old.webm" for stamp in stamps]
    assert len({path.stat().st_ino for path in copies}) == 1
    assert copies[0].stat().st_nlink == 3
    tails = [_LINK_TAIL.fullmatch(match["tail"]) for match in _ok_lines(root)]
    assert all(tails), tails
    assert [(tail["linked"], tail["mismatch"], tail["outside"]) for tail in tails] == [
        ("0", "0", "0"), ("1", "0", "0"), ("1", "0", "0")]
    for stamp in stamps:
        assert _verify_vps(daily / stamp, daily).returncode == 0


@pytest.mark.parametrize("damage", ["truncate", "append", "chmod"])
def test_daily_switch_on_counts_a_kept_recording_whose_size_or_mode_changed(damage, tmp_path):
    # 定稿录音从不改写;上一份同名文件长度或权限变了只能是坏了,同样另存新副本并计数。
    app, root, env, _trace = _traced_daily_harness(tmp_path)
    env["NMU_BACKUP_LINK_AUDIO"] = "1"
    live = _add_live_audio(app, "aud-old", b"unchanged-recording")
    assert _run_daily(app, env, "20261001-193000").returncode == 0
    older = root / "daily" / "20261001-193000" / "audio" / "aud-old.webm"
    if damage == "truncate":
        with older.open("r+b") as handle:
            handle.truncate(len(b"unchanged-recording") - 1)
    elif damage == "append":
        with older.open("ab") as handle:
            handle.write(b"!")
    else:
        older.chmod(0o640)

    completed = _run_daily(app, env, "20261002-193000")

    assert completed.returncode == 0, completed.stderr
    copied = root / "daily" / "20261002-193000" / "audio" / "aud-old.webm"
    assert copied.stat().st_nlink == 1
    assert copied.read_bytes() == live.read_bytes()
    tail = _LINK_TAIL.fullmatch(_ok_lines(root)[-1]["tail"])
    assert (tail["linked"], tail["mismatch"], tail["outside"]) == ("0", "1", "0")


def _link_step_source() -> str:
    text = (ROOT / "scripts" / "vps-backup-daily.sh").read_text(encoding="utf-8")
    start = text.index('if LINK_FACTS=$("$PYTHON_CMD" -I - "$DAILY" "$WORK" "$APP/data" <<\'PY\'')
    body = text[text.index("\n", start) + 1:]
    return body[:body.index("\nPY\n") + 1]


_LINK_FAULT_DRIVER = """
import builtins, errno, os, pathlib, sys
fault, target, source_file = sys.argv[1], sys.argv[2], sys.argv[3]
sys.argv = ["-", *sys.argv[4:]]


def eio():
    raise OSError(errno.EIO, os.strerror(errno.EIO), target)


class _Unreadable:
    def __init__(self, handle):
        self._handle = handle

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._handle.close()
        return False

    def read(self, *_args):
        eio()


real_open, real_lstat = builtins.open, pathlib.Path.lstat


def faulty_open(path, *args, **kwargs):
    if fault == "open" and os.fspath(path) == target:
        eio()
    handle = real_open(path, *args, **kwargs)
    return _Unreadable(handle) if fault == "read" and os.fspath(path) == target else handle


def faulty_lstat(self, *args, **kwargs):
    if fault == "lstat" and os.fspath(self) == target:
        eio()
    return real_lstat(self, *args, **kwargs)


builtins.open = faulty_open
pathlib.Path.lstat = faulty_lstat
exec(compile(open(source_file, encoding="utf-8").read(), "<link-step>", "exec"),
     {"__name__": "__main__"})
"""


@pytest.mark.parametrize(("fault", "unreadable"), [
    ("none", None), ("open", "kept"), ("read", "kept"), ("lstat", "kept"), ("read", "fresh")])
def test_link_step_counts_an_unreadable_kept_recording_instead_of_failing(
        fault, unreadable, tmp_path):
    # 坏块让上一份那个录音读不出来(EIO):原来整份备份失败,链接源还是它,之后每晚都败在
    # 同一处、库也不再备份。上一份读不出来只算不一致、另存新副本;这次的新副本读不出来照旧失败。
    daily = tmp_path / "daily"
    kept = daily / "20261001-193000" / "audio" / "aud-old.webm"
    fresh = daily / ".20261002-193000.partial.4242" / "audio" / "aud-old.webm"
    for path in (kept, fresh):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"unchanged-recording")
        path.chmod(0o600)
    (tmp_path / "app" / "data").mkdir(parents=True)
    source = tmp_path / "link_step.py"
    source.write_text(_link_step_source(), encoding="utf-8")
    driver = tmp_path / "driver.py"
    driver.write_text(_LINK_FAULT_DRIVER, encoding="utf-8")
    target = {"kept": kept, "fresh": fresh, None: tmp_path / "nothing"}[unreadable]

    completed = subprocess.run(
        [sys.executable, "-I", str(driver), fault, str(target), str(source),
         str(daily), str(fresh.parents[1]), str(tmp_path / "app" / "data")],
        check=False, capture_output=True, text=True)

    if unreadable == "fresh":
        assert completed.returncode != 0
        return
    assert completed.returncode == 0, completed.stderr
    size = len(b"unchanged-recording")
    if unreadable is None:
        assert completed.stdout.split() == ["1", "0", "0", "0"]
        assert fresh.stat().st_ino == kept.stat().st_ino
    else:
        assert completed.stdout.split() == ["0", str(size), "1", "0"]
        assert fresh.stat().st_nlink == 1 and fresh.read_bytes() == b"unchanged-recording"


@pytest.mark.parametrize("stray_name", ["outside", "inside-other-name"])
def test_daily_switch_on_copies_afresh_when_the_kept_file_has_a_link_the_verifier_cannot_see(
        stray_name, tmp_path):
    app, root, env, _trace = _traced_daily_harness(tmp_path)
    env["NMU_BACKUP_LINK_AUDIO"] = "1"
    live = _add_live_audio(app, "aud-old", b"unchanged-recording")
    assert _run_daily(app, env, "20261001-193000").returncode == 0
    daily = root / "daily"
    older = daily / "20261001-193000" / "audio" / "aud-old.webm"
    # 校验器只数 daily/ 下严格时间戳兄弟快照里同名的 audio/<name>；别处多一个名字，
    # 链上去的新快照就证不出来，每晚都会验失败。
    stray = (tmp_path / "stray-copy.webm" if stray_name == "outside"
             else daily / "20261001-193000" / "audio" / "aud-alias.webm")
    os.link(older, stray)

    completed = _run_daily(app, env, "20261002-193000")

    assert completed.returncode == 0, completed.stderr
    newer = daily / "20261002-193000"
    copied = newer / "audio" / "aud-old.webm"
    assert copied.stat().st_nlink == 1
    assert copied.stat().st_ino != older.stat().st_ino
    assert copied.read_bytes() == live.read_bytes()
    assert older.stat().st_nlink == 2
    tail = _LINK_TAIL.fullmatch(_ok_lines(root)[-1]["tail"])
    assert (tail["linked"], tail["mismatch"], tail["outside"]) == ("0", "0", "1")
    assert _verify_vps(newer, daily).returncode == 0


def test_backup_unit_reads_an_optional_switch_file_and_never_turns_it_on():
    unit = (ROOT / "deploy" / "systemd" / "nmu-backup.service").read_text(encoding="utf-8")
    directives = [line.strip() for line in unit.splitlines()
                  if line.strip() and not line.strip().startswith("#")]

    assert "EnvironmentFile=-/opt/nmu/backup-dedup.env" in directives
    assert not any("NMU_BACKUP_LINK_AUDIO" in line for line in directives)
