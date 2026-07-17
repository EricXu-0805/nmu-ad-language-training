# 部署手册 · 自有 VPS(Docker Compose)

面向：把本平台部署到课题组自有 VPS，长期在线供研究者使用。
定位：**承载于课题组自有 VPS，课题组已同意由该服务器承载本项目数据（含真实受试者数据）。**
数据全部落在 VPS 上的 Docker 数据卷（SQLite + 录音 + TTS 缓存），不外发第三方主机；
云语音/判分仅发送白名单闭集文本（永不携带受试者字段，见 `app/content.py` 守卫）。

架构：`caddy`(对外 80/443，TLS 终止) → 内网转发 → `app`(FastAPI + 前端静态，仅内网 8000)。
`app` 端口**不对宿主/公网开放**，外部只能经 Caddy 的 HTTPS 进来。

---

## 0. 认证模型(先懂再部署)

三层，按环境自动切换：

| 场景 | 触发 | 效果 |
|---|---|---|
| 回环单机开发 | 不设 `REQUIRE_AUTH`/`CONSOLE_PIN`、无账号 | 全开(老人端 localhost 麦克风照常) |
| 公网部署 | `REQUIRE_AUTH=1`(compose 已强制) | 受保护接口须**账号会话**或**共享 PIN** |

- **研究者操作端 = 账号登录**（用户名+密码，绑定审计身份 `display_id` → 谁锁的分/谁评的量表）。
- **老人端平板 = 共享 PIN 保底**（`CONSOLE_PIN`，可选；平板不登录研究者账号）。
- **fail-closed**：`REQUIRE_AUTH=1` 但既无账号又无 PIN → 容器**拒绝启动**（`app` lifespan 断言）。
- **暴力破解防护**：同 IP 连续失败达阈值(默认 8/300s)→ 锁定该 IP 一段时间(默认 300s)。
- **`/patients` 名单读口已纳入保护**（此前无斜杠列表接口曾漏网）。

---

## 1. 一次性：装 Docker

```bash
# Debian/Ubuntu
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # 重登生效
docker compose version            # 确认 compose v2 可用
```

## 2. 取代码 + 配 .env

```bash
git clone <你的私有仓库> nmu && cd nmu/platform   # 仓库须私有
cp .env.example .env
chmod 600 .env
vi .env
```

`.env` 至少改这几项：
- `SITE_ADDRESS=`：有域名填域名(自动证书)；仅 IP 填 `https://你的IP` 并按第 5 节改 Caddyfile。
- `DASHSCOPE_API_KEY=`：开通阿里百炼后填（不填也能跑，云语音降级）。**密钥的两种放法见第 7 节。**
- `CONSOLE_PIN=`（6+ 位数字）：**老人端是独立平板时必设**——平板没有研究者账号 cookie，靠这个 PIN
  认证心跳与录音上传；不设则平板写操作一直 401、前端弹 PIN 也无从放行。console+patient 同机双窗可不设。

`REQUIRE_AUTH=1` 和 `SESSION_COOKIE_SECURE=1` 由 compose 已强制，无需手动。

## 3. 构建镜像 + 建首个账号(顺序很重要)

先构建镜像(不急着 up)：

```bash
docker compose build
```

**必须先建账号，再起 app**。因为 fail-closed：`REQUIRE_AUTH=1` 且无任何账号时 app 会拒绝启动，
届时 `docker compose exec` 也无容器可进。用 `run --rm`(一次性容器，不依赖 app 常驻)建首个账号：

```bash
# 第一个建议给 admin;交互式输入密码(不回显、不进 shell 历史)。run 会先跑迁移建表,再建账号。
docker compose run --rm app python scripts/manage_users.py create 丁老师 --role admin
```

`run --rm` 与 `up` 共用同一数据卷(appdata)，账号即写入正式库。

## 4. 起服务

```bash
docker compose up -d
docker compose logs -f app     # 看到 alembic 迁移完成 + uvicorn 启动即成功
```

> ⚠️ 若日志显示 `RuntimeError: REQUIRE_AUTH=1 但…无任何凭据` —— 说明第 3 步账号没建成。
> 回第 3 步用 `run --rm` 建好账号,再 `docker compose up -d`。

后续账号管理(app 已在跑时 `exec`、没跑时 `run --rm`，两者都行)：

```bash
docker compose exec app python scripts/manage_users.py create 研究生A
docker compose exec app python scripts/manage_users.py list
docker compose exec app python scripts/manage_users.py passwd 研究生A    # 改密(并吊销其会话)
docker compose exec app python scripts/manage_users.py disable 离组研究生  # 停用(即时吊销会话)
```

`--display-id` 可让审计身份(落到锁分/量表)与登录名不同；默认同登录名。

## 5. TLS 两种模式

**A. 有域名(推荐)**：把域名 A 记录指向 VPS，`.env` 里 `SITE_ADDRESS=training.example.com`，
Caddyfile 保持 `tls internal` 注释状态。Caddy 自动申请 Let's Encrypt 证书，浏览器绿锁、无警告。

