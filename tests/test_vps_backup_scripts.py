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
  "$PYTHON_BIN" -I - "$FAKE_REMOTE_ROOT/daily/$stamp" <<'PY'
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
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
        if meta.st_nlink != 1:
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
    assert len(Path(env["TRANSFER_LOG"]).read_text().splitlines()) == transfer_count
    assert hashlib.sha256(local_audio.read_bytes()).hexdigest() == original_hash


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
    _make_executable(stubs / "sha256sum", r'''#!/usr/bin/env bash
if [ "${FAIL_COMMAND:-}" = sha256sum ]; then exit 92; fi
exec /sbin/sha256sum "$@"
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
