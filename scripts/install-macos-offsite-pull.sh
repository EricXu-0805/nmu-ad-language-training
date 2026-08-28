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
# 路径里不许有空格。第一版放在 ~/Library/Application Support/nmu-backup，config.env
# 被 `set -a; . config.env` 读进来时，"Application" 后面那半截直接被当成命令执行
# （`Support/nmu-backup/offsite: No such file or directory`）。拉取脚本本身有六百行
# shell、上百处路径展开，只要有一处没加引号就会以同样的方式在生产里炸——换个不带
# 空格的根目录，整类问题一次消掉。
ROOT="$HOME/Library/nmu-backup"
LEGACY_ROOT="$HOME/Library/Application Support/nmu-backup"
PLIST="$HOME/Library/LaunchAgents/com.nmu.vps-backup-pull.plist"
LABEL="com.nmu.vps-backup-pull"
SQLALCHEMY_PIN="2.0.51"

mkdir -p "$ROOT/runtime/scripts" "$ROOT/offsite"
chmod 700 "$ROOT" "$ROOT/runtime" "$ROOT/runtime/scripts" "$ROOT/offsite"

install -m 700 "$REPO/scripts/vps-backup-pull.sh" "$ROOT/runtime/scripts/vps-backup-pull.sh"
install -m 600 "$REPO/scripts/verify_backup_snapshot.py" "$ROOT/runtime/scripts/verify_backup_snapshot.py"
install -m 600 "$REPO/scripts/notify_ops.py" "$ROOT/runtime/scripts/notify_ops.py"
install -m 600 "$REPO/scripts/audit_anchor_check.py" "$ROOT/runtime/scripts/audit_anchor_check.py"

# 校验器的 SUPPORTED_ALEMBIC_HEADS 是写死的，必须与生产同版本，否则每份快照
# 都会被判成 legacy。把指纹留在运行时目录里，人工核对时一眼可比。
#
# 量的是**装好的那份**，不是 $REPO 里那份。两者此刻字节相同，所以值一样；
# 差别在于这个文件是 shasum -c 格式，路径写谁就校验谁。写 $REPO 的话，仓库
# 一往前走，`shasum -c verifier.sha256` 就报 FAILED——而那句 FAILED 跟
# "异地这份有没有被改过"毫无关系。2026-08-17 实测踩到过：仓库领先三个迁移头，
# 核对时当场 FAILED，真正的不变量其实成立。
shasum -a 256 "$ROOT/runtime/scripts/verify_backup_snapshot.py" > "$ROOT/runtime/verifier.sha256"
chmod 600 "$ROOT/runtime/verifier.sha256"

if [ ! -x "$ROOT/venv/bin/python" ]; then
  PY=$(command -v python3) || { echo "找不到 python3" >&2; exit 69; }
  "$PY" -m venv "$ROOT/venv"
fi
"$ROOT/venv/bin/python" -m pip install --quiet --disable-pip-version-check \
  "sqlalchemy==$SQLALCHEMY_PIN"

cat > "$ROOT/config.env" <<EOF
NMU_BACKUP_SSH_USER='$SSH_USER'
NMU_BACKUP_SSH_HOST='$SSH_HOST'
NMU_BACKUP_REMOTE_ROOT='$REMOTE_ROOT'
NMU_BACKUP_LOCAL_ROOT='$ROOT/offsite'
PYTHON_BIN='$ROOT/venv/bin/python'
EOF
chmod 600 "$ROOT/config.env"

