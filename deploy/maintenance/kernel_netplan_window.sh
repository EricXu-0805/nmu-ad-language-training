#!/usr/bin/env bash
# 内核与 netplan 维护窗口(2026-10-02 主机维护评审的 B1 方案)。
# 入库时还没有人跑过。跑之前要 Eric 定窗口:离钱凯下一轮现场至少 48 小时;KiwiVM 控制台
# 开着、看得到 GRUB 菜单;控制台 root 登录(或 KiwiVM 的 root shell)试过能进。
#
# 一次只换一个变量,每次重启单独一轮:先换内核(netplan 不动),再加按 MAC 匹配的 netplan
# 段(内核不动),最后清掉更旧的那个内核,上一个一直留作回退。
# 10-03 做完 194→198 与 netplan,网络从此走 by-mac 那份;之后每出一个新内核只走
# check → kernel →(观察一天)purge,netplan 阶段不用再跑。
#
# 在 Eric 的 Mac 上跑(/bin/bash 3.2 也行),用绝对路径,整屏输出发 Claude:
#   bash /Users/xiaogangxu/Downloads/南医大语言沟通训练阿尔茨海默病项目/platform/deploy/maintenance/kernel_netplan_window.sh <阶段>
# 阶段:
#   check           只读演练:门槛、基线、装包模拟都走一遍,不改生产上的任何东西,也不问问题
#   kernel          只装预期的 9 个内核包 → 重启前核对 → 重启 → 重启后核对(网络照旧走原来那份)
#   verify-kernel   只读:按 kernel 阶段的预期再核一遍(等待超时、从控制台救回来之后用)
#   netplan         演练生成 → 装死人开关 → 放上按 MAC 匹配的段并 apply → 新连接验证 → 拆开关
#                   → 再重启 → 重启后核对(网络改走 by-mac 那份)
#   verify-netplan  只读:按 netplan 阶段的预期再核一遍
#   purge           模拟后只清掉 PURGE_ABI 那个内核的包
# 任何一步打印「停」就停下,不要接着跑别的阶段。kernel、netplan 在「现在重启?」答了 no 的,
# 之后原样重跑同一阶段即可:已经装好的包、已经接管的新段会被认出来,直接走到重启。
#
# 永远不做:停用、停止或屏蔽任何 nmu-*.timer(deploy/systemd/README.md「维护重启不得停用
# 定时器」;它们开机随 timers.target 自己起来);full-upgrade;改 00-installer-config.yaml;
# netplan 里给网卡改名;打印 MAC(它和 KiwiVM 的 VM 编号挂钩)。
#
# kernel 阶段起不来、或开了机却没网:多半是新内核的事,KiwiVM 控制台 → GRUB 菜单(只亮 2 秒,
#   失败重启后 30 秒)→ Advanced options → 回退内核(这一轮是 5.15.0-198-generic)。by-mac 段
#   是现役网络,别动它。
# netplan 阶段开了机但没网:控制台 root 登录,把 /etc/netplan/50-nmu-by-mac.yaml 挪到 /root/,
#   再 netplan apply;nmu 与定时器都不用动。
# 换目标内核时改下面三个 *_ABI 常量,重新评审后再跑。
set -u
KEY="$HOME/.ssh/id_ed25519"
HOST="root@89.208.253.119"
SITE="https://89-208-253-119.sslip.io"
KIWIVM="https://kiwivm.64clouds.com/"
TARGET_ABI="5.15.0-202"
FALLBACK_ABI="5.15.0-198"
PURGE_ABI="5.15.0-194"
# 只点名这四个元包;apt 按依赖带进来的必须正好是 EXPECTED_INSTALL 这 9 个,多一个都停。
META_PACKAGES="linux-generic linux-image-generic linux-headers-generic linux-libc-dev"
EXPECTED_INSTALL="linux-generic linux-headers-${TARGET_ABI} linux-headers-${TARGET_ABI}-generic linux-headers-generic linux-image-${TARGET_ABI}-generic linux-image-generic linux-libc-dev linux-modules-${TARGET_ABI}-generic linux-modules-extra-${TARGET_ABI}-generic"
TIMERS="nmu-backup.timer nmu-backup-health.timer nmu-capacity.timer nmu-restore-drill.timer nmu-os-security.timer nmu-health.timer"
NET_ENS18="/run/systemd/network/10-netplan-ens18.network"
NET_BY_MAC="/run/systemd/network/10-netplan-by-mac.network"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
PHASE="${1:-}"
GATE_FAILURES=0
BOOT_BEFORE=""
DEPLOY_BEFORE=""
# 重启前开机自启的 sing-box* 单元;"-" 表示没记过(只读核对时)。
PROXIES_BEFORE="-"
# 这台 Mac 的代理环境变量会劫持直连的 curl,六个大小写都要清。
for v in http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY; do unset "$v"; done

RSH() { ssh -n -i "$KEY" -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=5 -o ServerAliveCountMax=6 "$HOST" "$@"; }
# 把 heredoc 当脚本送过去执行;不加 -n,stdin 就是那段脚本。
RBASH() { ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=5 -o ServerAliveCountMax=6 "$HOST" "$@"; }
bail() { echo "停:$*"; exit 1; }

confirm() {
  local answer
  printf '%s [yes/no] ' "${1}"
  read -r answer
  [ "${answer}" = "yes" ] || bail "没有确认:${1}"
}

# 演练(check)里门槛不过只记下来接着看;其余阶段当场停,停的时候生产还一点没动。
gate() {
  local what="${1}"
  shift
  if "$@"; then return 0; fi
  if [ "${PHASE}" = "check" ]; then
    echo "  ↑ 这一项不过:${what}"
    GATE_FAILURES=$((GATE_FAILURES + 1))
    return 0
  fi
  bail "${what}(生产没有任何改动)"
}

usage() {
  echo "用法:bash /Users/xiaogangxu/Downloads/南医大语言沟通训练阿尔茨海默病项目/platform/deploy/maintenance/kernel_netplan_window.sh <check|kernel|verify-kernel|netplan|verify-netplan|purge>"
  echo "各阶段做什么、什么时候跑,见脚本头注释。"
}

