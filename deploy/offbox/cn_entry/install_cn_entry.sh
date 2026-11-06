#!/usr/bin/env bash
# NMU 国内入口:在 Eric 的 Mac 上跑,经 ssh 改 openclaw-prod(147.182.176.237)上的 haproxy,
# 给转往 box A 的那一跳加 PROXY v2 头(send-proxy-v2),box A 的 Caddy 才看得到国内用户的真实地址。
# 要 Eric 亲手跑:改 /etc/haproxy、reload 生产入口,agent 那边一律被挡住。
#
# 顺序(反了国内入口当场全断):
#   装:先在 box A 的 Caddy 全局选项加 boxa-caddy-global.snippet 并核过(Claude 另给的脚本),再跑这里;
#       预检从 droplet 直连 box A 真发一个带 PROXY v2 头的请求,box A 没准备好就停,haproxy 不动。
#   回滚:反过来,先跑这里的 --rollback,再去 box A 删 snippet。
#
# 跑法(整屏输出发 Claude;任何一步打印「停」就停下,不要接着跑):
#   bash deploy/offbox/cn_entry/install_cn_entry.sh --plan        只列这次会做哪几步,不连任何机器
#   bash deploy/offbox/cn_entry/install_cn_entry.sh --preflight   阶段 0:只读预检
#   bash deploy/offbox/cn_entry/install_cn_entry.sh               阶段 0 1:预检、换配置并 reload,经 droplet 核对,不通自动还原
#   bash deploy/offbox/cn_entry/install_cn_entry.sh --verify      阶段 2:只读核对
#   bash deploy/offbox/cn_entry/install_cn_entry.sh --rollback    阶段 3:写回 11-03 那份、reload、核对
# 只动 /etc/haproxy/haproxy.cfg 和 haproxy 的 reload:不 restart,不碰 sing-box 与 OpenClaw,不读任何密钥。
# zsh/sh 跑会在半路以别的方式炸(BASH_SOURCE 为空),开头就拦下。
if [ -z "${BASH_VERSION:-}" ]; then echo "用 bash 跑:bash $0 $*"; exit 64; fi
set -euo pipefail

HOST="eric@147.182.176.237"
DROPLET_IP="147.182.176.237"
BOXA_IP="89.208.253.119"
SNI="147-182-176-237.sslip.io"
CFG="/etc/haproxy/haproxy.cfg"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
REPO_CFG="$HERE/haproxy.cfg"

RSH() { ssh -o BatchMode=yes -o ConnectTimeout=20 "$HOST" "$@"; }

describe() {
  case "$1" in
    0) echo "  0 只读预检:主机、sudo -n、haproxy 在跑、${CFG} 是 11-03 那份(去掉注释比)、ufw 放行 443、经 droplet 打 /build-id.txt 应 200(换之前的基线)、从 droplet 直连 box A 发 PROXY v2 头打 /build-id.txt 应 200(box A 没准备好就停)" ;;
    1) echo "  1 换配置:备份 ${CFG}.before-cn-proxy-<时间>,haproxy -c 过了才换上,systemctl reload(不 restart),经 droplet 自己的公网 IP 打 /build-id.txt 应 200;不对就还原备份再 reload" ;;
    2) echo "  2 只读核对:haproxy 在跑、${CFG} 是哪一份、经 droplet 打 /build-id.txt、box A 认不认 droplet 发的 PROXY v2 头" ;;
    3) echo "  3 回滚:写回 11-03 那份(没有 send-proxy-v2;已经是它就不重写),haproxy -c、reload、经 droplet 打 /build-id.txt 应 200" ;;
  esac
}

order() {
  echo "顺序:装 = 先在 box A 的 Caddy 加 boxa-caddy-global.snippet 并核过,再跑这里开 send-proxy-v2"
  echo "      回滚 = 反过来,先跑这里的 --rollback,再去 box A 删 snippet;反了国内入口当场全断"
}

MODE="install"
MODES=0
PLAN=0
for arg in "$@"; do
  case "$arg" in
    --plan) PLAN=1 ;;
    --preflight) MODE="preflight"; MODES=$((MODES + 1)) ;;
    --verify) MODE="verify"; MODES=$((MODES + 1)) ;;
    --rollback) MODE="rollback"; MODES=$((MODES + 1)) ;;
    -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "不认识的参数 ${arg},停"; exit 64 ;;
  esac
