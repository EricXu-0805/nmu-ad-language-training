# 生产状态（唯一权威记录）

> 这个仓库过去没有任何字段记录"生产上跑的是哪个版本"，导致 2026-08-09 那轮独立复核
> 在仓库里**找不到上一次上线的任何证据**，只能靠人的记忆。这份文件就是为了让那件事
> 不再发生：**每次上线必须在同一个提交里更新它**，不更新就当作没上线。
>
> 这里只记录事实，不代表任何批准。系统能不能给真实老人使用见
> `docs/handover/七道门现状表.md`。

> **待上线（2026-08-27 本机完成，未部署）**：审查处置批次，**含迁移
> `b6d4f8a2c917` → `c8e5a1f3b209`**，上线脚本在收据 228。
> ⚠️ **三个会让服务起不来的耦合，顺序不能换**：
> ① **CONSOLE_PIN 必须先换成 8 位以上**（新代码里不合格的 PIN 直接拒绝启动；
> 换 PIN 会让**所有现有配对码作废**，要重新发）；② 停服后迁移，起服前闸门验头；
> ③ 备份校验器恢复指纹随头前进到 `973f314a…`，异地拉取机要重装校验器。
> 内容：异地锚点基线重置（`--reset-baseline` + 世代边界）、告警三个死角
> （`Restart=always` 够不到 burst 永不进 failed / os-security `|| true` 吞退出码 /
> 无公网健康探针）、回滚与 RELEASE_STATE 改真话、四份对外文档补第四条出网路径、
> 量表可退回未评、老人端断网自愈与「设备卡死」不再显示成暂停、导出表列契约
> （空表也写表头、结局指标展成列、逐题明细出 JSON 列）、Patient 加四个研究协变量、
> 量表进 `/research/v1` 两个新数据集、量表期别序号与唯一约束、删死列
> `naming_latency_ms`、全局限速改成连续锁定阶梯、老人端绑定令牌 70 天寿命。
>
> **最新上线（2026-08-26 20:54 UTC）**：`ec682d3` → **`ec2d1b5`**，**零迁移热更新（后端+前端,重启）**。
> 量表电子记录接入 ACE-III(6 页,25 项/五域/总分 100)与动物流畅性测验:新增
> examiner_scored 题型(计分框/计数框→分档查表/闭集选项)+examiner_sum 计分(域小计
> 现算不落库、可选界值或按 choice 条目分层界值)。钱凯 08-26 五份文件里 GDS-15/NPI-Q/
> 功能沟通与 08-20 批逐字节相同,未动。「车」字分档表源件重叠,按英文原版实现并记
> 勘误待钱凯;AFT 文化程度分组施测时表内选,初中以下或不详只记分不判定(Eric 拍板)。
> 三路对抗审查处置完(转录保真 2 blocking 全修;P1=服务端多带 score_when 键致前端
> 整目录拒收,已删并加键面对齐常驻钉)。ci_gate 六关绿;本机真 Chrome 16/16。生产:
> content/questionnaires 五包在位,dist 15 文件完整,`OK database_at_head`,树核
> `MATCH revision=ec2d1b5 files=89 identical=89`,红线 404 / 量表目录 401。
> preflight 7/8:唯一 FAIL=备份新鲜度——08-26 19:38Z 夜备份被快照校验器以
> `audio_governed_bytes_present` 拒绝:受试者 demo-001 已登记研究撤回,其 7 条录音行
> withdrawn=1 而 7 个 .webm 仍在 data/audio(契约:已撤回者音频字节不得进快照)。既有
> 契约,与本次改动无关。**已处置(08-27 02:49Z)**:Eric 自跑收据 226——生产 .env 开
> `ENABLE_AUDIO_DELETE=1`(备份 .env.before-audio-delete-*)重启,7 条经撤回治理通道
> DELETE 全 200 → deleted,立跑备份 ok(快照 20260827-024951),health HEALTHY;临时 admin
> ops-runner 已停用。后端重启,readiness 探针缓存已清,自动带练前需重按
> 「检查 AI 服务」。**受控技术环境更新，不构成任何外部批准；两份新表待钱凯/丁老师终确认。**
>
> **上一次（2026-08-27 15:03 UTC）**：`7a80c9e` → **`ec682d3`**，**零迁移纯前端热更（零重启）**。
> 上一批立案的「领命窗口暂停死锁」修复:暂停恰落在设备领到命令之后、首个 ack
> 落地之前时,runtime_inactive 把设备踢进平静档,收麦交接 effect 因守卫要求
> 「仍在 server 模式」永不执行 → takeover_ready 永假 → 继续/转人工按钮永不出现。
> 修=守卫只看「场次已暂停」,平静档照常应答 drain-target/drain-ack;整页刷新后的
> 无上下文路径用 standaloneDrainedKey 去重。真 Chrome 复验:领命窗口暂停×3 按钮
> 0 秒即出(原 120s+不出)、暂停中刷新自愈、常规全弧回归 7 步全绿。树核
> `MATCH revision=ec682d3 files=89 identical=89`(纯前端,后端指纹自然不变);
> dist 完整性 15 文件通过;零重启,readiness 探针缓存未清。
> **受控技术环境更新，不构成任何外部批准。**
>
> **上一次（2026-08-27 10:42–10:45 UTC）**：`263d0bd` → **`7a80c9e`**，**零迁移热更新（后端+前端,重启）**。
> 四提交一次上产:①`61566c7` 轮询提速(老人端 0.8s/操作台 1s)+运行态失败宽限
> (单次丢包不再锁面板掐录音)+暂停/恢复拒因改 toast 不再被轮询抹掉+控制条真话
> 指引;②`084af69` 同周续训引导——序号按协议槽位全键(周+阶段+任务线)推导,中止
> 后同周续坐 create→approve→start 端到端回归钉住;③`ca72117` **AI 自动带练双向
> 恢复**:新端点 POST /sessions/{id}/autopilot/resume(控制面+runtime 同事务),暂停
> 后「继续 AI」、转人工后「切回 AI」;恢复位置=冻结计划首个无 TurnEvent 题位,
> 阶梯中段如实 409;generation 双 +1 围栏旧设备事实;5 条先红后绿;④`7a80c9e`
> 真 Chrome 全弧验证抓出的闩死修复——被代际围栏的滞留回执(409 command_not_current)
> 持久丢弃,老人端恢复后不再停摆,刷新自愈。真 Chrome 复验:全弧×2+旧时序复现全
> PASS,HAR 实证围栏 409 后 0.2s 继续拉新命令。树核 `MATCH revision=7a80c9e
> files=89 identical=89`;preflight 7/8(唯一 FAIL=OS 补丁积压 11 个,待人工 apt)。
> **已知未修**:暂停恰落在设备「领取命令」窗口时设备静默不交收麦证明,服务端无
> 超时兜底 → 继续/转人工按钮不出现,只能中止开新场(1/3 复现,既有竞态,已立案)。
> **受控技术环境更新，不构成任何外部批准。**(上一基线 `263d0bd` 为纯前端零重启
> 热更:今日队列平局键改码点比较,当时未记档,此处补记。)
>
> **上一次（2026-08-26 22:00–22:07 UTC）**：`083fa35` → **`08ad898`**，**零迁移热更新**。
> 第六堵墙:wk4 SE_花瓶 的③槽源稿误贴他题话术,勘误(errata_fixed 在案)把②④两句
> 按语体重分配后 unknown/silence 共用同一源句,撞引擎「cue1 三分支异源」规则 →
> wk4 全周启动 409 而就绪面报绿。修=引擎对「errata_fixed 登记过本题 cues.1.*」的题
> 精确豁免,未登记塌缩仍 fail-closed(反向测试钉);新增七周 78 题位全扫描常驻卫生钉
> (先红后绿,红在且只红在花瓶)。前端 PREWRITE 名单补 content_incomplete/
> device_not_paired 两码(此前折成 uncertain 屏上无痕)。生产逐周验证:wk5-8 启动+
> 首题 TTS+收麦全通(v5- 截图)。树核 `MATCH revision=08ad898 files=89 identical=89`;
> preflight 8/8 退 0。**受控技术环境更新，不构成任何外部批准；豁免所依据的勘误仍待
> 钱凯/丁老师书面确认。**
>
> **上一次（2026-08-26 21:20–21:25 UTC）**：`075e2c6` → **`083fa35`**，**零迁移热更新（纯后端）**。
> 第五堵墙:麦克风闸把「自助录音按钮待命(selfStart=true)」当「录音进行中」,而人工
> 呈现游标的 selfStart 残留无自纠机制 → wk4 自动带练启动 3 场 6 击全灭死锁。修=
> command_capture_active 只看 recording 活动标记;热麦保护(armed/设备权威 patientRec)
> 原样,既有三段测试一字未改全绿;新增「仅 selfStart 待命→放行」测试先红后绿。
> livestate/runtime 双游标分裂根因待深查。另:同窗口从 /opt/nmu/app 隔离出一个误传的
> `项目综合审计_20260717` 目录(→/opt/nmu/quarantine-junk-20260826,某次 rsync cwd 掉到
> 项目根所致)。树核 `MATCH revision=083fa35 files=89 identical=89`;preflight 8/8 退 0。
> **受控技术环境更新，不构成任何外部批准。**
>
> **上一次（2026-08-26 16:55–17:00 UTC）**：`679c0e0` → **`075e2c6`**，**零迁移热更新（纯后端）**。
> 第四堵墙:计划投影层 `_session_plan_for_account_projection` 只对 `week_no==2` 算
> `operational_autopilot_ready`,第 3~8 周被兜底硬写 False → 「启动 AI 自动带练」灰死
> +自相矛盾黄条,而服务端启动门明明放行 2..8。修=闸放宽 2..8;连修同族两处:照护员
> 摘要裸调回落第 2 周题库(3~8 周 scope/进度静默空白)、legacy 修复 worker 写死第 2 周
> (3~8 周 capture 卡死 received)。三处均有测试(投影 week3 先红后绿/照护员 2/3/8 三周
> +旧机制钉/legacy 既有套件回归)。树核 `MATCH revision=075e2c6 files=89 identical=89`;
> preflight 8/8 退 0。**受控技术环境更新，不构成任何外部批准。**
>
> **上一次（2026-08-26 16:15–16:20 UTC）**：`920a20c` → **`679c0e0`**，**零迁移热更新**
> （库头 `b6d4f8a2c917` 未变）。`/content/item-bank` 按周供数:端点此前写死第 2 周
> (就绪探针历史遗留),训练台不带周次取数再与计划比版本 → **第 3~8 周场次在训练台
> 必然「题库版本不一致」整屏 fail-closed**(Eric 08-25 实测第 3 周场撞死)。修=端点
> `?week=`(2..8,越界 422,默认保持第 2 周探针语义)、训练台按 `session.week_no`
> 取数(第 1 周关系建立绑第 2 周题库,不传周)。新增按周端点测试先红后绿。
> 旧 dist 隔离 `/opt/nmu/dist-stale-20260826-1615`。树核 `MATCH revision=679c0e0
> files=89 identical=89`;preflight 8/8 退 0。
>
> **上一次（2026-08-25 07:25–07:28 UTC）**：`60c2e7e` → **`920a20c`**，**零迁移热更新**
> （库头 `b6d4f8a2c917` 未变）。自动带练启动拒因逐分支说真话:prepareServerOwnership
> 返回具体拒因(技术失败闩/暂停中/状态未同步…)、删掉「老人端麦克风还未确认关闭」
> catch-all、`autopilot_existing_manual_evidence` 与 `autopilot_patient_microphone_active`
> 进写前拒绝清单(服务端拒因不再被吞成 uncertain)、后端人工证据拒启文案改人话。
> 背景:Eric 通宵实测撞墙 + agent 三轮确定性复现(收据待 223)。同窗口
> `demo-admin`/`demo-researcher` 两临时账号已停用(密码曾入转录)。旧 dist 隔离
> `/opt/nmu/dist-stale-20260825-0725`。树核 `MATCH revision=920a20c files=89
> identical=89`;preflight 8/8 退 0。
>
> **上一次（2026-08-25 06:10–06:12 UTC）**：`e33bb89` → **`60c2e7e`**，**纯前端零重启热更**：
> 编辑档案抽屉内置云处理授权入口(独立区块+单独确认,复用治理端点 CAS),不再借道
> 重复建档流程。旧 dist 隔离 `/opt/nmu/dist-stale-20260825-0610`。树核 `MATCH 89/89`;
> GitHub CI 绿;真 Chrome 三轮验证(VERIFY-003 授权→撤销→再授权全循环)。
>
> **上一次（2026-08-23 22:10–22:16 UTC）**：`edb363d` → **`e33bb89`**，**零迁移热更新**
> （库头 `b6d4f8a2c917` 未变，前后闸均退 0）。内容:演示前复审收口——同意状态反向
> 改写伦理门（已登记「未同意/已撤回」不能经档案编辑洗成「已同意」）+ 7 处文案与
> 交互修正 + **云 ASR 空转写段列表按「本轮无转写」处理**（生产直调三次实证:真实
> DashScope 对无语音返回 200+content=[]，原判成技术故障导致老人一冷场即安全暂停，
> silence 提示阶梯在生产上从未走到过）。旧 dist 10 文件隔离于
> `/opt/nmu/dist-stale-20260823-2210`。部署树逐文件核过 `MATCH revision=e33bb89
> files=89 identical=89`；preflight 8/8 全 PASS 退 0；GitHub CI 两个提交均绿。
> 当日早些时候另有生产运维动作:遗留暂停场次经 API 中止、`.env` 追加
> `PROVIDER_READINESS_FINGERPRINT_KEY`（备份 `.env.before-20260823-fpkey`）、
> 真云全链五项演示动线复验全 PASS（收据 220/221）。
> **受控技术环境更新，不构成任何外部批准。**
>
> **上一次（2026-08-23 11:04–11:22 UTC）**：`92040ac` → **`edb363d`**，**零迁移热更新**
> （库头 `b6d4f8a2c917` 未变，前后闸均退 0）。内容:自动带练全题位化(交互协议数据包
> autopilot-v2 关闭全部 58 缺口)+真实场次通道+按受试者配对+两轮可用性审计修复。
> 生产 `.env` 追加 5 变量（云 policy×2 + 自动带练开关×3，备份 `.env.before-20260823-111955`）；
> 旧 dist 隔离于 `/opt/nmu/dist-stale-20260823-110420`。部署树逐文件核过
> `MATCH revision=edb363d files=89 identical=89`；preflight 8/8 全 PASS（OS 补丁项
> 上线时 FAIL 2 项，随窗清零复验 PASS）；GitHub CI 五 job 绿。
> **受控技术环境更新，不构成任何外部批准；交互协议 qc_status=draft 待钱凯/丁老师书面确认。**
>
> **上一次（2026-08-20 18:58–18:59 上海 / 10:58 UTC）**：`b1765c0` → **`3ccb86d`**，
> **含迁移** `6f2a9c4d8e17` → **`b6d4f8a2c917`**（量表电子记录两张新表）。
> Eric 本人执行一键窗口脚本（`219-上线命令_3ccb86d.sh`），全程 8 步闸全过，
> 逐文件核树 `MATCH revision=3ccb86d files=87 identical=87`。详见下方上线记录。
>
> **上一次（2026-08-19 00:2x–00:4x 上海 / 08-18 16:2x–16:4x UTC）。**
> 本次把生产从 `167273f` / `b3e7c5a9d214` 推到 **`10c90e4` / `6f2a9c4d8e17`**，
> 一次跨三个结构版本。按 `docs/上线runbook_受控技术环境更新.md` 逐条执行。
>
> **这是受控技术环境更新，不构成任何外部批准**（七道门一道都没变，见
> `docs/handover/七道门现状表.md`）。这台机器仍然只有 1 个虚构受试者、1 个场次。
>
> 部署树版本这次是**逐文件核过的**：`scripts/verify_deployed_tree.py` 对 84 个
> `.py` 逐个比指纹，`MATCH revision=10c90e4 files=84 identical=84`。