banner() {
  echo "== 内核与 netplan 维护窗口:阶段 ${PHASE},${STAMP} UTC,目标 ${TARGET_ABI},回退 ${FALLBACK_ABI} =="
  RSH true || bail "ssh 连不上 ${HOST}(钥匙要先 ssh-add 进 agent)"
}

# ---------------- 门槛:都只读 ----------------

mac_route_ok() {
  echo "== Mac 这一侧:不走这台 VPS 上的 TUN,KiwiVM 面板直连打得开 =="
  local target iface code bad=0
  for target in default 89.208.253.119 kiwivm.64clouds.com; do
    iface=$(route -n get "${target}" 2>/dev/null | awk '/interface:/{print $2}')
    echo "  ${target} 走 ${iface:-查不到}"
    case "${iface}" in ""|utun*) bad=1 ;; esac
  done
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "${KIWIVM}" 2>/dev/null)
  echo "  KiwiVM 面板 HTTP ${code:-无}"
  case "${code}" in 2??|3??) ;; *) bad=1 ;; esac
  return "${bad}"
}

operator_ready() {
  echo "== 开窗前,三件事要你本人确认 =="
  confirm "KiwiVM 控制台已经打开,看得到 GRUB 菜单(只亮 2 秒,失败重启后 30 秒)?"
  confirm "控制台 root 登录试过能进(或者 KiwiVM 的 root shell 试过能用)?"
  confirm "Mac 的 TUN/全局代理已关,而且离钱凯下一轮现场至少 48 小时?"
}

# 机器上的定时任务(UTC,含随机推迟与运行时长):每天 17:25–18:35 apt 列表与夜间补丁,
# 19:25–19:50 nmu-backup;周日 21:25–22:00 恢复演练,周一 21:25–22:00 OS 周检。
busy_minute() {   # $1 = 一周里的第几分钟,周一 00:00 是 0
  local day=$(( ${1} / 1440 )) minute=$(( ${1} % 1440 ))
  if [ "${minute}" -ge $((17 * 60 + 25)) ] && [ "${minute}" -le $((18 * 60 + 35)) ]; then return 0; fi
  if [ "${minute}" -ge $((19 * 60 + 25)) ] && [ "${minute}" -le $((19 * 60 + 50)) ]; then return 0; fi
  if [ "${day}" -eq 6 ] || [ "${day}" -eq 0 ]; then
    if [ "${minute}" -ge $((21 * 60 + 25)) ] && [ "${minute}" -le $((22 * 60)) ]; then return 0; fi
  fi
  return 1
}

window_clear() {   # $1 = 这一阶段最多占用几分钟
  local clock dow hour minute start offset
  echo "== 时间窗:接下来 ${1} 分钟不撞这台机器上的定时任务 =="
  clock=$(RSH 'date -u "+%u %H %M"') || { echo "  取不到 VPS 的时钟"; return 1; }
  read -r dow hour minute <<< "${clock}"
  start=$(( (dow - 1) * 1440 + 10#${hour} * 60 + 10#${minute} ))
  echo "  VPS 现在是 UTC 周${dow} ${hour}:${minute}"
  offset=0
  while [ "${offset}" -le "${1}" ]; do
    if busy_minute $(( (start + offset) % 10080 )); then
      echo "  ${offset} 分钟后会撞上(每天 17:25–18:35、19:25–19:50,周日与周一 21:25–22:00,都是 UTC)"
      return 1
    fi
    offset=$((offset + 5))
  done
  echo "  不撞"
}

idle_ok() {
  echo "== 生产必须空闲:近 10 分钟非轮询请求 0 个(与 267 同一个过滤;journal 读不了也算不空闲) =="
  local idle
  idle=$(RSH 'out=$(journalctl -u nmu --since "-10 min" --no-pager -o cat 2>/dev/null) || { echo 查不到; exit 0; }; printf "%s\n" "$out" | grep -E "\"(POST|GET|PATCH|PUT|DELETE) " | grep -vcE "/health|/device/attach|/device/heartbeat|/autopilot/next|/autopilot/voice-settings|/sessions/[^/]+/autopilot/status|/live/state|/live/patient-heartbeat|/build-id.txt|/status HTTP|/runtime HTTP|/ai/provider-readiness|401 Unauthorized|\"GET /(patient|console|acceptance|index\.html|assets/|favicon|robots\.txt)"')
  echo "  近 10 分钟非轮询请求数:${idle:-查不到}"
  [ "${idle}" = "0" ]
}

db_quiet() {
  echo "== 库里没有进行中的场次、没有没到期的自动带练租约(只读打开) =="
  RBASH '/opt/nmu/venv/bin/python -I -' <<'PY'
import datetime
import sqlite3
import sys

connection = sqlite3.connect("file:/opt/nmu/app/data/app.db?mode=ro", uri=True, timeout=5)
try:
    by_status = dict(connection.execute(
        "SELECT status, count(*) FROM sessionruntimestate GROUP BY status"))
    # 租约截止时间存的是去掉时区的 UTC（autopilot_ledger.utc_now_naive），按同一格式比字符串。
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    leases = connection.execute(
        "SELECT count(*) FROM sessionautopilotstate"
        " WHERE lease_owner IS NOT NULL AND lease_expires_at > ?", (now,)).fetchone()[0]
finally:
    connection.close()
statuses = "、".join(f"{status} {count}" for status, count in sorted(by_status.items()))
print("  场次运行状态:", statuses or "无")
print("  没到期的自动带练租约:", leases)
sys.exit(1 if by_status.get("active", 0) or leases else 0)
PY
}

apt_idle() {
  echo "== apt/dpkg 空闲:夜间补丁没在跑、dpkg 锁没人拿、dpkg --audit 干净、没有 nmu 任务在跑 =="
  RBASH 'bash -s' <<'REMOTE'
set -u
bad=0
for unit in apt-daily.service apt-daily-upgrade.service; do
  state=$(systemctl --no-pager show -p ActiveState --value "${unit}")
  echo "  ${unit} ${state}"
  case "${state}" in inactive|failed) ;; *) bad=1 ;; esac
done
busy=$(pgrep -a -f '^(/usr/bin/)?(apt|apt-get|aptitude|dpkg)( |$)|/usr/bin/unattended-upgrade( |$)')
if [ -n "${busy}" ]; then echo "  正在跑:${busy}"; bad=1; fi
if locks=$(lslocks --noheadings --output PATH); then
  held=$(printf '%s\n' "${locks}" | grep -c '^/var/lib/dpkg/lock')
  echo "  被拿着的 dpkg 锁:${held} 个"
  [ "${held}" = "0" ] || bad=1
else
  echo "  lslocks 查不了锁"
  bad=1
fi
audit=$(dpkg --audit 2>&1)
if [ -n "${audit}" ]; then echo "  dpkg --audit 不干净:"; printf '%s\n' "${audit}" | sed 's/^/    /'; bad=1; fi
running=$(systemctl --no-pager list-units --no-legend --state=activating,deactivating 'nmu-*.service' | sed 's/^[^[:alnum:]]*//' | awk '{print $1}' | tr '\n' ' ')
if [ -n "${running}" ]; then echo "  正在跑的 nmu 任务:${running}"; bad=1; fi
exit "${bad}"
REMOTE
}

