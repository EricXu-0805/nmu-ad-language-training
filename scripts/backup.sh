#!/usr/bin/env bash
# 本机数据备份:数据库 + DB 权威采集音频 + 分析导出 + 受控声纹导出
# → data/backups/<时间戳>/,附 sha256 清单和 DB↔音频双向语义校验。
# 备份副本默认仍在本机；落到院内加密移动盘/内网共享盘时：
#   BACKUP_DIR=/Volumes/xxx ./scripts/backup.sh
# 恢复前必须停服、重跑 verify_backup_snapshot.py verify，再覆盖 data/ 中同名数据。
# PostgreSQL 部署时数据库不在 data/app.db，需另用 pg_dump；本脚本只覆盖 SQLite。
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."

DATA_DIR="data"
if [ "${BACKUP_DIR+x}" = x ] && [ -z "$BACKUP_DIR" ]; then
  echo "备份失败:备份根目录不可为空" >&2
  exit 64
fi
DEST_ROOT="${BACKUP_DIR:-$DATA_DIR/backups}"
STAMP="${BACKUP_STAMP_OVERRIDE:-$(date +%Y%m%d-%H%M%S)}"
# 只有所有字节、数据库语义和权限校验通过后才原子发布为严格时间戳目录。
# 中断/校验失败的目录不会被 daily/pull 误当成可恢复快照。
FINAL_DEST=""
DEST=""
GUARD="scripts/verify_backup_snapshot.py"
KEEP_STAGING=0
DISCARD_PARTIAL_ON_FAILURE="${BACKUP_DISCARD_PARTIAL_ON_FAILURE:-0}"

fail() {
  echo "备份失败:$*" >&2
  return 1
}

quarantine_partial() {
  local failed
  [ -n "${DEST:-}" ] && [ -d "$DEST" ] || return 0
  failed="$DEST_ROOT/$STAMP.failed"
  [ ! -e "$failed" ] || failed="$DEST_ROOT/$STAMP.$$.failed"
  if mv "$DEST" "$failed"; then
    echo "未完成快照已隔离(code=backup_incomplete)" >&2
    DEST=""
  else
    echo "严重:未完成快照无法隔离(code=backup_quarantine_failed)" >&2
  fi
}

discard_partial() {
  [ -n "${DEST:-}" ] && [ -d "$DEST" ] || return 0
  if rm -rf -- "$DEST"; then
    echo "未完成快照已删除(code=backup_incomplete_discarded)" >&2
    DEST=""
  else
    echo "严重:未完成快照无法删除(code=backup_partial_delete_failed)" >&2
    return 1
  fi
}

on_exit() {
  local rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ] && [ "$KEEP_STAGING" -ne 1 ]; then
    if [ "$DISCARD_PARTIAL_ON_FAILURE" = "1" ]; then
      discard_partial || true
    else
      quarantine_partial || true
    fi
  fi
  exit "$rc"
}
trap on_exit EXIT

find_python() {
  if [ -n "${PYTHON_BIN:-}" ]; then
    if [ -x "$PYTHON_BIN" ] || command -v "$PYTHON_BIN" >/dev/null 2>&1; then
      printf '%s\n' "$PYTHON_BIN"
      return 0
    fi
    return 1
  fi
  if [ -x ./.venv/bin/python ]; then
    printf '%s\n' ./.venv/bin/python
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  elif command -v python >/dev/null 2>&1; then
    command -v python
  else
    return 1
  fi
}

[ -d "$DATA_DIR" ] || fail "未找到 data 目录"
[ -f "$GUARD" ] || fail "缺少快照校验器"
case "$DISCARD_PARTIAL_ON_FAILURE" in
  0|1) : ;;
  *) fail "BACKUP_DISCARD_PARTIAL_ON_FAILURE 只能为 0 或 1" ;;
esac
case "$STAMP" in
  [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) : ;;
  *) fail "快照时间戳非法" ;;
