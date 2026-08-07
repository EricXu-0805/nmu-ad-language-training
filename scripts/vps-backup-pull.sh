#!/usr/bin/env bash
# 受控运维工作站(macOS/Linux):把 VPS 严格时间戳快照拉回异地副本。
# 这是不可变“存档”，不是镜像：每个远端新快照先进 .incoming，完整验证
# manifest + SQLite 恢复结构 + DB↔audio 语义闭包，收紧权限后再原子发布。
# 已存在同名快照只允许完整树字节一致的幂等重放；任何差异隔离到 conflicts/
# 并非零退出，绝不覆盖本地历史。不用 GNU flock/sha256sum，保持 macOS 可运行。
set -euo pipefail
umask 077

for required_var in NMU_BACKUP_SSH_HOST NMU_BACKUP_SSH_USER NMU_BACKUP_REMOTE_ROOT; do
  if [ -z "${!required_var:-}" ]; then
    echo "拒绝执行:必须显式设置 $required_var" >&2
    exit 64
  fi
done
if [[ ! "$NMU_BACKUP_SSH_HOST" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] \
   || [[ "$NMU_BACKUP_SSH_HOST" == *..* ]]; then
  echo "拒绝执行:主机名非法" >&2
  exit 64
fi
if [[ ! "$NMU_BACKUP_SSH_USER" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]]; then
  echo "拒绝执行:SSH 用户名非法" >&2
  exit 64
fi
if [[ ! "$NMU_BACKUP_REMOTE_ROOT" =~ ^/[A-Za-z0-9._/-]+$ ]] \
   || [[ "$NMU_BACKUP_REMOTE_ROOT" == "/" ]] \
   || [[ "$NMU_BACKUP_REMOTE_ROOT" == *//* ]] \
   || [[ "/$NMU_BACKUP_REMOTE_ROOT/" == *"/../"* ]] \
   || [[ "/$NMU_BACKUP_REMOTE_ROOT/" == *"/./"* ]]; then
  echo "拒绝执行:远端根目录非法" >&2
  exit 64
fi

for required_cmd in rsync ssh find chmod mktemp sort tail mv rm rmdir wc df awk; do
  command -v "$required_cmd" >/dev/null 2>&1 || {
    echo "拒绝执行:缺少命令 $required_cmd" >&2
    exit 69
  }
done

cd "$(dirname "$0")/.."
GUARD="scripts/verify_backup_snapshot.py"
[ -f "$GUARD" ] || { echo "拒绝执行:缺少快照校验器" >&2; exit 69; }
ANCHOR_CHECK="scripts/audit_anchor_check.py"
[ -f "$ANCHOR_CHECK" ] || { echo "拒绝执行:缺少审计锚定校验器" >&2; exit 69; }
if [ -n "${PYTHON_BIN:-}" ]; then
  [ -x "$PYTHON_BIN" ] || command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    echo "拒绝执行:PYTHON_BIN 不可执行" >&2; exit 69;
  }
elif [ -x ./.venv/bin/python ]; then
  PYTHON_BIN=./.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN=$(command -v python3)
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN=$(command -v python)
else
  echo "拒绝执行:缺少 Python 快照校验运行时" >&2
  exit 69
fi

if [ "${NMU_BACKUP_LOCAL_ROOT+x}" = x ] && [ -z "$NMU_BACKUP_LOCAL_ROOT" ]; then
  echo "拒绝执行:本地备份根目录不可为空" >&2
  exit 64
fi
DEST="${NMU_BACKUP_LOCAL_ROOT:-data/vps-backups}"
DAILY="$DEST/daily"
INCOMING="$DEST/.incoming"
QUARANTINE="$DEST/quarantine"
CONFLICTS="$DEST/conflicts"
LEGACY="$DEST/legacy-unvalidated"
LOG="$DEST/pull.log"
SNAP_GLOB='[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]'
if DEST=$("$PYTHON_BIN" -I - "$DEST" <<'PY'
from pathlib import Path
import sys

raw = sys.argv[1]
if not raw or not raw.strip() or any(ord(char) < 32 or ord(char) == 127 for char in raw):
    raise SystemExit(2)
candidate = Path(raw)
if not candidate.is_absolute():
    candidate = Path.cwd() / candidate
candidate = candidate.absolute()
cursor = Path(candidate.anchor)
for part in candidate.parts[1:]:
    cursor = cursor / part
    if cursor.is_symlink():
        raise SystemExit(2)
resolved = candidate.resolve(strict=False)
if resolved == Path(resolved.anchor):
    raise SystemExit(2)
print(resolved)
PY
); then
  DAILY="$DEST/daily"
  INCOMING="$DEST/.incoming"
  QUARANTINE="$DEST/quarantine"
  CONFLICTS="$DEST/conflicts"
  LEGACY="$DEST/legacy-unvalidated"
  LOG="$DEST/pull.log"
else
  echo "拒绝执行:本地备份路径的上级链含软链接" >&2
  exit 73
fi
for local_path in "$DEST" "$DAILY" "$INCOMING" "$QUARANTINE" "$CONFLICTS" "$LEGACY"; do
  if [ -L "$local_path" ]; then
    echo "拒绝执行:本地备份路径不可为软链接" >&2
    exit 73
  fi
done
mkdir -p "$DAILY" "$INCOMING" "$QUARANTINE" "$CONFLICTS" "$LEGACY"
for local_path in "$DEST" "$DAILY" "$INCOMING" "$QUARANTINE" "$CONFLICTS" "$LEGACY"; do
  [ -d "$local_path" ] && [ ! -L "$local_path" ] || {
    echo "拒绝执行:本地备份路径不是受控目录" >&2
    exit 73
  }
done
chmod 700 "$DEST" "$DAILY" "$INCOMING" "$QUARANTINE" "$CONFLICTS" "$LEGACY"
[ ! -L "$LOG" ] || { echo "拒绝执行:pull.log 不可为软链接" >&2; exit 73; }
[ ! -e "$LOG" ] || [ -f "$LOG" ] || {
  echo "拒绝执行:pull.log 不是普通文件" >&2; exit 73;
}
touch "$LOG"
chmod 600 "$LOG"

note() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$LOG"
}

# mkdir 锁在 macOS 默认工具链上可用；不依赖 GNU flock。异常中断留下锁时
# fail-closed，由运维者确认无进程后删除，不自动猜测并破锁。
LOCK_DIR="$DEST/.pull.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  note "FAIL code=pull_lock_busy"
  exit 75
fi
chmod 700 "$LOCK_DIR"

CURRENT_STAGE=""
CURRENT_STAMP=""
CURRENT_ALLOWLIST=""
CURRENT_DATA_ALLOWLIST=""
FAIL_CODE="pull_failed"
cleanup() {
  local rc="$1"
  local rejected
  trap - EXIT INT TERM
  set +e
  [ -z "$CURRENT_ALLOWLIST" ] || rm -f -- "$CURRENT_ALLOWLIST"
  [ -z "$CURRENT_DATA_ALLOWLIST" ] || rm -f -- "$CURRENT_DATA_ALLOWLIST"
  if [ "$rc" -ne 0 ] && [ -n "$CURRENT_STAGE" ] \
     && { [ -e "$CURRENT_STAGE" ] || [ -L "$CURRENT_STAGE" ]; }; then
    if [ -L "$CURRENT_STAGE" ]; then
      rm -f "$CURRENT_STAGE"
    else
      rejected=$(mktemp -d "$QUARANTINE/${CURRENT_STAMP:-unknown}.${FAIL_CODE:-pull_failed}.$(date +%Y%m%d-%H%M%S).XXXXXX")
      # Never recurse through an untrusted symlink while preparing quarantine.
      # find is physical by default and only tightens actual directories/files.
      find "$CURRENT_STAGE" -type d -exec chmod 700 {} + 2>/dev/null
      find "$CURRENT_STAGE" -type f -exec chmod 600 {} + 2>/dev/null
      mv "$CURRENT_STAGE" "$rejected/payload" 2>/dev/null
    fi
  fi
  rmdir "$LOCK_DIR" 2>/dev/null
  exit "$rc"
}
trap 'cleanup $?' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# 持锁后清理严格命名的陈旧传输临时目录和 30 天前的失败副本；
# conflicts/legacy 是需人工决策的证据，从不自动删除。
find "$INCOMING" -mindepth 1 -maxdepth 1 -type d -print \
  | while IFS= read -r stale; do
      stale_base=${stale##*/}
      if [[ "$stale_base" =~ ^[0-9]{8}-[0-9]{6}\.partial\.[A-Za-z0-9]+$ ]]; then
        rm -rf "$stale"
      fi
    done
find "$INCOMING" -mindepth 1 -maxdepth 1 -type f -name 'backup.log.*' -delete
find "$INCOMING" -mindepth 1 -maxdepth 1 -type f \
  \( -name '.files-from.*' -o -name '.data-files-from.*' \) -delete
# 网络中断的传输残片不是可审核快照证据；持锁后可直接清理。
# 语义校验/发布失败、conflict 和 legacy 仍保留等待人工处置。
find "$QUARANTINE" -mindepth 1 -maxdepth 1 -type d \
  \( -name '*.snapshot_transfer_failed.*' -o -name 'backup-log.remote_log_transfer_failed.*' \) \
  -exec rm -rf {} +
find "$QUARANTINE" -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +
FREE_MB=$(df -Pm "$DEST" | awk 'NR==2{print $4}')
case "$FREE_MB" in
  ''|*[!0-9]*) note "FAIL code=local_disk_probe_invalid"; exit 2 ;;
esac
if [ "$FREE_MB" -lt 512 ]; then
  note "FAIL code=local_disk_space_low free=${FREE_MB}MB"
  exit 1
fi

# Remote statistics are a preflight, not a quota boundary: a compromised VPS
# can mutate after the probe.  We still reject static oversize/flood states
# before rsync and require the received tree to match the probed facts exactly.
# The runbook therefore also requires DEST to live on an encrypted quota volume.
MAX_SNAPSHOT_BYTES="${NMU_BACKUP_MAX_SNAPSHOT_BYTES:-68719476736}"
MAX_PULL_BYTES="${NMU_BACKUP_MAX_PULL_BYTES:-274877906944}"
MAX_SNAPSHOT_FILES="${NMU_BACKUP_MAX_SNAPSHOT_FILES:-100000}"
MAX_PULL_FILES="${NMU_BACKUP_MAX_PULL_FILES:-500000}"
MAX_SNAPSHOT_DIRECTORIES="${NMU_BACKUP_MAX_SNAPSHOT_DIRECTORIES:-10000}"
MAX_PULL_DIRECTORIES="${NMU_BACKUP_MAX_PULL_DIRECTORIES:-50000}"
MAX_MANIFEST_BYTES=67108864
DISK_RESERVE_BYTES=$((512 * 1024 * 1024))

REMOTE="${NMU_BACKUP_SSH_USER}@${NMU_BACKUP_SSH_HOST}"
REMOTE_ROOT="${NMU_BACKUP_REMOTE_ROOT%/}"
SSH_ARGS=(-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=yes)
RSYNC_SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=yes"
REMOTE_LIST_COMMAND="find '$REMOTE_ROOT/daily' -mindepth 1 -maxdepth 1 -name '$SNAP_GLOB' -printf '%y %f\\n'"

if REMOTE_ENTRIES=$(ssh "${SSH_ARGS[@]}" "$REMOTE" "$REMOTE_LIST_COMMAND" 2>/dev/null); then
  :
else
  rc=$?
  note "FAIL code=remote_snapshot_list_failed rc=$rc"
  exit "$rc"
fi
REMOTE_SNAPSHOTS=""
REMOTE_STAMP_ARGS=()
remote_count=0
while read -r remote_type remote_stamp remote_extra; do
  [ -z "${remote_type:-}" ] && continue
  case "$remote_stamp" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) : ;;
    *) note "FAIL code=remote_snapshot_name_invalid"; exit 2 ;;
  esac
  if [ "$remote_type" != "d" ] || [ -n "${remote_extra:-}" ]; then
    note "FAIL code=remote_snapshot_type_invalid snapshot=$remote_stamp"
    exit 2
  fi
  REMOTE_SNAPSHOTS="${REMOTE_SNAPSHOTS}${remote_stamp}"$'\n'
  REMOTE_STAMP_ARGS+=("$remote_stamp")
  remote_count=$((remote_count + 1))
