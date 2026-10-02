#!/usr/bin/env bash
# NMU 异地探针安装器:在 Eric 的 Mac 上跑,把 deploy/offbox/ 装到 OpenClaw VPS(openclaw-prod)。
# 探针每 10 分钟从纽约打一次 NMU 公网 /health,状态变了才发 Discord(规则见 nmu_offbox_probe.py)。
# 要 Eric 亲手跑:写 crontab、真发 Discord 消息,agent 那边一律被挡住。
#
# 跑法(整屏输出发 Claude;任何一步打印「停」就停下,不要接着跑):
#   bash deploy/offbox/install_offbox_probe.sh --plan         只列这次会做哪几步,不连任何机器
#   bash deploy/offbox/install_offbox_probe.sh --preflight    阶段 0:只读预检
#   bash deploy/offbox/install_offbox_probe.sh                阶段 0 1 2 3 5:预检、推送、零消息自测、核频道、装 crontab
#   bash deploy/offbox/install_offbox_probe.sh --test-alert   同上,装 crontab 之前多真发一条测试消息(阶段 4)
#   bash deploy/offbox/install_offbox_probe.sh --heartbeat    阶段 6:把 check_nmu_probe 插进 heartbeat.py
#   bash deploy/offbox/install_offbox_probe.sh --verify       阶段 7:下一个 :x4 跑过之后核对(零消息)
# 除阶段 4 外不发任何 Discord 消息;不调 openclaw 命令行;secrets.env 只数行、不读值。
# zsh/sh 跑会在半路以别的方式炸(BASH_SOURCE 为空、${x:0:12} 不认),开头就拦下。
if [ -z "${BASH_VERSION:-}" ]; then echo "用 bash 跑:bash $0 $*"; exit 64; fi
set -euo pipefail

HOST="eric@147.182.176.237"
REMOTE_DIR="/home/eric/.openclaw/workspace/nmu-probe"
CRONTAB_BASE_SHA="31d8f685282856fe53ec4688d344aadc9dd9a564e313347a0c74b5af7c26b4d7"
HEARTBEAT_BASE_SHA="8d478629ba2bc0bc0d3ab26df85c6b6ee1ceb674d414b1507f34b8a6ca2ca9be"
DAILY_CHANNEL_ID="1490232840397389894"
GOOD_URL="https://89-208-253-119.sslip.io/health"
# 不存在的 /health* 路径:SPA 外壳回 200 + HTML,探针判 body_mismatch。上线脚本的
# 空闲检查(264:84)不数 /health 开头的请求,自测不会把后面的上线窗口挡住。
BAD_URL="https://89-208-253-119.sslip.io/health-selftest"
NMU_HOST="root@89.208.253.119"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PROBE="$HERE/nmu_offbox_probe.py"
LINE_FILE="$HERE/crontab.line"
HB_CHECK="$HERE/heartbeat_check.py"

RSH() { ssh -o BatchMode=yes -o ConnectTimeout=20 "$HOST" "$@"; }

describe() {
  case "$1" in
    0) echo "  0 只读预检:主机、crontab 基线 sha(不符就列差异并停)、--arm、keychain-get、webhook 行数、python3/curl/flock" ;;
    1) echo "  1 推送 nmu_offbox_probe.py 与 crontab.line 到 ${REMOTE_DIR}(sha256 对上才换名)" ;;
    2) echo "  2 零消息自测:--dry-run 写临时状态,真地址 → 坏地址两轮 → 真地址,应依次 - / - / DOWN / RECOVERED" ;;
    3) echo "  3 核 webhook 指向的频道(GET,不发消息):应为 #daily-digest ${DAILY_CHANNEL_ID}" ;;
    4) echo "  4 真发一条测试消息到 #daily-digest(只发一次、不重试,应 HTTP 204)" ;;
    5) echo "  5 装 crontab:备份到 ~/.openclaw/backups,一次 crontab - 追加一行,逐字节核对,不对就自动还原" ;;
    6) echo "  6 Heartbeat:heartbeat.py 是基线 sha 才插 check_nmu_probe,先备份,插完单独调这一项" ;;
    7) echo "  7 装后核对:state 新鲜且 UP、probe.log 最后一轮 UP ok、cron.log 空、NMU 那头看得到纽约来的 /health" ;;
  esac
}