## 当前生产

| 项 | 值 | 怎么核 |
| --- | --- | --- |
| 应用代码版本 | `ec2d1b5`（2026-08-26 20:54 UTC 上线；此前依次 `08ad898` → `263d0bd` → `7a80c9e` → `ec682d3`） | `scripts/verify_deployed_tree.py --manifest <清单> --revision ec2d1b5` 应输出 `MATCH … identical=89`（2026-08-26 20:54 UTC 实测通过） |
| 部署树后续同步 | 已与 `main` 一致 | `git diff ec2d1b5..main -- app web alembic` 应为空 |
| 数据库结构版本 | `b6d4f8a2c917`（2026-08-20 18:58 由 `6f2a9c4d8e17` 迁移，前闸退 78、后闸退 0） | `sqlite3 /opt/nmu/app/data/app.db "select version_num from alembic_version"` |
| 备份校验器指纹（前 20 位） | `79aea62e1a1ed4c6e01e` | `sha256sum /opt/nmu/app/scripts/verify_backup_snapshot.py`；必须与异地拉取机 `~/Library/nmu-backup/runtime/verifier.sha256` 的**第一列**一致。本次上线已重装，`shasum -c ~/Library/nmu-backup/runtime/verifier.sha256` 现在**输出 OK**（2026-08-17 之前那版第二列写的是仓库路径，仓库一往前走就报与事实无关的 FAILED，已修） |
| 回滚存档 | ⚠️ **最新一份仍是 `app-before-deploy-20260820-185806.tar.gz`（对应 `b1765c0`）——它比当前库头旧一个迁移，直接换上去起不来服务。** 8-20 之后的 8 次上线都是「按上一基线重同步」，一份 tar 都没产生。**同结构回滚 = 从仓库按上一基线 sha 重同步，不是解 tar。** 用 tar 之前必须先核：解开后在那棵树上跑 `python -I scripts/check_database_head.py`，退 0 才准换上。⚠️ `…170248-语法错疑含data勿用.tar.gz` 是失败残留已改名标记，勿用 | 解开到临时目录后 `cd <解开处> && /opt/nmu/venv/bin/python -I scripts/check_database_head.py` |
| 回滚锚点快照 | **跨结构**退回 `b1765c0`/`6f2a9c4d8e17` 用停写锚点 `20260820-105812`（旧头，现按 §8.1 在 `legacy-unvalidated/`），必须连**旧代码树 + 旧校验器（7d434d79…）**一起放回；同结构回退直接用最新 `daily/` 快照 |
| 起服前闸门 | 已装（`ExecStartPre` 验库头，以 `User=nmu` 身份跑） | `systemctl cat nmu.service \| grep ExecStartPre`；journal 里 `OK database_at_head` 应在 `Started` 之前 |
| 服务 | `nmu` + `nmu-caddy` 均 active | `systemctl is-active nmu nmu-caddy` |
| 库里数据（2026-08-27 只读实查） | 3 个演示受试者档案（`demo-001` 已登记撤回、`demo-002`、`Q`）、6 个场次（`data_classification` 全是 `research`）、77 条审计、0 条量表记录 | `sqlite3 …/app.db "select count(*) from patient"` 等。**没有真实入组受试者，但这台机器已经被用过**：云 TTS / ASR / LLM 都有实际使用记录，演示场次也留了录音。原来这一行写「从未被真实使用过」——那句话曾被当成「数据可以随便重建」的依据，删掉 |

