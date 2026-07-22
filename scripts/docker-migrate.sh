#!/usr/bin/env bash
# 具名维护员显式调用的单次向前迁移入口。常规 app 启动不调用本脚本。
set -euo pipefail
cd /app

if [ "$#" -ne 0 ]; then
  echo "REJECTED code=migration_arguments_forbidden" >&2
  exit 64
fi

python -I scripts/check_release_contract.py
alembic upgrade head
python -I scripts/check_database_head.py
