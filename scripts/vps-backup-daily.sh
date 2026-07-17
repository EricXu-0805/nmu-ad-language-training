#!/usr/bin/env bash
# VPS 每日自动备份包装(systemd nmu-backup.timer 调用):复用 scripts/backup.sh 做一致性快照,
# 外加:并发锁 / 磁盘护栏 / 14 份轮转 / backup.log 审计行——运维真相看审计文件,不只看 systemd 状态。
# 快照额外附 config/(.env、Caddyfile、systemd 单元),整机重建时照单恢复。
# 异地副本由 Eric Mac 端 launchd 每日 rsync 拉走(scripts/vps-backup-pull.sh),患者数据不经第三方云。
# 仅面向 Ubuntu VPS(GNU 工具链);勿在 macOS 直接跑。
set -u
APP=/opt/nmu/app
ROOT=/opt/nmu/backups
DAILY=$ROOT/daily
LOG=$ROOT/backup.log
KEEP=14
# 快照目录严格形如 20260718-033000;轮转/认领只碰这个形状,运维手工留档(如 xxx.bak)不受波及
SNAP_GLOB='[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]'
mkdir -p "$DAILY"
chmod 700 "$ROOT"
note() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# 并发锁:timer 与手工补跑同时进来,后到者直接退(backup.sh 的秒级时间戳目录会互踩)
exec 9>"$ROOT/.lock"
if ! flock -n 9; then
  note "SKIP 另一个备份进程持锁中"
  exit 0
fi

# 1GB 小机磁盘护栏:可用 <200MB 时宁可跳过本次也不能把生产盘写满
FREE_MB=$(df -Pm "$ROOT" | awk 'NR==2{print $4}')
if [ "${FREE_MB:-0}" -lt 200 ]; then
  note "FAIL free=${FREE_MB}MB <200MB,跳过本次备份,请先清盘"
  exit 1
fi

newest_snap() { ls -1d "$DAILY"/$SNAP_GLOB 2>/dev/null | sort | tail -1; }

PRE=$(newest_snap)
if ! OUT=$(cd "$APP" && BACKUP_DIR="$DAILY" ./scripts/backup.sh 2>&1); then
  note "FAIL backup.sh: $(echo "$OUT" | tail -3 | tr '\n' ' ')"
  # 中途死掉的残缺快照(可能无 MANIFEST/半份 audio)改名隔离:
  # 不占轮转名额、不被误当可恢复快照;7 天后由下方清理
  POST=$(newest_snap)
  if [ -n "$POST" ] && [ "$POST" != "$PRE" ]; then
    mv "$POST" "$POST.failed" && note "残缺快照已隔离为 $(basename "$POST").failed"
  fi
  exit 1
fi

# 认领本次快照:先信 backup.sh 自己报的目的地(最后一行"备份完成 → 路径(体积)"),解析不出再按名序兜底
NEW=$(echo "$OUT" | sed -n 's/^备份完成 → \(.*\)(.*/\1/p' | tail -1)
NEW=${NEW##*/}
case "$NEW" in
  [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]) : ;;
  *) NEW=$(basename "$(newest_snap)") ;;
esac
if [ -z "$NEW" ] || [ ! -d "$DAILY/$NEW" ]; then
  note "FAIL 无法认领本次快照目录(输出尾行:$(echo "$OUT" | tail -1))"
  exit 1
fi

# 配置快照(重建整机要用)。.env 含密钥,落盘必须 600;任一缺失都要在审计行里看得见
CONF="$DAILY/$NEW/config"
mkdir -p "$CONF"
chmod 700 "$CONF"
CF_MISS=""
if [ -f "$APP/.env" ]; then install -m 600 "$APP/.env" "$CONF/env"; else CF_MISS="$CF_MISS .env"; fi
for f in /opt/nmu/caddy/Caddyfile /etc/systemd/system/nmu.service /etc/systemd/system/nmu-caddy.service; do
  if [ -f "$f" ]; then cp -p "$f" "$CONF/"; else CF_MISS="$CF_MISS $(basename "$f")"; fi
done
( cd "$DAILY/$NEW" && find config -type f -exec sha256sum {} + >> MANIFEST.sha256 )
CF_RES="config=ok"
[ -n "$CF_MISS" ] && CF_RES="config=MISSING(${CF_MISS# })"

# 轮转:严格形状 + 按名字序(POSIX 写法,不用 GNU head -n -N),留最近 KEEP 份;隔离的残缺快照 7 天后清
ls -1d "$DAILY"/$SNAP_GLOB 2>/dev/null | sort -r | tail -n +$((KEEP + 1)) | while read -r old; do
  rm -rf "$old"
done
find "$DAILY" -maxdepth 1 -name "*.failed" -mtime +7 -exec rm -rf {} +

SZ=$(du -sh "$DAILY/$NEW" 2>/dev/null | cut -f1)
note "ok snapshot=$NEW size=$SZ $CF_RES keep=$KEEP free=${FREE_MB}MB"
case "$CF_RES" in *MISSING*) exit 1 ;; esac
