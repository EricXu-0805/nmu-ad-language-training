#!/usr/bin/env bash
# 在受控 macOS 工作站上装好"异地备份自动拉取"，并让它真的能被 launchd 跑起来。
#
# 为什么需要这个脚本，而不是直接把 plist 指向仓库里的 vps-backup-pull.sh：
#   1. 仓库在 ~/Downloads 下。macOS TCC 保护 Desktop/Documents/Downloads，
#      launchd 起的进程没有用户授权，打开那里的文件一律 "Operation not
#      permitted"。~/Library/Logs/nmu-vps-backup-pull.log 里从 2026-07-17 到
#      2026-08-05 每一天都是这一行，异地副本从来没有自动拉过一次。
#   2. 就算 TCC 放行，原 plist 也没有 EnvironmentVariables，脚本三个必填变量
#      一个都拿不到，会直接 exit 64。
#   3. 校验器 import sqlalchemy，而机器上 Downloads 之外没有任何一个 python
#      装了它。
# 所以运行时整体搬到 ~/Library/Application Support/nmu-backup 下：脚本副本、
# 专用 venv、配置、异地副本根，全在 TCC 管辖之外。
#
# 用法：
#   scripts/install-macos-offsite-pull.sh <ssh-user> <ssh-host> [remote-root]
# 装完立刻跑一次并打印结论；不成功就非零退出，不留"装好了"的假象。
set -euo pipefail
umask 077

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo "用法: $0 <ssh-user> <ssh-host> [remote-root]" >&2
  exit 64
fi
SSH_USER="$1"
SSH_HOST="$2"
REMOTE_ROOT="${3:-/opt/nmu/backups}"

[ "$(uname -s)" = "Darwin" ] || { echo "只面向 macOS 工作站" >&2; exit 64; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$HOME/Library/Application Support/nmu-backup"
PLIST="$HOME/Library/LaunchAgents/com.nmu.vps-backup-pull.plist"
LABEL="com.nmu.vps-backup-pull"
SQLALCHEMY_PIN="2.0.51"

mkdir -p "$ROOT/runtime/scripts" "$ROOT/offsite"
chmod 700 "$ROOT" "$ROOT/runtime" "$ROOT/runtime/scripts" "$ROOT/offsite"

install -m 700 "$REPO/scripts/vps-backup-pull.sh" "$ROOT/runtime/scripts/vps-backup-pull.sh"
install -m 600 "$REPO/scripts/verify_backup_snapshot.py" "$ROOT/runtime/scripts/verify_backup_snapshot.py"

# 校验器的 SUPPORTED_ALEMBIC_HEADS 是写死的，必须与生产同版本，否则每份快照
# 都会被判成 legacy。把仓库当前的指纹留在运行时目录里，人工核对时一眼可比。
shasum -a 256 "$REPO/scripts/verify_backup_snapshot.py" > "$ROOT/runtime/verifier.sha256"
chmod 600 "$ROOT/runtime/verifier.sha256"

if [ ! -x "$ROOT/venv/bin/python" ]; then
  PY=$(command -v python3) || { echo "找不到 python3" >&2; exit 69; }
  "$PY" -m venv "$ROOT/venv"
fi
"$ROOT/venv/bin/python" -m pip install --quiet --disable-pip-version-check \
  "sqlalchemy==$SQLALCHEMY_PIN"

cat > "$ROOT/config.env" <<EOF
NMU_BACKUP_SSH_USER=$SSH_USER
NMU_BACKUP_SSH_HOST=$SSH_HOST
NMU_BACKUP_REMOTE_ROOT=$REMOTE_ROOT
NMU_BACKUP_LOCAL_ROOT=$ROOT/offsite
PYTHON_BIN=$ROOT/venv/bin/python
EOF
chmod 600 "$ROOT/config.env"

cat > "$ROOT/run-pull.sh" <<'EOF'
#!/bin/bash
# launchd 入口。只做三件事：读配置、进运行时目录、跑拉取脚本。
set -euo pipefail
umask 077
ROOT="$HOME/Library/Application Support/nmu-backup"
set -a
. "$ROOT/config.env"
set +a
cd "$ROOT/runtime"
exec ./scripts/vps-backup-pull.sh
EOF
chmod 700 "$ROOT/run-pull.sh"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/run-pull.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>12</integer>
    <key>Minute</key>
    <integer>30</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/nmu-vps-backup-pull.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/nmu-vps-backup-pull.log</string>
</dict>
</plist>
EOF
chmod 644 "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "已安装到 $ROOT"
echo "现在立刻跑一次，验证 launchd 这条路真的通："
launchctl kickstart -k "gui/$(id -u)/$LABEL"

for _ in $(seq 1 60); do
  state=$(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | awk '/^\tstate = /{print $3}')
  [ "$state" = "running" ] || break
  sleep 2
done

status=$(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null \
  | awk '/last exit code = /{print $NF}')
echo "launchd 上次退出码: ${status:-未知}"
tail -3 "$ROOT/offsite/pull.log" 2>/dev/null || echo "(还没有 pull.log)"
[ "${status:-1}" = "0" ] || {
  echo "launchd 这一路仍然不通，看 $HOME/Library/Logs/nmu-vps-backup-pull.log" >&2
  exit 1
}
