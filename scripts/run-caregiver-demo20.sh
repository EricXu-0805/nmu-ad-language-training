#!/usr/bin/env bash
# 一条命令启动照护员 exact demo20 本机演练。
# 临时库、口令和 PIN 只用于本次进程；正常退出或 Ctrl+C 后清理。
set -euo pipefail

usage() {
  cat <<'EOF'
照护员 20 题本机演练

用法：
  scripts/run-caregiver-demo20.sh [--port 端口]
  scripts/run-caregiver-demo20.sh [--port 端口] --browser-check start-pause
  scripts/run-caregiver-demo20.sh --help

作用：
  1. 新建一个权限为 0700 的临时目录和全新数据库；
  2. 建立临时管理员、照护员和虚构档案；
  3. 真实跑通离线语音自检，建立「已审核」20 题安排；
  4. 只在 127.0.0.1 启动；正常退出或 Ctrl+C 后删除临时数据。

可选真实浏览器验收：
  --browser-check start-pause
  用本机已有 Chrome 走完开始、配对、朗读、录音、上传、
  ASR/判定、下一条命令和安全暂停，然后自动核对同一临时库。
  默认不执行浏览器验收，原有人工演练行为不变。

默认端口：8815
前置条件：web/dist/index.html 已完成构建，且 .venv 可用。
EOF
}

# --help 必须在查目录、建临时 root、调 Python 或启服务之前直接返回。
case "${1:-}" in
  -h|--help)
    [ "$#" -eq 1 ] || {
      echo "错误：--help 后不能再带其他参数" >&2
      exit 64
    }
    usage
    exit 0
    ;;
esac

PORT=8815
PORT_SEEN=0
BROWSER_CHECK=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --port)
      [ "$#" -ge 2 ] || {
        echo "错误：--port 后需要端口数字" >&2
        exit 64
      }
      [ "$PORT_SEEN" -eq 0 ] || {
        echo "错误：--port 不能重复" >&2
        exit 64
      }
      PORT="$2"
      PORT_SEEN=1
      shift 2
      ;;
    --browser-check)
      [ "$#" -ge 2 ] || {
        echo "错误：--browser-check 后需要 start-pause" >&2
        exit 64
      }
      [ -z "$BROWSER_CHECK" ] || {
        echo "错误：--browser-check 不能重复" >&2
        exit 64
      }
      [ "$2" = "start-pause" ] || {
        echo "错误：目前只支持 --browser-check start-pause" >&2
        exit 64
      }
      BROWSER_CHECK="$2"
      shift 2
      ;;
    *)
      echo "错误：不识别参数 $1；请用 --help 查看说明" >&2
      exit 64
      ;;
  esac
done
case "$PORT" in
  ''|*[!0-9]*)
    echo "错误：端口必须是 1024–65535 的数字" >&2
    exit 64
    ;;