**B. 仅 IP(暂无域名)**：`.env` 里 `SITE_ADDRESS=https://你的IP`，并取消 Caddyfile 里 `# tls internal` 的注释。
Caddy 用内部 CA 自签证书；浏览器首次访问会警告，人工"继续/信任"一次即可。
> 老人端平板麦克风(getUserMedia)只在 https 安全上下文可用，所以**必须走 TLS**，不能裸 http。
> 后续买了域名，改 `.env` 的 `SITE_ADDRESS`、注释回 `tls internal`、`docker compose up -d` 即可平滑切换。

## 6. 防火墙

```bash
sudo ufw default deny incoming
sudo ufw allow 22/tcp        # SSH
sudo ufw allow 80/tcp        # Caddy(HTTP→HTTPS 跳转 + ACME)
sudo ufw allow 443/tcp       # HTTPS
sudo ufw enable
# 确认 app 8000 没有对外(compose 未 publish;下面应查不到 0.0.0.0:8000)
sudo ss -tlnp | grep 8000 || echo "OK: 8000 未对外"
```

## 7. DASHSCOPE 密钥的放法

云语音/判分的密钥属中等敏感（受白名单守卫，不接触患者数据），Linux VPS 无 macOS 钥匙串，两种放法：
- **简单**：写进 `.env` 的 `DASHSCOPE_API_KEY=`，并 `chmod 600 .env`（仅 root 可读）。够用。
- **更稳**：用 Docker secret / 部署时环境注入，不落盘明文。团队小、机器专用时用第一种即可。

无论哪种：`.env` 已在 `.gitignore`，**绝不提交到仓库**；仓库须私有。

**换 Key(轮换到医院/课题组自己的百炼账号)**：新账号在百炼控制台创建 API Key 后，
改 `.env` 里的 `DASHSCOPE_API_KEY=` 并 `docker compose restart app` 即可，代码零改动。
注意三点：① 新账号的工作空间须开通所用模型（qwen3-tts-flash / qwen3-asr-flash /
qwen-plus；如切龙媛还需 cosyvoice-v2——`cosyvoice-v3-plus` 需单独开通，未开通一律
418）；② TTS 缓存(`data/tts-cache/`)按引擎+参数+文本作键、与 Key 无关，换 Key 后
已合成话术不重新计费；③ 换 Key 后建议先跑 `scripts/presynthesize_tts.py --dry-run`
再实跑一遍，确认新空间模型可用、增量话术打满。

## 8. 备份(重要——真机数据无第二份就没有回滚)

所有数据在 `appdata` 卷（SQLite `app.db` + 录音 + TTS 缓存）。定期备份：

```bash
# 冷备(停一下最稳,SQLite 一致)；或用 sqlite3 .backup 热备
docker compose exec app sh -c 'sqlite3 /app/data/app.db ".backup /app/data/backup-$(date +%F).db"'
docker compose cp app:/app/data/backup-"$(date +%F)".db ./backup-"$(date +%F)".db
# 录音目录一并拉走
docker compose cp app:/app/data/audio ./audio-backup-"$(date +%F)"
```

建议挂个 cron 每日备份并异地留存一份。恢复：把备份文件放回卷、`docker compose restart app`。

## 9. 升级 / 改配置

```bash
git pull
docker compose up -d --build     # 自动跑 alembic 迁移(只向前),不动既有数据
```

## 10. 数据库选型

- **默认 SQLite**（数据卷内）：小团队、单机、并发低 —— 完全够用，零额外运维。
- **要 Postgres**：`.env` 设 `DATABASE_URL=postgresql+psycopg://user:pass@db:5432/nmu`，
  在 compose 加一个 `db: image: postgres:16` 服务 + 卷，`app` 加 `depends_on: [db]`。
  代码已兼容(见 `app/db.py`：SQLite 专用参数不会误传 Postgres)。迁移同样 `alembic upgrade head`。

## 11. 运维注意

- **单 worker**：失败限速器在进程内。别给 uvicorn 加 `--workers>1`（会各自计数、削弱限速）；
  真要多进程，改用共享限速(Redis)或前置 Caddy 层限速。
- **会话有效期 12h**；过期后前端自动退回登录页。改密/停用会**立即吊销**该用户所有会话。
- **代理头 / 防伪造**：限速与审计按真实来访 IP 计,该 IP 不可被客户端伪造,靠两道:
  Caddy 用 `header_up X-Forwarded-For {remote_host}` 覆写掉客户端自带的 XFF;app 只信任 Caddy 的固定
  内网 IP(`FORWARDED_ALLOW_IPS=172.28.0.10`,compose 已设)。切勿把它改成 `*`。
  若日后在本机前再加一层 CDN/反代,需相应调整信任的那一跳,否则来访 IP 会被记成上游而非真实客户端。

---

## 部署自检清单

- [ ] 仓库私有；`.env` 已 `chmod 600` 且未入库
- [ ] `REQUIRE_AUTH=1`（compose 默认）；已建至少一个 admin 账号
- [ ] `SITE_ADDRESS` 正确；HTTPS 能打开(域名绿锁 / IP 自签已信任)
- [ ] `ss -tlnp` 确认 8000 不对外，只有 80/443
- [ ] ufw 已启用，仅放行 22/80/443
- [ ] 老人端平板 https 下麦克风可用（真机验收）
- [ ] 备份任务已配置并验证过一次恢复
- [ ] （合规）确认由课题组自有 VPS 承载本项目数据已获课题组同意；导出走去标识化通道