deployed_check_ok() {
  echo "== 生产上的 os_security_check.py 会扫还在用旧库的进程(重启后的核对要用它) =="
  RSH 'grep -q "def stale_library_units" /opt/nmu/app/scripts/os_security_check.py' || { echo "  还是旧版:先上线这一批再开窗"; return 1; }
  echo "  是新版"
}

backup_fresh() {
  echo "== 最近一次备份是 ok,而且在 30 小时内(重启前手里要有一份新快照) =="
  RBASH 'bash -s' <<'REMOTE'
set -u
line=$(tail -1 /opt/nmu/backups/backup.log)
echo "  ${line:0:90}"
stamp=$(printf '%s\n' "${line}" | sed -nE 's/^\[([0-9-]+ [0-9:]+)\] ok .*/\1/p')
[ -n "${stamp}" ] || exit 1
age=$(( $(date -u +%s) - $(date -u -d "${stamp}" +%s) ))
echo "  距今 $((age / 3600)) 小时"
[ "${age}" -lt 108000 ]
REMOTE
}

# 服务、failed 单元、六个定时器。只读:定时器不 start、不 restart、不 enable。
remote_health() {   # $1 = before|after:开窗前的 failed 单元只报告,重启后一条都不许有
  echo "== 服务与定时器(${1}) =="
  RBASH "TIMERS='${TIMERS}' WHEN=${1} PROXIES_BEFORE='${PROXIES_BEFORE}' bash -s" <<'REMOTE'
set -u
bad=0
pass() { echo "  PASS $*"; }
fail() { echo "  FAIL $*"; bad=1; }
for unit in nmu nmu-caddy nginx ssh systemd-networkd systemd-resolved qemu-guest-agent; do
  state=$(systemctl --no-pager is-active "${unit}")
  if [ "${state}" = "active" ]; then pass "${unit} active"; else fail "${unit} ${state}"; fi
done
# 代理跟着机器上的配置走:10-28 整个 IP 被墙后 sing-box 停用,改跑 sing-box-hy2。开机自启的必须在跑。
proxies=$(systemctl --no-pager list-unit-files --no-legend --state=enabled 'sing-box*.service' | awk '{print $1}' | tr '\n' ' ')
for unit in ${proxies}; do
  state=$(systemctl --no-pager is-active "${unit}")
  if [ "${state}" = "active" ]; then pass "${unit} active"; else fail "${unit} ${state}(开机自启却没在跑)"; fi
done
echo "  注 开机自启的代理:${proxies:-无}"
# 列不到时 systemctl 也退 1,分不清「真没有」和「出错」,所以重启后拿重启前的清单对。
if [ "${WHEN}" = "after" ] && [ "${PROXIES_BEFORE}" != "-" ] && [ "${proxies}" != "${PROXIES_BEFORE}" ]; then
  fail "开机自启的代理变了:重启前 ${PROXIES_BEFORE:-无},现在 ${proxies:-无}"
fi
failed_units=$(systemctl --no-pager list-units --no-legend --state=failed | sed 's/^[^[:alnum:]]*//' | awk '{print $1}' | tr '\n' ' ')
if [ -z "${failed_units}" ]; then
  pass "没有 failed 单元"
elif [ "${WHEN}" = "after" ]; then
  fail "开机后有 failed 单元:${failed_units}"
else
  echo "  注 开窗前就有 failed 单元(不拦;重启后必须清零):${failed_units}"
fi
for t in ${TIMERS}; do
  sub=$(systemctl --no-pager show -p SubState --value "${t}")
  nxt=$(systemctl --no-pager show -p NextElapseUSecRealtime --value "${t}")
  file_state=$(systemctl --no-pager show -p UnitFileState --value "${t}")
  if [ "${sub}" = "waiting" ] && [ -n "${nxt}" ] && [ "${file_state}" = "enabled" ]; then
    pass "${t} waiting,下一次 ${nxt}"
  else
    fail "${t} SubState=${sub} NEXT=${nxt:-空} UnitFileState=${file_state}(处置见 deploy/systemd/README.md 定时器一节)"
  fi
done
exit "${bad}"
REMOTE
}

record_baseline() {
  echo "== 基线(改动之前) =="
  BOOT_BEFORE=$(RSH 'cat /proc/sys/kernel/random/boot_id') || return 1
  DEPLOY_BEFORE=$(RSH 'head -1 /opt/nmu/last-deploy.state') || return 1
  PROXIES_BEFORE=$(RSH "systemctl --no-pager list-unit-files --no-legend --state=enabled 'sing-box*.service' | awk '{print \$1}' | tr '\n' ' '") || return 1
  echo "  boot_id ${BOOT_BEFORE}"
  echo "  last-deploy.state 首行:${DEPLOY_BEFORE}"
  echo "  开机自启的代理:${PROXIES_BEFORE:-无}"
  RBASH 'bash -s' <<'REMOTE'
set -u -o pipefail
echo "  内核 $(uname -r),$(uptime -p)"
echo "  /boot 里的内核:$(ls /boot | sed -n 's/^vmlinuz-//p' | tr '\n' ' ')"
echo "  $(networkctl --no-pager status eth0 | sed -nE 's/^ *(Network File: .*)$/\1/p')"
echo "  默认路由:$(ip -4 route show default)"
echo "  altname:$(ip -o link show dev eth0 | grep -o 'altname [^ ]*' | tr '\n' ' ')"
echo "  /etc/netplan:$(ls /etc/netplan | tr '\n' ' ')"
sha256sum /etc/netplan/00-installer-config.yaml /run/systemd/network/10-netplan-ens18.network | sed 's/^/  /'
echo "  最新备份:$(tail -1 /opt/nmu/backups/backup.log | cut -c1-90)"
echo "  夜间补丁下一次:$(systemctl --no-pager show -p NextElapseUSecRealtime --value apt-daily-upgrade.timer)"
REMOTE
}