最后一次只读核对：**2026-08-27 19:55 UTC**，方法见文首。实测到的：
服务 `nmu` active（02:49:43 UTC 起）；库头 `b6d4f8a2c917`；3 个演示受试者 / 6 个场次；
`preflight_check.py --require-all --os` **8 项全 PASS、退出码 0**（迁移头一致、备份新鲜度、
53 个包逐个对上锁、安全补丁积压 0、`/health` 200、安全头齐、红线全 404、受保护路由全 401/403）；
另有 5 个**非安全**更新待装（不计失败）；`daily/` 14 份，当夜 19:35 UTC 快照
`20260827-193521` ok；`capacity.state` HEALTHY（58.3%，剩 7.1 GB）；可用内存 565 MB；
`last-deploy.state` = `ec2d1b5`，与树核一致。

**异地副本链（2026-08-27 修复）**：8-26 那次按收据 224 清库重建让审计链从头开始，
锚点账里 8-07 记的 `count=12 tip` 与新链不再相等，于是**每一份后续快照**都以
`audit_anchor_check_failed / prefix_rewritten` 失败，异地副本停在 `20260825-193551`。
处置：`audit_anchor_check.py` 新增 `--reset-baseline <理由> --boundary <快照名>`，
往同一本追加账里写一条 `baseline-reset` 行（旧锚点一行未删，只是不再参与比对），
边界之前的快照只做链内校验、不做跨快照比对也不记账。重拉后
`ok snapshots=45 pulled=1`，最新异地副本 `20260827-193521`，`conflicts/` 清零。
详见 `~/Library/nmu-backup/offsite/AUDIT-ANCHOR-RESET-20260826.md`。

