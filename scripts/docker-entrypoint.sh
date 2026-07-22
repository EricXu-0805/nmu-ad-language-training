#!/usr/bin/env bash
# 容器常规入口：只校验不可变镜像合同和 DB=head，绝不自动改 schema。
# 向前迁移只能由 Compose maintenance profile 的 migrate service 显式执行；
# 因此启动失败不会在唯一 live 卷上留下无法靠旧镜像回退的隐式迁移。
set -euo pipefail
cd /app

python -I scripts/check_release_contract.py
python -I scripts/check_database_head.py

exec "$@"
