#!/usr/bin/env bash
# VPS 每日自动备份包装(systemd nmu-backup.timer 调用):基础快照、配置、
# manifest/DB↔audio 语义校验全部通过后才原子发布严格时间戳目录。
# 任一 copy/hash/permission/verify/publish/log 失败都非零退出并删除未完成 payload，
# 只在 0600 backup.log 留固定码，绝不写“ok”。只面向 Ubuntu VPS(GNU 工具链)。
set -Eeuo pipefail
umask 077

APP="${NMU_BACKUP_APP_DIR:-/opt/nmu/app}"
if [ "${NMU_BACKUP_ROOT+x}" = x ] && [ -z "$NMU_BACKUP_ROOT" ]; then
  echo "备份失败(code=backup_root_unsafe)" >&2
  exit 64
fi
ROOT="${NMU_BACKUP_ROOT:-/opt/nmu/backups}"
if [ -n "${PYTHON_BIN:-}" ]; then
  PYTHON_CMD="$PYTHON_BIN"
elif [ -x "$APP/.venv/bin/python" ]; then
  PYTHON_CMD="$APP/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD=$(command -v python3)
else
  echo "备份失败(code=python_missing)" >&2
  exit 69
fi
if ! [ -x "$PYTHON_CMD" ] && ! command -v "$PYTHON_CMD" >/dev/null 2>&1; then
  echo "备份失败(code=python_missing)" >&2
  exit 69
fi
if ROOT=$("$PYTHON_CMD" -I - "$ROOT" <<'PY'
from pathlib import Path
import sys

raw = sys.argv[1]
if not raw or not raw.strip() or any(ord(char) < 32 or ord(char) == 127 for char in raw):
    raise SystemExit(64)
candidate = Path(raw)
if not candidate.is_absolute():
    candidate = Path.cwd() / candidate
resolved = candidate.resolve(strict=False)
if resolved == Path(resolved.anchor):
    raise SystemExit(64)
print(resolved)
PY
); then
  :
else
  echo "备份失败(code=backup_root_unsafe)" >&2
  exit 64
fi
DAILY="$ROOT/daily"
LOG="$ROOT/backup.log"
KEEP=14
SNAP_GLOB='[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]'
CADDY_CONFIG="${NMU_BACKUP_CADDYFILE:-/opt/nmu/caddy/Caddyfile}"
APP_SERVICE="${NMU_BACKUP_APP_SERVICE:-/etc/systemd/system/nmu.service}"
CADDY_SERVICE="${NMU_BACKUP_CADDY_SERVICE:-/etc/systemd/system/nmu-caddy.service}"
BACKUP_SERVICE="${NMU_BACKUP_SERVICE:-/etc/systemd/system/nmu-backup.service}"
BACKUP_TIMER="${NMU_BACKUP_TIMER:-/etc/systemd/system/nmu-backup.timer}"
GUARD="$APP/scripts/verify_backup_snapshot.py"
CURRENT_PATH=""
CURRENT_STAMP=""
FAIL_CODE="command_failed"

note() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$LOG"
}

fail() {
  FAIL_CODE="$1"
  echo "备份失败(code=$FAIL_CODE)" >&2
  return 1
}

