# 部署手册 · 自有 VPS(Docker Compose)

面向：把本平台部署到课题组自有 VPS，供研究者进行开发、模拟预演和受控技术验证。

> **部署完成不等于可以处理正式受试者数据。** 当前版本尚未通过伦理、临床内容、云数据处理、目标设备和逐次研究留痕门禁；在这些条件留证通过前，服务器只能按开发/模拟环境管理。

主数据库、录音原件、导出包和 TTS 缓存持久化在课题组控制的 Docker 数据卷中；但配置百炼 API Key 后，后端会向第三方云服务发送不同类别的数据：

| 云能力 | 会发送的数据 | 当前保护边界 |
|---|---|---|
| TTS | 题库、脚本和固定 UI 白名单中的话术文本 | 白名单外文本 fail-closed，不调用云 TTS；固定研究话术本身仍会离开服务器 |
| ASR | **受试者原始回答音频** + 题库目标词等识别上下文 | 请求不附受试者编号/画像，但声纹与回答内容本身是敏感受试者数据 |
| LLM 初评 | 题目类型、目标/可接受表达等判分上下文 + **受试者回答文本** | JudgeInput 禁止画像和直接受试者字段；云结果只作实时运营决策/初评，不是研究真值 |

浏览器端只访问平台同源接口；上述外发发生在 FastAPI 后端。云 ASR/LLM 已经过按受试者保存的 provider、告知版本、明确允许与撤销时间门禁，且每次外呼前重读；未同意或已撤销时 fail-closed。这个技术门禁不代表已获得伦理许可：正式研究前仍必须由伦理批件和同意材料覆盖云 ASR 声纹、云 LLM 回答文本和云 TTS 固定话术，并核清供应商的数据区域、日志、留存、二次使用和删除条款。

架构：新装机可用 Compose `caddy`(对外 80/443，TLS 终止) → 内网转发 → `app`(FastAPI + 前端静态，仅内网 8000)。已有宿主 Caddy 的机器按第 9 节走 loopback 蓝绿覆盖文件，不启动 Compose Caddy。
默认 Compose 的 `app` 端口**不对宿主/公网开放**；宿主 Caddy 蓝绿模式只绑定 `127.0.0.1`，外部仍只能经 Caddy 的 HTTPS 进来。

---

## 0. 认证模型(先懂再部署)

三层，按环境自动切换：

| 场景 | 触发 | 效果 |
|---|---|---|
| 回环单机开发 | 不设 `REQUIRE_AUTH`/`CONSOLE_PIN`、无账号 | 全开(老人端 localhost 麦克风照常) |
| 公网部署 | `REQUIRE_AUTH=1`(compose 已强制) | 研究者接口用**账号会话**；老人端用 PIN 当面配对后获取**短时场次 capability** |

- **研究者操作端 = 账号登录**（用户名+密码，绑定审计身份 `display_id` → 谁锁定研究评分、谁完成现场记录；历史自由量表行不因此获得正式身份背书）。
- **老人端 = PIN 配对后的短时 capability**（`CONSOLE_PIN` 必设；老人端从不登录或携带研究者账号 cookie）。
- **fail-closed**：受保护部署缺研究者账号或缺 `CONSOLE_PIN` 都会拒绝启动。同机双窗也必须配对，不会把 console cookie 静默升级给 patient。
- **暴力破解防护**：同 IP 连续失败达阈值(默认 8/300s)→ 锁定该 IP 一段时间(默认 300s)；login 与床旁 pair 另有分域的全局短锁，换 IP 也不能无限占用 PBKDF2/PIN 校验。失败键采用有界 LRU，PBKDF2 默认最多 2 个并发。
- **`/patients` 名单读口已纳入保护**（此前无斜杠列表接口曾漏网）。

本版的账号边界是**单部署 = 单个获批课题组工作区**。受试者 roster 和只读的 patient-level legacy 量表迁移记录对该课题组的具名 `researcher` / `data_steward` / `admin` 共享；旧自由量表写入口已永久关闭，历史行中的 `assessor_id` 只是未验证的来源字段，不是服务器签发的正式施测证据、受试者所有权或访问 ACL。场次回答、录音和运行证据仍按 `trainer_id` 向场次负责 researcher 隔离，admin 的跨场监督必须留审计，data steward 仅按终态治理口径读场次。账号只能发给该获批课题组中确有 need-to-know 的人员；在 study/site/patient assignment ACL 完成迁移与验收前，多课题或多站点必须分库、分账号、分部署，不得共用本工作区。

认证 fail-closed 只解决“谁能访问平台”，不代替“这名受试者是否同意云处理”。平台已有独立的 provider/告知版本/允许/撤销门禁，每次云 ASR/LLM 外呼前都必须重读；缺失或撤销时 fail-closed。账号、PIN 或录音授权不能代替这一独立同意。

---

## 1. 一次性：在独立主机维护窗口安装 Docker

Docker 安装是宿主机基础设施变更，不是应用发布脚本的一部分。对已有
nginx/Caddy 或其他业务的主机，必须先取得云控制台/救援访问、完整快照与
回退窗口，再单独安装；不与应用 schema 迁移或生产切换同窗执行。

生产不使用 `get.docker.com` convenience script，也不把日常账号加入
`docker` 组（该组可等价获得宿主 root 能力）。Ubuntu 22.04/24.04 按 Docker 官方
签名 APT 源安装，由具名管理员用 `sudo docker ...` 操作：

```bash
sudo apt-get update
sudo apt-get install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt-get update
apt-cache madison docker-ce
```

从上一步输出选定已审查版本，把 Docker CE/CLI、containerd、buildx 和
Compose plugin 的精确包版本记入变更单后再安装；不在手册中写死会过期的
版本号，也不默认追随 `latest`。安装后开新 SSH 连接验证：

```bash
sudo docker version
sudo docker compose version
sudo systemctl is-active docker containerd
```

Docker 发布端口时会创建宿主 iptables 规则，单纯配置 UFW 不足以证明容器
端口被拦截。本平台 Compose 只允许 Caddy 发布 80/443，app 8000 不得 publish；
安装后必须同时复核 `DOCKER-USER` 链、云安全组、`docker compose config --quiet`
和 `ss -tlnp`，确认没有旁路公网端口。已有主机 nginx/Caddy 占用 80/443 时，
不得直接 `up`；先在不发布宿主端口的候选环境完成恢复/迁移/健康验证，
再在切换窗口中停旧反代并启动候选 Caddy。

## 2. 取代码 + 配 .env

```bash
git clone <你的私有仓库> nmu && cd nmu/platform   # 仓库须私有
cp .env.example .env
chmod 600 .env
vi .env
```