done <<< "$REMOTE_ENTRIES"
if [ "$remote_count" -lt 1 ]; then
  note "FAIL code=remote_snapshot_set_empty"
  exit 2
fi
if [ "$remote_count" -gt 14 ]; then
  note "FAIL code=remote_snapshot_set_over_contract count=$remote_count"
  exit 2
fi
if ! "$PYTHON_BIN" -I - "${REMOTE_STAMP_ARGS[@]}" <<'PY'
from datetime import datetime, timedelta
import sys

stamps = sys.argv[1:]
if len(stamps) != len(set(stamps)):
    raise SystemExit(2)
now = datetime.now()
for stamp in stamps:
    try:
        parsed = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    except ValueError:
        raise SystemExit(2)
    if parsed > now + timedelta(hours=36):
        raise SystemExit(2)
PY
then
  note "FAIL code=remote_snapshot_calendar_invalid"
  exit 2
fi


REMOTE_SNAPSHOT_FILE_COUNTS=()
REMOTE_SNAPSHOT_BYTE_COUNTS=()
REMOTE_SNAPSHOT_DIRECTORY_COUNTS=()
REMOTE_FACT_ROWS=()
for remote_stamp in "${REMOTE_STAMP_ARGS[@]}"; do
  REMOTE_FACTS_COMMAND="set -o pipefail; NMU_SNAPSHOT_FACTS_CONTRACT=1; find '$REMOTE_ROOT/daily/$remote_stamp' -xdev -printf '%y %s %n\\n' | awk 'BEGIN {files=0; bytes=0; dirs=0; invalid=0} \$1==\"f\" && \$3==1 {files++; bytes+=\$2; next} \$1==\"d\" {dirs++; next} {invalid++} END {printf \"%d %.0f %d %d\\n\", files, bytes, dirs, invalid}'"
  if REMOTE_FACTS=$(ssh "${SSH_ARGS[@]}" "$REMOTE" "$REMOTE_FACTS_COMMAND" 2>/dev/null); then
    :
  else
    rc=$?
    note "FAIL code=remote_snapshot_facts_failed snapshot=$remote_stamp rc=$rc"
    exit "$rc"
  fi
  if [[ "$REMOTE_FACTS" == *$'\n'* ]]; then
    note "FAIL code=remote_snapshot_facts_invalid snapshot=$remote_stamp"
    exit 2
  fi
  read -r remote_files remote_bytes remote_directories remote_invalid remote_extra \
    <<< "$REMOTE_FACTS"
  for numeric in "$remote_files" "$remote_bytes" "$remote_directories" "$remote_invalid"; do
    case "$numeric" in
      ''|*[!0-9]*) note "FAIL code=remote_snapshot_facts_invalid snapshot=$remote_stamp"; exit 2 ;;
    esac
  done
  if [ -n "${remote_extra:-}" ] || [ "$remote_invalid" -ne 0 ] \
     || [ "$remote_files" -lt 2 ] || [ "$remote_bytes" -lt 1 ] \
     || [ "$remote_directories" -lt 1 ]; then
    note "FAIL code=remote_snapshot_facts_invalid snapshot=$remote_stamp"
    exit 2
  fi
  REMOTE_SNAPSHOT_FILE_COUNTS+=("$remote_files")
  REMOTE_SNAPSHOT_BYTE_COUNTS+=("$remote_bytes")
  REMOTE_SNAPSHOT_DIRECTORY_COUNTS+=("$remote_directories")
  REMOTE_FACT_ROWS+=("$remote_files:$remote_bytes:$remote_directories")
