#!/usr/bin/env bash
# 部署启动脚本(医院内网/科室专机)。云语音(TTS/ASR/判分)为可选增强:
# 配了 DASHSCOPE_API_KEY 自动启用；没配时 TTS 降级本地/浏览器、ASR 转人工，
# 判分仅在有确定规则时本地处理，不把降级状态伪装成云处理成功。
#
# 单机模式(默认):  ./scripts/serve.sh
#   → 127.0.0.1:8000,同机开两窗 /console + /patient(localhost 即 secure context,麦克风可用)
#
# 本机 20 题模拟演示: DEMO20=1 ./scripts/serve.sh
#   → 仅回环地址；显式开启合成数据 + P0a 模拟自动流程，不放开真人训练。
#
# 内网双设备模式:   INTRANET=1 ./scripts/serve.sh
#   → 0.0.0.0:8443 + 自签 TLS。平板浏览器麦克风(getUserMedia)只在 https 下开放,
#     故内网模式必须带证书;首次访问浏览器会警告自签证书,人工信任一次即可。
#     证书只签给本机内网 IP/主机名,私钥留在本机 data/certs/(gitignored),不外发。
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."

usage() {
  cat <<'EOF'
用法：
  ./scripts/serve.sh                 本机双窗口
  DEMO20=1 ./scripts/serve.sh        本机 20 题合成模拟入口
  INTRANET=1 ./scripts/serve.sh      内网双设备技术验证

选项：
  -h, --help                         只显示本说明，不迁移数据库、不启动服务
EOF
}

if [ "$#" -gt 0 ]; then
  case "$1" in
    -h|--help)
      [ "$#" -eq 1 ] || { echo "✗ --help 后不能再带其他参数" >&2; exit 64; }
      usage
      exit 0
      ;;
    *)
      echo "✗ 不支持的参数：$1" >&2
      usage >&2
      exit 64
      ;;
  esac
fi

PY=./.venv/bin/python
[ -x "$PY" ] || { echo "缺 .venv,先: python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt"; exit 1; }

case "${DEMO20:-0}" in
  0|1) ;;
  *) echo "✗ DEMO20 只能是 0 或 1"; exit 1 ;;
esac
if [ "${DEMO20:-0}" = "1" ]; then
  if [ "${INTRANET:-0}" = "1" ]; then
    echo "✗ DEMO20 仅允许本机回环演示，不能与 INTRANET=1 同时使用"
    exit 1
  fi
  export ALLOW_SIMULATION_DATA=1
  export ENABLE_AUTOPILOT_P0A_SIMULATION=1
  echo "✓ 已启用本机 20 题合成模拟演示（非真人、非正式研究）"
fi

# 回环开发脚本本身就是显式的模拟环境入口；公网/内网双设备模式仍默认关闭，
# 必须由部署者在环境中主动设置，避免浏览器请求自行把真实数据降格为模拟。
if [ "${INTRANET:-0}" != "1" ] && [ -z "${ALLOW_SIMULATION_DATA:-}" ]; then
  export ALLOW_SIMULATION_DATA=1
  echo "✓ 回环开发模式：已启用显式模拟数据路径（真实研究门禁仍保持关闭）"
fi

# 1) 数据库迁移到最新(幂等;真机患者数据靠它升级,禁止删库重建)
./.venv/bin/alembic upgrade head

# 2) 前端产物:web/dist 缺失且有 npm 时现场构建(纯静态,构建后运行期不需要 node)
if [ ! -d web/dist ] && command -v npm >/dev/null 2>&1; then
  # 部署构建必须与已审查 lockfile 完全一致；不允许 npm install 在现场改锁。
  (cd web && npm ci --no-audit --no-fund && npm run build)
fi
[ -d web/dist ] || echo "⚠️ 无 web/dist(纯 API 模式);要带界面请先在有 node 的机器上构建后拷入"

# 云语音 Key(可选):环境没带时从 macOS 钥匙串取。录入(终端里输,别贴聊天):
#   security add-generic-password -s nmu-dashscope -a nmu -w '<你的KEY>'
# 必须放在 npm install/build 之后:任意 npm postinstall 脚本都不该读到 Key。
if [ -z "${DASHSCOPE_API_KEY:-}" ] && command -v security >/dev/null 2>&1; then
  DASHSCOPE_API_KEY=$(security find-generic-password -s nmu-dashscope -w 2>/dev/null || true)
  if [ -n "$DASHSCOPE_API_KEY" ]; then
    export DASHSCOPE_API_KEY
    echo "✓ 已从钥匙串加载云语音 Key(小语=云端人声,ASR/判分=云端)"
  fi
fi