esac
PYTHON_BIN="$(find_python)" || fail "找不到 Python，无法执行备份完整性校验"
# 高权限备份绝不能把文件系统根当成目标。先解析父级软链接与 `..`，再
# 构造任何 staging 路径；验证失败时 DEST 仍为空，退出 trap 不会触碰磁盘。
if DEST_ROOT=$("$PYTHON_BIN" -I - "$DEST_ROOT" <<'PY'
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
  fail "备份根目录解析后不安全"
fi
FINAL_DEST="$DEST_ROOT/$STAMP"
DEST="${BACKUP_STAGING_PATH:-$DEST_ROOT/.$STAMP.partial.$$}"
if [ -n "${BACKUP_STAGING_PATH:-}" ]; then
  [ -n "${BACKUP_STAMP_OVERRIDE:-}" ] || fail "显式 staging 必须同时锁定时间戳"
  [ "$(dirname "$DEST")" = "$DEST_ROOT" ] || fail "staging 必须位于备份根目录"
  STAGE_BASE=$(basename "$DEST")
  STAGE_PID=${STAGE_BASE#.$STAMP.partial.}
  if [ "$STAGE_BASE" = "$STAGE_PID" ] || [[ ! "$STAGE_PID" =~ ^[0-9]+$ ]]; then
    fail "staging 名称非法"
  fi
fi

# 脚本当前只实现 SQLite 快照。不能在 Postgres 部署中悄悄拷一份陈旧
# data/app.db；即使是 SQLite，活跃 URL 也必须精确指向本脚本要快照的文件。
if "$PYTHON_BIN" -I - "$PWD/$DATA_DIR/app.db" "$PWD/.env" <<'PY'
import os
from pathlib import Path
import sys

from sqlalchemy.engine import make_url

source = Path(sys.argv[1]).resolve()
env_path = Path(sys.argv[2])
configured = os.environ.get("DATABASE_URL")
if configured is not None:
    configured = configured.strip()
    # An explicitly blank process environment would otherwise suppress the
    # deployed .env and silently fall through to a stale data/app.db.
    if not configured:
        raise SystemExit(5)
elif env_path.is_file():
    matches = []
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            if line.split(None, 1)[0] == "DATABASE_URL":
                raise SystemExit(5)
            continue
        key, value = line.split("=", 1)
        if key.strip() != "DATABASE_URL":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not value:
            raise SystemExit(5)
        matches.append(value)
    if len(matches) > 1:
        raise SystemExit(5)
    configured = matches[0] if matches else None
if not configured:
    raise SystemExit(0)
try:
    parsed = make_url(configured)
except Exception:
    raise SystemExit(5)
if parsed.get_backend_name() != "sqlite":
    raise SystemExit(3)
database = parsed.database
if not database or database == ":memory:" or database.startswith("file:"):
    raise SystemExit(4)
target = Path(database)
if not target.is_absolute():
    target = Path.cwd() / target
if target.resolve() != source:
    raise SystemExit(4)
PY
then
  :
else
  DB_CONTRACT_RC=$?
  case "$DB_CONTRACT_RC" in
    3) fail "当前数据库不是 SQLite；Postgres pg_dump/restore 链未实现，拒绝伪备份" ;;
    4) fail "当前 SQLite URL 与 data/app.db 不一致，拒绝备份陈旧文件" ;;
    *) fail "无法安全解析数据库备份合同" ;;
  esac
fi
mkdir -p "$DEST_ROOT"
chmod 700 "$DEST_ROOT"
[ ! -e "$FINAL_DEST" ] || fail "同时间戳快照已存在，拒绝覆盖"
mkdir "$DEST"
chmod 700 "$DEST"