MODE="install"
MODES=0
TEST_ALERT=0
PLAN=0
for arg in "$@"; do
  case "$arg" in
    --plan) PLAN=1 ;;
    --preflight) MODE="preflight"; MODES=$((MODES + 1)) ;;
    --heartbeat) MODE="heartbeat"; MODES=$((MODES + 1)) ;;
    --verify) MODE="verify"; MODES=$((MODES + 1)) ;;
    --test-alert) TEST_ALERT=1 ;;
    -h|--help) sed -n '2,13p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "不认识的参数 ${arg},停"; exit 64 ;;
  esac
done
if [ "$MODES" -gt 1 ]; then
  echo "--preflight / --heartbeat / --verify 一次只能给一个,停"; exit 64
fi
if [ "$TEST_ALERT" = 1 ] && [ "$MODE" != "install" ]; then
  echo "--test-alert 只能跟完整安装一起用,停"; exit 64
fi
case "$MODE" in
  install)
    STAGES="0 1 2 3"
    if [ "$TEST_ALERT" = 1 ]; then STAGES="$STAGES 4"; fi
    STAGES="$STAGES 5" ;;
  preflight) STAGES="0" ;;
  heartbeat) STAGES="6" ;;
  verify) STAGES="7" ;;
esac

echo "stages: ${STAGES}"
for s in $STAGES; do describe "$s"; done
if [ "$PLAN" = 1 ]; then exit 0; fi

# 推一个文件:先写 .new,两边 sha256 对上才换名;权限 0600。
push() {
  local src="$1" dest="$2" want got
  want="$(shasum -a 256 "$src" | cut -d' ' -f1)"
  RSH "cat > '${dest}.new'" < "$src"
  got="$(RSH "sha256sum '${dest}.new'" | cut -d' ' -f1)"
  if [ "$got" != "$want" ]; then
    echo "  ✗ $(basename "$src") 传过去 sha256 不对(本地 ${want:0:12}…,远端 ${got:0:12}…),停"
    exit 1
  fi
  RSH "chmod 600 '${dest}.new' && mv -f '${dest}.new' '${dest}'"
  echo "  $(basename "$src") → ${dest}(sha256 ${want:0:12}…)"
}