# ---------------- 内核 ----------------

simulation_matches() {
  echo "== 装包模拟:点名 ${META_PACKAGES},带进来的必须正好是预期的 9 个 =="
  local sim names expected wrong removed
  sim=$(RSH "LC_ALL=C apt-get -s install ${META_PACKAGES}") || { echo "  模拟失败"; return 1; }
  names=$(printf '%s\n' "${sim}" | awk '/^Inst /{print $2}' | LC_ALL=C sort | tr '\n' ' ')
  expected=$(printf '%s\n' "${EXPECTED_INSTALL}" | tr ' ' '\n' | LC_ALL=C sort | tr '\n' ' ')
  echo "  模拟要装:${names:-无}"
  if [ "${names}" != "${expected}" ]; then echo "  预期的是:${expected}"; return 1; fi
  wrong=$(printf '%s\n' "${sim}" | grep '^Inst ' | grep -c -v -F -e "(${TARGET_ABI//-/.}." -e "(${TARGET_ABI}.")
  removed=$(printf '%s\n' "${sim}" | grep -c '^Remv ')
  echo "  新版本不是 ${TARGET_ABI} 的:${wrong} 个;要删的:${removed} 个"
  [ "${wrong}" = "0" ] && [ "${removed}" = "0" ]
}

install_kernel() {
  local state rc left
  state=$(RSH "dpkg-query -W -f='\${Status}' linux-image-${TARGET_ABI}-generic 2>/dev/null")
  if [ "${state}" = "install ok installed" ]; then
    echo "== linux-image-${TARGET_ABI}-generic 已经装好(上一轮装完没重启),跳过安装 =="
    return 0
  fi
  simulation_matches || bail "模拟出来的包不对(生产没有任何改动)"
  echo "== 装包:交给 systemd-run 跑,ssh 断了也不会把 dpkg 撂在半路 =="
  RSH "systemd-run --wait --collect --quiet --unit nmu-kernel-install-${STAMP} --property=StandardOutput=file:/root/nmu-kernel-install-${STAMP}.log --property=StandardError=inherit --setenv=DEBIAN_FRONTEND=noninteractive --setenv=NEEDRESTART_MODE=l apt-get install -y -q -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold ${META_PACKAGES}"
  rc=$?
  RSH "tail -12 /root/nmu-kernel-install-${STAMP}.log"
  [ "${rc}" = "0" ] || bail "装包退出码 ${rc}(日志 /root/nmu-kernel-install-${STAMP}.log);没有重启,正在跑的仍是 ${FALLBACK_ABI}"
  left=$(RSH "LC_ALL=C apt-get -s install ${META_PACKAGES}" | grep -c '^Inst ')
  [ "${left}" = "0" ] || bail "装完再模拟仍有 ${left} 个包要装;没有重启"
}

pre_reboot_asserts() {
  echo "== 重启前核对:${TARGET_ABI} 的映像与 initrd、驱动集合、GRUB 默认项、/boot 余量 =="
  RBASH "TARGET_ABI=${TARGET_ABI} FALLBACK_ABI=${FALLBACK_ABI} bash -s" <<'REMOTE'
set -u -o pipefail
bad=0
pass() { echo "  PASS $*"; }
fail() { echo "  FAIL $*"; bad=1; }
target="${TARGET_ABI}-generic"
fallback="${FALLBACK_ABI}-generic"
if [ -s "/boot/vmlinuz-${target}" ]; then pass "vmlinuz-${target} 在"; else fail "缺 /boot/vmlinuz-${target}"; fi
initrd_bytes=$(stat -c %s "/boot/initrd.img-${target}" 2>/dev/null || echo 0)
if [ "${initrd_bytes}" -ge 100000000 ]; then pass "initrd ${initrd_bytes} 字节"; else fail "initrd 只有 ${initrd_bytes} 字节(不到 100 MB)"; fi
list_new=$(lsinitramfs "/boot/initrd.img-${target}") || fail "lsinitramfs 读不了 ${target} 的 initrd"
list_old=$(lsinitramfs "/boot/initrd.img-${fallback}") || fail "lsinitramfs 读不了 ${fallback} 的 initrd"
count_new=$(printf '%s\n' "${list_new}" | wc -l | tr -d ' ')
count_old=$(printf '%s\n' "${list_old}" | wc -l | tr -d ' ')
if [ "${count_new}" -gt 100 ] && [ "${count_old}" -gt 100 ]; then pass "两个 initrd 都列得出(${count_new} / ${count_old} 个文件)"; else fail "initrd 列表太短(${count_new} / ${count_old})"; fi
storage_and_net() { printf '%s\n' "${1}" | grep -E '/kernel/drivers/(block|scsi|ata|virtio|net|nvme|md)/' | sed -E 's#^.*/kernel/##' | LC_ALL=C sort -u || true; }
drivers_new=$(storage_and_net "${list_new}")
drivers_old=$(storage_and_net "${list_old}")
if [ "${drivers_new}" = "${drivers_old}" ]; then
  pass "存储与网卡驱动和 ${fallback} 相同($(printf '%s\n' "${drivers_new}" | grep -c .) 个)"
else
  fail "存储与网卡驱动和 ${fallback} 不同(< 是 ${fallback},> 是 ${target}):"
  diff <(printf '%s\n' "${drivers_old}") <(printf '%s\n' "${drivers_new}") | sed 's/^/    /'
fi
if modinfo -k "${target}" virtio_net >/dev/null 2>&1; then pass "virtio_net 在 ${target} 里"; else fail "modinfo -k ${target} virtio_net 找不到"; fi
entry=$(grep -m1 '^menuentry ' /boot/grub/grub.cfg)
case "${entry}" in "menuentry 'Ubuntu'"*) pass "第一个启动项是 Ubuntu" ;; *) fail "第一个启动项不是 Ubuntu:${entry}" ;; esac
linux_line=$(awk '/^menuentry /{n++} n == 1 && $1 == "linux" {print; exit}' /boot/grub/grub.cfg)
case "${linux_line}" in *"/vmlinuz-${target} "*) pass "默认项引导 ${target}" ;; *) fail "默认项不是 ${target}:${linux_line}" ;; esac
case "${linux_line}" in *" net.ifnames=0"*) pass "默认项仍带 net.ifnames=0(网卡还叫 eth0)" ;; *) fail "默认项丢了 net.ifnames=0" ;; esac
if grep -qx 'GRUB_DEFAULT=0' /etc/default/grub; then pass "GRUB_DEFAULT=0"; else fail "GRUB_DEFAULT 不是 0"; fi
if grub-editenv list 2>/dev/null | grep -q '^next_entry='; then fail "grubenv 里有一次性的 next_entry"; else pass "grubenv 没有一次性启动项"; fi
avail=$(df -B1 --output=avail /boot | tail -1 | tr -d ' ')
if [ "${avail}" -ge 100000000 ]; then pass "/boot 还剩 $((avail / 1048576)) MiB"; else fail "/boot 只剩 $((avail / 1048576)) MiB"; fi
echo "  needrestart:$(needrestart -b -r l 2>/dev/null </dev/null | grep -E '^NEEDRESTART-(KCUR|KEXP|KSTA)' | tr '\n' ' ')"
exit "${bad}"
REMOTE
}

