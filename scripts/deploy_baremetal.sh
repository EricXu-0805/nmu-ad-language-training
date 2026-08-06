#!/usr/bin/env bash
# 裸机部署 / 回滚。把 2026-08-06 那次人工上线的顺序钉成脚本，别再靠记忆重演。
#
# 硬规矩（每一条都对应踩过的坑）：
#   - 永远 --exclude=.env 和 --exclude=data/。2026-07-23 那次 `rsync --delete`
#     把线上 .env 删了，是靠手里另存的副本救回来的。
#   - 永远不用 --delete。线上多出来的陈旧文件由人工清，不由同步顺手删。
#   - 动任何东西之前先出一份**通过校验**的快照。没有可回滚点就不许上线。
#   - 上线后跑 preflight；不通过不自动回滚，而是打印回滚命令让人来决定——
#     自动回滚脚本自己出错时，破坏比它要修的更大。
#
# 用法：
#   scripts/deploy_baremetal.sh deploy   <user@host> [公网入口]
#   scripts/deploy_baremetal.sh rollback <user@host> [公网入口]  # 用上次的存档回退
# 给了公网入口，验收才会连红线与受保护路由一起查；不给就只查本机那两组。
set -euo pipefail
umask 077

MODE="${1:-}"
TARGET="${2:-}"
BASE_URL="${3:-}"
case "$MODE" in
  deploy|rollback) : ;;
  *) echo "用法: $0 deploy|rollback <user@host> [公网入口]" >&2; exit 64 ;;
esac
[ -n "$TARGET" ] || { echo "缺少 <user@host>" >&2; exit 64; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

APP_DIR=/opt/nmu/app
VENV_PY=/opt/nmu/venv/bin/python
BACKUP_ROOT=/opt/nmu/backups
STATE_FILE=/opt/nmu/last-deploy.state
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15)

remote() { "${SSH[@]}" "$TARGET" "$@"; }

# 给了公网入口就连红线与受保护路由一起验；没给就只验本机那两组，并在输出里
# 留下 SKIP，不假装全验过了。
preflight_url=""
[ -z "$BASE_URL" ] || preflight_url="--base-url $BASE_URL"

stamp=$(date +%Y%m%d-%H%M%S)

if [ "$MODE" = "rollback" ]; then
  echo "== 读取上一次部署记录 =="
  state=$(remote "cat $STATE_FILE") || {
    echo "没有部署记录，无法自动回退。" >&2; exit 1; }
  printf '%s\n' "$state"
  archive=$(printf '%s\n' "$state" | awk -F'= ' '/^archive/{print $2}')
  [ -n "$archive" ] || { echo "记录里没有 archive 路径" >&2; exit 1; }
  remote "test -f '$archive'" || { echo "存档不在了：$archive" >&2; exit 1; }

  echo "== 停服 → 还原代码 → 起服 =="
  remote "systemctl stop nmu.service"
  # 只还原代码。数据库不动：迁移是向前的，回退数据库要按 DEPLOY.md 8.1 的
  # 停服恢复 SOP 走候选卷，不在这个脚本的职责里。
  remote "rm -rf ${APP_DIR}.rollback-$stamp && mv $APP_DIR ${APP_DIR}.rollback-$stamp"
  remote "tar -xzf '$archive' -C /opt/nmu"
  remote "systemctl start nmu.service"
  echo "== 代码已回到存档版本。数据库仍在新头，若结构不兼容必须按 DEPLOY.md 8.1 处置。 =="
  remote "$VENV_PY $APP_DIR/scripts/preflight_check.py --db $APP_DIR/data/app.db --backup-root $BACKUP_ROOT --lock $APP_DIR/requirements-deploy.lock.txt $preflight_url" || true
  exit 0
fi

echo "== 0. 本地工作树必须干净 =="
if [ -n "$(git status --porcelain)" ]; then
  echo "工作树不干净。发布包必须可追溯到一个 commit，先提交或 stash。" >&2
  exit 1
fi
head_commit=$(git rev-parse HEAD)
echo "   commit $head_commit"

echo "== 1. 先出一份通过校验的快照 =="
remote "systemctl start nmu-backup.service"
snapshot=$(remote "ls -1 $BACKUP_ROOT/daily | tail -1")
[ -n "$snapshot" ] || { echo "没有可用快照，拒绝上线。" >&2; exit 1; }
remote "$VENV_PY -I $APP_DIR/scripts/verify_backup_snapshot.py verify-vps $BACKUP_ROOT/daily/$snapshot" \
  || { echo "快照校验没过，拒绝上线。" >&2; exit 1; }
echo "   快照 $snapshot 已验"

echo "== 2. 存档当前代码 =="
archive="/opt/nmu/app-before-deploy-$stamp.tar.gz"
remote "tar -czf '$archive' -C /opt/nmu --exclude=app/data --exclude=app/.git app"
remote "test -s '$archive'"
echo "   $archive"

previous_head=$(remote "cd $APP_DIR && $VENV_PY -m alembic current 2>/dev/null | tail -1")

echo "== 3. 停服 =="
remote "systemctl stop nmu.service"

echo "== 4. 同步代码（不带 --delete，排除 .env 与 data/）=="
rsync -a \
  --exclude='.env' --exclude='.env.*' --exclude='data/' --exclude='.venv/' \
  --exclude='web/node_modules/' --exclude='.git/' --exclude='.remember/' \
  --exclude='.claude/' --exclude='output/' --exclude='__pycache__/' \
  --exclude='.pytest_cache/' --exclude='.ruff_cache/' --exclude='.gstack/' \
  --exclude='.playwright-cli/' --exclude='caddy_data/' --exclude='*.before-*' \
  --exclude='.DS_Store' \
  -e "${SSH[*]}" ./ "$TARGET:$APP_DIR/"

echo "== 5. 迁移 =="
remote "cd $APP_DIR && $VENV_PY -m alembic upgrade head"

echo "== 6. 起服 =="
remote "systemctl start nmu.service"

remote "printf 'commit = %s\narchive = %s\nsnapshot = %s\nprevious_head = %s\nat = %s\n' \
  '$head_commit' '$archive' '$snapshot' '$previous_head' '$stamp' > $STATE_FILE"
remote "chmod 600 $STATE_FILE"

echo "== 7. 验收 =="
if remote "$VENV_PY $APP_DIR/scripts/preflight_check.py --db $APP_DIR/data/app.db --backup-root $BACKUP_ROOT --lock $APP_DIR/requirements-deploy.lock.txt $preflight_url"; then
  # 变量后面紧跟全角标点必须加花括号：bash 在 UTF-8 locale 下会把那几个字节
  # 算进变量名，set -u 报一个查不出来的 unbound variable。
  echo "上线完成。commit ${head_commit}，回滚存档 ${archive}"
else
  echo "" >&2
  echo "preflight 没过。不自动回滚——自动回滚脚本自己出错时破坏更大。" >&2
  echo "要回退就跑：$0 rollback $TARGET" >&2
  echo "（该命令只还原代码；数据库已在新头，结构不兼容时按 DEPLOY.md 8.1 处置）" >&2
  exit 1
fi