## last-deploy.state 已过期（2026-08-17 发现，未修）

`/opt/nmu/last-deploy.state` 的内容是：

```text
commit = 9c34dcb37724a9ae84bb843d9e537efb819d8975
at = 20260808-154014
previous_head = e4a7c1d9b206 (head)
```

而逐文件核过的部署树是 `167273f`（2026-08-15），库头是 `b3e7c5a9d214`。
**这个文件比现实旧六天，跨了至少一次上线没有被更新。**

为什么要当回事：回滚流程要"旧代码树 + 旧校验器一起放回"，而判断"旧的是哪一个"
时，机器上唯一像样的记录就是这个文件。它现在会把人指向 `9c34dcb`——那是一个
**结构更老**的版本，照它回滚会把库头和代码错配。

没有当场改，因为改生产上的文件不在只读复核的范围内。修法有两条，选哪条要 Eric
定：① 上线流程里补一步写这个文件，并在预检里加一条"文件里的 commit 必须等于
部署树实测指纹"；② 干脆废掉它，改成每次上线往部署树里写一个 `RELEASE_SHA`
（现在那个文件不存在，我这次是靠 80 个文件的指纹反查才定位到 `167273f` 的）。
**倾向 ②**：一个会过期的记录比没有记录更危险，而逐文件指纹反正才是真判据。