# 后台起服务 → 探测真正监听后才报地址(横幅先于就绪打印会诱导过早开浏览器→连接被拒);
# 单机模式就绪后自动开两窗(macOS;NO_OPEN=1 关闭)。Ctrl-C 正常退出并带走服务进程。
# 本机回环探测一律绕过代理:装了 Clash/VPN 的机器,shell 里的 http_proxy 会把
# curl 127.0.0.1 劫持到代理、代理回 502,curl 收到响应即 exit 0,会被误判成"端口已占用"。
probe_local() { curl -sk --noproxy '*' -o /dev/null "$1" 2>/dev/null; }

run_server() { # $1=探测URL $2=就绪后要打印/打开的说明,其余=uvicorn 参数
  local probe="$1" ready_msg="$2"; shift 2
  if probe_local "$probe"; then
    echo "✗ 端口上已有服务在跑(多半是上一个 serve.sh 没关)。"
    echo "  先到旧窗口按 Ctrl-C,或执行: pkill -f app.main:app  再重新启动。"
    exit 1
  fi
  # 路径中含研究、场次和录音标识，不应进入终端/systemd 访问日志。
  # 保留 Uvicorn 启动/错误日志；研究动作由应用内审计账本记录。
  ./.venv/bin/uvicorn --no-access-log "$@" &
  local srv=$!
  trap 'kill "$srv" 2>/dev/null; wait "$srv" 2>/dev/null; exit 0' INT TERM
  for _ in $(seq 1 120); do
    if ! kill -0 "$srv" 2>/dev/null; then
      echo "✗ 服务启动失败(常见:端口被占,查 lsof -i :${probe##*:} 的端口段;或看上方报错)"
      exit 1
    fi
    if probe_local "$probe"; then
      echo "════════════════════════════════════════"
      echo "✓ 服务已就绪"
      printf '%b\n' "$ready_msg"
      echo "  (停止:本窗口按 Ctrl-C)"
      echo "════════════════════════════════════════"
      if [ "${INTRANET:-0}" != "1" ] && [ "${NO_OPEN:-0}" != "1" ] && command -v open >/dev/null 2>&1; then
        open "http://127.0.0.1:8000/console"
        open "http://127.0.0.1:8000/patient"
      fi
      break
    fi
    sleep 0.5
  done
  wait "$srv"
}

if [ "${INTRANET:-0}" = "1" ]; then
  # 3a) 内网双设备:研究者账号 + 床旁配对 PIN + 自签证书 + https。
  # PIN 只签发短时场次 capability，不是 console 管理员凭据。
  if [ -z "${CONSOLE_PIN:-}" ]; then
    CONSOLE_PIN=$("$PY" -c 'import secrets; print(secrets.randbelow(90_000_000) + 10_000_000)')
  fi
  export CONSOLE_PIN
  # 不靠服务启动后的 401 推测坏配置；内网暴露前即检查账号+PIN。
  "$PY" scripts/manage_users.py check-ready
  if [ -t 1 ]; then
    echo "════════════════════════════════════════"
    echo "  老人端配对 PIN: $CONSOLE_PIN"
    echo "  (老人端首次连接时输入;固定 PIN 请用受控环境变量配置)"
    echo "════════════════════════════════════════"
  else
    echo "✓ 老人端配对 PIN 已就绪（非交互日志不显示凭据）"
  fi
  CERT_DIR=data/certs
  mkdir -p "$CERT_DIR"
  HOST_IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo 127.0.0.1)
  if [ ! -f "$CERT_DIR/server.crt" ]; then
    echo "生成自签证书(CN=$HOST_IP,10 年,仅内网用)…"
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
      -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.crt" \
      -subj "/CN=$HOST_IP" -addext "subjectAltName=IP:$HOST_IP,DNS:localhost" >/dev/null 2>&1
  fi
  chmod 700 "$CERT_DIR"
  chmod 600 "$CERT_DIR/server.key" "$CERT_DIR/server.crt"
  run_server "https://127.0.0.1:8443/patient" \
    "  操作电脑: https://$HOST_IP:8443/console\n  平板:     https://$HOST_IP:8443/patient(首次需信任自签证书)" \
    app.main:app --host 0.0.0.0 --port 8443 \
    --ssl-keyfile "$CERT_DIR/server.key" --ssl-certfile "$CERT_DIR/server.crt"
else
  # 3b) 单机双窗:只听本机回环
  run_server "http://127.0.0.1:8000/patient" \
    "  操作端: http://127.0.0.1:8000/console\n  老人端: http://127.0.0.1:8000/patient(已自动打开;NO_OPEN=1 可关)" \
    app.main:app --host 127.0.0.1 --port 8000
fi