esac
if [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
  echo "错误：端口必须在 1024–65535 之间" >&2
  exit 64
fi

REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
PYTHON="$REPO/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "错误：缺少可用的 .venv/bin/python，已停止" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "错误：本机缺少启动检查所需的 curl，已停止" >&2
  exit 1
fi
if [ ! -f "$REPO/web/dist/index.html" ]; then
  echo "错误：缺少 web/dist/index.html，请先完成网页构建；未建库、未启动" >&2
  exit 1
fi
"$PYTHON" "$REPO/scripts/verify_browser_dist.py" \
  --source-root "$REPO" "$REPO/web/dist"

BROWSER_PYTHON=""
if [ -n "$BROWSER_CHECK" ]; then
  BROWSER_CANDIDATES=("$PYTHON")
  if command -v python3 >/dev/null 2>&1; then
    BROWSER_CANDIDATES+=("$(command -v python3)")
  fi
  BROWSER_CANDIDATES+=("/opt/homebrew/bin/python3")
  for candidate in "${BROWSER_CANDIDATES[@]}"; do
    [ -x "$candidate" ] || continue
    if "$candidate" -I -c 'from playwright.sync_api import sync_playwright' \
        >/dev/null 2>&1; then
      BROWSER_PYTHON="$candidate"
      break
    fi
  done
  if [ -z "$BROWSER_PYTHON" ]; then
    echo "错误：本机没有可用的 Python Playwright；已停止，不会自动联网安装" >&2
    exit 1
  fi
  if [ ! -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
    echo "错误：本机没有可用的 Google Chrome，已停止" >&2
    exit 1
  fi
fi

HARNESS_ROOT=""
SERVER_PID=""
cleanup() {
  status=$?
  trap - EXIT
  # 清理期间忽略第二次 Ctrl+C/TERM/HUP，避免留下半清理的临时数据库。
  trap '' INT TERM HUP
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    # Ctrl+C 会同时送到前台 Uvicorn。先给它一点时间自行优雅退出，
    # 避免再补一个 TERM 后向非技术用户打印误导性的 “Terminated”。
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$SERVER_PID" 2>/dev/null || break
      sleep 0.1
    done
  fi
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    stop_deadline=$((SECONDS + 3))
    while kill -0 "$SERVER_PID" 2>/dev/null && [ "$SECONDS" -lt "$stop_deadline" ]; do
      sleep 0.1
    done
    if kill -0 "$SERVER_PID" 2>/dev/null; then
      kill -KILL "$SERVER_PID" 2>/dev/null || true
    fi
  fi
  if [ -n "$SERVER_PID" ]; then
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if [ -n "$HARNESS_ROOT" ] && [ "$HARNESS_ROOT" != "/" ]; then
    root_name="${HARNESS_ROOT##*/}"
    case "$root_name" in
      nmu-caregiver-demo20.*)
        if [ -d "$HARNESS_ROOT" ]; then
          chmod -R u+w "$HARNESS_ROOT" 2>/dev/null || true
          rm -rf -- "$HARNESS_ROOT"
        fi
        ;;
    esac
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

HARNESS_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/nmu-caregiver-demo20.XXXXXX")"
chmod 700 "$HARNESS_ROOT"
HARNESS_ROOT="$(cd "$HARNESS_ROOT" && pwd -P)"
mkdir -m 700 "$HARNESS_ROOT/tmp"

ADMIN_USERNAME="demo20-admin"
CAREGIVER_USERNAME="demo20-caregiver"
cd "$REPO"
generate_secret() {
  env -i \
    "PATH=${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}" \
    "HOME=$HARNESS_ROOT" \
    "TMPDIR=$HARNESS_ROOT/tmp" \
    "$PYTHON" -I -c "$1"
}
ADMIN_PASSWORD="$(generate_secret 'import secrets; print(secrets.token_urlsafe(24))')"
CAREGIVER_PASSWORD="$(generate_secret 'import secrets; print(secrets.token_urlsafe(24))')"
CONSOLE_PIN="$(generate_secret 'import secrets; print("".join(str(secrets.randbelow(10)) for _ in range(8)))')"
INSTANCE_MARKER="$(generate_secret 'import secrets; print(secrets.token_urlsafe(32))')"

# env -i 保证父 shell 里的云 key、指纹 key 和其他 provider 开关不会流入。
HARNESS_ENV=(
  "PATH=${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
  "HOME=$HARNESS_ROOT"
  "TMPDIR=$HARNESS_ROOT/tmp"
  "LANG=${LANG:-C.UTF-8}"
  "PYTHONPATH=$REPO"
  "PYTHONDONTWRITEBYTECODE=1"
  "NMU_HARNESS_ROOT=$HARNESS_ROOT"
  "DATABASE_URL=sqlite:///$HARNESS_ROOT/caregiver-demo.db"
  "AUDIO_DIR=$HARNESS_ROOT/audio"
  "NMU_HARNESS_TTS_CACHE_DIR=$HARNESS_ROOT/tts-cache"
  "NMU_HARNESS_TTS_MODE=synthetic"
  "NMU_HARNESS_ACTOR=$ADMIN_USERNAME"
  "NMU_HARNESS_PASSWORD=$ADMIN_PASSWORD"
  "NMU_CAREGIVER_USERNAME=$CAREGIVER_USERNAME"
  "NMU_CAREGIVER_PASSWORD=$CAREGIVER_PASSWORD"
  "NMU_CAREGIVER_HARNESS_INSTANCE=$INSTANCE_MARKER"
  "CONSOLE_PIN=$CONSOLE_PIN"
  "ALLOW_SIMULATION_DATA=1"
  "ENABLE_AUTOPILOT_P0A_SIMULATION=1"
  "REQUIRE_AUTH=1"
  "TTS_ENGINE=null"
  "ASR_ENGINE=null"
  "LLM_JUDGE=off"
)
if [ -n "$BROWSER_CHECK" ]; then
  HARNESS_ENV+=("NMU_HARNESS_TTS_DURATION_PROFILE=browser-start-pause")
fi

env -i "${HARNESS_ENV[@]}" \
  "$PYTHON" -m harness.caregiver_demo_harness --migrate-and-seed

env -i "${HARNESS_ENV[@]}" \
  "$PYTHON" -m harness.caregiver_demo_harness --serve --port "$PORT" &
SERVER_PID=$!

# 只有实际监听、harness marker 正确且两张网页都能返回时，才向非技术
# 使用者显示口令和“已就绪”。避免准备完成与服务可打开混为一谈。
READY=0
READY_DEADLINE=$((SECONDS + 60))
page_content_type() {
  local headers instance
  if ! headers=$(curl -fsS --noproxy '*' --connect-timeout 0.5 --max-time 0.5 \
      -D - -o /dev/null "$1" 2>/dev/null | tr -d '\r'); then
    return 1
  fi
  instance=$(printf '%s\n' "$headers" \
    | awk -F ': ' 'tolower($1)=="x-nmu-caregiver-harness-instance" {print $2; exit}')
  [ "$instance" = "$INSTANCE_MARKER" ] || return 1
  printf '%s\n' "$headers" \
    | awk -F ': ' 'tolower($1)=="content-type" {print tolower($2); exit}'
}
while [ "$SECONDS" -lt "$READY_DEADLINE" ]; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    wait "$SERVER_PID" 2>/dev/null || true
    SERVER_PID=""
    echo "错误：演练服务启动失败，请查看上方提示" >&2
    exit 1
  fi
  if ! HEALTH_HEADERS=$(curl -fsS --noproxy '*' --connect-timeout 0.5 --max-time 0.5 \
      -D - -o /dev/null "http://127.0.0.1:$PORT/health" 2>/dev/null \
      | tr -d '\r'); then
    HEALTH_HEADERS=""
  fi
  MARKER=$(printf '%s\n' "$HEALTH_HEADERS" \
    | awk -F ': ' 'tolower($1)=="x-nmu-test-harness" {print $2; exit}')
  HEALTH_INSTANCE=$(printf '%s\n' "$HEALTH_HEADERS" \
    | awk -F ': ' 'tolower($1)=="x-nmu-caregiver-harness-instance" {print $2; exit}')
  HEALTH_TYPE=$(printf '%s\n' "$HEALTH_HEADERS" \
    | awk -F ': ' 'tolower($1)=="content-type" {print tolower($2); exit}')
  [ "$SECONDS" -lt "$READY_DEADLINE" ] || break
  CONSOLE_TYPE=$(page_content_type "http://127.0.0.1:$PORT/console" || true)
  [ "$SECONDS" -lt "$READY_DEADLINE" ] || break
  PATIENT_TYPE=$(page_content_type "http://127.0.0.1:$PORT/patient" || true)
  if [ "$MARKER" = "demo20-profile-tts-ack-v1" ] \
      && [ "$HEALTH_INSTANCE" = "$INSTANCE_MARKER" ] \
      && [ "${HEALTH_TYPE%%;*}" = "application/json" ] \
      && [ "${CONSOLE_TYPE%%;*}" = "text/html" ] \
      && [ "${PATIENT_TYPE%%;*}" = "text/html" ] \
      && [ "$SECONDS" -lt "$READY_DEADLINE" ]; then
    READY=1
    break
  fi
  sleep 0.25
done
if [ "$READY" -ne 1 ]; then
  echo "错误：演练服务在 60 秒内未完成就绪，已停止并清理" >&2
  exit 1
fi

if [ -n "$BROWSER_CHECK" ]; then
  echo "真实 Chrome 验收开始（不显示临时凭据）…"
  env -i "${HARNESS_ENV[@]}" \
    "$BROWSER_PYTHON" -I "$REPO/harness/caregiver_browser_acceptance.py" \
    --start-pause --origin "http://127.0.0.1:$PORT"
  env -i "${HARNESS_ENV[@]}" \
    "$PYTHON" -m harness.caregiver_browser_acceptance --verify-ledger
  echo "真实 Chrome start-pause 本机验收已全部通过"
  exit 0
elif [ -t 0 ] && [ -t 1 ]; then
  echo ""
  echo "照护员本机演练已就绪："
  echo "  工作人员窗口：http://127.0.0.1:$PORT/console"
  echo "  老人窗口：http://127.0.0.1:$PORT/patient"
  echo "  照护员账号：$CAREGIVER_USERNAME"
  echo "  临时口令：$CAREGIVER_PASSWORD"
  echo "  床旁配对 PIN：$CONSOLE_PIN"
  echo "  按 Ctrl+C 退出并清理全部临时数据。"
  echo ""
else
  echo "照护员本机演练服务已就绪（非交互模式不显示临时凭据）"
fi

wait "$SERVER_PID"
SERVER_PID=""