**不管选哪条，"真判据"这一步现在有工具了**：`scripts/verify_deployed_tree.py`。
只读、不联网、不碰生产、没有任何写调用（有测试走 AST 逐个调用点钉住这一点，
不是靠注释保证）。两步：

```bash
# 1) 打印一条只读命令，人自己拿到目标机上跑，结果存成 manifest.txt
scripts/verify_deployed_tree.py --print-remote-command --tree-root /opt/nmu/app
# 2) 回本仓库比对
scripts/verify_deployed_tree.py --manifest manifest.txt --revision 167273f
```

退出码 **0 = 完全一致 / 1 = 有漂移 / 2 = 根本没量成**。第三种单独分出来，
是因为"没量成"和"量到了没差别"看起来太像。2026-08-17 实跑的结果：

```text
167273f -> exit=0  MATCH  files=80 identical=80
9c34dcb -> exit=1  DRIFT  identical=57 differing=15 absent_in_revision=8
```

第二行就是 `last-deploy.state` 声称的版本。

预检 `--require-all` 结果：**8 项全 PASS、退出码 0**。这台机器上线以来第一次全绿——
2026-08-15 装完 8 个 systemd 安全补丁后，OS 补丁那一项从 FAIL 转 PASS。

## 迁移头前进的部署收尾三件套（缺一必出误报）

数据库结构一升级，备份校验器就只认新结构版本。所以每次带迁移的上线，收尾必须做满三件：

1. 升库（`alembic upgrade head`）
2. **重装异地拉取机上的校验器副本**（它的支持版本是写死的）
3. **手动触发一次备份**，让最新一份快照是新结构版本产出的

少最后一件，当晚的健康检查会拿新校验器去验旧结构的快照，报一个假故障。

副作用要提前知道：**升级之后，升级前拍的所有快照都不再能被新校验器背书**，
会被移进 `legacy-unvalidated/` 等人处置。它们不是坏备份，但绝不能改名当成当前合格快照。

## 待上线增量

**无。生产 = `ec2d1b5` = origin/main 的最新代码提交（2026-08-26 20:54 UTC，docs 提交除外）。**
（2026-08-21 00:5x–01:0x 两次零迁移热更新：`70f2fec` 量表定义包勘误 v2——Eric 拍板
修正 NPI-Q 五处笔误+SFACS 四处空格，定义按请求装载免重启；`7d70cce` 前端 UX——
toast 底部居中+warn 8s、安排屏未完成计划前置横幅+锚点滚动、槽位 409 人话翻译。
旧 dist 隔离进 `/opt/nmu/dist-stale-20260821-005900`，`--source-root` 复查 15 文件过，
公网 build-id 与本地一致，服务未重启、库未动。真机死点清查结论：11 按钮全有响应，
「点了没反应」真因=反馈错位——toast 在左下角而按钮在右侧。）

## 2026-08-20 晚上线记录（3ccb86d：量表电子记录原型道，含迁移 b6d4f8a2c917）

受控技术环境更新，Eric 本人执行一键窗口脚本（`项目综合审计_20260717/
PM_20260730_自动对话/219-上线命令_3ccb86d.sh`），Claude 只读预检与收尾核验。
（Claude 曾按 Eric 口头授权尝试自行执行，被 auto 模式分类器在第 2 步拦下；
服务当场恢复原状、health 200 后改走"脚本交 Eric"路径——分类器边界与三十四轮一致。）