reboot_and_wait() {   # $1 = 重启前的 boot_id
  confirm "现在重启?整机约 16 秒,代理、nginx、ssh 一起断;控制台盯着 GRUB 菜单"
  echo "== 重启 =="
  RSH 'systemctl --no-pager reboot'
  echo "  等新的一次开机,最多 5 分钟(ssh 通了不算,boot_id 换了才算)…"
  local waited=0 current=""
  while [ "${waited}" -lt 300 ]; do
    sleep 5
    waited=$((waited + 5))
    current=$(ssh -n -i "$KEY" -o BatchMode=yes -o ConnectTimeout=5 "$HOST" 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null)
    if [ -n "${current}" ] && [ "${current}" != "${1}" ]; then
      echo "  回来了:新的 boot_id,用时约 ${waited} 秒"
      return 0
    fi
  done
  if [ "${current}" = "${1}" ]; then bail "5 分钟了 boot_id 还是原来那个:重启根本没发生"; fi
  bail "5 分钟没回来:按脚本头注释「起不来时」那几步在 KiwiVM 控制台里处理"
}

post_reboot_asserts() {   # $1 = 期望的 Network File;$2 = 重启前的 boot_id(只读核对时为空)
  echo "== 重启后核对 =="
  local bad=0 health docs deploy_now
  remote_health after || bad=1
  RBASH "TARGET_ABI=${TARGET_ABI} EXPECT_NETWORK=${1} BOOT_BEFORE=${2} bash -s" <<'REMOTE' || bad=1
set -u -o pipefail
bad=0
pass() { echo "  PASS $*"; }
fail() { echo "  FAIL $*"; bad=1; }
target="${TARGET_ABI}-generic"
if [ -n "${BOOT_BEFORE}" ]; then
  if [ "$(cat /proc/sys/kernel/random/boot_id)" != "${BOOT_BEFORE}" ]; then pass "boot_id 变了"; else fail "boot_id 没变"; fi
fi
running=$(uname -r)
if [ "${running}" = "${target}" ]; then pass "内核 ${running}"; else fail "内核是 ${running},不是 ${target}"; fi
route=$(ip -4 route show default)
case "${route}" in *" dev eth0 "*"proto dhcp"*) pass "默认路由走 eth0、来自 DHCP" ;; *) fail "默认路由:${route}" ;; esac
netfile=$(networkctl --no-pager status eth0 2>/dev/null | sed -nE 's/^ *Network File: *//p')
if [ "${netfile}" = "${EXPECT_NETWORK}" ]; then pass "Network File ${netfile}"; else fail "Network File 是 ${netfile:-空},应为 ${EXPECT_NETWORK}"; fi
altnames=$(ip -o link show dev eth0 | grep -o 'altname [^ ]*' | wc -l | tr -d ' ')
if [ "${altnames}" = "2" ]; then pass "altname 2 个(ens18 那条后路还在)"; else fail "altname ${altnames} 个,应为 2"; fi
links=$(find /run/systemd/network -name '*.link' | wc -l | tr -d ' ')
if [ "${links}" = "0" ]; then pass "没有生成 .link"; else fail "/run/systemd/network 里有 ${links} 个 .link"; fi
if resolvectl --no-pager query ubuntu.com >/dev/null 2>&1; then pass "DNS 解析正常"; else fail "resolvectl 解析 ubuntu.com 失败"; fi
for unit in $(systemctl --no-pager list-unit-files --no-legend --state=enabled 'sing-box*.service' | awk '{print $1}'); do
  restarts=$(systemctl --no-pager show -p NRestarts --value "${unit}")
  if [ "${restarts}" = "0" ]; then pass "${unit} 开机后没重启过"; else fail "${unit} NRestarts=${restarts}"; fi
done
if systemctl --no-pager start nmu-health.service; then
  result=$(systemctl --no-pager show -p Result --value nmu-health.service)
  if [ "${result}" = "success" ]; then pass "公网探针手动跑一次:success"; else fail "公网探针 Result=${result}"; fi
else
  fail "公网探针没跑成"
fi
kernel_state=$(needrestart -b -r l 2>/dev/null </dev/null | sed -nE 's/^NEEDRESTART-KSTA: *//p')
if [ "${kernel_state}" = "1" ]; then pass "needrestart KSTA=1(跑的就是已装的最新内核)"; else fail "needrestart KSTA=${kernel_state:-空}"; fi
pending_linux=$(LC_ALL=C apt-get -s full-upgrade 2>/dev/null </dev/null | grep -c '^Inst linux-')
if [ "${pending_linux}" = "0" ]; then pass "没有待装的 linux-* 包"; else fail "还有 ${pending_linux} 个 linux-* 包待装"; fi
OS_REPORT=$(/usr/bin/python3 /opt/nmu/app/scripts/os_security_check.py --json) TARGET="${target}" /usr/bin/python3 - <<'PY' || bad=1
import json
import os

