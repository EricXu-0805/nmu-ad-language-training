#!/usr/bin/env bash
# 容器入口 shim：先迁移到最新(幂等；真机数据禁止删库重建)，再 exec 传入的命令。
# 正常 up → 跑默认 CMD(uvicorn)。管理命令 → docker compose run --rm app python scripts/manage_users.py …
#   (走 run 而非 exec：首个账号必须能在 app 起来之前建——app 因 fail-closed 无账号时会拒绝启动)。
set -euo pipefail
cd /app

alembic upgrade head

exec "$@"