`.env` 至少改这几项：
- `APP_IMAGE=`：只接受已审查镜像的完整 `registry/name@sha256:<64 位小写 hex>`。只写 `latest` 或版本 tag 会在 Uvicorn 监听前 fail-closed。
- `APPDATA_VOLUME=`：已由具名运维员创建并验收的应用数据卷精确名。升级时它指向“从已验证快照恢复的候选卷”，不是原 live 卷。
- `CADDY_DATA_VOLUME=` + `CADDY_CONFIG_VOLUME=`：已预建的 Caddy 状态卷精确名；特别是使用内部 CA 时不得丢失原 `caddydata`。
- `APP_ENV_FILE=.env`：应用容器要读取的权限 `0600` 配置文件。候选卷预演时用独立 `.env.candidate`，不覆盖当前运行容器的环境。
- `SITE_ADDRESS=`：有域名填域名(自动证书)；仅 IP 填 `https://你的IP` 并按第 5 节改 Caddyfile。
- `TRUSTED_HOSTS=`：填与浏览器访问一致的精确域名或 IP（不含 `https://`、端口或通配符）；Compose 缺失时直接拒绝启动。
- `DASHSCOPE_API_KEY=`：不填仍可运行基础管理、受监督模拟和本地降级能力，但云 ASR/LLM 不可用，不能据此启用全自动干预。**密钥的两种放法见第 7 节。**
- `PROVIDER_READINESS_FINGERPRINT_KEY=`：配置云 Key 时必设的独立随机秘密（至少 32 bytes，不得复用 API Key/PIN/密码）。平台用它在内存中生成凭据世代 HMAC，再只持久化整体配置指纹；轮换任一秘密都会让旧检查立即失效。
- `PROVIDER_READINESS_TTL_MINUTES=30`：AI 服务合成检查的有效期（5..1440 分钟）。过期或配置指纹变化后，自动干预启动 fail-closed。
- `AI_QUALITY_RESEARCH_MIN_SUBJECTS=`：真实研究 AI 质量 overall 的最小不同受试者数，只接受 2..100。必须由 PI/隐私负责人按研究与发布风险批准，不要照抄示例值。缺失或无效时立即抑制；当前即使值有效，在持久化冻结 cohort/release epoch 与逐单元互补抑制实现前，服务端仍会在读取场次/逐次证据前整行抑制，不公开人数、覆盖、运行或真值计数。该门槛不适用于模拟分区，也不是伦理或统计放行线。质量接口的行数、文本字节、待核音频总量、单文件流式哈希和模拟请求频率均使用代码内固定安全预算，部署时不得通过环境变量临时放宽。模拟投影必须在支持的 PostgreSQL/SQLite 上从第一条 SELECT 开始持有稳定只读快照，九表加载后对实际对象重做总预算，并必须在目录扫描/音频哈希前解绑对象、结束数据库事务；未知数据库后端或无法建立快照时必须整体 503，不返回部分结果。Caddy/CDN/上游代理不得缓存该接口。
- `CONSOLE_PIN=`（6–32 位 ASCII 数字）：**所有受保护部署必设，包括 console+patient 同机双窗**。PIN 只用于当面配对；成功后老人端使用库中仅保存哈希的短时、绑定单场次 capability 认证心跳、呈现和录音上传。
- `DEIDENTIFICATION_KEY=` + `DEIDENTIFICATION_KEY_ID=`：开放去标识导出前必设。前者是独立随机密钥（至少
  32 bytes），后者是非机密轮换标识（例如 `nmu-2026-01`）。任一缺失/无效时导出 fail-closed，不会退回无盐哈希。

`REQUIRE_AUTH=1` 和 `SESSION_COOKIE_SECURE=1` 由 compose 已强制，无需手动。

## 3. 取得不可变镜像 + 显式迁移 + 建首个账号

生产 Compose 的项目名固定为 `nmu-platform`，不再根据检出目录派生；生产命令
禁止传 `-p` 或设置 `COMPOSE_PROJECT_NAME`，否则 Compose 仍会覆盖文件内身份，必须
立即中止而不是创建一套空项目。`app` 没有 `build:` 路径，服务器不得从
dirty tree 现场构建。先从受控构建机产出一次镜像，
保存 commit/tag、镜像 digest、SBOM、扫描和签名证据，再在 VPS 拉取或导入同一构件。

新装时必须先以 `.env` 中的精确名称预建三个外部卷。下面是
`.env.example` 的非生产示例名；已有服务器绝不能先创建同名空卷，而必须先盘点
现存 Compose project 和真实卷名，再把合同显式指向它们：

```bash
docker volume create nmu-platform-appdata-initial
docker volume create nmu-platform-caddydata
docker volume create nmu-platform-caddyconfig

# 只做语法/插值/外部卷合同校验，不把 env_file 密钥展开到终端或 CI 日志。
docker compose config --quiet
# APP_IMAGE 和 Caddy 都是 digest 引用；也可以先用受控 OCI 包 docker load。
docker compose pull app caddy
```

发布构件有三层固定边界：Node、Python、Caddy 基础镜像均按 OCI digest 固定；前端使用 `npm ci` 和仓库 lockfile；Python 只从 `requirements-deploy.lock.txt` 以 `--require-hashes` 安装完整传递依赖。不要在服务器上手工补包或改 lock。依赖升级必须在受控分支重新生成 lock、审查版本/哈希差异、完成全量回归后再替换固定构件。

前端构建会同时生成 `build-provenance.json` 与 `browser-dist-sha256.json`。前者分开记录 lock 声明的 Vite/TypeScript/React 插件版本与本次实际解析到的安装版本（任一不一致立即中止构建），并记录实际 Node/V8/平台/架构和 Dockerfile/Compose 声明的三层固定镜像；后者按字节排序覆盖最终 `dist/` 的每个普通文件，并明确只排除清单自身以避免递归悖论。`build-fingerprint.sha256` **只标识代码中明确枚举的输入字节**，不等于跨环境可复现证明、基础镜像证明、依赖漏洞扫描、SBOM、签名或供应链证明。正式发布构建必须在固定 Node 22 builder 内重建，确认 provenance 的 `node_major_matches_declared_release_builder=true`，逐文件复核 dist 清单，并另外保存镜像 digest、SBOM/扫描和签名证据；本机 Node 不匹配时的绿色构建只能作为开发回归。

默认容器不复制工作区 `data/`，也不安装可选 Piper。若部署本地 Piper 模型，必须把模型与固定版本 `piper-tts` 作为同一个受审查镜像变更安装并完成实际合成验收；不能只挂模型、也不能只装包。未做该变更时系统应明确降级到已配置云 TTS 或浏览器语音，不得把缺包伪装成已具备本地音色。

默认镜像不含 Piper 也是许可门禁。当前离线审计到的 `piper-tts 1.4.2` 包元数据为 `GPL-3.0-or-later`，而音色模型需逐个核对独立来源和许可。任何含 Piper/模型的镜像或离线安装包在交付前，必须形成依赖/模型许可清单、使用与再分发权证据、许可文本和适用的 GPL 合规交付方案；否则必须继续同时排除包与模型。

构建完成后还必须在有 Docker daemon 的受控机器实际启动并验证健康状态、只读根文件系统和唯一可写目录 `/app/data`。本仓库的静态检查或空库测试不能替代这一步。

常规 app 入口只检查镜像引用不可变且当前数据库精确位于镜像的唯一
Alembic head，**绝不自动迁移**。新卷或候选卷只能在备份/恢复门禁通过后，
由具名运维员显式执行一次 maintenance service：

```bash
docker compose config --quiet
docker compose --profile maintenance run --rm --no-deps migrate
```

`migrate` 只执行 `alembic upgrade head`，然后再读回 DB=head；任何额外参数都会被拒绝。
这不代替候选卷的恢复验证、数量/checksum 核对和回退演练。

**必须先建账号，再起 app**。因为 fail-closed：`REQUIRE_AUTH=1` 且无任何账号时 app 会拒绝启动，
届时 `docker compose exec` 也无容器可进。用 `run --rm`(一次性容器，不依赖 app 常驻)建首个账号：

```bash
# 第一个建议给 admin；交互式输入密码(不回显、不进 shell 历史)。
# app 入口只会验证 DB=head，不会暗中改 schema。
docker compose run --rm --no-deps app python scripts/manage_users.py create study-admin --role admin
```

`run --rm` 与 `up` 共用同一数据卷(appdata)，账号即写入正式库。

## 4. 起服务

```bash
docker compose config --quiet
docker compose up -d --no-build
docker compose logs -f app     # 看到不可变镜像 + DB=head 检查通过，然后 uvicorn 启动
docker compose ps              # app 与 caddy 都应为 running/healthy
```

> ⚠️ 若日志显示 `RuntimeError: REQUIRE_AUTH=1 但…无任何凭据` —— 说明第 3 步账号没建成。
> 回第 3 步用 `run --rm --no-deps` 建好账号,再 `docker compose up -d --no-build`。

后续账号管理(app 已在跑时 `exec`、没跑时 `run --rm`，两者都行)：