done

if PREFLIGHT_SUMMARY=$("$PYTHON_BIN" -I - \
    "$FREE_MB" "$DISK_RESERVE_BYTES" \
    "$MAX_SNAPSHOT_BYTES" "$MAX_PULL_BYTES" \
    "$MAX_SNAPSHOT_FILES" "$MAX_PULL_FILES" \
    "$MAX_SNAPSHOT_DIRECTORIES" "$MAX_PULL_DIRECTORIES" \
    "${REMOTE_FACT_ROWS[@]}" <<'PY'
import sys

try:
    (free_mb, reserve, snapshot_bytes, pull_bytes, snapshot_files, pull_files,
     snapshot_dirs, pull_dirs) = (
        int(value) for value in sys.argv[1:9]
    )
    rows = [tuple(int(part) for part in row.split(":")) for row in sys.argv[9:]]
except (ValueError, TypeError):
    raise SystemExit(64)
limits = (snapshot_bytes, pull_bytes, snapshot_files, pull_files,
          snapshot_dirs, pull_dirs)
if (free_mb < 0 or reserve < 1 or any(value < 1 for value in limits)
        or any(value > 2**63 - 1 for value in limits)
        or any(len(row) != 3 or any(value < 1 for value in row) for row in rows)):
    raise SystemExit(64)
if any(files > snapshot_files or size > snapshot_bytes or dirs > snapshot_dirs
       for files, size, dirs in rows):
    raise SystemExit(70)
total_files = sum(files for files, _, _ in rows)
total_bytes = sum(size for _, size, _ in rows)
total_dirs = sum(dirs for _, _, dirs in rows)
if total_files > pull_files or total_bytes > pull_bytes or total_dirs > pull_dirs:
    raise SystemExit(71)
free_bytes = free_mb * 1024 * 1024
if total_bytes + reserve > free_bytes:
    raise SystemExit(72)
print(f"{total_files} {total_bytes} {total_dirs}")
PY
); then
  :
