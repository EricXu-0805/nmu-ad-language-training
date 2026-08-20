# 生产状态（唯一权威记录）

> 这个仓库过去没有任何字段记录"生产上跑的是哪个版本"，导致 2026-08-09 那轮独立复核
> 在仓库里**找不到上一次上线的任何证据**，只能靠人的记忆。这份文件就是为了让那件事
> 不再发生：**每次上线必须在同一个提交里更新它**，不更新就当作没上线。
>
> 这里只记录事实，不代表任何批准。系统能不能给真实老人使用见
> `docs/handover/七道门现状表.md`。

> **最新上线（2026-08-20 18:58–18:59 上海 / 10:58 UTC）**：`b1765c0` → **`3ccb86d`**，
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
| 应用代码版本 | `3ccb86d`（2026-08-20 18:58 上线；此前 `b1765c0` 08-20 00:10、`10c90e4` 08-19） | `scripts/verify_deployed_tree.py --manifest <清单> --revision 3ccb86d` 应输出 `MATCH … identical=87`（2026-08-20 19:0x 实测通过） |
| 部署树后续同步 | 已与 `main` 一致 | `git diff 3ccb86d..main -- app web alembic` 应为空 |
| 数据库结构版本 | `b6d4f8a2c917`（2026-08-20 18:58 由 `6f2a9c4d8e17` 迁移，前闸退 78、后闸退 0） | `sqlite3 /opt/nmu/app/data/app.db "select version_num from alembic_version"` |
| 备份校验器指纹（前 20 位） | `79aea62e1a1ed4c6e01e` | `sha256sum /opt/nmu/app/scripts/verify_backup_snapshot.py`；必须与异地拉取机 `~/Library/nmu-backup/runtime/verifier.sha256` 的**第一列**一致。本次上线已重装，`shasum -c ~/Library/nmu-backup/runtime/verifier.sha256` 现在**输出 OK**（2026-08-17 之前那版第二列写的是仓库路径，仓库一往前走就报与事实无关的 FAILED，已修） |
| 回滚存档 | `/opt/nmu/app-before-deploy-20260820-185806.tar.gz`（退到 `b1765c0`）；更早按 `ls -t` 逐级回退。⚠️ `…170248-语法错疑含data勿用.tar.gz` 是失败残留已改名标记，勿用 | `ls -t /opt/nmu/app-before-deploy-*.tar.gz \| head -1` |
| 回滚锚点快照 | **跨结构**退回 `b1765c0`/`6f2a9c4d8e17` 用停写锚点 `20260820-105812`（旧头，现按 §8.1 在 `legacy-unvalidated/`），必须连**旧代码树 + 旧校验器（7d434d79…）**一起放回；同结构回退直接用最新 `daily/` 快照 |
| 起服前闸门 | 已装（`ExecStartPre` 验库头，以 `User=nmu` 身份跑） | `systemctl cat nmu.service \| grep ExecStartPre`；journal 里 `OK database_at_head` 应在 `Started` 之前 |
| 服务 | `nmu` + `nmu-caddy` 均 active | `systemctl is-active nmu nmu-caddy` |
| 库里数据 | 1 个受试者、1 个场次、0 条云语音使用记录 | 这台机器**从未被真实使用过** |

最后一次只读核对：**2026-08-17 13:40（上海时间）**，方法见文首。实测到的：
服务 `nmu` active；库头 `b3e7c5a9d214`；1 受试者 / 1 场次（这台机器仍然从未被
真实使用过）；校验器指纹 `2d50ce0cad5f7813a5c3`（与本条表格一致）；
连续三夜备份都是 `ok`（最新 `20260816-193921`）；`health.state` HEALTHY；
`capacity.state` HEALTHY（用量 51.1%，剩 8.5 GB，按 1.79 MB/日估还有十年以上）；
自动恢复演练 **2026-08-16 通过**（在 `b3e7c5a9d214` 上，`/health` 200）；
可用内存 568 MB。

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

**无。生产 = `92040ac` = origin/main（2026-08-21 01:3x 编号契约收口：中文编号建档口 422+表单拦截+存量行可见原因；含 systemctl restart；MATCH 87/87）。**
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
| 2026-08-19 00:3x | `10c90e4` | `b3e7c5a9d214` → **`6f2a9c4d8e17`**（三级） | 锚点快照 `20260818-162027` + 旧代码 `167273f` + 旧校验器 `2d50ce0c…`（三者一体） | 求助四态、研究分区披露控制与冻结发布纪元、`/research/v1/*` 绑纪元、切纪元 CLI、240 场机械容量 harness、只读部署树校验器。预检 8/8 退出码 0；部署树逐文件核过 `MATCH 84/84`；数据一条没动。**受控技术环境更新，不构成任何外部批准。** |
| 2026-08-15 06:05 | `167273f` | `b3e7c5a9d214`（未变） | `app-before-deploy-20260815-0605.tar.gz` | 无迁移的安全修（两轮对抗复核 149 agent、坐实 30 条的处置，收据 212）：授权判定改用与路由器同源的 `scope["path"]`（原来被解码出的 `#` 截断，researcher 拿到 404 而非 403）、`/research` 整个命名空间收窄（原来只盖一段，其余拼写掉回含 researcher 的兜底）、跨站顶层跳转不再能带 SameSite=Lax cookie 拉走 CSV、角色判定挪到 404/422 之前、墓碑保住分母、撤回判定改调全仓权威判据、翻页判据下推 SQL、字典宣称的两列真填。 |
| 2026-08-14 20:28 | `f813af0` | `b3e7c5a9d214`（未变） | `app-before-deploy-20260814-2030.tar.gz` | 无迁移的安全修：分页游标从"只签名"改成 AES-GCM 真加密（原来 base64 解码就是明文 patient_id）、撤回判定改调全仓权威判据、补上研究取数的限速策略与审计账本。**这些缺陷在配上 `DEIDENTIFICATION_KEY` 之前打不出来（端点一律 503），配之前必须先上这一版。** |
| 2026-08-14 09:52 | `d8d3edd` | `b3e7c5a9d214` | `app-before-deploy-20260814-015250.tar.gz` | 受控技术环境更新：照护员弧、老人端暂停、研究数据面 `/research/v1/*` 与总览屏、真机验收向导页、起服前库头闸门。**不构成任何外部批准。** |
| 2026-08-08 15:40 | `9c34dcb` | `b8e5f2a91c07` | `app-before-deploy-20260808-154014.tar.gz` | 量表注册生产入口收口 |
| 2026-07-23 02:4x | `release/curated-landing-20260722` | `f9b2d6e4a801` | `app-before-cutover-20260722.tgz` | 首次公网上线 |