```bash
docker compose exec app python scripts/manage_users.py create researcher-a
docker compose exec app python scripts/manage_users.py list
docker compose exec app python scripts/manage_users.py passwd researcher-a   # 改密(并吊销其会话)
docker compose exec app python scripts/manage_users.py disable former-member # 停用(即时吊销会话)
```

`--display-id` 可让服务器签发的审计身份（例如研究评分锁定与现场收尾）与登录名不同；默认同登录名。它不会追认历史自由量表行中的 `assessor_id`。

## 5. TLS 两种模式

**A. 有域名(推荐)**：把域名 A 记录指向 VPS，`.env` 里 `SITE_ADDRESS=training.example.com`，
Caddyfile 保持 `tls internal` 注释状态。Caddy 自动申请 Let's Encrypt 证书，浏览器绿锁、无警告。

**B. 仅 IP(暂无域名)**：`.env` 里 `SITE_ADDRESS=https://你的IP`，并取消 Caddyfile 里 `# tls internal` 的注释。
Caddy 用内部 CA 自签证书；浏览器首次访问会警告，人工"继续/信任"一次即可。
> 老人端平板麦克风(getUserMedia)只在 https 安全上下文可用，所以**必须走 TLS**，不能裸 http。
> 后续买了域名，改 `.env` 的 `SITE_ADDRESS`、注释回 `tls internal`，先运行 `docker compose config --quiet`，再运行 `docker compose up -d --no-build` 切换。

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

## 7. DASHSCOPE 密钥与云数据开关

`DASHSCOPE_API_KEY` 是可调用付费模型并触达研究数据处理链的**高敏感凭据**。TTS 白名单不能保护 ASR 音频或 LLM 回答文本；一旦泄露，既有费用风险，也有被滥用模拟本项目请求的风险。Linux VPS 无 macOS 钥匙串，两种放法：
- **最低要求**：写进 `.env` 的 `DASHSCOPE_API_KEY=`，并 `chmod 600 .env`（仅部署账号可读）。
- **推荐**：用 Docker secret / 部署系统的密钥注入，不落仓库和普通备份明文；生产与开发使用不同 Key，按课题组账号和最小权限管理。

无论哪种：`.env` 已在 `.gitignore`，**绝不提交到仓库、聊天、工单或截图**；仓库须私有。定期轮换，并在人员离组、疑似泄露或供应商策略变化时立即吊销旧 Key。

引擎默认 `auto`，所以“写入 Key”同时改变数据流：

- TTS：有 Key 时优先百炼 Qwen TTS；无 Key/失败时回退本地 Piper，再回退浏览器系统语音。
- ASR：有 Key 时默认把回答音频交百炼 qwen3-asr；无 Key/失败时明确降级为人工转写。
- LLM：有 Key 时默认调用 qwen-plus 产生 AI 初评；无 Key/失败时回退规则判定，开放式环节仍需人工复核。

若某次验证不允许任何研究内容出网，不要只删除 Key 后凭感觉判断；应显式设置本地/关闭引擎、重启服务，并在断网环境检查实际行为。浏览器系统语音是否联网取决于操作系统和已安装语音包，也须真机验证。

**换 Key(轮换到医院/课题组自己的百炼账号)**：新账号在百炼控制台创建 API Key 后，
改 `.env` 里的 `DASHSCOPE_API_KEY=`，先执行 `docker compose config --quiet`，再执行 `docker compose restart app`；只换 API Key 已会让旧检查立即失效。`PROVIDER_READINESS_FINGERPRINT_KEY=` 应按独立的密钥轮换制度管理，疑似泄露时必须同步轮换，但不要求每次 API Key 例行轮换都更换它。重启只让进程重新解析配置，不等于新 Key 有效、模型已开通或三类链路通过，仍须由管理员重新执行合成检查。
注意三点：① 新账号的工作空间须开通所用模型（qwen3-tts-flash / qwen3-asr-flash /
qwen-plus；如切龙媛还需 cosyvoice-v2——`cosyvoice-v3-plus` 需单独开通，未开通一律
418）；② TTS 缓存(`data/tts-cache/`)按引擎+参数+文本作键、与 Key 无关，换 Key 后
已合成话术不重新计费；③ `scripts/presynthesize_tts.py --dry-run` 只能检查 TTS 白名单与缓存，不能替代下面的三能力合成检查。

### 7.1 启用 AI 自动干预前的强制合成检查

1. 用具名 `admin` 账号登录操作台，进入模拟场次的“服务器 AI 自动干预”面板。
2. 点击“执行 AI 服务合成检查”。服务器只使用固定白名单话术“您好”：实际 TTS 合成后把内存中音频交给当前 ASR，并用固定无画像 `JudgeInput` 验证已配置 LLM 的结构返回。
3. 检查过程**不读取 Patient/Turn/Audio，不保存合成音频、识别文字、固定输入或 Key**。账本只保存不含 Key 的配置指纹、引擎/模型版本、各能力成败码、时间/过期时间和管理员审计身份。
4. 当前 P0a 运行合同必需 TTS+ASR。LLM 有确定式规则/rubric 降级，因此明确标为“非必需”；已配置 LLM 仍会实测，失败时不得显示为“已配置能力全部就绪”。
5. 其他账号可只读 `GET /ai/provider-readiness`；只有 admin 可 `POST /ai/provider-readiness/probe`。缺失、过期、配置不匹配或必需能力失败时，`POST /sessions/{session_id}/autopilot/start` 在取得控制权前返回结构化 409。
   合成检查是付费高成本操作，应用内每个管理员/IP 限制为最多每分钟一次；反向代理仍应设置第二层配额。

当前 `autopilot/start` 所能进入的唯一服务范围是 **P0a 模拟切片**。正式研究级 autopilot scope 尚未实现，这是代码内部阻断项，不是部署开关；不得删除 simulation guard、修改受试者分类或放宽启动门禁来“启用正式研究”。
受控模拟环境必须同时显式设置 `ALLOW_SIMULATION_DATA=1` 与 `ENABLE_AUTOPILOT_P0A_SIMULATION=1`；后者默认关闭。两者都不会绕过 VisitPlan、整计划协议、provider readiness 或设备 capability。当前默认内容仍有缺口，因此按整计划启动时会在接管前 fail-closed；不要用这两个开关解释为当前自动驾驶已经可运行或正式研究可放行。

### 7.2 去标识导出 HMAC 密钥

用与账号、PIN 和百炼 Key 无关的随机值，例如在受控终端执行 `openssl rand -base64 32`，把结果仅写入权限为
`0600` 的 `.env`/secret store，绝不写入代码、日志、聊天或导出包。`DEIDENTIFICATION_KEY_ID` 不是秘密，用于识别轮换世代。

导出对受试者、场次、音频和历史批次采用不同分域的版本化 HMAC-SHA256 token；新批次号是不带日期的随机值。自由文本默认删除，精确采集/处理时间不进包（只保留序号、时长等相对证据），所有 CSV 字符串单元格经公式注入防护。导出 API 强制高熵幂等键并只持久化摘要；`ExportBatch`/`ExportArtifact` 账本、相对路径、文件 SHA-256 和最终 manifest 是发布真源。manifest 之前的文件写入由可续租数据库租约与 `.staging-receipt.json` 双重定界；租约未过期不得清理，过期后也必须核对上一 owner 才能重建。claim 与 manifest 意图持久化都必须重验当前受试者同意/撤回和每条音频隔离状态。已发布批次在后续撤回后必须对 GET 和 checksum 失效；不在请求路径中自动删除不可变账本或文件，必须由 PI 批准的撤回/销毁/已交付副本 SOP 另行处置。提交结果不确定时不得删除可能已经发布的文件，必须使用同一幂等键恢复 `artifacts_ready` 批次。
**HMAC 文件名不会消除声纹与口语内容：`controlled-audio-exports` 始终是原始敏感音频的受控副本，不属于去标识分析包。** 它与 CSV 物理分目录、按原始研究数据权限和备份策略管理。
转动密钥会改变后续 token：转动前应先完成待处理批次的受控副本 checksum，并在受控密钥管理中保留旧世代，否则历史音频删除闸门会安全地拒绝校验。