| 步骤 | 结果 |
| --- | --- |
| 前提 | 依赖锁零变化（venv 不碰）；`verify_backup_snapshot.py` 本次有变（头钉/表清单/恢复指纹 `36c979…`）→ 窗口内拍新头快照 + 窗口后重装异地校验器 |
| 锚点 | 停 timer → 停服 → 停写快照 `20260820-105812` **ok**（旧校验器·旧头） |
| 回滚存档 | `/opt/nmu/app-before-deploy-20260820-185806.tar.gz`（排除 data/） |
| 同步 | 旧 dist 隔离进 `/opt/nmu/dist-stale-20260820-185806`；`RSYNC EXIT=0`；权限规范化后不可读 0 |
| 校验器 | 部署树与本地同为 `79aea62e…` ✓ |
| dist 绑定 | `--source-root` 15 个受管文件通过 |
| 迁移 | 前闸 **退 78**（正确的待迁移态）→ `alembic upgrade head` EXIT=0（`6f2a9c4d8e17 → b6d4f8a2c917`）→ 后闸退 0 |
| 起服 | 本地/公网 `/health` 200；`/docs` 404 |
| 新头快照 | **立刻拍**：`20260820-105854` **ok**（新校验器·新头，备份链未断）→ timer 恢复 active |
| 预检 | `--require-all` 7/8 PASS；唯一 FAIL = OS 安全补丁积压 12 个（curl/nginx 等，与本次上线无关，待 Eric 批 `apt-get upgrade`） |
| 异地 | 校验器重装为 `79aea62e…`；launchd 真跑一轮 `partial snapshots=37 pulled=1 held=8`，新头快照 `20260820-105854` 落入**已验证** `daily/`；旧头快照（含两份停写锚点）按 §8.1 进 `legacy-unvalidated/` 等运维处置——**迁移头前进后的预期形态，不是故障**；两条 08-14 旧快照 FAIL `snapshot_unresolved_evidence` 为历史遗留 |
| 收尾核验 | `verify_deployed_tree.py --revision 3ccb86d` → **MATCH files=87 identical=87**；`last-deploy.state` 已写本次事实 |

## 2026-08-20 上线记录（b1765c0：内容交付 + 快速演练修复 + UI 迭代，零迁移）

受控技术环境更新，Eric 本人执行一键窗口脚本（`项目综合审计_20260717/
PM_20260730_自动对话/218-上线命令_b1765c0.sh`），Claude 只读核验前提与收尾：

| 步骤 | 结果 |
| --- | --- |
| 前提 | `10c90e4..b1765c0` 迁移与依赖锁**零变化** → 没停定时器、没碰 venv、没拍停写快照（夜间照常）；备份链三件套脚本零变化 → 异地校验器免重装 |
| 回滚存档 | `/opt/nmu/app-before-deploy-20260820-001042.tar.gz`（排除 data/） |
| 同步 | 旧 dist 整体隔离进 `/opt/nmu/dist-stale-20260820-001042`（移走不删）→ `RSYNC_EXIT=0`；权限规范化后不可读文件 0，`.env` 600、`data/` 700 |
| 校验器 | 部署树 `verify_backup_snapshot.py` sha == 本地 `7d434d79…` ✓ |
| dist 绑定 | 首查 FAIL——**服务器 `web/src` 里躺着一个早已删除的 `SessionCreateScreen.tsx`**（历次无 `--delete` 同步的残留；以前的复查不带 `--source-root` 从没量过源指纹）。隔离进 `/opt/nmu/src-stale-20260820-001042` 后复查 **15 个受管文件通过** |
| 库头闸 | `OK database_at_head`（`6f2a9c4d8e17` 不变，数据一行没动） |
| 起服 | 本地与公网 `/health` 200；`/docs` 404；公网 `build-id` 与本地 dist 逐字符一致 |
| 预检 | `--require-all` **8/8 全 PASS 退出码 0**（含 OS 安全补丁积压 0） |
| 收尾核验 | `verify_deployed_tree.py --revision b1765c0` → **MATCH files=85 identical=85** |
| 部署记录 | `/opt/nmu/last-deploy.state` 已写本次事实（此前它还停在 08-08 的 `9c34dcb`） |
| 异地 | 手动拉取 `ok snapshots=35 pulled=1`；当日 12:31 定时拉取曾 FAIL rc=255 属 VPN 链路瞬断；held=12 为 08-18 升库前历史快照按 §8.1 等运维处置（设计如此） |

**runbook 教训（已体现在下次照做的清单里）**：不带 `--delete` 的同步会让被删除的
源文件永远留在服务器上；`verify_browser_dist.py --source-root` 是唯一能照出它们的
检查，每次上线都要带上，多出来的文件隔离进 `src-stale-<ts>`（移走不删）。

## 2026-08-19 这次上线的实际记录

按 runbook 逐条走完，**每一步的退出码都单独取过**（不经管道）：

| 步骤 | 结果 |
| --- | --- |
| §1 旧结构恢复演练 | `DRILL_EXIT=0`，快照 `20260817-193051`，头 `b3e7c5a9d214`，`/health` 200，行数 patient=1 session=1 |
| §3 停写快照（旧校验器） | `20260818-162027` `ok`；异地拉取并 `MANIFEST` 7/7 通过 = **回滚锚点** |
| §4 同步代码 | `RSYNC_EXIT=0`；部署树校验器 sha == `10c90e4` 的值 ✓ |
| §4.1 权限 | 不可读文件 0；`.env` 600；`data/` 700 |
| §4.1 dist | 9 个陈旧 bundle **移进** `/opt/nmu/dist-stale-20260818-163118`（没删），复查 14 个受管文件通过 |
| §5 依赖锁 | `167273f..10c90e4` 无变化 → **没碰 venv** |
| §6 升结构 | 升级前闸门 **78**（闸是活的）→ `ALEMBIC_EXIT=0` 三级依次升 → 升级后闸门 **0 `OK database_at_head`** |
| §7 起服 + 预检 | `/health` 本地与公网均 200；预检首轮 7 PASS 1 FAIL（**OS 补丁，与本次上线无关**）→ 装掉 `libpng16-16` 后 **8/8 PASS，退出码 0** |
| §8 新结构快照 | `20260818-163310` `ok`，快照内库头 `6f2a9c4d8e17` |
| §9 异地重装 + 拉取 | 冻结副本 pin 改为 `6f2a9c4d8e17`；新快照已验证落盘且不在 `legacy-unvalidated/` |
| §11 定时器 | 四个全部 `active`，补跑无异常 |
| 收尾核验 | `verify_deployed_tree.py` → `MATCH revision=10c90e4 files=84 identical=84` |