else
  rc=$?
  case "$rc" in
    64) note "FAIL code=pull_limit_configuration_invalid" ;;
    70) note "FAIL code=remote_snapshot_over_limit" ;;
    71) note "FAIL code=remote_pull_set_over_limit" ;;
    72) note "FAIL code=local_disk_capacity_insufficient" ;;
    *) note "FAIL code=pull_preflight_failed rc=$rc" ;;
  esac
  exit "$rc"
fi
read -r PREFLIGHT_FILES PREFLIGHT_BYTES PREFLIGHT_DIRECTORIES PREFLIGHT_EXTRA \
  <<< "$PREFLIGHT_SUMMARY"
if [ -n "${PREFLIGHT_EXTRA:-}" ]; then
  note "FAIL code=pull_preflight_output_invalid"
  exit 2
fi

# 同一自然日的新完成快照数量有上限:每日 timer 一份之外,每次部署也各留一份
# 前置快照,密集部署日 2026-08-06 实测到 9 份,原上限 4 把合法部署日整天判洪泛。
# 上限仍然存在——受损 VPS 用大量克隆名称耗尽本地盘的路数照样被拦。
MAX_DAILY_SNAPSHOTS="${NMU_BACKUP_MAX_DAILY_SNAPSHOTS:-12}"
case "$MAX_DAILY_SNAPSHOTS" in
  ''|*[!0-9]*) note "FAIL code=daily_flood_config_invalid"; exit 2 ;;
esac
for remote_stamp in "${REMOTE_STAMP_ARGS[@]}"; do
  [ ! -e "$DAILY/$remote_stamp" ] && [ ! -L "$DAILY/$remote_stamp" ] || continue
  stamp_day=${remote_stamp%%-*}
  existing_day=$(find "$DAILY" -mindepth 1 -maxdepth 1 -type d \
    -name "$stamp_day-[0-9][0-9][0-9][0-9][0-9][0-9]" | wc -l | tr -d ' ')
  proposed_day=0
  for proposed in "${REMOTE_STAMP_ARGS[@]}"; do
    if [ "${proposed%%-*}" = "$stamp_day" ] \
       && [ ! -e "$DAILY/$proposed" ] && [ ! -L "$DAILY/$proposed" ]; then
      proposed_day=$((proposed_day + 1))
    fi
  done
  if [ $((existing_day + proposed_day)) -gt "$MAX_DAILY_SNAPSHOTS" ]; then
    note "FAIL code=remote_snapshot_daily_flood day=$stamp_day"
    exit 2
  fi
done

REMOTE_LOG_FACTS_COMMAND="find '$REMOTE_ROOT/backup.log' -maxdepth 0 -printf '%y %s\\n'"
if REMOTE_LOG_FACTS=$(ssh "${SSH_ARGS[@]}" "$REMOTE" "$REMOTE_LOG_FACTS_COMMAND" 2>/dev/null); then
  :
else
  rc=$?
  note "FAIL code=remote_log_probe_failed rc=$rc"
  exit "$rc"
fi
read -r REMOTE_LOG_TYPE REMOTE_LOG_SIZE REMOTE_LOG_EXTRA <<< "$REMOTE_LOG_FACTS"
case "$REMOTE_LOG_SIZE" in
  ''|*[!0-9]*) note "FAIL code=remote_log_size_invalid"; exit 2 ;;
esac
if [ "$REMOTE_LOG_TYPE" != "f" ] || [ -n "${REMOTE_LOG_EXTRA:-}" ]; then
  note "FAIL code=remote_log_not_regular"
  exit 2
fi
if [ "$REMOTE_LOG_SIZE" -lt 1 ] || [ "$REMOTE_LOG_SIZE" -gt 10485760 ]; then
  note "FAIL code=remote_log_too_large"
  exit 2