## 8. 备份(重要——真机数据无第二份就没有回滚)

**Compose 发布门禁：当前 `vps-backup-daily.sh` 是 2026-07-17 裸机拓扑的历史实现，
它默认读 `/opt/nmu/app/data`、宿主 `.venv` 和外部 systemd 单元，不会自动发现
Compose 的 external `APPDATA_VOLUME`。在 Docker-aware 定时任务、单元文件和实际恢复
链经版本化、审查并在目标机演练前，不得把它安装成 Compose 的自动备份，
也不得把宿主遗留的 `data/app.db` 当作当前容器数据。**

**2026-07-17 留存的裸机运维记录（本轮未连接服务器，必须现场复核，不能作为当前 build 或 Compose 卷的备份验收证据）：**

- **VPS 端**：systemd `nmu-backup.timer` 每日 03:30（上海）调用
  `scripts/vps-backup-daily.sh`。脚本先在隐藏 staging 中生成 SQLite 在线快照，复制已完成录音、导出与六份重建配置（`.env`、Caddyfile、应用/Caddy/备份 service 和备份 timer），再执行完整 manifest、SQLite `integrity_check`、外键检查、当前 Alembic head/恢复表列合同及 DB↔录音双向语义闭包。复制前按 SQLite 逻辑页、录音、两类导出与配置估算源逻辑字节、文件数和目录数，并加入文件/目录 metadata、至少 64MiB/10% 增长余量和 `NMU_BACKUP_RESERVE_BYTES` 硬保留空间（默认 512MiB）；源文件最多 99999 个、目录最多 10000 个，可分别用 `NMU_BACKUP_SOURCE_FILE_LIMIT` / `NMU_BACKUP_SOURCE_DIRECTORY_LIMIT` 向下收紧，不能调高 current snapshot 合同。空间不足、文件/目录超限或源目录不可扫描时 fail-closed。所有检查和落盘同步成功后才同文件系统原子发布 `/opt/nmu/backups/daily/<时间戳>/`；任何一步失败都删除未完成 payload，只在权限 `0600` 的 `backup.log` 保留固定原因码且不得写 `ok`。VPS 只保留最近 14 份。解释器按显式 `PYTHON_BIN` → `/opt/nmu/app/.venv/bin/python` → `python3` 解析，并由同一个解释器完成预检、快照和发布；systemd 单元应显式设置 `Environment=PYTHON_BIN=/opt/nmu/app/.venv/bin/python`，且该环境须包含部署锁文件声明的 SQLAlchemy 等运行依赖。**运维真相看 `/opt/nmu/backups/backup.log` 审计行**，不要只看 systemd 状态。
- **异地副本**：受控运维工作站定时调用 `scripts/vps-backup-pull.sh`。每份远端快照先进入私有 `.incoming`，用同一 current-only VPS 合同验证后原子发布；同名重放只有完整树逐字节一致才接受，差异进入 `conflicts/`，旧 Alembic head 进入 `legacy-unvalidated/`。传输前会聚合验证单快照/本轮的文件数、目录数与字节数并校验本地剩余空间；随后只先拉取固定名称且最大 64MiB 的 manifest，由严格解析器生成权限 `0600`、NUL 分隔的临时 allowlist，第二次 rsync 只接收 manifest 列出的精确文件路径。接收后先重新取得同一远端快照的文件/字节/目录/非法类型事实，必须与 preflight 完全一致，再核对本地接收事实并验证内容；临时 allowlist 无论成败都删除。任何未成为清单文件父级的空目录（包括 orphan/staging/classification）都按 current-only 合同拒绝并隔离，不能在拉取时静默净化；真正的历史版本必须由运维员按 8.1 SOP 做明确的 `legacy-unvalidated/` handoff，不得靠重命名伪装为当前合格快照。默认上限可通过六个 `NMU_BACKUP_MAX_*` 环境变量向下收紧或按经批准容量调整。该白名单会阻断探测后注入的未列名文件/目录洪泛，但受损远端仍可能在探测与传输之间放大已列名文件，所以异地根目录必须位于**独立、加密且有硬 quota/限容的卷**，代码门不能替代宿主配额。脚本不会根据可能已受损的远端目录名自动删除本地已完成存档；深历史容量、配额和修剪由受控本地存储/Time Machine 运维负责。这里描述的是**持久化备份副本**的流向；启用云 ASR/LLM 时，音频和回答文本仍会按本文开头的数据流发往第三方云，不能据此宣称“患者数据只在自有机器间流动”。

### 8.1 恢复 SOP（必须做真实演练）

`sha256sum -c` 只能证明清单中的字节，不能证明数据库可恢复、版本正确或录音与数据库一致。正式恢复必须由具名运维员执行：

1. 先在隔离恢复环境选定快照；VPS 完整快照执行
   `python3 -I scripts/verify_backup_snapshot.py verify-vps <快照目录>`（容器内 `backup.sh` 产生的无配置快照使用 `verify`）。验证器只接受当前 Alembic head，并检查 manifest 全覆盖、权限/文件类型、SQLite/FK、恢复 schema 及 DB↔audio 闭包。
2. 停止应用与所有写入任务；在**真正复制或切换前立即再运行一次同样的验证命令**。不要把文件逐个覆盖进正在使用的 `data/`，也不要盲目恢复快照内的旧密钥配置。
3. Compose 形态必须预建一个新的 external 候选卷，按快照在该卷重建 `app.db`、`audio/`和导出目录；原 live 卷保留不动，不做逐文件覆盖。先让候选镜像在无公网出口、无公开端口的隔离环境连接候选卷，显式执行 maintenance migrate，完成应用启动、健康检查、外键/核心行数和抽样录音 checksum 核对。
4. 只有演练证据通过，才在服务保持停写时同时切换 `APP_IMAGE` 和 `APPDATA_VOLUME` 指针。启动后复核健康、账号、受试者/场次/录音聚合数和审计记录。任何检查失败立即停止所有写入，把两个指针一起切回保留的旧镜像+旧卷；不得 Alembic downgrade、删除失败候选卷或继续在半恢复状态写入。

`legacy-unvalidated/` 不是“坏备份”，但也绝不能重命名成当前合格快照。应从该快照对应的历史 release/tag 取出当时的验证器，在隔离环境重验并做完整恢复演练；记录历史代码版本、数据库 head、验证器哈希和演练结果后，才可作为灾难恢复证据。**目标 VPS 上一次真实、含数据的停服恢复演练仍是发布门禁；本地合成绿色测试不能替代。**

### 8.2 已过期 pre-intent 导出 staging 的离线隔离

`scripts/reconcile_export_staging.py` 是一个窄用途的事故处置工具，**不是通用导出修复、删除或“强制继续”工具**。它只接受下列状态：当前 Alembic head 和精确 schema 指纹的 SQLite；`ExportBatch.status=staging`；owner/lease 成对且租约已过期；manifest/publication intent 均未产生；没有 `ExportArtifact` 或 `AudioAssetRow.export_batch_id` 绑定。分析目录必须恰好是 9 份固定 CSV 和 canonical `.staging-receipt.json`；受控目录要么不存在，要么只有 `audio/` 下的 HMAC 文件名，文件数和聚合字节分别受 10,000 份与 64GiB 硬上限。每份受控音频还会使用当前受控 `DEIDENTIFICATION_KEY`/`DEIDENTIFICATION_KEY_ID` 在内存里重算音频 HMAC 文件名和 `ExportBatch.export_scope_hash`，再与 `audio_manifest.csv`、同一权威场次的 `AudioAssetRow`、不可变 `AudioCaptureReceipt` 及原始音频字节做唯一 checksum/长度/格式/分类闭合。密钥不写收据、输出或诊断；若事故期间已轮换密钥，必须在受控密钥管理中恢复该 batch 使用的旧世代后才能处置，不得猜测 token。活跃租约、部分 intent、未知文件、空孤儿目录、软链接（包括用户提供根路径的任一父级）、硬链接、非普通文件或任一闭合歧义都会 fail-closed。