discard_current_partial() {
  local base
  [ -n "$CURRENT_PATH" ] && { [ -e "$CURRENT_PATH" ] || [ -L "$CURRENT_PATH" ]; } \
    || return 3
  base=${CURRENT_PATH##*/}
  if [ "${CURRENT_PATH%/*}" != "$DAILY" ] \
     || [[ ! "$base" =~ ^\.${CURRENT_STAMP}\.partial\.[0-9]+$ ]]; then
    return 1
  fi
  if rm -rf -- "$CURRENT_PATH"; then
    CURRENT_PATH=""
    return 0
  fi
  return 1
}

on_error() {
  local rc="$1"
  local cleanup_rc
  trap - ERR
  set +e
  discard_current_partial
  cleanup_rc=$?
  if [ "$cleanup_rc" -eq 0 ]; then
    note "FAIL code=$FAIL_CODE snapshot=${CURRENT_STAMP:-none} rc=$rc payload=discarded"
  elif [ "$cleanup_rc" -eq 3 ]; then
    note "FAIL code=$FAIL_CODE snapshot=${CURRENT_STAMP:-none} rc=$rc payload=not-present"
  else
    printf '严重:失败快照无法删除(code=backup_partial_delete_failed)\n' >&2
    note "FAIL code=backup_partial_delete_failed snapshot=${CURRENT_STAMP:-none} rc=$rc payload=retained"
  fi
  exit "$rc"
}
on_exit() {
  local rc=$?
  trap - EXIT ERR INT TERM
  if [ "$rc" -ne 0 ]; then
    on_error "$rc"
  fi
  exit 0
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for required_cmd in flock df awk find sort tail install sha256sum chmod du cut rm; do
  command -v "$required_cmd" >/dev/null 2>&1 || fail "missing_command"
done
[ -x "$APP/scripts/backup.sh" ] || fail "backup_script_missing"
[ -f "$GUARD" ] || fail "snapshot_guard_missing"

[ ! -L "$ROOT" ] && [ ! -L "$DAILY" ] || fail "backup_root_symlink"
mkdir -p "$DAILY"
[ -d "$ROOT" ] && [ ! -L "$ROOT" ] && [ -d "$DAILY" ] && [ ! -L "$DAILY" ] \
  || fail "backup_root_invalid"
chmod 700 "$ROOT" "$DAILY"
[ ! -L "$LOG" ] || fail "backup_log_symlink"
[ ! -e "$LOG" ] || [ -f "$LOG" ] || fail "backup_log_not_regular"
touch "$LOG"
chmod 600 "$LOG"

# timer 与手工补跑不得并发发布同一快照。Ubuntu 服务端显式依赖 flock。
LOCK_FILE="$ROOT/.lock"
[ ! -L "$LOCK_FILE" ] || fail "backup_lock_symlink"
[ ! -e "$LOCK_FILE" ] || [ -f "$LOCK_FILE" ] || fail "backup_lock_not_regular"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  note "SKIP code=backup_lock_busy"
  exit 0
fi

# 持锁后清理上次 SIGKILL 留下的严格临时目录和旧版 *.failed payload。
# 失败证据只保留不含受试者标识的小型 backup.log；名称不符合本脚本产生
# 形状的目录一律不碰。
find "$DAILY" -mindepth 1 -maxdepth 1 -type d -print \
  | while IFS= read -r stale; do
      stale_base=${stale##*/}
      if [[ "$stale_base" =~ ^\.[0-9]{8}-[0-9]{6}\.partial\.[0-9]+$ ]]; then
        rm -rf "$stale"
      fi
    done
find "$DAILY" -mindepth 1 -maxdepth 1 -type d -name "*.failed" -print \
  | while IFS= read -r stale_failed; do
      stale_base=${stale_failed##*/}
      if [[ "$stale_base" =~ ^[0-9]{8}-[0-9]{6}(\.[0-9]+)?\.failed$ ]]; then
        rm -rf "$stale_failed"
      fi
    done

for source in "$APP/.env" "$CADDY_CONFIG" "$APP_SERVICE" "$CADDY_SERVICE" \
              "$BACKUP_SERVICE" "$BACKUP_TIMER"; do
  [ -f "$source" ] && [ ! -L "$source" ] && [ -s "$source" ] \
    || fail "config_missing_or_invalid"
done

# 先按本轮实际会复制的 SQLite 逻辑页、final 音频、两类导出和六份配置
# 估算源逻辑字节。容量门额外加入 10%/64MiB（二者取大）复制增长余量、
# metadata 余量和可配置的硬保留空间；任何扫描/类型/数值异常都 fail-closed。
FAIL_CODE="source_size_probe_failed"
if SOURCE_FACTS=$("$PYTHON_CMD" -I - "$APP/data" \
    "$APP/.env" "$CADDY_CONFIG" "$APP_SERVICE" "$CADDY_SERVICE" \
    "$BACKUP_SERVICE" "$BACKUP_TIMER" <<'PY' 2>/dev/null
import os
from pathlib import Path
import sqlite3
import stat
import sys
from urllib.parse import quote


def regular_file(path: Path, *, nonempty: bool = False) -> int:
    meta = path.lstat()
    if (stat.S_ISLNK(meta.st_mode) or not stat.S_ISREG(meta.st_mode)
            or meta.st_nlink != 1 or (nonempty and meta.st_size <= 0)):
        raise OSError
    return meta.st_size


def tree_size(path: Path) -> tuple[int, int, int]:
    if not path.exists():
        return 0, 0, 0
    root_meta = path.lstat()
    if stat.S_ISLNK(root_meta.st_mode) or not stat.S_ISDIR(root_meta.st_mode):
        raise OSError
    def walk_error(error: OSError) -> None:
        raise OSError from error

    files = 0
    total = 0
    directories = 0
    for current, dirnames, filenames in os.walk(
            path, followlinks=False, onerror=walk_error):
        directories += 1
        current_path = Path(current)
        for name in dirnames:
            meta = (current_path / name).lstat()
            if stat.S_ISLNK(meta.st_mode) or not stat.S_ISDIR(meta.st_mode):
                raise OSError
        for name in filenames:
            meta = (current_path / name).lstat()
            if (stat.S_ISLNK(meta.st_mode) or not stat.S_ISREG(meta.st_mode)
                    or meta.st_nlink != 1):
                raise OSError
            files += 1
            total += meta.st_size
    return files, total, directories


data = Path(sys.argv[1])
database = data / "app.db"
db_size = regular_file(database, nonempty=True)
uri = "file:" + quote(str(database.resolve(strict=True)), safe="/") + "?mode=ro"
connection = sqlite3.connect(uri, uri=True, timeout=30)
try:
    connection.execute("PRAGMA query_only=ON")
    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
finally:
    connection.close()
db_size = max(db_size, page_count * page_size)
file_count = 1
total = db_size
# Snapshot root and the always-materialized config directory.
directory_count = 2

audio = data / "audio"
if audio.exists():
    audio_meta = audio.lstat()
    if stat.S_ISLNK(audio_meta.st_mode) or not stat.S_ISDIR(audio_meta.st_mode):
        raise OSError
    final_audio_count = 0
    for entry in os.scandir(audio):
        meta = entry.stat(follow_symlinks=False)
        if entry.is_symlink() or not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
            raise OSError
        if entry.name.startswith("."):
            continue
        file_count += 1
        final_audio_count += 1
        total += meta.st_size
    if final_audio_count:
        directory_count += 1

for name in ("exports", "controlled-audio-exports"):
    files, size, directories = tree_size(data / name)
    file_count += files
    total += size
    directory_count += directories
for raw in sys.argv[2:]:
    total += regular_file(Path(raw), nonempty=True)
    file_count += 1
print(f"{file_count} {total} {directory_count}")
PY
); then
  :
else
  fail "source_size_probe_failed"
fi
read -r SOURCE_FILES SOURCE_BYTES SOURCE_DIRECTORIES SOURCE_EXTRA <<< "$SOURCE_FACTS"
if [ -n "${SOURCE_EXTRA:-}" ]; then
  fail "source_size_probe_invalid"
fi

FREE_KB=$(df -Pk "$ROOT" | awk 'NR==2{print $4}')
RESERVE_BYTES="${NMU_BACKUP_RESERVE_BYTES:-536870912}"
SOURCE_FILE_LIMIT="${NMU_BACKUP_SOURCE_FILE_LIMIT:-99999}"
SOURCE_DIRECTORY_LIMIT="${NMU_BACKUP_SOURCE_DIRECTORY_LIMIT:-10000}"
FAIL_CODE="disk_capacity_invalid"
if CAPACITY_FACTS=$("$PYTHON_CMD" -I - \
    "$FREE_KB" "$RESERVE_BYTES" "$SOURCE_FILES" "$SOURCE_BYTES" \
    "$SOURCE_DIRECTORIES" "$SOURCE_DIRECTORY_LIMIT" \
    "$SOURCE_FILE_LIMIT" <<'PY'
import sys

try:
    (free_kb, reserve, files, source, directories, directory_limit,
     file_limit) = (
        int(value) for value in sys.argv[1:]
    )
except ValueError:
    raise SystemExit(64)
if (free_kb < 0 or reserve < 1 or reserve > 2**63 - 1
        or files < 1 or source < 1 or source > 2**63 - 1
        or directories < 2 or directory_limit < 1 or directory_limit > 10_000
        or file_limit < 1 or file_limit > 99_999):
    raise SystemExit(64)
if directories > directory_limit:
    raise SystemExit(73)
if files > file_limit:
    raise SystemExit(74)
metadata = 16 * 1024 * 1024 + files * 512 + directories * 4096
growth = max(64 * 1024 * 1024, (source + 9) // 10)
copy_budget = source + metadata + growth
free = free_kb * 1024
required = copy_budget + reserve
if required > free:
    raise SystemExit(72)
print(f"{source} {copy_budget} {reserve} {free} {directories}")
PY
); then
  :
else
  rc=$?
  if [ "$rc" -eq 72 ]; then
    FAIL_CODE="disk_capacity_insufficient"
    note "FAIL code=$FAIL_CODE"
  elif [ "$rc" -eq 73 ]; then
    FAIL_CODE="source_directory_limit_exceeded"
    note "FAIL code=$FAIL_CODE"
  elif [ "$rc" -eq 74 ]; then
    FAIL_CODE="source_file_limit_exceeded"
    note "FAIL code=$FAIL_CODE"
  else
    FAIL_CODE="disk_capacity_invalid"
    note "FAIL code=$FAIL_CODE"
  fi
  exit "$rc"
fi
read -r SOURCE_BYTES COPY_BUDGET RESERVE_BYTES FREE_BYTES SOURCE_DIRECTORIES \
  CAPACITY_EXTRA \
  <<< "$CAPACITY_FACTS"
if [ -n "${CAPACITY_EXTRA:-}" ]; then
  fail "disk_capacity_output_invalid"
fi

# backup.sh 在 staging-only 模式不发布时间戳完成目录。daily 追加配置、
# 重做完整校验和权限断言后，再用 guard 同文件系统原子发布。
NEW=$(date +%Y%m%d-%H%M%S)
WORK="$DAILY/.$NEW.partial.$$"
CURRENT_PATH="$WORK"
CURRENT_STAMP="$NEW"
if OUT=$(cd "$APP" && BACKUP_DIR="$DAILY" BACKUP_STAGING_ONLY=1 \
    BACKUP_DISCARD_PARTIAL_ON_FAILURE=1 \
    PYTHON_BIN="$PYTHON_CMD" \
    BACKUP_STAMP_OVERRIDE="$NEW" BACKUP_STAGING_PATH="$WORK" \
    ./scripts/backup.sh 2>&1); then
  :
else
  rc=$?
  FAIL_CODE="base_snapshot_failed"
  # 只写固定码会把"备份连着十四夜没跑成"压缩成一个查不动的字符串:
  # 2026-07-23 之后每晚都是 base_snapshot_failed rc=1,真因(子进程解释器缺
  # sqlalchemy)全在这个被丢掉的 $OUT 里。取最后三行压成单行、按字符(不是字节,
  # 免得截断出半个汉字)截到 200,写进审计日志,同时回吐 stderr 让 journal 也有。
  detail=$(printf '%s' "$OUT" | tail -n 3 | tr -d '\000' | tr '\n\t' '  ' \
    | tr -s ' ' | sed 's/^ *//; s/ *$//' | cut -c1-200)
  note "FAIL code=$FAIL_CODE rc=$rc detail=${detail:-none}"
  printf '备份失败(code=%s rc=%s): %s\n' "$FAIL_CODE" "$rc" "${detail:-none}" >&2
  exit "$rc"
fi
[ -d "$WORK" ] && [ ! -L "$WORK" ] || fail "staging_handoff_invalid"

CONF="$WORK/config"
FAIL_CODE="config_directory_failed"
mkdir "$CONF"
chmod 700 "$CONF"
# Recheck immediately before copy to close the preflight-to-install window.
for source in "$APP/.env" "$CADDY_CONFIG" "$APP_SERVICE" "$CADDY_SERVICE" \
              "$BACKUP_SERVICE" "$BACKUP_TIMER"; do
  [ -f "$source" ] && [ ! -L "$source" ] && [ -s "$source" ] || fail "config_missing_or_invalid"
done
FAIL_CODE="config_install_failed"
install -m 600 "$APP/.env" "$CONF/env"
install -m 600 "$CADDY_CONFIG" "$CONF/Caddyfile"
install -m 600 "$APP_SERVICE" "$CONF/nmu.service"
install -m 600 "$CADDY_SERVICE" "$CONF/nmu-caddy.service"
install -m 600 "$BACKUP_SERVICE" "$CONF/nmu-backup.service"
install -m 600 "$BACKUP_TIMER" "$CONF/nmu-backup.timer"

FAIL_CODE="config_manifest_failed"
( cd "$WORK" && find config -type f -exec sha256sum {} + >> MANIFEST.sha256 )

# cp/install 可以带入宽松 mode；发布前统一收紧，任一 chmod 失败即隔离。
FAIL_CODE="snapshot_permission_failed"
find "$WORK" -type d -exec chmod 700 {} +
find "$WORK" -type f -exec chmod 600 {} +
chmod 600 "$LOG"

FAIL_CODE="snapshot_verify_failed"
"$PYTHON_CMD" -I "$GUARD" verify-vps "$WORK" >/dev/null

FINAL="$DAILY/$NEW"
FAIL_CODE="snapshot_publish_failed"
"$PYTHON_CMD" -I "$GUARD" publish-vps "$WORK" "$FINAL" >/dev/null
# 此时快照已经通过 VPS 合同并原子发布；后续轮转/日志失败不得把有效完成态
# 误删或伪装成 partial。
CURRENT_PATH=""

# 轮转不用 ls/glob；空目录在 pipefail 下仍稳定成功。只删严格时间戳完成态。
FAIL_CODE="snapshot_rotation_failed"
find "$DAILY" -mindepth 1 -maxdepth 1 -type d -name "$SNAP_GLOB" -print \
  | LC_ALL=C sort -r \
  | tail -n +$((KEEP + 1)) \
  | while IFS= read -r old; do
      [ -n "$old" ] && rm -rf "$old"
    done

SZ=$(du -sh "$FINAL" | cut -f1)
FAIL_CODE="success_log_failed"
note "ok snapshot=$NEW size=$SZ config=ok keep=$KEEP source_bytes=$SOURCE_BYTES source_dirs=$SOURCE_DIRECTORIES reserve_bytes=$RESERVE_BYTES"
CURRENT_PATH=""
CURRENT_STAMP=""