fi

pulled=0
unchanged=0
# 单份快照的"证据已归档"类结局(legacy/conflict=3,既有证据待人工=5)不中止
# 整体:一份坏快照不该挡住其余十几份的异地归档。硬故障(其他非零)照旧整体终止。
pull_skipped=0
pull_held=0
expected_snapshot_facts() {
  local stamp="$1" index
  for index in "${!REMOTE_STAMP_ARGS[@]}"; do
    if [ "${REMOTE_STAMP_ARGS[$index]}" = "$stamp" ]; then
      printf '%s %s %s\n' \
        "${REMOTE_SNAPSHOT_FILE_COUNTS[$index]}" \
        "${REMOTE_SNAPSHOT_BYTE_COUNTS[$index]}" \
        "${REMOTE_SNAPSHOT_DIRECTORY_COUNTS[$index]}"
      return 0
    fi
  done
  return 1
}

received_snapshot_facts() {
  "$PYTHON_BIN" -I - "$1" <<'PY'
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
files = 0
byte_count = 0
directory_count = 0
invalid = 0
try:
    root_meta = root.lstat()
    if stat.S_ISLNK(root_meta.st_mode) or not stat.S_ISDIR(root_meta.st_mode):
        raise OSError
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directory_count += 1
        for name in dirnames:
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
except OSError:
    raise SystemExit(2)
print(f"{files} {byte_count} {directory_count} {invalid}")
PY
}