# 数据库:只做 SQLite 在线 backup(服务运行中也能拿到一致数据库时点)。
# 数据库和文件系统无法在无全局锁时获得同一时点，所以拷贝后还会执行
# DB↔audio 双向校验。并发删除/上传造成不一致时 fail-closed，绝不产生成功快照。
if [ -f "$DATA_DIR/app.db" ]; then
  "$PYTHON_BIN" -I - "$DATA_DIR/app.db" "$DEST/app.db" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1], timeout=30)
target = sqlite3.connect(sys.argv[2], timeout=30)
try:
    with target:
        source.backup(target)
    result = target.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise SystemExit("backup integrity_check failed")
finally:
    target.close()
    source.close()
PY
  echo "数据库:Python sqlite3 在线快照 + 完整 integrity_check ✓"
else
  fail "SQLite app.db 不存在，拒绝产生空/诱饵快照"
fi

# 音频根是平铺、不可覆盖的 final blobs。隐藏 pending/lock 是运行时协调物，
# 不是备份证据；只拷贝非隐藏普通文件，再由 guard 按 DB 收据双向闭包。
if [ -d "$DATA_DIR/audio" ]; then
  [ ! -L "$DATA_DIR/audio" ] || fail "音频根目录不可为软链接"
  find "$DATA_DIR/audio" -mindepth 1 -maxdepth 1 -print >/dev/null 2>&1 \
    || fail "音频根目录扫描失败(code=audio_scan_failed)"
  while IFS= read -r -d '' hidden_audio; do
    hidden_name=${hidden_audio##*/}
    [ ! -L "$hidden_audio" ] && [ -f "$hidden_audio" ] \
      || fail "音频根目录存在非普通隐藏条目"
    if [[ "$hidden_name" =~ ^\.[A-Za-z0-9][A-Za-z0-9._-]{0,159}\.upload\.lock$ ]]; then
      :
    elif [[ "$hidden_name" =~ ^\.[A-Za-z0-9][A-Za-z0-9._-]{0,159}\.[A-Za-z0-9_-]+\.pending$ ]]; then
      :
    else
      fail "音频根目录存在未知隐藏条目"
    fi
  done < <(find "$DATA_DIR/audio" -mindepth 1 -maxdepth 1 -name '.*' -print0 2>/dev/null)
  if ! nonregular_audio=$(find "$DATA_DIR/audio" -mindepth 1 -maxdepth 1 \
      ! -name '.*' ! -type f -print -quit 2>/dev/null); then
    fail "音频根目录扫描失败(code=audio_scan_failed)"
  fi
  if [ -n "$nonregular_audio" ]; then
    fail "音频根目录存在非普通 final 条目"
  fi
  if audio_first=$(find "$DATA_DIR/audio" -mindepth 1 -maxdepth 1 \
      -type f ! -name '.*' -print -quit 2>/dev/null); then
    :
  else
    fail "音频根目录扫描失败(code=audio_scan_failed)"
  fi
  if [ -n "$audio_first" ]; then
    mkdir "$DEST/audio"
    audio_count=0
    while IFS= read -r -d '' source_audio; do
      cp -p "$source_audio" "$DEST/audio/" >/dev/null 2>&1 \
        || fail "音频复制失败(code=audio_copy_failed)"
      audio_count=$((audio_count + 1))
    done < <(find "$DATA_DIR/audio" -mindepth 1 -maxdepth 1 \
      -type f ! -name '.*' -print0 2>/dev/null)
    echo "audio/:$audio_count 个 DB 待校验 final 文件"
  else
    echo "audio/:无 final 文件,跳过"
  fi
else
  echo "audio/:不存在,跳过"
fi

for sub in exports controlled-audio-exports; do
  source_export="$DATA_DIR/$sub"
  if [ -e "$source_export" ] || [ -L "$source_export" ]; then
    [ -d "$source_export" ] && [ ! -L "$source_export" ] \
      || fail "导出源目录类型非法(code=export_source_invalid)"
    find "$source_export" -print >/dev/null 2>&1 \
      || fail "导出源目录扫描失败(code=export_scan_failed)"
    if empty_export_directory=$(find "$source_export" -mindepth 1 \
        -type d -empty -print -quit 2>/dev/null); then
      :
    else
      fail "导出源目录扫描失败(code=export_scan_failed)"
    fi
    [ -z "$empty_export_directory" ] \
      || fail "导出源含未结算空目录(code=export_empty_directory)"
    if export_first=$(find "$source_export" -mindepth 1 -print -quit 2>/dev/null); then
      :
    else
      fail "导出源目录扫描失败(code=export_scan_failed)"
    fi
  else
    export_first=""
  fi
  if [ -n "$export_first" ]; then
    cp -R "$source_export" "$DEST/$sub" >/dev/null 2>&1 \
      || fail "导出副本复制失败(code=export_copy_failed)"
    if ! copied_count=$(find "$DEST/$sub" -type f -print 2>/dev/null \
        | wc -l | tr -d ' '); then
      fail "导出副本计数失败(code=export_count_failed)"
    fi
    echo "$sub/:$copied_count 个文件 ✓"
  else
    echo "$sub/:空或不存在,跳过"
  fi
done

# 先做语义校验，捕获 DB 快照与音频拷贝之间的删除/上传竞态。
find "$DEST" -type d -exec chmod 700 {} + >/dev/null 2>&1 \
  || fail "快照目录权限收紧失败(code=snapshot_permission_failed)"
find "$DEST" -type f -exec chmod 600 {} + >/dev/null 2>&1 \
  || fail "快照文件权限收紧失败(code=snapshot_permission_failed)"
"$PYTHON_BIN" -I "$GUARD" verify-audio "$DEST" >/dev/null

# sha256 清单必须覆盖快照中除清单自身外的每个普通文件。
if command -v shasum >/dev/null 2>&1; then
  ( cd "$DEST" && find . -type f ! -name MANIFEST.sha256 \
      -exec shasum -a 256 {} + > MANIFEST.sha256 ) 2>/dev/null \
    || fail "快照清单生成失败(code=manifest_generation_failed)"
  SHA_LABEL="shasum -a 256"
elif command -v sha256sum >/dev/null 2>&1; then
  ( cd "$DEST" && find . -type f ! -name MANIFEST.sha256 \
      -exec sha256sum {} + > MANIFEST.sha256 ) 2>/dev/null \
    || fail "快照清单生成失败(code=manifest_generation_failed)"
  SHA_LABEL="sha256sum"
else
  fail "找不到 shasum/sha256sum"
fi

# 来源权限不得被 cp 带入研究备份；校验后才可发布。
find "$DEST" -type d -exec chmod 700 {} + >/dev/null 2>&1 \
  || fail "快照目录权限收紧失败(code=snapshot_permission_failed)"
find "$DEST" -type f -exec chmod 600 {} + >/dev/null 2>&1 \
  || fail "快照文件权限收紧失败(code=snapshot_permission_failed)"
"$PYTHON_BIN" -I "$GUARD" verify "$DEST" >/dev/null

echo "清单:$DEST/MANIFEST.sha256($(wc -l < "$DEST/MANIFEST.sha256" | tr -d ' ') 条)"
echo "校验命令:$PYTHON_BIN -I $GUARD verify <快照目录>"

if [ "${BACKUP_STAGING_ONLY:-0}" = "1" ]; then
  # VPS daily 还要追加配置。交给它隐藏 staging，不提前暴露时间戳完成态。
  KEEP_STAGING=1
  printf 'BACKUP_STAMP=%s\n' "$STAMP"
  printf 'BACKUP_STAGING_PATH=%s\n' "$DEST"
  echo "备份基础快照已校验，等待 daily 追加配置"
else
  "$PYTHON_BIN" -I "$GUARD" publish "$DEST" "$FINAL_DEST" >/dev/null
  DEST=""
  echo "备份完成 → $FINAL_DEST"
fi
