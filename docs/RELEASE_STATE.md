# 生产状态（唯一权威记录）

> 这个仓库过去没有任何字段记录"生产上跑的是哪个版本"，导致 2026-08-09 那轮独立复核
> 在仓库里**找不到上一次上线的任何证据**，只能靠人的记忆。这份文件就是为了让那件事
> 不再发生：**每次上线必须在同一个提交里更新它**，不更新就当作没上线。
>
> 这里只记录事实，不代表任何批准。系统能不能给真实老人使用见
> `docs/handover/七道门现状表.md`。

> **2026-08-31 23:3x UTC 已上线 `eb6dcd1`（含迁移 `c8e5a1f3b209` → `d0c22a6dae2a`：只追加
> 发声账本 `rapportutteranceevent` + `ttsserveevidence` 扩来源/utterance_id）。**
> 部署树 `MATCH revision=eb6dcd1 files=90 identical=90`；预检 7/8（唯一 FAIL = bzip2 系
> 4 个安全补丁，走收据 237）。新头快照 `20260831-233817` ok 且已验证归档异地 `daily/`；
> 异地校验器已重装（`c27cd173…`）；旧头快照按 §8.1 进 `legacy-unvalidated/` 等人处置。
> 云 TTS 语速终态 = **1.0**（Eric 拍板；`.env` 已改、备份 `.env.before-rate-flip-20260831-234554`，
> 重启生效 + 1.0 预合成核验 + bzip2 补丁 = 收据 237 交 Eric 执行）。执行记录见收据 236 §七。
>
> **2026-08-31 02:2x UTC 已上线 `a986f54`（零迁移；库头仍 `c8e5a1f3b209`）。**
> 部署树 `MATCH revision=a986f54 files=89 identical=89`；预检 8/8 退出码 0。
> 同一窗口把云 TTS 语速从 0.9 改成 1.0（生产 `.env` 的 `TTS_CLOUD_RATE`，就地改、
> 没有留新的明文副本）。**缓存键带语速，1290 句已按新语速预合成完毕、0 句失败**；
> 旧 0.9 那一套仍留在盘上，改回去是改 `.env` 一行即时生效，不用重跑。执行记录见收据 235。
>
> **2026-08-30 03:2x–03:5x UTC 已上线 `0755e09`（含迁移 `b6d4f8a2c917` → `c8e5a1f3b209`）。**
> 部署树逐文件比对 `MATCH revision=0755e09 files=89 identical=89`；预检 8/8 退出码 0；
> 新头快照 `20260830-034733` ok；异地校验器已重装（指纹 `118233d4…`），锚点
> `20260829-231747` 与新头快照都在 `offsite/daily`，conflicts 0。
> **CONSOLE_PIN 已轮换成 10 位**——所有旧配对码作废，每位受试者要重新发码。
> 顺带装了 10 个 OS 补丁（安全积压归零）。执行记录见收据 230/232。

## 当前生产

| 项 | 值 | 怎么核 |
| --- | --- | --- |
| 应用代码版本 | `eb6dcd1`（2026-08-31 23:3x UTC 上线；此前依次 `a986f54` → `74532be` → `0755e09` → `ec2d1b5`） | `scripts/verify_deployed_tree.py --manifest <清单> --revision eb6dcd1` 应输出 `MATCH … identical=90`（2026-08-31 23:4x UTC 实测通过） |
| 部署树后续同步 | 已与 `main` 一致 | `git diff eb6dcd1..main -- app web alembic` 应为空 |
| 云 TTS 语速 | `TTS_CLOUD_RATE=1.0`（2026-08-31 23:4x UTC Eric 拍板终态；`.env` 已改，重启生效走收据 237；0.9/1.0 两套缓存都留盘、可即切） | `grep ^TTS_CLOUD_RATE= /opt/nmu/app/.env`；缓存键带语速，改完必须重跑 `scripts/presynthesize_tts.py`，否则每句新话术都要现场云调用 |
| 数据库结构版本 | `d0c22a6dae2a`（2026-08-31 23:38 由 `c8e5a1f3b209` 迁移，前闸退 78、后闸退 0） | `sqlite3 /opt/nmu/app/data/app.db "select version_num from alembic_version"` |
| 备份校验器指纹（前 20 位） | `c27cd1731aed7bf35c1b`（2026-08-31 随头前进；异地已重装并核对一致） | `sha256sum /opt/nmu/app/scripts/verify_backup_snapshot.py`；必须与异地拉取机 `~/Library/nmu-backup/runtime/verifier.sha256` 的**第一列**一致。本次上线已重装，`shasum -c ~/Library/nmu-backup/runtime/verifier.sha256` 现在**输出 OK**（2026-08-17 之前那版第二列写的是仓库路径，仓库一往前走就报与事实无关的 FAILED，已修） |
| 回滚存档 | 最新一份 = `app-before-deploy-20260831-183746.tar.gz`（对应 `a986f54`，库头 `c8e5a1f3b209`——比当前库头落后一次迁移，直接换上去起不来服务）。**同结构回滚 = 从仓库按上一基线 sha 重同步，不是解 tar。** 用 tar 之前必须先核：解开后在那棵树上跑 `python -I scripts/check_database_head.py`，退 0 才准换上。⚠️ `…170248-语法错疑含data勿用.tar.gz` 是失败残留已改名标记，勿用 | 解开到临时目录后 `cd <解开处> && /opt/nmu/venv/bin/python -I scripts/check_database_head.py` |
| 回滚锚点快照 | **退回 `a986f54`/`c8e5a1f3b209`** 用 2026-08-31 上线前的停写锚点 **`20260831-233749`**（旧头、旧校验器 `118233d4…` 产出，VPS 侧在档；异地侧新校验器不背书旧头，按 §8.1 处置时配旧校验器用），必须连**旧代码树 + 旧校验器**一起放回；同结构回退直接用最新 `daily/` 快照 |
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