pull_one_snapshot() {
  local stamp="$1"
  local final="$DAILY/$stamp"
  local stage compare_rc conflict expected_facts expected_files expected_bytes expected_remote_dirs
  local received_facts received_files received_bytes received_dirs received_invalid received_extra
  local plan_facts plan_files plan_dirs plan_extra
  local post_facts post_files post_bytes post_dirs post_invalid post_extra post_command
  case "$stamp" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) : ;;
    *) FAIL_CODE="remote_snapshot_name_invalid"; note "FAIL code=$FAIL_CODE"; return 1 ;;
  esac

  # 未决证据按危险度分流:conflict/校验失败件(同名不同内容、验不过=潜在篡改)
  # 返回 3——每天 partial 收尾+告警,直到有人清掉;legacy(良性历史件,首次归档
  # 那天已经告警过)返回 5——静默跳过,不拿一份旧快照天天拉响。
  if [ -n "$(find "$CONFLICTS" -mindepth 1 -maxdepth 1 -type d \
      -name "$stamp.*" -print -quit)" ]; then
    FAIL_CODE="snapshot_unresolved_evidence"
    note "FAIL code=$FAIL_CODE snapshot=$stamp"
    return 3
  fi
  if [ -n "$(find "$QUARANTINE" -mindepth 1 -maxdepth 1 -type d \
      \( -name "$stamp.snapshot_verify_failed.*" \
         -o -name "$stamp.snapshot_publish_failed.*" \) -print -quit)" ]; then
    FAIL_CODE="snapshot_unresolved_evidence"
    note "FAIL code=$FAIL_CODE snapshot=$stamp"
    return 3
  fi
  if [ -n "$(find "$LEGACY" -mindepth 1 -maxdepth 1 -type d \
      -name "$stamp.*" -print -quit)" ]; then
    FAIL_CODE="snapshot_unresolved_evidence"
    note "FAIL code=$FAIL_CODE snapshot=$stamp"
    return 5
  fi

  stage=$(mktemp -d "$INCOMING/$stamp.partial.XXXXXX")
  CURRENT_STAGE="$stage"
  CURRENT_STAMP="$stamp"
  chmod 700 "$stage"

  # 只先拉固定名称、硬上限的 manifest。严格解析器生成 0600 allowlist；
  # 第二次 rsync 只读取 manifest 覆盖的精确路径，不递归接收 probe 后注入的
  # file/dir flood。预取 manifest 保留在 staging，不允许第二次被远端替换。
  if rsync -a -x --no-links --no-devices --no-specials \
       --max-size="$MAX_MANIFEST_BYTES" \
       --timeout=60 -e "$RSYNC_SSH" \
       "$REMOTE:$REMOTE_ROOT/daily/$stamp/MANIFEST.sha256" \
       "$stage/MANIFEST.sha256" >/dev/null 2>&1; then
    :
  else
    compare_rc=$?
    FAIL_CODE="manifest_transfer_failed"
    note "FAIL code=$FAIL_CODE snapshot=$stamp rc=$compare_rc"
    return "$compare_rc"
  fi
  if [ ! -f "$stage/MANIFEST.sha256" ] || [ -L "$stage/MANIFEST.sha256" ]; then
    FAIL_CODE="manifest_not_regular"
    note "FAIL code=$FAIL_CODE snapshot=$stamp"
    return 2
  fi
  chmod 600 "$stage/MANIFEST.sha256" >/dev/null 2>&1 || {
    FAIL_CODE="manifest_permission_failed"
    note "FAIL code=$FAIL_CODE snapshot=$stamp"
    return 2
  }
  CURRENT_ALLOWLIST=$(mktemp "$INCOMING/.files-from.$stamp.XXXXXX")
  CURRENT_DATA_ALLOWLIST=$(mktemp "$INCOMING/.data-files-from.$stamp.XXXXXX")
  chmod 600 "$CURRENT_ALLOWLIST" "$CURRENT_DATA_ALLOWLIST"
  if "$PYTHON_BIN" -I "$GUARD" manifest-files "$stage/MANIFEST.sha256" \
      > "$CURRENT_ALLOWLIST" 2>/dev/null; then
    :
  else
    compare_rc=$?
    FAIL_CODE="manifest_allowlist_invalid"
    note "FAIL code=$FAIL_CODE snapshot=$stamp"
    return "$compare_rc"
  fi
  if plan_facts=$("$PYTHON_BIN" -I - \
      "$CURRENT_ALLOWLIST" "$CURRENT_DATA_ALLOWLIST" <<'PY'
from pathlib import PurePosixPath
import sys

lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
if not lines or lines[0] != "./MANIFEST.sha256" or len(lines) != len(set(lines)):
    raise SystemExit(2)
directories = {"."}
for line in lines:
    if not line.startswith("./"):
        raise SystemExit(2)
    parent = PurePosixPath(line[2:]).parent
    while parent.as_posix() != ".":
        directories.add(parent.as_posix())
        parent = parent.parent
with open(sys.argv[2], "wb") as handle:
    for line in lines[1:]:
        handle.write(line.encode("utf-8") + b"\0")
print(f"{len(lines)} {len(directories)}")
PY
  ); then
    :
  else
    FAIL_CODE="manifest_plan_invalid"
    note "FAIL code=$FAIL_CODE snapshot=$stamp"
    return 2
  fi

  expected_facts=$(expected_snapshot_facts "$stamp") || {
    FAIL_CODE="snapshot_preflight_missing"
    note "FAIL code=$FAIL_CODE snapshot=$stamp"
    return 2
  }
  read -r expected_files expected_bytes expected_remote_dirs <<< "$expected_facts"
  read -r plan_files plan_dirs plan_extra <<< "$plan_facts"
  if [ -n "${plan_extra:-}" ] || [ "$plan_files" -ne "$expected_files" ] \
     || [ "$plan_dirs" -ne "$expected_remote_dirs" ] \
     || [ "$plan_dirs" -gt "$MAX_SNAPSHOT_DIRECTORIES" ]; then
    FAIL_CODE="manifest_plan_mismatch"
    note "FAIL code=$FAIL_CODE snapshot=$stamp"
    return 2
  fi

  if [ -s "$CURRENT_DATA_ALLOWLIST" ]; then
    if rsync -a -x --no-links --no-devices --no-specials --no-implied-dirs \
         --max-size="$MAX_SNAPSHOT_BYTES" \
         --from0 --files-from="$CURRENT_DATA_ALLOWLIST" \
         --chmod=Du=rwx,Dgo=,Fu=rw,Fgo= --timeout=120 -e "$RSYNC_SSH" \
         "$REMOTE:$REMOTE_ROOT/daily/$stamp/" "$stage/" >/dev/null 2>&1; then
      :
    else
      compare_rc=$?
      FAIL_CODE="snapshot_transfer_failed"
      note "FAIL code=$FAIL_CODE snapshot=$stamp rc=$compare_rc"
      return "$compare_rc"
    fi
  fi

  # The allowlist prevents unlisted objects from landing locally, but without a
  # second remote probe that same protection could silently sanitize a snapshot
  # changed after preflight. Re-read the exact bounded facts before publication
  # and reject any file/byte/directory/type drift.
  post_command="set -o pipefail; NMU_SNAPSHOT_FACTS_CONTRACT=1; find '$REMOTE_ROOT/daily/$stamp' -xdev -printf '%y %s %n\\n' | awk 'BEGIN {files=0; bytes=0; dirs=0; invalid=0} \$1==\"f\" && \$3==1 {files++; bytes+=\$2; next} \$1==\"d\" {dirs++; next} {invalid++} END {printf \"%d %.0f %d %d\\n\", files, bytes, dirs, invalid}'"
  if post_facts=$(ssh "${SSH_ARGS[@]}" "$REMOTE" "$post_command" 2>/dev/null); then
    :
  else
    compare_rc=$?
    FAIL_CODE="remote_snapshot_reprobe_failed"
    note "FAIL code=$FAIL_CODE snapshot=$stamp rc=$compare_rc"
    return "$compare_rc"
  fi
  if [[ "$post_facts" == *$'\n'* ]]; then
    FAIL_CODE="remote_snapshot_reprobe_invalid"
    note "FAIL code=$FAIL_CODE snapshot=$stamp"
    return 2
  fi
  read -r post_files post_bytes post_dirs post_invalid post_extra <<< "$post_facts"
  for numeric in "$post_files" "$post_bytes" "$post_dirs" "$post_invalid"; do
    case "$numeric" in
      ''|*[!0-9]*)
        FAIL_CODE="remote_snapshot_reprobe_invalid"
        note "FAIL code=$FAIL_CODE snapshot=$stamp"
        return 2
        ;;
    esac
  done
  if [ -n "${post_extra:-}" ] || [ "$post_invalid" -ne 0 ] \
     || [ "$post_files" -ne "$expected_files" ] \
     || [ "$post_bytes" -ne "$expected_bytes" ] \
     || [ "$post_dirs" -ne "$expected_remote_dirs" ]; then
    FAIL_CODE="snapshot_changed_during_transfer"
    note "FAIL code=$FAIL_CODE snapshot=$stamp"
    return 2
  fi

  if received_facts=$(received_snapshot_facts "$stage"); then
    :
  else
    compare_rc=$?
    FAIL_CODE="snapshot_received_facts_invalid"
    note "FAIL code=$FAIL_CODE snapshot=$stamp rc=$compare_rc"
    return "$compare_rc"
  fi
  read -r received_files received_bytes received_dirs received_invalid received_extra \
    <<< "$received_facts"
  if [ -n "${received_extra:-}" ] || [ "$received_invalid" -ne 0 ] \
     || [ "$received_files" -ne "$expected_files" ] \
     || [ "$received_bytes" -ne "$expected_bytes" ] \
     || [ "$received_dirs" -ne "$plan_dirs" ]; then
    FAIL_CODE="snapshot_changed_during_transfer"
    note "FAIL code=$FAIL_CODE snapshot=$stamp"
    return 2
  fi

  # 先收紧再校验；宽权限远端文件永远不进入完成存档。
  if find "$stage" -type d -exec chmod 700 {} + >/dev/null 2>&1 \
     && find "$stage" -type f -exec chmod 600 {} + >/dev/null 2>&1; then
    :
  else
    FAIL_CODE="snapshot_permission_failed"
    note "FAIL code=$FAIL_CODE snapshot=$stamp"
    return 2
  fi
  if VERIFY_RESULT=$("$PYTHON_BIN" -I "$GUARD" verify-vps "$stage" 2>&1); then
    :
  else
    compare_rc=$?
    if [[ "$VERIFY_RESULT" == *"code=alembic_revision_unsupported"* ]]; then
      conflict=$(mktemp -d "$LEGACY/$stamp.$(date +%Y%m%d-%H%M%S).XXXXXX")
      mv "$stage" "$conflict/historical"
      CURRENT_STAGE=""
      CURRENT_STAMP=""
      note "FAIL code=legacy_snapshot_unvalidated snapshot=$stamp"
      # 返回 3=本份证据已归档,继续其余快照;整体以 partial 收尾并告警一次。
      return 3
    fi
    FAIL_CODE="snapshot_verify_failed"
    note "FAIL code=$FAIL_CODE snapshot=$stamp"
    return "$compare_rc"
  fi

  rm -f -- "$CURRENT_ALLOWLIST" "$CURRENT_DATA_ALLOWLIST"
  CURRENT_ALLOWLIST=""
  CURRENT_DATA_ALLOWLIST=""

  # 审计链异地锚定:重算快照内哈希链,并与本机追加账本对分叉/前缀改写/回退。
  # VPS 上重算整条链+改锚点的"全权伪造",过不了这台机器已经记下的历史锚点。
  # 篡改级(1)与读不出(2)同样按 conflict 处置留证据,fail-closed。
  if ANCHOR_RESULT=$("$PYTHON_BIN" -I "$ANCHOR_CHECK" "$stage/app.db" \
      --snapshot "$stamp" --anchors-log "$DEST/audit-anchors.log" --record 2>&1); then
    :
  else
    compare_rc=$?
    conflict=$(mktemp -d "$CONFLICTS/$stamp.$(date +%Y%m%d-%H%M%S).XXXXXX")
    mv "$stage" "$conflict/audit-anchor"
    CURRENT_STAGE=""
    CURRENT_STAMP=""
    FAIL_CODE="audit_anchor_check_failed"
    note "FAIL code=$FAIL_CODE snapshot=$stamp rc=$compare_rc detail=$ANCHOR_RESULT"
    return 3
  fi

  if [ -e "$final" ] || [ -L "$final" ]; then
    if "$PYTHON_BIN" -I "$GUARD" compare-vps "$final" "$stage" >/dev/null 2>&1; then
      rm -rf "$stage"
      CURRENT_STAGE=""
      CURRENT_STAMP=""
      unchanged=$((unchanged + 1))
      return 0
    else
      compare_rc=$?
    fi
    conflict=$(mktemp -d "$CONFLICTS/$stamp.$(date +%Y%m%d-%H%M%S).XXXXXX")
    mv "$stage" "$conflict/remote"
    CURRENT_STAGE=""
    CURRENT_STAMP=""
    if [ "$compare_rc" -eq 3 ]; then
      FAIL_CODE="snapshot_conflict"
    else
      FAIL_CODE="existing_snapshot_invalid"
    fi
    note "FAIL code=$FAIL_CODE snapshot=$stamp rc=$compare_rc"
    # 同为"证据已入 conflicts、等人工"的结局:不挡其余快照,partial 收尾告警。
    return 3
  fi

  if ! "$PYTHON_BIN" -I "$GUARD" publish-vps "$stage" "$final" >/dev/null; then
    FAIL_CODE="snapshot_publish_failed"
    note "FAIL code=$FAIL_CODE snapshot=$stamp"
    return 2
  fi
  CURRENT_STAGE=""
  CURRENT_STAMP=""
  pulled=$((pulled + 1))
}