若 pre-intent 崩溃后又发生了受试者撤回，本工具不会擅自删除原始音频。`recorded + isolated_by_subject_withdrawal` 会固定返回 `withdrawn_audio_cleanup_required`；须先由具名 admin 按既有 withdrawal DELETE 治理通道提交 `deleted + delete_gate_passed`，并确认原始字节已物理消失。只有 patient 撤回终态、当前 governance revision 对应的不可变 `PatientWithdrawalEvent`、音频 deleted gate、原声缺失和上述受控副本闭合同时成立，才能继续隔离。

执行顺序：

1. 进入正式维护窗，用具名运维账号停止应用、所有导出 worker、备份 timer 和其他 SQLite 写入者；记录工单时间和执行人。`--confirm-offline` 是具名人对这个事实的声明，不会替代停服。工具同时持有 SQLite write fence；获取不到就拒绝。
2. 在与 live `data/` **相同的加密文件系统**内，预建独立私有、具备硬容量配额且权限为 `0700` 的隔离目录或子卷；不得使用不同 `st_dev` 的独立挂载卷。这样两棵 batch 目录才能只做不覆盖的原子 rename。隔离根不得位于 `data/`、常规备份目录或公共同步目录内。受控音频即使用 HMAC 文件名仍含声纹和口语内容，必须按原始敏感数据管理。
3. 先只检查资格；该步也必须停服：

```bash
./.venv/bin/python -I scripts/reconcile_export_staging.py \
  --data-root /srv/nmu/live-data \
  --quarantine-root /srv/nmu-private-quarantine \
  --batch-id EXP-current-random-id \
  --confirm-offline --dry-run --json
```

4. 只有 `status=eligible` 且工单已批准时，去掉 `--dry-run` 重跑同一命令。工具会先 fsync 文件和目录，再把分析/受控 batch 目录收入唯一隔离 bundle；仅用 `rmdir` 收掉由该 batch 留下的空 classification/root scaffolding，绝不递归删除或碰其他 batch。随后一个事务仅将该 `ExportBatch` 的 owner+lease 置空；**不删除、不改状态、不改 intent 字段**。任一个提交前步骤失败会把本次已移目录和必要父目录恢复原位；恢复不能确认时只报固定错误码，不猜测。数据库提交后会先重读 owner/lease 权威事实，再发布 success receipt；如果提交回包、post-commit probe 或 receipt 写入失败，下次同 batch 调用只有在“DB owner/lease 已空 + deterministic intent + 两棵隔离树逐字节一致 + 原路径不存在”全部成立时才进入幂等 finalize；`--dry-run` 此时只返回 `completion_pending`。
5. `status=reconciled` 后，在仍然停服的状态立即生成并验证一份新备份，再做隔离恢复演练。bundle 内的 canonical intent/reconciliation receipt 只有 batch id 的 SHA-256、分类、树摘要、租约和固定操作事实，不保存明文 batch id，也不含 patient/raw-audio ID、自由文本或密钥。不要把收据内容或 bundle 路径贴到公共聊天/工单。bundle 的保留、解除隔离或销毁必须另走 PI/数据治理批准，没有自动删除路径。

若返回 `database_commit_uncertain` 或 `database_commit_uncertain_marker_failed`，不要把目录移回、手工改数据库或立即重启导出。canonical quarantine intent 在 commit 前已耐久落盘；工具会幂等尝试再写一份不确定标记，标记本身无法落盘会显式返回后一错误码，不伪称已保存。由第二名具名运维员在停服、只读状态下核对该 batch 是否仍保留 owner+lease。owner 已空时，先用同一命令的 `--dry-run` 验证 `completion_pending`，工单二次批准后去掉 `--dry-run` 完成上述 deterministic finalize；owner 仍为原值时，同样只能在核准后用同一 release/同一 batch id 恢复。任何部分 manifest/publication intent 或 artifact/audio 绑定都必须转入专门数据治理，不得用本工具绕过。PostgreSQL 也不在本工具范围内。

Docker 形态部署时的等价手动命令:

```bash
# 默认 config 输出会展开 env_file 密密；只允许 quiet 校验。
docker compose config --quiet
# 在容器内做在线一致快照(数据库 + 音频 + 导出 + 校验清单)
docker compose exec app bash scripts/backup.sh
# 必须再复制出容器/数据卷，否则不算异地备份
docker compose cp app:/app/data/backups ./nmu-backups
```

上述第一份快照仍位于同一 `APPDATA_VOLUME`，只有复制到独立加密、有硬
quota 的存储并重验后才是回退证据。Compose 整机恢复还必须单独保护
Caddy 状态卷、发布镜像 digest、Compose 文件和卷指针；不得只复制 `app.db`。

异地拉取脚本不会保存或猜测生产目标；定时任务必须显式注入以下三个环境变量，并预先把正确主机公钥写入运行账号的 `known_hosts`。脚本强制 `StrictHostKeyChecking=yes`，目标未知或公钥变化时会拒绝连接。下面只展示非生产占位值：

```bash
export NMU_BACKUP_SSH_HOST=backup-vps.example.org
export NMU_BACKUP_SSH_USER=nmu-backup
export NMU_BACKUP_REMOTE_ROOT=/srv/nmu/backups
# 可按批准的存储容量设置；默认分别为 64GiB/单快照、256GiB/本轮、
# 100000 文件/单快照、500000 文件/本轮、10000 目录/单快照、
# 50000 目录/本轮。
export NMU_BACKUP_MAX_SNAPSHOT_BYTES=68719476736
export NMU_BACKUP_MAX_PULL_BYTES=274877906944
export NMU_BACKUP_MAX_SNAPSHOT_FILES=100000
export NMU_BACKUP_MAX_PULL_FILES=500000
export NMU_BACKUP_MAX_SNAPSHOT_DIRECTORIES=10000
export NMU_BACKUP_MAX_PULL_DIRECTORIES=50000
./scripts/vps-backup-pull.sh
```

远端账号应是只能读取备份目录的专用最小权限账号，不要复用系统管理员或应用运行账号；SSH 私钥路径和口令不得写入仓库。

## 9. 升级 / 改配置

升级不能从 `git pull`、服务器现场 build 或覆盖 live 卷开始。发布单元是
**已审查 APP_IMAGE digest + 从已验证快照恢复的候选 APPDATA_VOLUME**；两个指针
必须一起切换和一起回退。

### 9.0 已有宿主 Caddy 的 1 GiB VPS：loopback 蓝绿路径

当前这类现网不得启动 Compose Caddy，也不得先停旧服务。只有云厂商整机快照、
网页控制台/救援登录、旧版本快照的隔离恢复演练和最终停写备份全部留证通过后，
才可进入本节；缺任一项都保持 NO-GO。Docker 首次启动还会改变转发/iptables，
必须另开维护窗口复核 SSH、云安全组、`DOCKER-USER` 和现有非本项目端口。

候选环境使用 `docker-compose.host-caddy.yml`，只把 app 映射到宿主回环端口；
覆盖文件会把 Compose Caddy 放入默认不启用的 profile，并把候选 app 限制为
384 MiB 内存、512 MiB 含 swap。`.env.candidate` 另设：

- `APP_HOST_PORT=18000`（现场若已占用则选另一个只回环端口）；
- `HOST_CADDY_BRIDGE_IP=` 为宿主 Caddy 经 Docker NAT 到 app 时容器实际看到的
  **单个私网 bridge source IP**，先现场测量再填；禁止 `*`、CIDR、
  逗号列表、回环或公网地址。容器入口会在 Uvicorn 监听前强制校验，
  任一宽泛/错误值都使候选容器 fail-closed；