（无。）

## 2026-08-31 晚上线记录（`eb6dcd1`：第1周互动态 + 发声账本 + failure_stage，含迁移 `c8e5a1f3b209` → `d0c22a6dae2a`）

**2026-08-31 完成，当晚窗口 `20260831-183746` 上线（Eric 执行 17 步窗口脚本，全过；收据 236 §七）。**
含迁移 `c8e5a1f3b209` → `d0c22a6dae2a`（新表 `rapportutteranceevent` 只追加发声账本；
`ttsserveevidence` 扩 source 闭集 + utterance_id）。要点：
- 老人答完（录音落账）→ 云 ASR → qwen-plus 现编一句 → 服务端持久行 → 苏瑶嗓子读出。
  受与第 2–8 周同一套逐受试者云授权门禁；ASR 听不清 / 老人没说话 / LLM 不可用 /
  未授权云处理，四级各自落回冻结句库（j1/j2/k1），老人永远有回应。
- LLM 现编句**不在**云 TTS 静态白名单：发声只走「按持久行合成」的专用设备端点
  （`/sessions/{sid}/rapport/utterances/{id}/tts`），客户端结构上递不进任何文本。
  这是对白名单红线的一次显式放宽，边界与理由写在
  `docs/handover/第一周AI自由对话_方案与待决.md` §七。
- 机器人在回应拍说的每一句（含手点句库/脚本句）都会落 `rapportutteranceevent`——
  五十九轮那个「说了哪句无法还原」的缺口在这次关闭。
- 上线走窗口脚本（收据 236）：停服 → 旧校验器停写锚点快照 → rsync（不带
  --delete、排除 .env/data、迁移文件补 644）→ alembic upgrade → 起服 →
  preflight --require-all → 新头快照 → Mac 侧重装异地校验器 → 重跑 presynthesize
  （0.9 补跑 1289 句、1 句失败；语速随后拍板终态 1.0，该失败就地失效）。
- 新环境变量（可选）：`RAPPORT_REPLY`（auto/qwen/off，默认 auto）、
  `RAPPORT_REPLY_MODEL`（默认 qwen-plus）。不配置也能跑：一律落回句库。
- 四面对抗复核（22 条发现）已全部处置：自动回应收紧为**节级**（自我介绍节
  整节只可手点——录音只绑到节，防身份问录音冒名进云）；llm 行发声时刻在
  subject 围栏内重查逐人云授权（撤销后落回本地 piper）；provider 窗口后的
  落账围栏（撤回/隔离/并发幂等三查）；分析后台 TTS 证据契约两端同步
  （utterance_id 键 + rapport_utterance 来源，否则全站证据面板拒收）。
- **上线后提醒**：仍开着的旧老人端页面会拒收带新键的 rapportStep（严格解析
  器特性）——让在场老人端刷新一次即可。

（上一状态：生产 = `a986f54`，2026-08-31 02:2x UTC 上线。）
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
| 2026-08-31 23:3x UTC | `eb6dcd1` | `d0c22a6dae2a`（由 `c8e5a1f3b209` 前进） | 锚点 `20260831-233749`（旧校验器）+ tar `app-before-deploy-20260831-183746.tar.gz` | 第1周互动态:老人答完→云 ASR→qwen-plus 现编一句→按持久行端点发声(白名单一条窄缝,客户端递不进文本)+四级降级梯全落冻结句库+机器人每句落只追加发声账本+ACK failure_stage(8值闭集)。窗口 17 步全过;preflight 7/8(bzip2×4→收据 237);树核 MATCH 90/90;新头快照 20260831-233817 已验证异地归档;语速终态 1.0。**受控技术环境更新，不构成任何外部批准。** |
| 2026-08-31 02:2x UTC | `a986f54` | `c8e5a1f3b209`（未变） | `dist-stale-20260830-212455` + 上一基线 `74532be` 重同步 | 零迁移热更新:第 1 周一问两拍(老人答完机器人自己开口,此前那句只印在研究者屏上让人代说)+ 开放式回应库 28 句 7 组(研究者点场景、组内轮换;姓名/年龄两问服务端拒绝走这条路)+ 云 TTS 语速 0.9→1.0(1290 句重跑预合成 0 失败)。真 Chrome 走查 21/21;树核 `MATCH 89/89`;preflight 8/8 退 0;CI 五 job 绿。⚠️ 机器人说了哪一句尚未落进任何持久记录,第 1 周会话不再可完整还原——待钱凯拍板(收据 235 §4)。**受控技术环境更新，不构成任何外部批准。** |
| 2026-08-30 03:5x UTC | `74532be` | `c8e5a1f3b209`（未变） | 上一基线 `0755e09` 重同步 | 零迁移热更新:同期别重测回归修复(phase_ordinal 递增 + 取代指针;此前唯一约束加了但序号恒为 1,手册明写的「锁完发现错了就新建一条」直接撞 500)+ 导出包量表两列。树核 `MATCH 89/89`;preflight 8/8 退 0;CI 五 job 绿。**受控技术环境更新，不构成任何外部批准。** |
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