stage0() {
  echo "== 0. 只读预检(什么都不改) =="
  local rc=0
  RSH "bash -s -- '$CRONTAB_BASE_SHA' '$REMOTE_DIR'" <<'PREFLIGHT' || rc=$?
set -u
want="$1"; dir="$2"
bad=0
note() { printf '  %s\n' "$*"; }
fail() { printf '  ✗ %s\n' "$*"; bad=1; }
# 差异里可能有 KEY=… 或 webhook,发给 Claude 之前先遮住。
mask() { sed -E 's#(https://(discord|discordapp)\.com/api/webhooks/)[^[:space:]]+#\1<webhook>#g; s#([A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASS|WEBHOOK|key|token|secret|pass|webhook)[A-Za-z0-9_]*=)[^[:space:]]+#\1<遮住>#g'; }
host_name="$(hostname)"
if [ "$host_name" != "openclaw-prod" ]; then echo "  ✗ 主机是 ${host_name},不是 openclaw-prod,停"; exit 1; fi
note "主机 openclaw-prod"
[ "$HOME" = "/home/eric" ] || fail "HOME 是 ${HOME},探针路径写死在 /home/eric 下"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
crontab -l > "$work/now" 2>/dev/null || { echo "  ✗ 读不到 crontab,停"; exit 1; }
have="$(sha256sum < "$work/now" | cut -d' ' -f1)"
if [ "$have" != "$want" ]; then
  if [ "$(grep -c nmu_offbox_probe "$work/now")" = 1 ]; then
    grep -v nmu_offbox_probe "$work/now" > "$work/rest" || true
    if [ "$(sha256sum < "$work/rest" | cut -d' ' -f1)" = "$want" ]; then
      note "crontab 里已经有探针那一行,其余各行与基线逐字节相同"
      exit 3
    fi
  fi
  echo "  ✗ crontab 不是基线(基线 ${want:0:16}…,现在 ${have:0:16}…),停。差异:"
  ref=""
  for f in $(ls -t "$HOME"/.openclaw/backups/crontab.* 2>/dev/null); do
    if [ "$(sha256sum < "$f" | cut -d' ' -f1)" = "$want" ]; then ref="$f"; break; fi
  done
  if [ -z "$ref" ]; then ref="$(ls -t "$HOME"/.openclaw/backups/crontab.* 2>/dev/null | head -n 1)"; fi
  if [ -n "$ref" ]; then
    echo "  --- ${ref}(与基线同 sha 的备份;没有就是最新一份)"
    echo "  +++ 现在的 crontab"
    diff -u "$ref" "$work/now" | tail -n +3 | mask | sed 's/^/  /'
  else
    echo "  (没有任何 crontab 备份可比,下面是现在的 crontab)"
    mask < "$work/now" | nl -ba | sed 's/^/  /'
  fi
  exit 1
fi
note "crontab = 基线 ($(wc -l < "$work/now" | tr -d ' ') 行)"
[ "$(grep -c nmu_offbox_probe "$work/now")" = 0 ] || fail "crontab 里已经有 nmu_offbox_probe"
arm="$(grep -c -- '--arm' "$work/now")"
if [ "$arm" = 1 ]; then note "--arm 行数 1(watchdog v1 armed)"; else fail "--arm 行数 ${arm},应为 1"; fi
if [ -x "$HOME/.openclaw/bin/keychain-get" ]; then note "keychain-get 在"; else fail "没有可执行的 ~/.openclaw/bin/keychain-get"; fi
hooks="$(grep -c '^OPENCLAW_DISCORD_DAILY_WEBHOOK=' "$HOME/.openclaw/secrets.env" 2>/dev/null)"
if [ "$hooks" = 1 ]; then note "secrets.env 里 OPENCLAW_DISCORD_DAILY_WEBHOOK 1 行(只数行,不读值)"; else fail "OPENCLAW_DISCORD_DAILY_WEBHOOK 行数 ${hooks:-读不到},应为 1"; fi
for tool in /usr/bin/python3 /usr/bin/curl /usr/bin/flock; do
  if [ -x "$tool" ]; then note "${tool} 在"; else fail "没有 ${tool}"; fi
done
if /usr/bin/python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' </dev/null 2>/dev/null; then
  note "$(/usr/bin/python3 -V 2>&1)"
else
  fail "/usr/bin/python3 低于 3.12 或跑不起来"
fi
if [ -e "$dir" ]; then note "${dir} 已存在(上次装到一半?):$(ls -A "$dir" | tr '\n' ' ')"; fi
exit "$bad"
PREFLIGHT
  case "$rc" in
    0) echo "  预检通过" ;;
    3) echo "  探针已经装过,不重复装;看它跑得怎样用 --verify"; exit 0 ;;
    *) echo "  预检没过,停(什么都没改)"; exit 1 ;;
  esac
}

stage1() {
  echo "== 1. 推送探针与 crontab 那一行(先写 .new,sha256 对上才换名) =="
  RSH "install -d -m 700 '$REMOTE_DIR'"
  push "$PROBE" "$REMOTE_DIR/nmu_offbox_probe.py"
  push "$LINE_FILE" "$REMOTE_DIR/crontab.line"
  local out
  out="$(RSH "/usr/bin/python3 -B '$REMOTE_DIR/nmu_offbox_probe.py' --status </dev/null" 2>&1)" \
    || { echo "  ✗ 推上去的脚本跑不起来,停:${out}"; exit 1; }
  echo "  --status:${out}"
}