**数据一条没动**：patient=1、session=1、researchuser=2、auditlog=15，与升级前完全一致。

### 这次要知道的三件事

1. **历史快照批量进了 `legacy-unvalidated/`**（20 → 28 份）。结构一升级，
   新校验器就不再背书旧结构的快照。**这是设计，不是故障**；它们不是坏备份，
   但绝不能改名当成当前合格快照。
2. **回滚锚点是 `20260818-162027` + 旧代码 `167273f` + 旧校验器 `2d50ce0c…`**，
   三者必须**作为一个单元**一起放回。**绝不 `alembic downgrade`**——本次三个迁移的
   降级会 drop 表。
3. **`/opt/nmu/last-deploy.state` 仍然是错的**（还写着 `9c34dcb @ 08-08`）。
   本次上线**没有**更新它，因为怎么修还没定（见上一节）。判断"生产跑的是哪个版本"
   请用 `verify_deployed_tree.py`，不要读那个文件。

## 这次上线踩的三个坑## 这次上线踩的三个坑（下次照 runbook §4.1 走可以全避开）

1. **dist 里堆着 9 次历史构建的陈旧 bundle**。不带 `--delete` 的必然代价，
   而 `verify_browser_dist.py` 对**多出来的文件**同样 fail-closed（对的：旧缓存
   可能加载到带旧安全行为的 chunk）。51 个陈旧文件已移进
   `/opt/nmu/dist-stale-20260814-015250`，**没删**。
2. **`rsync -a` 把本机的权限位原样搬了过去**。本机一个迁移文件恰好是 `-rw-------`，
   同步后新装的 `ExecStartPre` 闸门以 `User=nmu` 身份读不到迁移图，服务起不来
   （`REJECTED code=migration_graph_unreadable`）。连同 106 个 root 用 `umask 077`
   生成的 `.pyc`，已用 `chmod o+rX` 规范化；`.env` 仍 600、`data/` 仍 700。
   **这个问题在装闸门之前就存在，只是没有东西去读它**——闸门抓到的是真问题。
3. **异地拉取脚本的告警分支是坏的**。`install-macos-offsite-pull.sh` 里
   `rc=$rc。` 少一对花括号（`c99d3ad` 修过同类的全角逗号，这是第二处全角句号）：
   `set -u` 下只要退出码不是 0/3/4，脚本就崩在 "unbound variable" 而不是发告警——
   正好是那个脚本当初要解决的"静默失败没人知道"。已修，并加了
   `tests/test_shell_script_hygiene.py` 钉住这一类（先红后绿验过）。

## 历史