- `APP_IMAGE` 与 `APPDATA_VOLUME` 仍分别指向候选 digest 和从已验证快照恢复的新卷。

所有候选命令都显式同时传两个 Compose 文件，且只启动 `app`：

```bash
docker compose -f docker-compose.yml -f docker-compose.host-caddy.yml --env-file .env.candidate config --quiet
docker compose -f docker-compose.yml -f docker-compose.host-caddy.yml --env-file .env.candidate --profile maintenance run --rm --no-deps migrate
docker compose -f docker-compose.yml -f docker-compose.host-caddy.yml --env-file .env.candidate up -d --no-build app
```

确认 `127.0.0.1:18000` 健康、DB=head、聚合数/checksum、账号/配对和 384 MiB
限制下的合成压力通过后，复制一份权限 0600 的宿主 Caddy 候选配置，只把该站点
upstream 从旧 `127.0.0.1:8000` 改到候选 `127.0.0.1:18000`。先用现网同一
Caddy 二进制执行 `caddy validate`，再 reload；不 stop、不覆盖证书目录。切换失败
立即把 upstream reload 回 8000。旧 app、旧数据库和旧卷保持不动，直到新版本
完成观察期和新 head 的异地可恢复快照；不得删除失败候选或执行 downgrade。

### 9.1 切换前：旧卷不动，候选卷预演

1. 用**当前已部署版本自带的历史验证器**生成并验证升级前快照，异地保存并绑定旧 release/head。新验证器不为旧 head 背书。
2. 保存权限 `0600` 的 `.env.previous`；复制一份 `.env.candidate`，只把 `APP_IMAGE`、`APPDATA_VOLUME` 和 `APP_ENV_FILE=.env.candidate` 改为候选值。不输出、`source` 或提交任一文件。
3. 以 `.env.candidate` 中的精确名称创建全新 external 卷；原 live 卷保持挂载给旧容器，不删除、不重命名、不就地迁移。
4. 按 8.1 恢复 SOP 把已验证快照恢复到候选卷。本仓库故意不提供“猜测宿主卷路径并盲拷”脚本；具名运维员必须根据现场存储拓扑完成受控恢复并留证。
5. 只针对候选卷显式执行单次迁移：

```bash
docker compose --env-file .env.candidate config --quiet
docker compose --env-file .env.candidate pull app caddy
docker compose --env-file .env.candidate --profile maintenance run --rm --no-deps migrate
```

6. 在无公网出口、无公开端口的隔离环境使用候选镜像+候选卷启动预演；验证 DB=head、SQLite integrity/FK、核心聚合数、抽样录音 checksum、健康和合成流程。不使用真实受试者路径做 UI 点击测试。

### 9.2 维护窗口切换

1. 确认无活动场次、上传、导出或其他写入，停止 app/写任务，立即重做一份最终停写快照和异地验证。
2. 记录旧 APP_IMAGE digest、旧 APPDATA_VOLUME、旧验证器哈希和旧 schema head。
3. 不能把仍含 `APP_ENV_FILE=.env.candidate` 的文件直接改名为 `.env`：否则
   常规启动会继续追踪候选文件。先在同一受控文件系统制作权限
   `0600` 的 `.env.next`，内容与已验证的 `.env.candidate` 相同，但把
   `APP_ENV_FILE` 改回 `.env`。不输出、`source` 或提交它；人工逐项核对候选
   `APP_IMAGE` 和 `APPDATA_VOLUME` 后，原子将 `.env.next` 替换为 `.env`，再执行：

```bash
docker compose config --quiet
docker compose up -d --no-build
docker compose ps
```

4. 确认实际 app 镜像 ID 对应候选 digest、`/app/data` 挂载候选卷、app healthy、8000 未公开、当前 head/数量/checksum 与预演一致。通过后立即用新版本验证器生成、异地复制并验证新 head 快照。

### 9.3 回退

切换后任何健康、schema、数据或安全检查失败：

1. 立即停止 app 和所有写入者；不执行 Alembic downgrade，不向原卷或候选卷逐文件覆盖。
2. 保留失败候选卷作为只读事故证据；绝不执行 `docker compose down -v`、`docker volume rm` 或自动清理。
3. 把权限 `0600` 的 `.env.previous` 原子恢复为 `.env`，使 APP_IMAGE 和 APPDATA_VOLUME 同时回到旧值，然后：

```bash
docker compose config --quiet
docker compose up -d --no-build
docker compose ps
```

4. 用旧版本验证器复核旧 head、integrity/FK、健康和核心聚合数。候选卷中的任何新数据只能进入具名数据治理/对账流程，不得手工合并回旧卷。

**源码目录的 rsync/覆盖发布路径已取消。** 历史上曾因删除式同步遗漏 `.env`
排除而删掉密钥配置；本轮只接受受审查的不可变镜像和显式外部卷指针，不保留
任何可执行的 `rsync --delete`、现场 build 或 dirty-tree 部署命令。

## 10. 数据库选型

- **默认 SQLite**（数据卷内）：小团队、单机、并发低 —— 完全够用，零额外运维。
- **要 Postgres**：`.env` 设 `DATABASE_URL=postgresql+psycopg://user:pass@db:5432/nmu`，
  在 compose 加一个 `db: image: postgres:16` 服务 + 卷，`app` 加 `depends_on: [db]`。
  代码已兼容(见 `app/db.py`：SQLite 专用参数不会误传 Postgres)。迁移仍只能通过
  maintenance `migrate` service 显式执行；常规 app 启动只检查 DB=head。
  **但当前备份/恢复脚本只实现并验证了 SQLite**：检测到 PostgreSQL 会 fail-closed，尚无经演练的 `pg_dump`/`pg_restore`、录音一致性时点和恢复验证链。因此任何 PostgreSQL 正式部署在这条链实现并完成含数据恢复演练前都是备份 NO-GO，不能把残留的 `data/app.db` 当作备份。

## 11. 运维注意

- **只读运行边界**：应用以 UID/GID 10001 运行，根文件系统只读、capabilities 全部移除，仅 `/app/data` 持久可写；`/tmp` 是受限且不可执行的内存盘。不要为了兼容临时文件而把整个 `/app` 改成可写。
- **题图/答案不走静态目录**：`web/public` 与 `web/dist` 不应含 `img/` 或完整题库/脚本。模拟题图只经 current-only capability 路由返回；完整内容包只经具名账号固定路由读取。30 张私有 WebP 仍是 `simulation_only`，没有来源/权利/临床冻结，不得改成公开静态目录或用于正式受试者。
- **单 worker**：失败限速器在进程内。别给 uvicorn 加 `--workers>1`（会各自计数、削弱限速）；
  真要多进程，改用共享限速(Redis)或前置 Caddy 层限速。
- **会话有效期 12h**；过期后前端自动退回登录页。改密/停用会**立即吊销**该用户所有会话。
- **代理头 / 防伪造**：限速与审计按真实来访 IP 计，该 IP 不可被客户端伪造，靠两道：
  Caddy `reverse_proxy` 默认忽略客户端自带的 `X-Forwarded-For`，再按直连 peer 设置或扩展
  该头；app 只信任 Caddy 的固定内网 IP
  (`FORWARDED_ALLOW_IPS=172.28.0.10`，Compose 已设)。切勿把它改成 `*`。
  若日后在本机前再加 CDN/反代，必须在 Caddy 的全局 `trusted_proxies` 中精确信任
  该上游，并重新验证真实客户端 IP；不得为此放宽 app 信任面。

---

## 12. 供应链

### 12.1 钉住了什么，在哪儿