report = json.loads(os.environ["OS_REPORT"])
target = os.environ["TARGET"]
checks = [
    (report.get("stale_library_units") == {},
     f"没有进程还映射着被换掉的库（{report.get('stale_library_units')}）"),
    (report.get("kernel_running") == target, f"os_security_check 看到的内核 {report.get('kernel_running')}"),
    (report.get("kernel_newest_installed") == target,
     f"/boot 里最新的内核 {report.get('kernel_newest_installed')}"),
]
for ok, text in checks:
    print(("  PASS " if ok else "  FAIL ") + text)
print(f"  安全积压 {report.get('security_pending')}，其他待装 {report.get('other_pending')}")
raise SystemExit(0 if all(ok for ok, _text in checks) else 1)
PY
exit "${bad}"
REMOTE
  echo "== 从 Mac 直连公网(不走代理) =="
  health=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "${SITE}/health")
  docs=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "${SITE}/docs")
  echo "  /health ${health:-无}(应 200),/docs ${docs:-无}(应 404)"
  if [ "${health}" != "200" ] || [ "${docs}" != "404" ]; then bad=1; fi
  echo "== 完整预检(--require-all --os,永不 --release;应全过) =="
  RSH "/opt/nmu/venv/bin/python /opt/nmu/app/scripts/preflight_check.py --require-all --db /opt/nmu/app/data/app.db --backup-root /opt/nmu/backups --lock /opt/nmu/app/requirements-deploy.lock.txt --os --base-url ${SITE}" || bad=1
  deploy_now=$(RSH 'head -1 /opt/nmu/last-deploy.state')
  if [ -n "${DEPLOY_BEFORE}" ] && [ "${deploy_now}" != "${DEPLOY_BEFORE}" ]; then
    echo "  FAIL last-deploy.state 变了:${deploy_now}"
    bad=1
  else
    echo "  last-deploy.state 首行:${deploy_now}(这个窗口只动系统,它不该变)"
  fi
  return "${bad}"
}

# ---------------- netplan ----------------

netplan_dry_run() {
  echo "== netplan:拼出按 MAC 匹配的段,在临时根目录里演练生成(生产网络配置不动) =="
  RBASH "STAMP=${STAMP} bash -s" <<'REMOTE'
set -u -o pipefail
umask 077
mac=$(cat /sys/class/net/eth0/address)
case "${mac}" in
  [0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]) ;;
  *) echo "  FAIL eth0 的 MAC 不是 xx:xx:xx:xx:xx:xx 的样子(不打印)"; exit 1 ;;
esac
stage="/root/nmu-netplan-${STAMP}"
mkdir -p "${stage}/dry/etc/netplan"
cat > "${stage}/50-nmu-by-mac.yaml" <<YAML
# 由 deploy/maintenance/kernel_netplan_window.sh 写入。按 MAC 认 eth0,作为按 ens18 这个
# 别名认它的后路:网卡换了 PCI 槽位、别名没了,也还能连上网。不改网卡的名字。
network:
  version: 2
  ethernets:
    by-mac:
      match:
        macaddress: "${mac}"
      dhcp4: true
YAML
chmod 600 "${stage}/50-nmu-by-mac.yaml"
cp -p /etc/netplan/*.yaml "${stage}/dry/etc/netplan/"
cp -p "${stage}/50-nmu-by-mac.yaml" "${stage}/dry/etc/netplan/"
netplan generate --root-dir "${stage}/dry" </dev/null || { echo "  FAIL netplan generate 演练失败"; exit 1; }
bad=0
generated=$(ls "${stage}/dry/run/systemd/network" | tr '\n' ' ')
if [ "${generated}" = "10-netplan-by-mac.network 10-netplan-ens18.network " ]; then
  echo "  PASS 演练只生成两份:${generated}"
else
  echo "  FAIL 演练生成的是:${generated}"; bad=1
fi
if cmp -s "${stage}/dry/run/systemd/network/10-netplan-ens18.network" /run/systemd/network/10-netplan-ens18.network; then
  echo "  PASS ens18 那份和现役逐字节相同"
else
  echo "  FAIL ens18 那份和现役不一样"; bad=1
fi
# netplan 0.107 给它管的每块网卡都写一条 udev 规则,只把网卡列进 NetworkManager 的不管名单
# (ENV{NM_UNMANAGED}="1"),生产机开机就有这个文件(10-03 第一次跑就因为它停在这里)。
# 只放过这一种行;.link 和其他任何 udev 规则(改名用的 NAME= 之类)照旧判失败。
links=$(find "${stage}/dry" -name '*.link' -type f | wc -l | tr -d ' ')
other_rules=$(find "${stage}/dry" -path '*/udev/*' -type f -exec cat {} + </dev/null \
  | grep -vE '^(#.*)?$' \
  | grep -cvE '^SUBSYSTEM=="net", ACTION=="add\|change\|move", (ENV\{ID_NET_NAME\}|ATTR\{address\})=="[^"]+", ENV\{NM_UNMANAGED\}="1"$')
if [ "${links}" = "0" ] && [ "${other_rules}" = "0" ]; then
  echo "  PASS 没有 .link;udev 规则只把网卡列进 NetworkManager 不管名单,不改名"
else
  echo "  FAIL 演练生成了 ${links} 个 .link、${other_rules} 行不认识的 udev 规则"; bad=1
fi
matches=$(grep -cE '^(Permanent)?MACAddress=' "${stage}/dry/run/systemd/network/10-netplan-by-mac.network")
if [ "${matches}" = "1" ] && grep -qx 'DHCP=ipv4' "${stage}/dry/run/systemd/network/10-netplan-by-mac.network"; then
  echo "  PASS by-mac 那份按 MAC 匹配、DHCP=ipv4(MAC 不打印)"
else
  echo "  FAIL by-mac 那份内容不对"; bad=1
fi
cp -a /etc/netplan "${stage}/etc-netplan-before"
echo "  原配置备份在 ${stage}/etc-netplan-before"
exit "${bad}"
REMOTE
}

netplan_apply_with_dead_man() {
  echo "== 先装死人开关(180 秒后自动撤回新段并重新 apply),再放上新段、apply =="
  RBASH "STAMP=${STAMP} bash -s" <<'REMOTE'
set -u
stage="/root/nmu-netplan-${STAMP}"
systemd-run --unit nmu-netplan-revert --on-active=180 --timer-property=AccuracySec=1s \
  /bin/sh -c "if [ -e /etc/netplan/50-nmu-by-mac.yaml ]; then mv /etc/netplan/50-nmu-by-mac.yaml ${stage}/reverted-50-nmu-by-mac.yaml; fi; /usr/sbin/netplan apply" \
  || { echo "  FAIL 死人开关没装上,没有 apply"; exit 1; }
if [ "$(systemctl --no-pager is-active nmu-netplan-revert.timer)" != "active" ]; then echo "  FAIL 死人开关没在计时,没有 apply"; exit 1; fi
install -m 600 -o root -g root "${stage}/50-nmu-by-mac.yaml" /etc/netplan/50-nmu-by-mac.yaml
timeout 90 netplan apply </dev/null
rc=$?
echo "  apply 退出码 ${rc}"
exit "${rc}"
REMOTE
}

netplan_live_ok() {   # 每次都是新开的 ssh 连接;再从 Mac 直连公网
  local tries=0 netfile="" health
  while [ "${tries}" -lt 12 ]; do
    sleep 5
    tries=$((tries + 1))
    netfile=$(RSH 'networkctl --no-pager status eth0 | sed -nE "s/^ *Network File: *//p"' 2>/dev/null)
    if [ "${netfile}" = "${NET_BY_MAC}" ]; then break; fi
  done
  echo "  新连接看到的 Network File:${netfile:-连不上}"
  [ "${netfile}" = "${NET_BY_MAC}" ] || return 1
  health=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "${SITE}/health")
  echo "  Mac 直连 /health ${health:-无}"
  [ "${health}" = "200" ]
}