stage2() {
  echo "== 2. 零消息自测(--dry-run 写临时状态,不发消息、不碰正式状态;约 2 分钟) =="
  RSH "bash -s -- '$REMOTE_DIR' '$GOOD_URL' '$BAD_URL'" <<'SELFTEST' || { echo "  自测没过,停(crontab 没动)"; exit 1; }
set -u
dir="$1"; good="$2"; bad_url="$3"
scratch="$(mktemp -d /tmp/nmu-probe-selftest.XXXXXX)"; trap 'rm -rf "$scratch"' EXIT
probe() { /usr/bin/python3 -B "$dir/nmu_offbox_probe.py" --dry-run --state "$scratch/state.json" "$@" </dev/null 2>&1; }
bad=0
expect() {
  local label="$1" out="$2"; shift 2
  echo "  ${label}: ${out}"
  for want in "$@"; do
    case "$out" in *"$want"*) ;; *) echo "    ✗ 应当含 ${want}"; bad=1 ;; esac
  done
  case "$out" in *"result=probe_side"*) echo "    ✗ 纽约这头连 Discord 都打不通,这一轮不算数"; bad=1 ;; esac
}
out="$(probe --url "$good")"; expect "真地址" "$out" "status=up result=ok" "would_send=-"
out="$(probe --url "$bad_url")"; expect "坏地址第 1 轮" "$out" "status=up" "streak=1" "would_send=-"
out="$(probe --url "$bad_url")"; expect "坏地址第 2 轮" "$out" "status=down" "would_send=DOWN"
out="$(probe --url "$good")"; expect "真地址恢复" "$out" "status=up result=ok" "would_send=RECOVERED"
exit "$bad"
SELFTEST
  echo "  自测通过:0 条消息"
}

stage3() {
  echo "== 3. 核 webhook 指向的频道(GET,不发消息) =="
  local out
  out="$(RSH "/usr/bin/python3 -B '$REMOTE_DIR/nmu_offbox_probe.py' --check-webhook </dev/null" 2>&1)" \
    || { echo "  ✗ ${out}"; echo "  取不到 webhook 信息,停"; exit 1; }
  echo "  ${out}"
  case "$out" in
    *"channel_id=${DAILY_CHANNEL_ID} "*) echo "  频道对:#daily-digest" ;;
    *) echo "  ✗ 频道不是 ${DAILY_CHANNEL_ID},停"; exit 1 ;;
  esac
}

stage4() {
  echo "== 4. 真发一条测试消息到 #daily-digest(只发一次,不重试) =="
  local out
  out="$(RSH "/usr/bin/python3 -B '$REMOTE_DIR/nmu_offbox_probe.py' --test-alert </dev/null" 2>&1)" || true
  echo "  ${out}"
  case "$out" in
    *"test_alert_sent status=204"*) echo "  去 #daily-digest 看一眼:应有一条「NMU 异地探针 · 测试消息」" ;;
    *) echo "  ✗ 测试消息没发成功,停(crontab 没动)"; exit 1 ;;
  esac
}

stage5() {
  echo "== 5. 装 crontab(备份、一次 crontab -、逐字节核对,不对就自动还原) =="
  RSH "bash -s -- '$CRONTAB_BASE_SHA' '$REMOTE_DIR/crontab.line'" <<'CRONTAB' || { echo "  装 crontab 没成,停"; exit 1; }
set -euo pipefail
want="$1"; line_file="$2"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$HOME/.openclaw/backups"
backup="$HOME/.openclaw/backups/crontab.before-nmu-probe-${stamp}"
crontab -l > "$backup"
chmod 600 "$backup"
if [ "$(sha256sum < "$backup" | cut -d' ' -f1)" != "$want" ]; then
  echo "  ✗ 备份这一刻 crontab 已经不是基线(预检之后有人改过),停;crontab 没动"
  exit 1
fi
line="$(cat "$line_file")"
case "$line" in
  "4-59/10 "*nmu_offbox_probe.py*) ;;
  *) echo "  ✗ ${line_file} 不是探针那一行,停;crontab 没动"; exit 1 ;;