while IFS= read -r stamp; do
  [ -z "$stamp" ] && continue
  # </dev/null 必须有:循环体里的 rsync 会起 ssh,ssh 继承并吸干喂循环的
  # stdin,第一份快照拉完 read 就 EOF——每日一份的节奏下永远发现不了,
  # 2026-08-07 首次多份补拉时现形(远端 14 份只落地 1 份)。
  snapshot_rc=0
  pull_one_snapshot "$stamp" </dev/null || snapshot_rc=$?
  case "$snapshot_rc" in
    0) : ;;
    3) pull_skipped=$((pull_skipped + 1)) ;;
    5) pull_held=$((pull_held + 1)) ;;
    # 硬故障(传输/校验器本身/清单畸形…)维持整体 fail-closed:EXIT trap 会把
    # 当前 staging 按 FAIL_CODE 隔离成证据。
    *) exit "$snapshot_rc" ;;
  esac
done < <(printf '%s\n' "$REMOTE_SNAPSHOTS" | LC_ALL=C sort -ru)

# backup.log 是远端当前审计视图，可以替换；也必须先拉入临时文件再原子替换。
REMOTE_LOG_STAGE=$(mktemp "$INCOMING/backup.log.XXXXXX")
CURRENT_STAGE="$REMOTE_LOG_STAGE"
CURRENT_STAMP="backup-log"
if rsync -a --no-links --no-devices --no-specials --max-size=10485760 \
     --timeout=60 -e "$RSYNC_SSH" \
     "$REMOTE:$REMOTE_ROOT/backup.log" "$REMOTE_LOG_STAGE" >/dev/null 2>&1; then
  :