done
if [ "$MODES" -gt 1 ]; then
  echo "--preflight / --verify / --rollback 一次只能给一个,停"; exit 64
fi
case "$MODE" in
  install) STAGES="0 1" ;;
  preflight) STAGES="0" ;;
  verify) STAGES="2" ;;
  rollback) STAGES="3" ;;
esac

echo "stages: ${STAGES}"
for s in $STAGES; do describe "$s"; done
order
if [ "$PLAN" = 1 ]; then exit 0; fi

# 11-03 手装、跑到现在的那份;--rollback 写回的就是它。预检拿它去掉注释后比对 droplet 上的现状。
pre_proxy_cfg() {
  cat <<'PRE_PROXY'
# NMU 国内入口:11-03 手装的那份,没有 send-proxy-v2(install_cn_entry.sh --rollback 写回的就是它)。
global
    log /dev/log local0
    chroot /var/lib/haproxy
    stats socket /run/haproxy/admin.sock mode 660 level admin
    user haproxy
    group haproxy
    daemon
    maxconn 512
defaults
    log global
    mode tcp
    option tcplog
    timeout connect 10s
    timeout client 10m
    timeout server 10m
frontend nmu_cn_entry
    bind 147.182.176.237:443
    default_backend nmu_box_a
backend nmu_box_a
    server box_a 89.208.253.119:443
PRE_PROXY
}

# 两份配置用 base64 当参数带过去,远端一条 ssh 里比对、写入、reload,不在 droplet 上留中间文件。
remote() {
  RSH "bash -s -- '$1' '$CFG' '$DROPLET_IP' '$BOXA_IP' 443 '$SNI' '$REPO_B64' '$PRE_B64'" <<'REMOTE'
set -u
mode="$1"; cfg="$2"; droplet="$3"; boxa="$4"; boxa_port="$5"; sni="$6"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
printf '%s' "$7" | base64 -d > "$work/repo.cfg"
printf '%s' "$8" | base64 -d > "$work/pre.cfg"
bad=0
note() { printf '  %s\n' "$*"; }
fail() { printf '  ✗ %s\n' "$*"; bad=1; }
# haproxy 按空白切词、# 起注释:去掉注释与空行、压掉多余空白之后比,手装那份的注释怎么写都不影响。
norm() { sed -E -e 's/^[[:space:]]+//' -e '/^#/d' -e 's/[[:space:]]+#.*$//' -e 's/[[:space:]]+/ /g' -e 's/ $//' -e '/^$/d' "$1"; }
nsha() { norm "$1" | sha256sum | cut -d' ' -f1; }
mask() { sed -E 's/(^|[[:space:]])(auth|password|insecure-password)[[:space:]]+[^[:space:]]+/\1\2 <遮住>/g'; }
# 不能放进 $(…) 里调:读不到时的 exit 只会退出子 shell。
now() {
  sudo -n cat "$cfg" > "$work/now.cfg" || { echo "  ✗ 读不到 ${cfg},停"; exit 1; }
  case "$(nsha "$work/now.cfg")" in
    "$(nsha "$work/repo.cfg")") which=repo ;;
    "$(nsha "$work/pre.cfg")") which=pre ;;
    *) which=other ;;
  esac
}
show_diff() {
  norm "$work/$1.cfg" > "$work/a"; norm "$work/now.cfg" > "$work/b"
  echo "  --- $2(去掉注释)"; echo "  +++ 现在的 ${cfg}(去掉注释)"
  diff -u "$work/a" "$work/b" | tail -n +3 | mask | sed 's/^/  /'
}
# 经 droplet 自己的公网 IP 走一遍 haproxy:国内用户走的就是这一跳。
via_droplet() {
  local code="000" i
  for i in 1 2 3; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 --resolve "${sni}:443:${droplet}" "https://${sni}/build-id.txt" 2>/dev/null)" || true
    [ "$code" = 200 ] && break
    sleep 2
  done
  echo "${code:-000}"
}
# Type=notify 的 haproxy reload 前后会短暂报 reloading:等它落定再判,最多约 10 秒。结果在 state 里。
settle() {
  local i
  for i in 1 2 3 4 5; do
    state="$(systemctl is-active haproxy 2>/dev/null)" || true
    case "$state" in reloading|activating) sleep 2 ;; *) return 0 ;; esac
  done
}
# 从 droplet 直连 box A,先发 PROXY v2 头再走 TLS(验证书)打 /build-id.txt:haproxy 换上之后每条连接都是这样。
ready() {
  /usr/bin/python3 -I - "$boxa" "$boxa_port" "$sni" "$droplet" <<'READY'
import socket
import ssl
import struct
import sys

boxa, port, sni, droplet = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
# 源地址用 TEST-NET-1(RFC 5737):box A 那头的限速、锁定只会记在一个没有真人用的地址上。
header = (b"\r\n\r\n\x00\r\nQUIT\n" + bytes((0x21, 0x11)) + struct.pack("!H", 12)
          + socket.inet_aton("192.0.2.1") + socket.inet_aton(droplet) + struct.pack("!HH", 40000, 443))
request = (f"GET /build-id.txt HTTP/1.1\r\nHost: {sni}\r\nUser-Agent: nmu-cn-entry-preflight\r\n"
           "Connection: close\r\n\r\n").encode()
head = b""
try:
    with socket.create_connection((boxa, port), timeout=15) as raw:
        raw.sendall(header)
        with ssl.create_default_context().wrap_socket(raw, server_hostname=sni) as tls:
            tls.sendall(request)
            while b"\r\n" not in head and len(head) < 4096:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                head += chunk
except OSError as error:
    print(f"ready=no {type(error).__name__}: {error}")
    sys.exit(1)
status = head.split(b"\r\n", 1)[0].decode("latin-1")
ok = status.split(" ")[1:2] == ["200"]
print(f"ready={'yes' if ok else 'no'} status={status or '-'}")
sys.exit(0 if ok else 1)
READY
}
install_cfg() {
  local src="$1" tag="$2"
  sudo -n haproxy -c -f "$src" > "$work/check" 2>&1 \
    || { echo "  ✗ 要换上的那份 haproxy -c 不过,停;没动:"; sed 's/^/    /' "$work/check"; exit 1; }
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup="${cfg}.before-${tag}-${stamp}"
  sudo -n cp -p "$cfg" "$backup" || { echo "  ✗ 备份没成,停;没动"; exit 1; }
  echo "  备份:${backup}"
  # 先在同目录写好 .new(cp -p 带上原来的属主与权限),再一次 mv 换上。
  sudo -n cp -p "$cfg" "${cfg}.${tag}.new" && sudo -n tee "${cfg}.${tag}.new" < "$src" > /dev/null \
    && sudo -n mv -f "${cfg}.${tag}.new" "$cfg" \
    || { echo "  ✗ 写 ${cfg} 没成,停(没换上的话会留下 ${cfg}.${tag}.new)"; exit 1; }
}