esac
{ cat "$backup"; [ -z "$(tail -c1 "$backup")" ] || printf '\n'; printf '%s\n' "$line"; } | crontab -
after="$(mktemp)"; trap 'rm -f "$after"' EXIT
crontab -l > "$after"
before_n="$(wc -l < "$backup" | tr -d ' ')"
after_n="$(wc -l < "$after" | tr -d ' ')"
bad=0
[ "$after_n" -eq $((before_n + 1)) ] || { echo "  ✗ 写入后 ${after_n} 行,应为 $((before_n + 1))"; bad=1; }
head -n "$before_n" "$after" | cmp -s - "$backup" || { echo "  ✗ 前 ${before_n} 行与备份不是逐字节相同"; bad=1; }
[ "$(grep -c nmu_offbox_probe "$after")" = 1 ] || { echo "  ✗ 探针那一行不是正好一行"; bad=1; }
[ "$(grep -c -- '--arm' "$after")" = 1 ] || { echo "  ✗ --arm 行数变了"; bad=1; }
[ "$(tail -n 1 "$after")" = "$line" ] || { echo "  ✗ 最后一行不是探针那一行"; bad=1; }
if [ "$bad" != 0 ]; then
  crontab "$backup"
  if [ "$(crontab -l | sha256sum | cut -d' ' -f1)" = "$want" ]; then
    echo "  已从 ${backup} 还原,crontab 回到基线;停"
  else
    echo "  ✗✗ 还原后仍不是基线,手工处理:crontab ${backup}"
  fi
  exit 1
fi
echo "  crontab ${before_n} → ${after_n} 行:前 ${before_n} 行与备份逐字节相同,探针一行,--arm 仍是 1 行"
echo "  备份:${backup}"
echo "  回滚:crontab ${backup}"
CRONTAB
  echo "  第一轮在下一个 :x4(UTC 每小时 04/14/24/34/44/54 分);过两分钟跑 --verify"
}

stage6() {
  echo "== 6. Heartbeat:插 check_nmu_probe(基线 sha 对上才动,先备份,只调这一项不跑整份) =="
  RSH "test -f '$REMOTE_DIR/nmu_offbox_probe.py'" \
    || { echo "  ✗ 探针还没装(先跑不带参数的安装),停"; exit 1; }
  push "$HB_CHECK" "$REMOTE_DIR/heartbeat_check.py"
  local rc=0
  RSH "bash -s -- '$HEARTBEAT_BASE_SHA' '$REMOTE_DIR'" <<'HEARTBEAT' || rc=$?
set -euo pipefail
want="$1"; dir="$2"
hb="$HOME/.openclaw/workspace/heartbeat/heartbeat.py"
[ "$(hostname)" = "openclaw-prod" ] || { echo "  ✗ 不是 openclaw-prod,停"; exit 1; }
[ -f "$hb" ] || { echo "  ✗ 没有 ${hb},停"; exit 1; }
have="$(sha256sum < "$hb" | cut -d' ' -f1)"
if [ "$have" != "$want" ]; then
  if grep -q 'nmu-offbox-probe: check_nmu_probe BEGIN' "$hb"; then
    echo "  heartbeat.py 里已经有 check_nmu_probe"
    exit 3
  fi
  echo "  ✗ heartbeat.py 不是基线(基线 ${want:0:16}…,现在 ${have:0:16}…):09-22 之后被改过,先对一下改了什么,停"
  exit 1
fi
staged="${hb}.nmu-probe.new"
/usr/bin/python3 -B "$dir/heartbeat_check.py" --insert "$hb" --out "$staged" </dev/null | sed 's/^/  /'
echo "  新文件单独调一次:$(/usr/bin/python3 -B "$dir/heartbeat_check.py" --call "$staged" </dev/null)"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="${hb}.before-nmu-probe-${stamp}"
cp -p "$hb" "$backup"
mv -f "$staged" "$hb"
echo "  换上之后再调一次:$(/usr/bin/python3 -B "$dir/heartbeat_check.py" --call "$hb" </dev/null)"
echo "  备份:${backup}"
echo "  回滚:cp -p ${backup} ${hb}"
HEARTBEAT
  case "$rc" in
    0) echo "  今晚 22:00 CT 的 Heartbeat 里应出现一行「NMU 异地探针」" ;;
    3) echo "  已经插过,没动 heartbeat.py" ;;
    *) echo "  heartbeat 这一步没过,停(heartbeat.py 没换)"; exit 1 ;;
  esac
}