else
  rc=$?
  FAIL_CODE="remote_log_transfer_failed"
  note "FAIL code=$FAIL_CODE rc=$rc"
  exit "$rc"
fi
[ -f "$REMOTE_LOG_STAGE" ] && [ ! -L "$REMOTE_LOG_STAGE" ] || {
  FAIL_CODE="remote_log_not_regular"
  note "FAIL code=$FAIL_CODE"
  exit 2
}
chmod 600 "$REMOTE_LOG_STAGE"
REMOTE_LOG_BYTES=$(wc -c < "$REMOTE_LOG_STAGE" | tr -d ' ')
case "$REMOTE_LOG_BYTES" in
  ''|*[!0-9]*) FAIL_CODE="remote_log_size_invalid"; note "FAIL code=$FAIL_CODE"; exit 2 ;;
esac
if [ "$REMOTE_LOG_BYTES" -ne "$REMOTE_LOG_SIZE" ]; then
  FAIL_CODE="remote_log_changed_during_transfer"
  note "FAIL code=$FAIL_CODE"
  exit 2
fi
if [ -e "$DEST/backup.log" ] || [ -L "$DEST/backup.log" ]; then
  [ -f "$DEST/backup.log" ] && [ ! -L "$DEST/backup.log" ] || {
    FAIL_CODE="local_log_not_regular"
    note "FAIL code=$FAIL_CODE"
    exit 2
  }
fi
mv "$REMOTE_LOG_STAGE" "$DEST/backup.log"
CURRENT_STAGE=""
CURRENT_STAMP=""

# 完成存档不根据受损远端提供的名称自动删除;只自动清理 30 天前的明确失败隔离件。
find "$QUARANTINE" -mindepth 1 -maxdepth 1 -mtime +30 -exec rm -rf {} +

# 2026-08-07 拍板的异地保留策略:daily 留最新 60 份(VPS 端留 14,这里是两个月
# 深度),更深历史归 Time Machine。修剪只按本地严格日期命名选择 daily/ 里的目录;
# conflicts/quarantine/legacy 是人工决策证据,永不自动删。
RETAIN="${NMU_BACKUP_OFFSITE_RETAIN:-60}"
case "$RETAIN" in ''|*[!0-9]*) note "FAIL code=retain_config_invalid"; exit 2 ;; esac
[ "$RETAIN" -ge 1 ] || { note "FAIL code=retain_config_invalid"; exit 2; }
find "$DAILY" -mindepth 1 -maxdepth 1 -type d -name "$SNAP_GLOB" -print \
  | LC_ALL=C sort -r | awk -v keep="$RETAIN" 'NR > keep' \
  | while IFS= read -r expired; do
      rm -rf "$expired"
      note "pruned snapshot=${expired##*/} retain=$RETAIN"
    done

count=$(find "$DAILY" -mindepth 1 -maxdepth 1 -type d -name "$SNAP_GLOB" | wc -l | tr -d ' ')

# 软配额:超了不删数据(这次拉取本身是成功的),用专属退出码 3 交给包装层拉响
# 告警,由人决定提配额还是降保留。
QUOTA_MB="${NMU_BACKUP_OFFSITE_QUOTA_MB:-20480}"
case "$QUOTA_MB" in ''|*[!0-9]*) note "FAIL code=quota_config_invalid"; exit 2 ;; esac
USED_MB=$(du -sm "$DEST" 2>/dev/null | awk '{print $1}')
case "$USED_MB" in ''|*[!0-9]*) note "FAIL code=offsite_du_probe_invalid"; exit 2 ;; esac
HELD_SUFFIX=""
[ "$pull_held" -eq 0 ] || HELD_SUFFIX=" held=$pull_held"
if [ "$pull_skipped" -gt 0 ]; then
  # 本轮有新证据入 legacy/conflicts:其余快照照常归档了,但要人看一眼。
  note "partial snapshots=$count pulled=$pulled unchanged=$unchanged skipped=$pull_skipped$HELD_SUFFIX used_mb=$USED_MB"
  exit 4
fi
if [ "$USED_MB" -gt "$QUOTA_MB" ]; then
  note "ok snapshots=$count pulled=$pulled unchanged=$unchanged$HELD_SUFFIX used_mb=$USED_MB quota_mb=$QUOTA_MB verdict=quota_exceeded"
  exit 3
fi
note "ok snapshots=$count pulled=$pulled unchanged=$unchanged$HELD_SUFFIX used_mb=$USED_MB"