# config.env 是被 `set -a; .` 读进去的。值没加引号时，bash 会把值里第一个空格
# 之后的部分当成一条命令去执行，而这个错误只会出现在 launchd 的日志里——第一版
# 就是这么静默失败的。这里直接查发出去的每一行是不是 KEY='...' 的形状，不靠执行。
while IFS= read -r config_line; do
  [ -n "$config_line" ] || continue
  [[ "$config_line" =~ ^[A-Z_]+=\'[^\']*\'$ ]] || {
    echo "config.env 有一行没有正确加引号：${config_line%%=*}" >&2
    exit 1
  }
done < "$ROOT/config.env"

cat > "$ROOT/run-pull.sh" <<EOF
#!/bin/bash
# launchd 入口:读配置、进运行时目录、跑拉取脚本;失败/超配额时把事实推到 Discord。
# 告警是失败才响的——2026-07-17 起三周静默失败没人知道,就是缺这一步。
set -euo pipefail
umask 077
set -a
. '$ROOT/config.env'
set +a
cd '$ROOT/runtime'
# 只报**本轮**写的行。2026-08-26 链断那次,tail -5 端出来的全是 8-22 起每晚一模一样的
# 稳态 held 项,真因(audit_anchor_check_failed)排在第 1 行、被挤出了消息。
log_before=\$(wc -l < '$ROOT/offsite/pull.log' 2>/dev/null || echo 0)
rc=0
./scripts/vps-backup-pull.sh || rc=\$?
this_run=\$(sed -n "\$((log_before + 1)),\\\$p" '$ROOT/offsite/pull.log' 2>/dev/null || true)
first_fail=\$(printf '%s\\n' "\$this_run" | grep -m1 ' FAIL ' || true)
fail_count=\$(printf '%s\\n' "\$this_run" | grep -c ' FAIL ' || true)
summary=\$(printf '%s\\n' "\$this_run" | grep -E ' (ok|partial) snapshots=' | tail -1 || true)
detail="本轮 FAIL \${fail_count} 条。第一条:\${first_fail:-(无)}
收尾:\${summary:-(本轮没写收尾行)}"
if [ "\$rc" -ne 0 ]; then
  if [ -f '$ROOT/ops-webhook.env' ]; then
    if [ "\$rc" -eq 3 ]; then
      msg="异地备份卷超软配额(拉取本身成功)。看 $ROOT/offsite/pull.log 尾行,提配额或降保留。"
    elif [ "\$rc" -eq 4 ]; then
      msg="异地备份部分完成:有快照进了 legacy/conflicts 等人工处置,其余已归档。
\$detail"
    else
      msg="异地备份拉取失败 rc=\${rc}。
\$detail"
    fi
    '$ROOT/venv/bin/python' ./scripts/notify_ops.py \\
      --env-file '$ROOT/ops-webhook.env' --message "\$msg" || true
  else
    echo "ops-webhook.env 不存在,失败告警未发" >&2
  fi
fi
exit "\$rc"
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

if [ -d "$LEGACY_ROOT" ]; then
  # 变量后面紧跟全角标点必须加花括号：bash 在 UTF-8 locale 下会把那几个字节
  # 当成变量名的一部分，set -u 直接报一个查不出来的 unbound variable。
  echo "提示：旧的带空格根目录还在 ${LEGACY_ROOT}，确认无快照后自行 trash 掉。"
fi

echo "已安装到 $ROOT"
echo "现在立刻跑一次，验证 launchd 这条路真的通："
launchctl kickstart -k "gui/$(id -u)/$LABEL"

job_state() {
  launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null \
    | awk -F'= ' '/^\tstate = /{print $2; exit}'
}

# 首轮要把远端十几份快照逐个校验、按 manifest 白名单二次 rsync，十分钟是常态。
# 第一版只等两分钟就去读退出码，读到的是 "(never exited)"，于是把一次正在正常
# 运行的拉取报成了失败。kickstart 到 launchd 状态翻成 running 也有延迟——
# 第二版在翻转前就读了一次"不在跑"，跳过等待直接判失败。先等它真的启动。
echo "等它跑完（首轮可能十几分钟）……"
started=0
waited=0
while [ "$waited" -lt 1800 ]; do
  state=$(job_state)
  if [ "$state" = "running" ]; then
    started=1
  elif [ "$started" -eq 1 ] || [ "$waited" -ge 60 ]; then
    break
  fi
  sleep 10
  waited=$((waited + 10))
done

if [ "$(job_state)" = "running" ]; then
  echo "等了 ${waited} 秒还在跑。自己盯：launchctl print gui/$(id -u)/$LABEL" >&2
  exit 1
fi

status=$(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null \
  | awk -F'= ' '/last exit code/{print $2; exit}')
echo "launchd 上次退出码: ${status:-未知}"
tail -3 "$ROOT/offsite/pull.log" 2>/dev/null || echo "(还没有 pull.log)"
case "${status:-1}" in
  0) echo "异地拉取这条路已经跑通，每天 12:30 自动执行。" ;;
  # 退出码 4 = 拉取本身成功，但有快照进了 legacy/conflicts 等人工处置。
  # 迁移头一前进，升级前拍的所有快照都会一次性变成这个状态——那是设计。
  # 第一版把 4 也报成"不通"，于是每次带迁移的上线之后都会有人被派去查一个
  # 根本不存在的故障。装没装上和有没有待处置的快照，是两件事。
  4) echo "异地拉取这条路已经跑通，每天 12:30 自动执行。"
     echo "注意：有快照待人工处置（见上面 pull.log 尾部与 $ROOT/offsite/legacy-unvalidated/）。" >&2
     echo "刚做过迁移头前进的话，这是预期结果，不是故障。" >&2 ;;
  *) echo "launchd 这一路仍然不通，看 $HOME/Library/Logs/nmu-vps-backup-pull.log" >&2
     exit 1 ;;
esac