stage7() {
  echo "== 7. 装后核对(零消息) =="
  RSH "bash -s -- '$REMOTE_DIR'" <<'VERIFY' || { echo "  核对没过,把输出发 Claude"; exit 1; }
set -u
dir="$1"
bad=0
st="$(/usr/bin/python3 -B "$dir/nmu_offbox_probe.py" --status </dev/null 2>&1)" || true
printf '%s\n' "$st" | sed 's/^/  /'
case "$st" in *"state=missing"*) echo "  还没跑过第一轮:等下一个 :x4 过两分钟再来"; exit 1 ;; esac
age="$(printf '%s\n' "$st" | sed -n 's/.* age_s=\([0-9][0-9]*\) .*/\1/p' | head -n 1)"
if [ -z "$age" ] || [ "$age" -gt 660 ]; then echo "  ✗ 最近一轮是 ${age:-?} 秒前,超过一个周期"; bad=1; fi
case "$st" in *"status=up"*) ;; *) echo "  ✗ 状态不是 up"; bad=1 ;; esac
echo "  probe.log 最后 3 行:"
tail -n 3 "$dir/probe.log" | sed 's/^/    /'
tail -n 1 "$dir/probe.log" | grep -q ' UP ok ' || { echo "  ✗ 最后一轮不是 UP ok"; bad=1; }
if [ -s "$dir/cron.log" ]; then
  echo "  ✗ cron.log 不是空的(探针抛过异常):"; tail -n 5 "$dir/cron.log" | sed 's/^/    /'; bad=1
else
  echo "  cron.log 空(没有异常)"
fi
echo "  crontab 里探针行数(应 1):$(crontab -l | grep -c nmu_offbox_probe)"
exit "$bad"
VERIFY
  local hits
  hits="$(ssh -n -o BatchMode=yes -o ConnectTimeout=20 -i "$HOME/.ssh/id_ed25519" "$NMU_HOST" \
    "journalctl -u nmu --since '-15 min' --no-pager -o cat 2>/dev/null | grep -c '147.182.176.237.*GET /health HTTP'" 2>/dev/null)" || true
  echo "  NMU 那头近 15 分钟来自 147.182.176.237 的 GET /health:${hits:-查不到}(应 ≥1;查不到只是少一条旁证)"
}

echo "== 本地:装的是哪一版 =="
for tool in ssh shasum git; do
  command -v "$tool" >/dev/null || { echo "本机没有 ${tool},停"; exit 1; }
done
for f in "$PROBE" "$LINE_FILE" "$HB_CHECK"; do
  [ -f "$f" ] || { echo "缺 ${f},停"; exit 1; }
done
if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  echo "${REPO} 不是 git 仓库:装上去的东西对不上任何一个提交,停"
  exit 1
fi
if [ -n "$(git -C "$REPO" --no-pager status --porcelain -- deploy/offbox)" ]; then
  echo "deploy/offbox 有没提交的改动,装上去的东西对不上任何一个提交,停:"
  git -C "$REPO" --no-pager status --short -- deploy/offbox
  exit 1
fi
echo "deploy/offbox @ $(git -C "$REPO" --no-pager log -1 --format='%h %s' -- deploy/offbox | cut -c1-80)"

for s in $STAGES; do "stage$s"; done

if [ "$MODE" = "install" ]; then
    cat <<'AFTER'
== 装好了。以后用得上的几条(都在 Mac 上敲) ==
  看状态:  ssh eric@147.182.176.237 '/usr/bin/python3 -B ~/.openclaw/workspace/nmu-probe/nmu_offbox_probe.py --status'
  维护超过 10 分钟先静默(最多认 24 小时,过期自动失效):
           ssh eric@147.182.176.237 'date -u -d "+90 min" +%s > ~/.openclaw/workspace/nmu-probe/silence-until'
  提前结束静默:ssh eric@147.182.176.237 'gio trash ~/.openclaw/workspace/nmu-probe/silence-until'
  回滚 crontab:ssh eric@147.182.176.237 'crontab ~/.openclaw/backups/crontab.before-nmu-probe-<上面打印的时间戳>'
  整个撤掉(先回滚 crontab):ssh eric@147.182.176.237 'gio trash ~/.openclaw/workspace/nmu-probe'
  下一步(可选):bash deploy/offbox/install_offbox_probe.sh --heartbeat
AFTER
fi