| 层 | 钉法 | 文件 |
|---|---|---|
| 容器底座 | `FROM …@sha256:` 双镜像 digest；tag 只作可读注释 | `Dockerfile` |
| 底座系统包 | `apk add bash=5.3.9-r1` 精确版本 | `Dockerfile` |
| 运行期 Python | 全量传递锁 + 每个分发包 sha256，安装走 `--require-hashes` | `requirements-deploy.lock.txt` |
| 前端 | `package-lock.json` v3，含每个包的 integrity；构建走 `npm ci` | `web/package-lock.json` |
| 物料清单 | CycloneDX 1.6，确定性输出，入库 | `sbom.cdx.json` |
| 漏洞豁免 | 必须写理由和到期日 | `security/vuln-waivers.json` |
| CI 第三方 action | 按 commit SHA 钉，tag 只作注释 | `.github/workflows/ci.yml` |

最后一行同理：tag 可以被移动，被移动之后没有任何本地信号，而这些 action 在 CI
里拿得到仓库内容。它们不能比依赖松。

**锁以当前不可变镜像的 Python 3.12 为运行下限。** 历史裸机解释器不再是支持的发布
路径，也不能反过来决定新镜像的依赖口径。

下限不是装饰：按 3.12 编译的锁装到 3.10 上会缺 `tomli`/`exceptiongroup`/
`async-timeout` 三个垫片，反过来则可以，所以 `supply_chain_check.py` 把
"机器 Python 低于锁的下限"判成硬失败。`--universal` 允许同一个包按 marker 分叉出
两条（下限还是 3.10 时 `websockets` 就是 16.1.1 / 17.0.1 两条）——那不是重复，
对账必须按集合包含判。现役锁里暂时没有分叉的包，但机制还在。

### 12.2 三道自动门禁

```bash
scripts/ci_gate.sh              # 全部：ruff / 后端 / 前端 / SBOM / 漏洞 / 锁自洽
scripts/ci_gate.sh --offline    # 不出网(漏洞扫描改用存好的 OSV 应答)
```

- **锁 ↔ 运行环境**：`scripts/supply_chain_check.py --python <解释器>`。锁只在
  安装那一刻起作用；这条守的是"安装之后有没有人手工往里加东西"。已接进
  `preflight_check.py --lock …`。历史 `deploy_baremetal.sh` 已硬停用，不再存在可执行的
  rsync 发布或裸机回滚路径；不可以该历史脚本的输出充当当前发布证据。
- **SBOM ↔ 依赖**：`scripts/generate_sbom.py --check`。改了依赖没重出 SBOM 就红。
  输出不带时间戳、serialNumber 由内容摘要推出，所以"无 diff"是个可靠判据。
- **漏洞**：`scripts/vuln_scan.py` 打 OSV。只发包名和版本号（都是公开信息），
  不发仓库内容、不发患者字段、不带凭据。查不通网络退 2 —— 查不动就是没查过，
  不当作通过。

### 12.3 依赖升级只进入不可变镜像

当前唯一发布形态是第 3、8、9 章定义的“受审查镜像 + 显式候选数据卷”。依赖升级必须在
受控构建环境重出锁、SBOM、镜像和安全扫描结果，再以镜像 digest 进入候选环境。生产机上
不得手工 `pip install`、新建或替换 venv、源码编译 Python、现场 build、rsync 源码、直接
改 shebang 或用 systemd 切换一套未绑定镜像的解释器。

2026-08-06 的裸机 venv 和自编译 Python 记录只作为历史事故背景保存于版本历史，不再
提供可复制执行的命令。`scripts/deploy_baremetal.sh` 也只剩不可绕过的停用 stub。

### 12.4 已知的、还没解决的

- **底座镜像里的系统包没有完整本地扫描。** CI 的 `image` 作业用 trivy 扫构建产物；
  本机门禁通过不代表候选镜像已经完成扫描，更不代表养老院发布获批。
- **具名外部批准验证器尚未冻结。** `preflight_check.py --release` 目前会验证自动检查和
  证据字节绑定，但必定在“正式发布批准”处失败；在养老院、PI、伦理/隐私、法规和运维
  的证据类型、签发主体和校验规则确定前，不得把该失败改成通过。
- **真实目标环境的含数据恢复和回退证据仍缺失。** 本地合成备份检查不能替代第 8、9 章
  的候选卷恢复、停写切换和回退演练。

---

## 13. 迁往医院内网（收真实数据前必做）

代码里零硬编码公网 IP/域名（`89.208.253.119`/`sslip.io` 只出现在文档），前端同源
相对路径、无 CDN、CSP `connect-src 'self'`——迁移的全部工作量在配置、数据搬运和
出网依赖的替代上。以下清单来自 2026-08-07 的逐文件可移植性盘点。

### 13.1 出网白名单

- **保留云 AI**：`dashscope.aliyuncs.com:443`（HTTPS；备选 CosyVoice 音色额外走
  同域名 wss）。SDK 端点可用 `DASHSCOPE_HTTP_BASE_URL` / `DASHSCOPE_WEBSOCKET_BASE_URL`
  覆盖。非流式 OSS 下载分支只接受 `*.aliyuncs.com`:443，且**解析到非公网 IP 直接
  拒绝**（`app/tts.py` 的 fail-closed 守卫）——不要用内网 DNS 把阿里域指向代理。
- **全离线配置**（合规降级，不是故障态）：TTS=本地 Piper；模型必须随受审查镜像
  或独立签名制品进入，逐字绑定摘要和许可材料，禁止手搬 `data/tts` 绕过供应链门禁；
  generic/manual 才可再降浏览器语音，exact/autopilot 仍须按其严格合同暂停；ASR=人工
  转写；判分=规则/rubric。要**显式**设
  `TTS_ENGINE`/`ASR_ENGINE`/`LLM_JUDGE`，不要靠删 Key 凭感觉（见 §7）。
- **可选出网**：`discord.com`（运维告警；不通则所有 `OnFailure=` 告警变哑，需换成
  医院认可的内网告警出口——**未定，医院侧待办**）；`api.osv.dev`（漏扫有
  `--offline security/osv-response.json` 全离线路径）；apt 源（`os_security_check`
  无源可用会如实 FAIL，挂内网镜像或用 `--simulate-file`）。

### 13.2 带走什么（容易漏的）

- 已审查且按 digest 锁定的 OCI 镜像；若内网不能访问镜像仓库，只能搬运该精确镜像的
  离线 OCI 包及其摘要、签名/来源和扫描证据，导入后重新核对 image ID。
- 第 8 章验证通过的备份快照。目标机预建新的 external 候选卷，再按恢复 SOP 一次性重建
  `app.db`、`audio/`、导出目录和其他受控持久数据；不得把正在使用的 `data/` 目录逐文件
  搬过去或覆盖候选卷。TTS 缓存缺失时按批准的提供商/离线模型重新生成，不把旧缓存当作
  可绕过来源和合规门禁的发布必需品。
- 受审查的 `docker-compose.yml`、`Caddyfile` 和本院环境配置模板。密钥通过院方受控渠道
  重新签发，不能从历史 `.env` 或备份中复制个人 Key。宿主调度、备份和告警配置必须按
  目标环境另行审查，不搬运旧 `deploy/systemd/*` 作为默认答案。

### 13.3 必改配置清单

- `.env`：`TRUSTED_HOSTS=<内网主机名或 IP>`（精确匹配，不吃通配符）、
  `SITE_ADDRESS=https://<同>`；`SESSION_COOKIE_SECURE=1` 保持——**内网也必须 TLS，
  否则 cookie 发不出去，且患者端麦克风（getUserMedia 要求安全上下文）直接不可用**。
- `Caddyfile`：取消 `# tls internal` 注释（内网无 ACME）；操作台与患者平板的浏览器
  都要信任内部 CA，这一步要写进装机 SOP。
- 宿主调度器的时区、身份、最小权限和失败告警按医院环境重新冻结，不照搬历史 timer。
- 备份链：backup/health/capacity/restore-drill 四类任务必须在院内重新布置并实演；
  **Mac 异地拉取（ssh 进公网 VPS）必断**——异地副本与 `audit-anchors.log` 异地锚定
  账本都要换到医院认可的、与主机不同故障域的位置，否则审计链外部锚定退化为摆设。