[ "$(hostname)" = "openclaw-prod" ] || { echo "  ✗ 主机是 $(hostname),不是 openclaw-prod,停"; exit 1; }
sudo -n true 2>/dev/null || { echo "  ✗ sudo -n 要密码,停"; exit 1; }

case "$mode" in
  preflight)
    note "主机 openclaw-prod,sudo -n 可用"
    if version="$(sudo -n haproxy -v 2>&1 | head -n 1)" && [ -n "$version" ]; then note "$version"; else fail "没有 haproxy"; fi
    state="$(systemctl is-active haproxy 2>/dev/null)" || true
    if [ "$state" = active ]; then note "haproxy active"; else fail "haproxy 是 ${state:-查不到},不是 active"; fi
    now
    note "${cfg} sha256 $(sha256sum < "$work/now.cfg" | cut -d' ' -f1)(原样)"
    installed=0
    case "$which" in
      pre) note "${cfg} 去掉注释后 = 11-03 那份(还没有 send-proxy-v2)" ;;
      repo) note "${cfg} 去掉注释后 = 仓库那份(send-proxy-v2 已经开着)"; installed=1 ;;
      *) fail "${cfg} 既不是 11-03 那份也不是仓库那份,停。差异:"; show_diff pre "11-03 那份" ;;
    esac
    if sudo -n haproxy -c -f "$cfg" > "$work/check" 2>&1; then note "现在这份 haproxy -c 通过"; else fail "现在这份 haproxy -c 都不过:$(head -n 3 "$work/check" | tr '\n' ' ')"; fi
    sudo -n ufw status > "$work/ufw" 2>&1 || true
    if grep -q '^Status: active' "$work/ufw" && grep -Eq '^443/tcp[[:space:]]+ALLOW[[:space:]]' "$work/ufw"; then
      note "ufw active,放行 443:"; grep -E '^443/tcp' "$work/ufw" | sed 's/^/    /'
    else
      fail "ufw 不是 active 或没有放行 443/tcp 的规则:"; sed 's/^/    /' "$work/ufw"
    fi
    code="$(via_droplet)"
    if [ "$code" = 200 ]; then note "经 droplet 打 /build-id.txt = 200(换配置之前的基线)"; else fail "换配置之前经 droplet 打 /build-id.txt 就是 ${code},先查这个"; fi
    if out="$(ready)"; then
      note "box A 认 droplet 发的 PROXY v2 头(TEST-NET-1 源地址、SNI ${sni}、证书已验):${out}"
    else
      fail "box A 还不认 droplet 发的 PROXY v2 头:${out}"
      fail "先在 box A 装 boxa-caddy-global.snippet 并核过,再来;haproxy 一个字没动"
    fi
    [ "$bad" = 0 ] && [ "$installed" = 1 ] && exit 3
    exit "$bad" ;;

  install)
    now
    [ "$which" = pre ] || { echo "  ✗ 这一刻 ${cfg} 已经不是 11-03 那份(预检之后有人改过),停;没动"; exit 1; }
    install_cfg "$work/repo.cfg" cn-proxy
    restore() {
      echo "  ✗ $1:从 ${backup} 还原、reload"
      sudo -n cp -p "$backup" "$cfg"
      reloaded=yes
      sudo -n systemctl reload haproxy || reloaded=no
      settle
      code="$(via_droplet)"
      now
      if [ "$which" != pre ]; then
        echo "  ✗✗ 还原没写上,${cfg} 不是 11-03 那份;手工处理:sudo cp -p ${backup} ${cfg} && sudo systemctl reload haproxy"
      elif [ "$state" != active ]; then
        # 停掉的 unit systemd 不让 reload,只能 start(不是 restart)。
        echo "  ✗✗ ${cfg} 已还原成 11-03 那份,haproxy 是 ${state:-查不到},不是 active;是 inactive/failed 的话手工处理:sudo systemctl start haproxy"
      elif [ "$reloaded" = no ]; then
        echo "  ✗✗ ${cfg} 已还原成 11-03 那份,但 reload 失败,跑着的可能还是换上去的那份;手工处理:sudo systemctl reload haproxy"
      elif [ "$code" != 200 ]; then
        echo "  ✗✗ ${cfg} 已还原成 11-03 那份、reload 完 haproxy active,经 droplet 打 /build-id.txt 仍是 ${code}(预检时是 200):不是配置的事,停"
      else
        echo "  已还原:${cfg} 回到 11-03 那份,经 droplet 打 /build-id.txt = 200;停"
      fi
      exit 1
    }
    now
    [ "$which" = repo ] || restore "换上去的不是仓库那份"
    sudo -n systemctl reload haproxy || restore "systemctl reload haproxy 失败"
    settle
    [ "$state" = active ] || restore "reload 之后 haproxy 不是 active(是 ${state:-查不到})"
    code="$(via_droplet)"
    [ "$code" = 200 ] || restore "经 droplet 打 /build-id.txt = ${code},不是 200"
    echo "  ${cfg} = 仓库那份(send-proxy-v2),reload 完 haproxy active,经 droplet 打 /build-id.txt = 200"
    echo "  回滚:bash deploy/offbox/cn_entry/install_cn_entry.sh --rollback(先这里,再去 box A 删 snippet)" ;;

  verify)
    state="$(systemctl is-active haproxy 2>/dev/null)" || true
    if [ "$state" = active ]; then note "haproxy active"; else fail "haproxy 是 ${state:-查不到},不是 active"; fi
    now
    case "$which" in
      repo) note "${cfg} = 仓库那份(send-proxy-v2 开着)" ;;
      pre) note "${cfg} = 11-03 那份(send-proxy-v2 没开)" ;;
      *) fail "${cfg} 既不是 11-03 那份也不是仓库那份:"; show_diff repo "仓库那份" ;;
    esac
    code="$(via_droplet)"
    if [ "$code" = 200 ]; then note "经 droplet 打 /build-id.txt = 200"; else fail "经 droplet 打 /build-id.txt = ${code}"; fi
    if out="$(ready)"; then note "box A 认 PROXY v2 头:${out}"; else note "box A 不认 PROXY v2 头:${out}(send-proxy-v2 开着时这就是故障)"; fi
    exit "$bad" ;;

  rollback)
    now
    case "$which" in
      # 文件是这份不等于跑着的是这份(上回 reload 失败、手工改过):照样 reload、核对,才说能删 snippet。
      pre) echo "  ${cfg} 已经是 11-03 那份,不重写;照样 reload 一次,让跑着的也是它" ;;
      repo) install_cfg "$work/pre.cfg" cn-rollback ;;
      *) echo "  ✗ ${cfg} 既不是仓库那份也不是 11-03 那份,停;没动。差异:"; show_diff repo "仓库那份"; exit 1 ;;
    esac
    # 回滚之后不再自动换回去:不带头的 11-03 那份,box A 加没加 snippet 都接得住,是两头都安全的状态。
    if ! sudo -n systemctl reload haproxy; then
      # 文件要跟着跑着的那份走:不然 --verify 会说 send-proxy-v2 没开。
      [ "$which" = repo ] && sudo -n cp -p "$backup" "$cfg" && echo "  ${cfg} 已换回仓库那份,和跑着的一致"
      echo "  ✗ reload 失败:haproxy 可能还在跑带 send-proxy-v2 的配置,box A 的 snippet 先别删,停"
      exit 1
    fi
    settle
    [ "$state" = active ] || { echo "  ✗ reload 之后 haproxy 是 ${state:-查不到},不是 active;${cfg} 保持 11-03 那份,停"; exit 1; }
    code="$(via_droplet)"
    if [ "$code" != 200 ]; then
      echo "  ✗ 回滚后经 droplet 打 /build-id.txt = ${code};${cfg} 保持 11-03 那份(box A 两种状态都接得住),问题不在这一跳,停"
      exit 1
    fi
    echo "  ${cfg} = 11-03 那份,reload 完经 droplet 打 /build-id.txt = 200;现在可以去 box A 删 snippet" ;;
