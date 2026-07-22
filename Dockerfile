# 多阶段构建：node 编译前端静态产物 → python 运行期(不带 node)。
# 目录布局镜像仓库：/app/web 与 /app/content 并列，仅用于把三份服务端冻结定义
# 纳入确定性构建指纹；Vite public 已禁用，定义文件不会复制进浏览器产物。
# 基础镜像同时保留可读 tag 与 2026-07-19 复核的多架构 manifest digest；
# 安全升级必须显式查询新 digest、构建/回归后再改，不能让浮动 tag 静默换底座。

# ---------- 前端构建 ----------
FROM node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2 AS web
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
COPY content/ /app/content/
RUN npm run build          # tsc + vite build → /app/web/dist(含 build-id.txt)

# ---------- 运行期 ----------
FROM python:3.12-alpine3.24@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# 入口、显式迁移与快照脚本使用 bash 的 pipefail、[[ ]]、进程替换等语义。
# 包版本与 Alpine 3.24 底座一起锁定；升级底座时必须同步重新扫描与回归。
RUN apk add --no-cache bash=5.3.9-r1

# 云端为主：默认只装核心运行依赖(不含 pytest/piper)。要本地神经兜底音色可另装 piper。
COPY requirements-deploy.txt requirements-deploy.lock.txt ./
# requirements-deploy.txt 是人工维护的直接依赖输入；容器只安装经复核的
# 完整传递锁，并让 pip 强制校验每个分发包哈希。升级须重新生成 lock 后回归。
RUN pip install --no-cache-dir --require-hashes -r requirements-deploy.lock.txt \
    && python -m pip uninstall --yes pip

COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY content/ ./content/
# 只带容器运行/管理所需脚本；裸机同步、VPS 拉取等脚本含运维定位信息，
# 不应进入可分发的应用镜像。
COPY scripts/docker-entrypoint.sh scripts/docker-migrate.sh scripts/check_release_contract.py scripts/check_database_head.py scripts/manage_users.py scripts/presynthesize_tts.py scripts/backup.sh scripts/verify_backup_snapshot.py ./scripts/
COPY --from=web /app/web/dist ./web/dist

# 所有持久化数据(SQLite / 录音 / ASR 临时副本 / TTS 缓存 / 导出与备份)
# 都落 /app/data —— compose 只挂载这一个应用数据卷。空命名卷首次挂载会继承
# 镜像中 /app/data 的属主和权限，因此只把该目录交给运行用户。
# 应用代码、迁移、题库/manifest、运维脚本与前端静态资源保持 root 所有；
# 即使应用进程被接管，appuser 也不能改写下一次请求会执行/展示的内容。
RUN addgroup -S -g 10001 appuser \
    && adduser -S -D -H -u 10001 -G appuser -s /sbin/nologin appuser \
    && mkdir -p /app/data \
    && chown appuser:appuser /app/data \
    && chmod 0700 /app/data \
    && chown root:root /app /app/alembic.ini /app/requirements-deploy.txt /app/requirements-deploy.lock.txt \
    && chown -R root:root /app/app /app/alembic /app/content /app/scripts /app/web \
    && chmod 0755 /app \
    && chmod 0644 /app/alembic.ini /app/requirements-deploy.txt /app/requirements-deploy.lock.txt \
    && chmod -R u=rwX,go=rX /app/app /app/alembic /app/content /app/scripts /app/web
USER appuser
EXPOSE 8000
# entrypoint 只做不可变镜像 + DB=head 检查后 exec "$@"，不会迁移。
# 迁移由 Compose maintenance profile 的单次 migrate service 显式执行。
# 单 worker：进程内失败限速器需全局共享,勿加 --workers。
# --proxy-headers 让限速/审计按真实来访 IP 计;信任哪一跳由 FORWARDED_ALLOW_IPS 环境变量给
# (compose 里设为 Caddy 的固定内网 IP)——不写 --forwarded-allow-ips=* (会让 X-Forwarded-For 可伪造、限速失效)。
ENTRYPOINT ["bash", "scripts/docker-entrypoint.sh"]
# URL paths contain research, session, and audio identifiers. Keep Uvicorn's
# error/startup log but disable its raw per-request access log; research actions
# belong in the structured in-app audit ledger instead of Docker stdout.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log"]