### 13.4 迁移后验收

- 候选容器专用的只读预检执行器尚未进入镜像，当前 `--release` 也会固定停在外部批准门。
  在这两项实现并绑定镜像 digest、候选卷和内网地址前，迁移验收保持 NO-GO；不得改用
  目标机 venv、源码目录或历史裸机脚本绕过。
- 跑一遍 `harness/research_rehearsal_harness`（隔离 root，不碰生产库）：零模拟开关
  下真实档案 create→approve→开场→出图→录音→转写全链，等于把「收人链路」在内网机
  上重新点火验证一次。
- 保留云 AI 时由管理员执行 §7.1 合成检查。

### 13.5 医院 Key 切换与个人 Key 退役

按 §7 换 Key 流程执行后，**必须在百炼控制台吊销个人旧 Key**：历史每日快照里的
`.env` 副本都含旧 Key 明文（异地拉取机上另有多达 60 份），吊销后这些副本才无害化；
不吊销等于把有效凭据留在多台机器的备份里。

---

## 部署自检清单

下面项目中有一部分已有本地自动检查，但当前镜像还没有候选容器专用只读预检执行器，
正式批准验证器也没有冻结。因此这里不再给目标机 venv/源码命令；任何人都不能用本机
`preflight_check.py` 的绿色结果代替目标镜像、候选卷、真机或院方批准。正式自动入口完成后，
它必须绑定同一个镜像 digest、数据库迁移头、候选卷、访问地址和发布证据索引；任一项
FAIL 或 SKIP 都非零退出。

- [ ] 仓库私有；`.env` 已 `chmod 600` 且未入库
- [ ] Compose project 精确为 `nmu-platform`；命令和运行环境均无 `-p` / `COMPOSE_PROJECT_NAME` 覆盖；现场旧 project/卷已先盘点，没有因身份漂移新建空卷
- [ ] `APP_IMAGE` 是已审查的 `@sha256` 引用，实际容器镜像 ID 与发布证据一致；生产 Compose 不含 `build:`
- [ ] app/Caddy 三个卷皆是显式命名的 external 卷；从未执行 `down -v` 或删卷作为回退
- [ ] 常规 app 入口不执行迁移；候选卷只由具名运维员调用 maintenance `migrate`，启动前 DB=head 校验通过
- [ ] 已配置独立的 `DEIDENTIFICATION_KEY`(≥32 bytes) 和非机密 `DEIDENTIFICATION_KEY_ID`；缺失时确认导出 fail-closed
- [ ] 当前环境已明确标记为开发/模拟；正式研究门禁未通过前未导入真实受试者数据
- [ ] `REQUIRE_AUTH=1`（compose 默认）；已建至少一个 admin 账号
- [ ] `SITE_ADDRESS` 正确；HTTPS 能打开(域名绿锁 / IP 自签已信任)
- [ ] `TRUSTED_HOSTS` 是无 scheme/端口/通配符的精确访问主机；恶意 Host 请求返回 400
- [ ] `docker compose ps` 显示 app healthy 且 caddy 已启动；健康探针使用同一 `TRUSTED_HOSTS`，未靠放宽 127.0.0.1 绕过 Host 校验
- [ ] 实际容器内非 `/app/data` 写入失败；进程 UID/GID=10001，根文件系统只读、无额外 Linux capability
- [ ] `web/public`/`web/dist` 不含完整题库、脚本或历史 `/img`；匿名、设备 capability、大小写/编码别名均不能读取答案整包
- [ ] 已保存受审查 commit/tag、镜像 digest、依赖锁与 SBOM/漏洞扫描结果；未提交工作树或本机构建成功不算可追溯发布包
- [ ] `ss -tlnp` 确认 8000 不对外，只有 80/443
- [ ] ufw 已启用，仅放行 22/80/443
- [ ] 老人端平板 https 下麦克风可用（真机验收）
- [ ] 当前 head 快照已通过语义验证并形成不可变异地副本；升级前后各有匹配版本的验证证据；目标环境已完成一次含数据、停服、staging 切换与回退的真实恢复演练
- [ ] 升级全程保留旧 APP_IMAGE+旧 APPDATA_VOLUME；已实演“候选卷失败后两个指针同时切回”，而不是只换旧镜像
- [ ] Compose 自动备份任务确实读取 external `APPDATA_VOLUME`，且不是裸机脚本对宿主遗留 `data/` 的伪备份
- [ ] 已由具名管理员执行纯合成、无受试者内容的服务检查，并核对 TTS、ASR 及已配置 LLM 的实际引擎/模型版本；自动干预启动会校验未过期且配置匹配的结果，但不会代替管理员主动发起这项可能计费的检查。无 Key、云超时和断网时的降级路径仍须在目标设备验证
- [ ] 若交付任何 Piper/音色模型：已留存包与逐模型的来源、版本、许可文本和使用/再分发权证据，并完成适用的 GPL 合规交付方案；否则已确认默认镜像不含二者
- [ ] 若启用云能力：伦理/同意材料明确覆盖固定话术、原始声纹、回答文本及供应商条款；使用课题组自有账号与独立 Key
- [ ] 云服务的数据区域、日志、留存、二次使用、删除和 DPA/合同责任已有书面结论
- [ ] PI/隐私负责人已书面确认 `AI_QUALITY_RESEARCH_MIN_SUBJECTS`（2..100）；配置缺失/无效与配置有效但发布批次未冻结三种情形均已验证为整行“已抑制”，不读取研究场次/逐次证据，不暴露实际人数或用 0 伪装无风险。持久化冻结 cohort/release epoch 与逐单元互补抑制尚未实现时，不得开放真实研究数值
- [ ] AI 质量接口已按角色复核可见范围：researcher=本人场次、data steward=已进入终态的场次、admin=全部授权场次；前端明确显示范围且未跨权限比较；反向代理未缓存响应，非 JSON 429 仍保留可验证的 `Retry-After`；PostgreSQL/SQLite 稳定快照、加载后总预算及 hash 前事务释放已用目标后端并发测试验收；固定资源预算和模拟查询限速保持启用
- [ ] AI 质量口径已确认：`prompt_level=3` 只表示录音尝试所处提示上下文；在床旁答案呈现收据实现前，告知答案次数/比例必须保持未知；延迟只称“录音上传完成→判类完成”，不得包装成完整交互时延
- [ ] 双真相已执行：AI 只驱动实时流程/初评；人工确认和锁分只发生在 `intervention_completed` 复核窗，现场 closeout 已独立保存，最终完成时与研究真值一起锁定
- [ ] 第 2 周源脚本的 60 个交付缺口（70-position 运行计划内 50 个 + 尚未结构化的多要素源位置 10 个）已全部清零，`operational_autopilot_ready=true`；否则 AI 启动必须保持拒绝。当前 20 个单要素只代表字段级协议完整，默认整计划仍是 0 个可启动
- [ ] 正式研究级 autopilot scope 已独立实现、审核并在合成研究切片验收；当前 P0a 模拟实现不得通过移除 simulation guard 获得放行
- [ ] 两类正式结局工具的具体名称、版本、授权、条目、施测/缺失/中止规则、计分算法和批准事实已由 PI/临床团队冻结；已实现的通用 `AssessmentEvent` / `AssessmentInstance` / `ItemResponse` / `ScoringEvidence` / `approved-deferred` / `closeout` / `switch` / 幂等命令合同已在目标环境验收；真实定义与计分制品、逐题录音服务端收据（绑定 patient/event/instance/item/revision 且不可复用）及冻结工作流政策均已安装为可信执行适配器。definitions 完整本身不得显示为“已冻结可用”
- [ ] 逐次录音、转写、AI 判断、提示和接管可追溯
- [ ] 确认由课题组自有 VPS 承载本项目持久化数据已获批准；导出走去标识化通道