esac
REMOTE
}

stage0() {
  echo "== 0. 只读预检(什么都不改) =="
  local rc=0
  remote preflight || rc=$?
  case "$rc" in
    0) echo "  预检通过" ;;
    3) echo "  droplet 上已经是仓库那份,不重复装;看它跑得怎样用 --verify"; exit 0 ;;
    *) echo "  预检没过,停(什么都没改)"; exit 1 ;;
  esac
}

stage1() {
  echo "== 1. 换配置(备份、haproxy -c、reload、经 droplet 核对;不通自动还原) =="
  remote install || { echo "  换配置没成,停"; exit 1; }
}

stage2() {
  echo "== 2. 只读核对 =="
  remote verify || { echo "  核对没过,把输出发 Claude"; exit 1; }
}

stage3() {
  echo "== 3. 回滚到 11-03 那份 =="
  remote rollback || { echo "  回滚没成,停(box A 的 snippet 先别删)"; exit 1; }
}

echo "== 本地:装的是哪一版 =="
for tool in ssh git base64; do
  command -v "$tool" >/dev/null || { echo "本机没有 ${tool},停"; exit 1; }
done
[ -f "$REPO_CFG" ] || { echo "缺 ${REPO_CFG},停"; exit 1; }
if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  echo "${REPO} 不是 git 仓库:装上去的东西对不上任何一个提交,停"
  exit 1
fi
if [ -n "$(git -C "$REPO" --no-pager status --porcelain -- deploy/offbox/cn_entry)" ]; then
  echo "deploy/offbox/cn_entry 有没提交的改动,装上去的东西对不上任何一个提交,停:"
  git -C "$REPO" --no-pager status --short -- deploy/offbox/cn_entry
  exit 1
fi
echo "deploy/offbox/cn_entry @ $(git -C "$REPO" --no-pager log -1 --format='%h %s' -- deploy/offbox/cn_entry | cut -c1-80)"
REPO_B64="$(base64 < "$REPO_CFG" | tr -d '\n')"
PRE_B64="$(pre_proxy_cfg | base64 | tr -d '\n')"

for s in $STAGES; do "stage$s"; done
