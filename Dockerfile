# 多阶段构建：node 编译前端静态产物 → python 运行期(不带 node)。
# 目录布局镜像仓库：/app/web 与 /app/content 并列，仅用于把三份服务端冻结定义
# 纳入确定性构建指纹；Vite public 已禁用，定义文件不会复制进浏览器产物。
# 基础镜像同时保留可读 tag 与 2026-11-20（UTC）复核的多架构 manifest digest
# （node 22.23.5、python 3.12.16，均为 Alpine 3.24.4）；
# 安全升级必须显式查询新 digest、构建/回归后再改，不能让浮动 tag 静默换底座。

# ---------- 前端构建 ----------
FROM node:22-alpine@sha256:1314fb30ec37592d5845e5a160d522c0e680972aa534549940b8d73bde88481a AS web
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
COPY content/ /app/content/
RUN npm run build          # tsc + vite build → /app/web/dist(含 build-id.txt)

# ---------- 运行期 ----------
FROM python:3.12-alpine3.24@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89 AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# 入口、显式迁移与快照脚本使用 bash 的 pipefail、[[ ]]、进程替换等语义。
# 下面每项都是安全下限（>=），不是精确钉版：Alpine 仓库每个包只留最新一版，精确钉版
# 每逢上游出新修订就失效、让 apk 选包失败（2026-09-30、11-06、11-18 三次）。
# 下限让 apk 补到仓库现行版本，仓库低于下限则构建失败；底座追上后这些项是空操作，但仍然
# 留着，换底座 digest 时它们是硬下限。底座已满足下限的包不会再升级，之后的新修复靠 CI
# image 任务的扫描发现，再抬下限。每项都要加引号：裸写的 > 是 shell 重定向，下限会被静默丢掉。
# 下限出处（https://secdb.alpinelinux.org/v3.24/main.json）：
#   libcrypto3/libssl3 3.5.9-r1：CVE-2026-14456（3.5.8-r0 起修），
#     CVE-2026-88822、-88823、-95691、-95692、-107496（3.5.9-r1 修）。
#   libuuid 2.42.3-r1：util-linux 七项 HIGH（r1 补上 CVE-2026-78408）。不豁免底座旧包。
#   zlib 1.4-r0：CVE-2026-85091（gz_vacate 堆溢出，HIGH）、CVE-2026-4738；so:libz.so.1 不变。
#   musl/musl-utils 1.2.6-r3：CVE-2026-107891。
#   krb5-libs 1.22.3-r0：11-12 镜像扫描在底座 1.22.2-r1 上命中的 Kerberos HIGH。
#   bash 5.3.9-r1：SEC-30 复扫通过时的版本。
# 抬下限时同步改 tests/test_docker_runtime_permissions.py 的下限表、DEPLOY.md 12.1 与 sbom.cdx.json。
RUN apk add --no-cache 'bash>=5.3.9-r1' 'libcrypto3>=3.5.9-r1' 'libssl3>=3.5.9-r1' 'libuuid>=2.42.3-r1' 'zlib>=1.4-r0' 'musl>=1.2.6-r3' 'musl-utils>=1.2.6-r3' 'krb5-libs>=1.22.3-r0'

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