| 日期 | 代码版本 | 结构版本 | 回滚存档 | 备注 |
| --- | --- | --- | --- | --- |
| 2026-08-26 22:07 UTC | `08ad898` | `b6d4f8a2c917`（未变） | `dist-stale-20260826-2205` + 上一基线 `083fa35` 重同步 | 零迁移热更新:第六堵墙——wk4 花瓶勘误借句获引擎 errata 精确豁免(未登记塌缩仍拒)+七周 78 题位全扫描进门禁+前端两拒因码上屏。**受控技术环境更新，不构成任何外部批准。** |
| 2026-08-26 21:25 UTC | `083fa35` | `b6d4f8a2c917`（未变） | 按上一基线 `075e2c6` 重同步(纯后端) | 零迁移热更新:第五堵墙——selfStart 待命不再当热麦拒启(wk4 死锁整族消除);热麦真保护(recording 标记+设备权威)原样。树核 `MATCH 89/89`;preflight 8/8 退 0。**受控技术环境更新，不构成任何外部批准。** |
| 2026-08-26 17:00 UTC | `075e2c6` | `b6d4f8a2c917`（未变） | 按上一基线 `679c0e0` 重同步(纯后端无 dist 变化) | 零迁移热更新:第四堵墙——计划投影周次闸 ==2 放宽 2..8(第 3~8 周启动按钮不再灰死)+照护员摘要/legacy worker 按周取库。树核 `MATCH 89/89`;preflight 8/8 退 0。**受控技术环境更新，不构成任何外部批准。** |
| 2026-08-26 16:20 UTC | `679c0e0` | `b6d4f8a2c917`（未变） | `dist-stale-20260826-1615` + 上一基线 `920a20c` 重同步 | 零迁移热更新:/content/item-bank 按周供数(端点 ?week= 2..8,训练台按场次周次取数)——第 3~8 周场次不再在训练台撞「题库版本不一致」fail-closed。树核 `MATCH 89/89`;preflight 8/8 退 0。**受控技术环境更新，不构成任何外部批准。** |
| 2026-08-25 07:28 UTC | `920a20c` | `b6d4f8a2c917`（未变） | `dist-stale-20260825-0725` + 上一基线 `60c2e7e` 重同步 | 零迁移热更新:自动带练启动拒因逐分支说真话(prepareServerOwnership 返回具体拒因、删麦克风 catch-all、existing_manual_evidence/patient_microphone_active 进写前拒绝清单、后端拒启文案改人话)。背景=Eric 通宵实测+agent 三轮复现两堵墙。同窗口停用 demo-admin/demo-researcher(密码曾入转录)。树核 `MATCH 89/89`;preflight 8/8 退 0。**受控技术环境更新，不构成任何外部批准。** |
| 2026-08-25 06:12 UTC | `60c2e7e` | `b6d4f8a2c917`（未变） | `dist-stale-20260825-0610` + 上一基线 `e33bb89` 重同步 | 纯前端零重启热更:编辑档案抽屉内置云处理授权入口(独立区块+单独确认+CAS)。真 Chrome 三轮验证含授权全循环。树核 `MATCH 89/89`;CI 绿。**受控技术环境更新，不构成任何外部批准。** |
| 2026-08-23 22:16 UTC | `e33bb89` | `b6d4f8a2c917`（未变） | `dist-stale-20260823-2210` + 上一基线 `edb363d` 重同步 | 零迁移热更新:同意状态反向改写伦理门 + 7 处文案交互修 + 云 ASR 空转写段列表按沉默处理(生产直调实证,原先老人一冷场即误判 asr_degraded 安全暂停)。当日生产运维:遗留暂停场中止、`.env` 加 readiness 指纹密钥(备份 `.env.before-20260823-fpkey`)、真云五项演示动线复验全 PASS(收据 220/221)。树核 `MATCH 89/89`;preflight 8/8 退 0;两提交 CI 绿。**受控技术环境更新，不构成任何外部批准。** |
| 2026-08-23 11:22 UTC | `edb363d` | `b6d4f8a2c917`（未变） | `dist-stale-20260823-110420` + 上一基线 `92040ac` 重同步 | 零迁移热更新（151 文件）：交互协议数据包 autopilot-v2（58 缺口清零，qc=draft 待书面确认）、引擎全题位化、真实场次通道（新开关+云前置）、按受试者配对/档案编辑/登记表归档、两轮可用性审计修复（含研究者端录音回执致盲根因 `sourceWseq`）。`.env` +5 变量（备份 `.env.before-20260823-111955`）。对抗审查 5 发现全处置；真 Chrome 双窗 E2E（22 题位自动推进+安全链）通过；树核 `MATCH 89/89`；preflight 8/8。**受控技术环境更新，不构成任何外部批准。** |
| 2026-08-19 00:3x | `10c90e4` | `b3e7c5a9d214` → **`6f2a9c4d8e17`**（三级） | 锚点快照 `20260818-162027` + 旧代码 `167273f` + 旧校验器 `2d50ce0c…`（三者一体） | 求助四态、研究分区披露控制与冻结发布纪元、`/research/v1/*` 绑纪元、切纪元 CLI、240 场机械容量 harness、只读部署树校验器。预检 8/8 退出码 0；部署树逐文件核过 `MATCH 84/84`；数据一条没动。**受控技术环境更新，不构成任何外部批准。** |
| 2026-08-15 06:05 | `167273f` | `b3e7c5a9d214`（未变） | `app-before-deploy-20260815-0605.tar.gz` | 无迁移的安全修（两轮对抗复核 149 agent、坐实 30 条的处置，收据 212）：授权判定改用与路由器同源的 `scope["path"]`（原来被解码出的 `#` 截断，researcher 拿到 404 而非 403）、`/research` 整个命名空间收窄（原来只盖一段，其余拼写掉回含 researcher 的兜底）、跨站顶层跳转不再能带 SameSite=Lax cookie 拉走 CSV、角色判定挪到 404/422 之前、墓碑保住分母、撤回判定改调全仓权威判据、翻页判据下推 SQL、字典宣称的两列真填。 |
| 2026-08-14 20:28 | `f813af0` | `b3e7c5a9d214`（未变） | `app-before-deploy-20260814-2030.tar.gz` | 无迁移的安全修：分页游标从"只签名"改成 AES-GCM 真加密（原来 base64 解码就是明文 patient_id）、撤回判定改调全仓权威判据、补上研究取数的限速策略与审计账本。**这些缺陷在配上 `DEIDENTIFICATION_KEY` 之前打不出来（端点一律 503），配之前必须先上这一版。** |
| 2026-08-14 09:52 | `d8d3edd` | `b3e7c5a9d214` | `app-before-deploy-20260814-015250.tar.gz` | 受控技术环境更新：照护员弧、老人端暂停、研究数据面 `/research/v1/*` 与总览屏、真机验收向导页、起服前库头闸门。**不构成任何外部批准。** |
| 2026-08-08 15:40 | `9c34dcb` | `b8e5f2a91c07` | `app-before-deploy-20260808-154014.tar.gz` | 量表注册生产入口收口 |
| 2026-07-23 02:4x | `release/curated-landing-20260722` | `f9b2d6e4a801` | `app-before-cutover-20260722.tgz` | 首次公网上线 |
