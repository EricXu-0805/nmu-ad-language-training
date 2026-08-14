# 生产状态（唯一权威记录）

> 这个仓库过去没有任何字段记录"生产上跑的是哪个版本"，导致 2026-08-09 那轮独立复核
> 在仓库里**找不到上一次上线的任何证据**，只能靠人的记忆。这份文件就是为了让那件事
> 不再发生：**每次上线必须在同一个提交里更新它**，不更新就当作没上线。
>
> 这里只记录事实，不代表任何批准。系统能不能给真实老人使用见
> `docs/handover/七道门现状表.md`。

## 当前生产

| 项 | 值 | 怎么核 |
| --- | --- | --- |
| 应用代码版本 | `167273f`（2026-08-15 06:05 上线；此前 `f813af0` 08-14 20:28、`d8d3edd` 08-14 09:52） | 部署树里有 `app/research_read.py`、`scripts/content_gap_workbook.py`、`web/dist` 里有 `AcceptanceScreen-*.js` |
| 部署树后续同步 | 已与 `main` 一致 | `git diff f813af0..main -- app web alembic` 应为空 |
| 数据库结构版本 | `b3e7c5a9d214` | `sqlite3 /opt/nmu/app/data/app.db "select version_num from alembic_version"` |
| 备份校验器指纹（前 20 位） | `2d50ce0cad5f7813a5c3` | `sha256sum /opt/nmu/app/scripts/verify_backup_snapshot.py`；必须与异地拉取机 `~/Library/nmu-backup/runtime/verifier.sha256` 一致 |
| 回滚存档 | `/opt/nmu/app-before-deploy-20260815-0605.tar.gz`（退到 `f813af0`）；更早的存档按 `ls -t` 逐级回退，跨结构退回 `9c34dcb` 用 `app-before-deploy-20260814-015250.tar.gz` | `ls -t /opt/nmu/app-before-deploy-*.tar.gz \| head -1` |
| 回滚锚点快照 | 退到 `f813af0`：`20260814-220517`（同结构，新校验器验过，直接可用）。**跨结构**退回 `9c34dcb` 才用 `20260814-015251`（旧结构，现在 `legacy-unvalidated/`），那时要连**旧代码树 + 旧校验器**一起放回 |
| 起服前闸门 | 已装（`ExecStartPre` 验库头，以 `User=nmu` 身份跑） | `systemctl cat nmu.service \| grep ExecStartPre`；journal 里 `OK database_at_head` 应在 `Started` 之前 |
| 服务 | `nmu` + `nmu-caddy` 均 active | `systemctl is-active nmu nmu-caddy` |
| 库里数据 | 1 个受试者、1 个场次、0 条云语音使用记录 | 这台机器**从未被真实使用过** |

最后一次只读核对：**2026-08-15 06:10（上海时间）**。

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

无。`main` 与生产部署树一致。

## 这次上线踩的三个坑（下次照 runbook §4.1 走可以全避开）

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
| 2026-08-15 06:05 | `167273f` | `b3e7c5a9d214`（未变） | `app-before-deploy-20260815-0605.tar.gz` | 无迁移的安全修（两轮对抗复核 149 agent、坐实 30 条的处置，收据 212）：授权判定改用与路由器同源的 `scope["path"]`（原来被解码出的 `#` 截断，researcher 拿到 404 而非 403）、`/research` 整个命名空间收窄（原来只盖一段，其余拼写掉回含 researcher 的兜底）、跨站顶层跳转不再能带 SameSite=Lax cookie 拉走 CSV、角色判定挪到 404/422 之前、墓碑保住分母、撤回判定改调全仓权威判据、翻页判据下推 SQL、字典宣称的两列真填。 |
| 2026-08-14 20:28 | `f813af0` | `b3e7c5a9d214`（未变） | `app-before-deploy-20260814-2030.tar.gz` | 无迁移的安全修：分页游标从"只签名"改成 AES-GCM 真加密（原来 base64 解码就是明文 patient_id）、撤回判定改调全仓权威判据、补上研究取数的限速策略与审计账本。**这些缺陷在配上 `DEIDENTIFICATION_KEY` 之前打不出来（端点一律 503），配之前必须先上这一版。** |
| 2026-08-14 09:52 | `d8d3edd` | `b3e7c5a9d214` | `app-before-deploy-20260814-015250.tar.gz` | 受控技术环境更新：照护员弧、老人端暂停、研究数据面 `/research/v1/*` 与总览屏、真机验收向导页、起服前库头闸门。**不构成任何外部批准。** |
| 2026-08-08 15:40 | `9c34dcb` | `b8e5f2a91c07` | `app-before-deploy-20260808-154014.tar.gz` | 量表注册生产入口收口 |
| 2026-07-23 02:4x | `release/curated-landing-20260722` | `f9b2d6e4a801` | `app-before-cutover-20260722.tgz` | 首次公网上线 |
