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

if [ "${INTRANET:-0}" = "1" ]; then
  # 3a) 内网双设备:自签证书(仅首次生成)+ https 监听所有网卡
  CERT_DIR=data/certs
  mkdir -p "$CERT_DIR"
  if [ ! -f "$CERT_DIR/server.crt" ]; then
    HOST_IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo 127.0.0.1)
    echo "生成自签证书(CN=$HOST_IP,10 年,仅内网用)…"
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
      -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.crt" \
      -subj "/CN=$HOST_IP" -addext "subjectAltName=IP:$HOST_IP,DNS:localhost" >/dev/null 2>&1
    echo "平板访问: https://$HOST_IP:8443/patient  (首次需信任自签证书)"
  fi
  exec ./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8443 \
    --ssl-keyfile "$CERT_DIR/server.key" --ssl-certfile "$CERT_DIR/server.crt"
else
  # 3b) 单机双窗:只听本机回环
  echo "操作端: http://127.0.0.1:8000/console   老人端: http://127.0.0.1:8000/patient"
  exec ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
fi