disarm_dead_man() {
  echo "== 拆死人开关 =="
  RBASH 'bash -s' <<'REMOTE'
set -u
systemctl --no-pager stop nmu-netplan-revert.timer
if [ "$(systemctl --no-pager is-active nmu-netplan-revert.timer)" = "active" ]; then echo "  FAIL 死人开关还在计时"; exit 1; fi
if [ ! -e /etc/netplan/50-nmu-by-mac.yaml ]; then echo "  FAIL 新段已经被死人开关撤回了(拆晚了),网络回到只认 ens18"; exit 1; fi
echo "  PASS 死人开关已拆,新段留在 /etc/netplan"
REMOTE
}

# ---------------- 各阶段 ----------------

common_gates() {   # $1 = 这一阶段最多占用几分钟
  gate "时间窗" window_clear "${1}"
  gate "生产空闲" idle_ok
  gate "库里没有进行中的场次和租约" db_quiet
  gate "apt/dpkg 空闲" apt_idle
  gate "生产上的 os_security_check 是新版" deployed_check_ok
  gate "最近一次备份" backup_fresh
  gate "开窗前机器本身是好的" remote_health before
}

phase_check() {
  echo "(只读演练:不改生产上的任何东西,也不问问题)"
  gate "Mac 侧路由与 KiwiVM 直连" mac_route_ok
  common_gates 45
  gate "按 MAC 匹配的段与 eth0 的 Network File 一致" network_consistent
  gate "基线" record_baseline
  gate "装包模拟只带进预期的 9 个包" simulation_matches
  if [ "${GATE_FAILURES}" = "0" ]; then
    echo "== 演练:每一项都过 =="
  else
    echo "== 演练:${GATE_FAILURES} 项不过(上面 ↑ 标出来的)=="
    exit 1
  fi
}

# 内核这一轮网络不动:按 MAC 匹配的段在,eth0 就该走 by-mac 那份(10-03 起);不在就该走
# ens18 那份。两样对不上说明 netplan 改到一半,输出空串。
current_network() {
  local stanza netfile
  stanza=$(RSH 'if [ -e /etc/netplan/50-nmu-by-mac.yaml ]; then echo present; else echo absent; fi')
  netfile=$(RSH 'networkctl --no-pager status eth0 | sed -nE "s/^ *Network File: *//p"')
  case "${stanza}:${netfile}" in
    "present:${NET_BY_MAC}") echo "${NET_BY_MAC}" ;;
    "absent:${NET_ENS18}") echo "${NET_ENS18}" ;;
    *) echo "" ;;
  esac
}

network_consistent() {
  local network
  echo "== 网络:按 MAC 匹配的段在不在,和 eth0 正在用的 Network File 对得上 =="
  network=$(current_network)
  echo "  ${network:-对不上}"
  [ -n "${network}" ]
}

phase_kernel() {
  local running network
  gate "Mac 侧路由与 KiwiVM 直连" mac_route_ok
  operator_ready
  common_gates 45
  running=$(RSH 'uname -r')
  [ "${running}" != "${TARGET_ABI}-generic" ] || bail "已经在跑 ${TARGET_ABI}-generic,这一阶段做过了;要复核就跑 verify-kernel"
  [ "${running}" = "${FALLBACK_ABI}-generic" ] || bail "正在跑的是 ${running:-查不到},不是预期的 ${FALLBACK_ABI}-generic:常量过时了,先重新评审"
  network=$(current_network)
  [ -n "${network}" ] || bail "按 MAC 匹配的段在不在,和 eth0 正在用的 Network File 对不上:netplan 改到一半,内核这一轮不该和它叠在一起"
  record_baseline || bail "基线取不全(生产没有任何改动)"
  install_kernel
  pre_reboot_asserts || bail "重启前核对没过,没有重启。注意 ${TARGET_ABI} 已装、GRUB 默认项已经指向它:查清之前别让这台机器重启"
  reboot_and_wait "${BOOT_BEFORE}"
  post_reboot_asserts "${network}" "${BOOT_BEFORE}" || bail "重启后核对有 FAIL(看上面)"
  echo "== kernel 阶段完成:内核 ${TARGET_ABI},网络照旧走 ${network}。观察一天没问题再跑 purge =="
}

phase_verify() {   # $1 = 期望的 Network File
  post_reboot_asserts "${1}" "" || bail "核对有 FAIL(看上面)"
  echo "== 核对全过 =="
}

phase_verify_kernel() {
  local network
  network=$(current_network)
  [ -n "${network}" ] || bail "按 MAC 匹配的段在不在,和 eth0 正在用的 Network File 对不上:先查网络,再核内核"
  phase_verify "${network}"
}

