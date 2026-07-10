#!/usr/bin/env bash
# 部署启动脚本(医院内网/科室专机,全程离线,不上云)。
#
# 单机模式(默认):  ./scripts/serve.sh
#   → 127.0.0.1:8000,同机开两窗 /console + /patient(localhost 即 secure context,麦克风可用)
#
# 内网双设备模式:   INTRANET=1 ./scripts/serve.sh
#   → 0.0.0.0:8443 + 自签 TLS。平板浏览器麦克风(getUserMedia)只在 https 下开放,
#     故内网模式必须带证书;首次访问浏览器会警告自签证书,人工信任一次即可。
#     证书只签给本机内网 IP/主机名,私钥留在本机 data/certs/(gitignored),不外发。
set -euo pipefail
cd "$(dirname "$0")/.."

PY=./.venv/bin/python
[ -x "$PY" ] || { echo "缺 .venv,先: python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt"; exit 1; }

# 1) 数据库迁移到最新(幂等;真机患者数据靠它升级,禁止删库重建)
./.venv/bin/alembic upgrade head

# 2) 前端产物:web/dist 缺失且有 npm 时现场构建(纯静态,构建后运行期不需要 node)
if [ ! -d web/dist ] && command -v npm >/dev/null 2>&1; then
  (cd web && npm install --no-audit --no-fund && npm run build)
fi
[ -d web/dist ] || echo "⚠️ 无 web/dist(纯 API 模式);要带界面请先在有 node 的机器上构建后拷入"

# 后台起服务 → 探测真正监听后才报地址(横幅先于就绪打印会诱导过早开浏览器→连接被拒);
# 单机模式就绪后自动开两窗(macOS;NO_OPEN=1 关闭)。Ctrl-C 正常退出并带走服务进程。
run_server() { # $1=探测URL $2=就绪后要打印/打开的说明,其余=uvicorn 参数
  local probe="$1" ready_msg="$2"; shift 2
  if curl -sk -o /dev/null "$probe" 2>/dev/null; then
    echo "✗ 端口上已有服务在跑(多半是上一个 serve.sh 没关)。"
    echo "  先到旧窗口按 Ctrl-C,或执行: pkill -f app.main:app  再重新启动。"
    exit 1
  fi
  ./.venv/bin/uvicorn "$@" &
  local srv=$!
  trap 'kill "$srv" 2>/dev/null; wait "$srv" 2>/dev/null; exit 0' INT TERM
  for _ in $(seq 1 120); do
    if ! kill -0 "$srv" 2>/dev/null; then
      echo "✗ 服务启动失败(常见:端口被占,查 lsof -i :${probe##*:} 的端口段;或看上方报错)"
      exit 1
    fi
    if curl -sk -o /dev/null "$probe" 2>/dev/null; then
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
  # 3a) 内网双设备:PIN 门(病房 WiFi 里任何设备都不得裸操作写接口)+ 自签证书 + https
  if [ -z "${CONSOLE_PIN:-}" ]; then
    CONSOLE_PIN=$(( (RANDOM % 900000) + 100000 ))
  fi
  export CONSOLE_PIN
  echo "════════════════════════════════════════"
  echo "  操作端 PIN: $CONSOLE_PIN"
  echo "  (两端首次写操作时输入;固定 PIN 可用 CONSOLE_PIN=xxxxxx 启动)"
  echo "════════════════════════════════════════"
  CERT_DIR=data/certs
  mkdir -p "$CERT_DIR"
  HOST_IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo 127.0.0.1)
  if [ ! -f "$CERT_DIR/server.crt" ]; then
    echo "生成自签证书(CN=$HOST_IP,10 年,仅内网用)…"
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
      -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.crt" \
      -subj "/CN=$HOST_IP" -addext "subjectAltName=IP:$HOST_IP,DNS:localhost" >/dev/null 2>&1
  fi
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
