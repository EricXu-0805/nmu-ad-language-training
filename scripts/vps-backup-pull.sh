#!/usr/bin/env bash
# Eric Mac 端:把 VPS 每日备份拉回本机异地副本(launchd com.nmu.vps-backup-pull 每日调用;
# Mac 睡过点会在唤醒后补跑,关机错过就等下一天——VPS 端自身留 14 份,不依赖本端天天在线)。
# 威胁模型:这是"存档"不是"镜像"——故意不用 --delete,VPS 端快照被误删/清空/加密改写
# 不会传播过来抹掉本地历史;本地自行按份数修剪(60 份≈2 个月),深历史再由 Time Machine 兜底。
# 目的地 data/vps-backups/ 在 gitignore 内(患者数据不入库);流量只在自有机器间,不经第三方云。
set -euo pipefail
cd "$(dirname "$0")/.."
DEST=data/vps-backups
KEEP=60
SNAP_GLOB='[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]'
mkdir -p "$DEST/daily"
LOG=$DEST/pull.log
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15"

# 已有快照目录内容不可变(VPS 端写完才轮到拉),--ignore-existing 之类不必要;新目录整份进来即可。
# backup.log 单独拉一份现值;pull.log 本身就是本端独立的审计史,远端日志被截断不影响它。
if rsync -a --timeout=120 -e "$SSH" \
     root@89.208.253.119:/opt/nmu/backups/daily/ "$DEST/daily/" \
   && rsync -a --timeout=60 -e "$SSH" \
     root@89.208.253.119:/opt/nmu/backups/backup.log "$DEST/backup.log"; then
  # 本地修剪:严格形状按名序留最近 KEEP 份;VPS 端隔离的 *.failed 残缺快照本地留 30 天后清
  ls -1d "$DEST/daily"/$SNAP_GLOB 2>/dev/null | sort -r | tail -n +$((KEEP + 1)) | while read -r old; do
    rm -rf "$old"
  done
  find "$DEST/daily" -maxdepth 1 -name "*.failed" -mtime +30 -exec rm -rf {} +
  echo "[$(date '+%F %T')] ok snapshots=$(ls -1d "$DEST/daily"/$SNAP_GLOB 2>/dev/null | wc -l | tr -d ' ')" >> "$LOG"
else
  RC=$?
  echo "[$(date '+%F %T')] FAIL rsync rc=$RC" >> "$LOG"
  exit "$RC"
fi