phase_netplan() {
  local running stanza netfile applied
  gate "Mac 侧路由与 KiwiVM 直连" mac_route_ok
  operator_ready
  common_gates 45
  running=$(RSH 'uname -r')
  [ "${running}" = "${TARGET_ABI}-generic" ] || bail "正在跑的是 ${running:-查不到},不是 ${TARGET_ABI}-generic:先做完 kernel 阶段"
  stanza=$(RSH 'if [ -e /etc/netplan/50-nmu-by-mac.yaml ]; then echo present; else echo absent; fi')
  netfile=$(RSH 'networkctl --no-pager status eth0 | sed -nE "s/^ *Network File: *//p"')
  record_baseline || bail "基线取不全(生产没有任何改动)"
  if [ "${stanza}" = "absent" ] && [ "${netfile}" = "${NET_ENS18}" ]; then
    netplan_dry_run || bail "演练没过(生产网络配置没动)"
    netplan_apply_with_dead_man
    applied=$?
    if [ "${applied}" != "0" ] || ! netplan_live_ok; then
      bail "新段没接管或网不通。死人开关会在 180 秒内撤回新段;3 分钟后跑 verify-kernel,确认网络回到 ens18 那份"
    fi
    disarm_dead_man || bail "死人开关没拆干净(看上面);跑 verify-kernel 看网络现在走哪份"
  elif [ "${stanza}" = "present" ] && [ "${netfile}" = "${NET_BY_MAC}" ]; then
    echo "== 新段上一轮已经放上并接管(那一轮没重启),这一轮只重启验证 =="
  else
    bail "状态对不上:新段 ${stanza:-查不到},Network File ${netfile:-查不到}"
  fi
  reboot_and_wait "${BOOT_BEFORE}"
  post_reboot_asserts "${NET_BY_MAC}" "${BOOT_BEFORE}" || bail "重启后核对有 FAIL(看上面)。开了机却没网,就按脚本头注释在控制台撤回新段"
  echo "== netplan 阶段完成。观察一天没问题再跑 purge =="
}

phase_purge() {
  local running packages sim purged removed rc
  common_gates 15
  running=$(RSH 'uname -r')
  [ "${running}" = "${TARGET_ABI}-generic" ] || bail "正在跑的是 ${running:-查不到},不是 ${TARGET_ABI}-generic:先做完 kernel 阶段"
  RSH "test -s /boot/vmlinuz-${FALLBACK_ABI}-generic" || bail "回退内核 ${FALLBACK_ABI} 不在 /boot 里,不清 ${PURGE_ABI}"
  packages=$(RSH "dpkg-query -W -f='\${Package} \${db:Status-Abbrev}\n' 'linux-*${PURGE_ABI}*' 2>/dev/null" | awk '$2 ~ /^(ii|rc)$/ {print $1}' | LC_ALL=C sort | tr '\n' ' ')
  if [ -z "${packages}" ]; then echo "== 没有 ${PURGE_ABI} 的包,不用清 =="; return 0; fi
  echo "== 模拟清掉:${packages}=="
  sim=$(RSH "LC_ALL=C apt-get -s purge ${packages}") || bail "清包模拟失败"
  purged=$(printf '%s\n' "${sim}" | awk '/^Purg /{print $2}' | LC_ALL=C sort | tr '\n' ' ')
  removed=$(printf '%s\n' "${sim}" | grep -c '^Remv ')
  [ "${purged}" = "${packages}" ] && [ "${removed}" = "0" ] || bail "模拟要动的不止这些包:Purg=[${purged}],Remv ${removed} 个"
  confirm "清掉上面这些 ${PURGE_ABI} 的包?(${FALLBACK_ABI} 留作回退)"
  RSH "systemd-run --wait --collect --quiet --unit nmu-kernel-purge-${STAMP} --property=StandardOutput=file:/root/nmu-kernel-purge-${STAMP}.log --property=StandardError=inherit --setenv=DEBIAN_FRONTEND=noninteractive --setenv=NEEDRESTART_MODE=l apt-get purge -y -q -o DPkg::Lock::Timeout=600 ${packages}"
  rc=$?
  RSH "tail -8 /root/nmu-kernel-purge-${STAMP}.log"
  [ "${rc}" = "0" ] || bail "清包退出码 ${rc}(日志 /root/nmu-kernel-purge-${STAMP}.log)"
  RBASH "TARGET_ABI=${TARGET_ABI} FALLBACK_ABI=${FALLBACK_ABI} PURGE_ABI=${PURGE_ABI} bash -s" <<'REMOTE' || bail "清完核对有 FAIL(看上面)"
set -u
bad=0
left=$(dpkg-query -W -f='${Package} ${db:Status-Abbrev}\n' "linux-*${PURGE_ABI}*" 2>/dev/null | awk '$2 ~ /^(ii|rc)$/' | wc -l | tr -d ' ')
if [ "${left}" = "0" ]; then echo "  PASS ${PURGE_ABI} 的包都清了"; else echo "  FAIL 还剩 ${left} 个"; bad=1; fi
for abi in "${TARGET_ABI}" "${FALLBACK_ABI}"; do
  if [ -s "/boot/vmlinuz-${abi}-generic" ] && grep -q "vmlinuz-${abi}-generic" /boot/grub/grub.cfg; then
    echo "  PASS ${abi} 还在 /boot 和 GRUB 里"
  else
    echo "  FAIL ${abi} 不在 /boot 或 GRUB 里"; bad=1
  fi
done
if grep -q "vmlinuz-${PURGE_ABI}-generic" /boot/grub/grub.cfg; then echo "  FAIL GRUB 里还有 ${PURGE_ABI}"; bad=1; else echo "  PASS GRUB 里没有 ${PURGE_ABI} 了"; fi
echo "  /boot 还剩 $(df -h --output=avail /boot | tail -1 | tr -d ' ')"
exit "${bad}"
REMOTE
  echo "== purge 阶段完成 =="
}

case "${PHASE}" in
  check) banner; phase_check ;;
  kernel) banner; phase_kernel ;;
  verify-kernel) banner; phase_verify_kernel ;;
  netplan) banner; phase_netplan ;;
  verify-netplan) banner; phase_verify "${NET_BY_MAC}" ;;
  purge) banner; phase_purge ;;
  *) usage; exit 64 ;;
esac
