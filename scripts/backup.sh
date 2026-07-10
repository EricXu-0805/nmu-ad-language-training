#!/usr/bin/env bash
# 本机数据备份:数据库 + 音频字节 + 导出批次 → data/backups/<时间戳>/,附 sha256 清单。
# 患者数据不出机构:备份目的地默认仍在本机;要落到院内加密移动盘/内网共享盘,用 BACKUP_DIR=/Volumes/xxx ./scripts/backup.sh。
# 恢复:停服(Ctrl-C serve.sh)→ 用备份目录里的 app.db / audio/ 覆盖 data/ 下同名 → 重启 serve.sh(会自动 alembic upgrade)。
# 注:PostgreSQL 部署时数据库不在 data/app.db,需另用 pg_dump;本脚本只覆盖 SQLite 形态。
set -euo pipefail
cd "$(dirname "$0")/.."

DATA_DIR="data"
DEST_ROOT="${BACKUP_DIR:-$DATA_DIR/backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$DEST_ROOT/$STAMP"

[ -d "$DATA_DIR" ] || { echo "未找到 $DATA_DIR/,无可备份数据"; exit 1; }
mkdir -p "$DEST"

# 数据库:优先 sqlite3 在线 .backup(服务运行中也能拿到一致快照),无 sqlite3 CLI 再退回 cp
if [ -f "$DATA_DIR/app.db" ]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    # cd 进目的地用相对文件名写快照:dot-command 里不内插路径,BACKUP_DIR 带撇号/空格(如 /Volumes/Eric's Disk)也安全
    ( cd "$DEST" && sqlite3 "$OLDPWD/$DATA_DIR/app.db" ".backup app.db" )
    echo "数据库:sqlite3 在线快照 ✓"
  else
    cp "$DATA_DIR/app.db" "$DEST/app.db"
    echo "数据库:cp 副本(无 sqlite3 CLI;建议停服后备份以保证一致性)"
  fi
else
  echo "数据库:$DATA_DIR/app.db 不存在,跳过"
fi

for sub in audio exports; do
  if [ -d "$DATA_DIR/$sub" ] && [ -n "$(ls -A "$DATA_DIR/$sub" 2>/dev/null)" ]; then
    cp -R "$DATA_DIR/$sub" "$DEST/$sub"
    echo "$sub/:$(find "$DEST/$sub" -type f | wc -l | tr -d ' ') 个文件 ✓"
  else
    echo "$sub/:空或不存在,跳过"
  fi
done

# sha256 清单:恢复前先校验,防备份介质坏块静默损坏
SHA_CMD="shasum -a 256"; command -v shasum >/dev/null 2>&1 || SHA_CMD="sha256sum"
( cd "$DEST" && find . -type f ! -name MANIFEST.sha256 -exec $SHA_CMD {} + > MANIFEST.sha256 )
echo "清单:$DEST/MANIFEST.sha256($(wc -l < "$DEST/MANIFEST.sha256" | tr -d ' ') 条)"
echo "校验命令:cd \"$DEST\" && $SHA_CMD -c MANIFEST.sha256"

echo "备份完成 → $DEST($(du -sh "$DEST" | cut -f1))"
