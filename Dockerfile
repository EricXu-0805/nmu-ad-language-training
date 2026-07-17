# 多阶段构建：node 编译前端静态产物 → python 运行期(不带 node)。
# 目录布局镜像仓库：/app/web 与 /app/content 并列，好让 web 的 sync-content(读 ../content)照常工作。
# 生产建议：把下面的浮动镜像标签换成 @sha256 digest 固定,并定期重建以拉安全补丁
#   (取 digest: docker pull python:3.12-slim && docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim)。

# ---------- 前端构建 ----------
FROM node:22-alpine AS web
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
COPY content/ /app/content/
RUN npm run build          # tsc + vite build → /app/web/dist(含 build-id.txt)

# ---------- 运行期 ----------
FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# 云端为主：默认只装核心运行依赖(不含 pytest/piper)。要本地神经兜底音色可另装 piper。
COPY requirements-deploy.txt ./
RUN pip install --no-cache-dir -r requirements-deploy.txt

COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY content/ ./content/
COPY scripts/ ./scripts/
COPY --from=web /app/web/dist ./web/dist

# 所有持久化数据(SQLite / 录音 / TTS 缓存)都落 /app/data —— compose 挂数据卷于此。
# 非 root 运行:承载真实受试者数据,万一 app 出 RCE 也不给容器内 root。app 用户拥有 /app,
# 空的命名卷首次挂载会继承镜像里该目录的属主,故 app 用户能写 /app/data。
RUN mkdir -p /app/data \
    && useradd -u 10001 -r -s /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
# entrypoint = 迁移 + exec "$@";CMD = 默认起服务。管理命令用 docker compose run --rm app <cmd> 覆盖 CMD。
# 单 worker：进程内失败限速器需全局共享,勿加 --workers。
# --proxy-headers 让限速/审计按真实来访 IP 计;信任哪一跳由 FORWARDED_ALLOW_IPS 环境变量给
# (compose 里设为 Caddy 的固定内网 IP)——不写 --forwarded-allow-ips=* (会让 X-Forwarded-For 可伪造、限速失效)。
ENTRYPOINT ["bash", "scripts/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
