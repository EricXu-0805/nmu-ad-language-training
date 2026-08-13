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
| 代码版本 | `9c34dcb`（2026-08-08 15:40 上线） | 部署树里有 `web/src/console/AssessmentExecutionDrawer.tsx`、**没有** `app/caregiver_service.py` |
| 数据库结构版本 | `b8e5f2a91c07` | `sqlite3 /opt/nmu/app/data/app.db "select version_num from alembic_version"` |
| 备份校验器指纹（前 20 位） | `fd5710a787cc90c9a626` | `sha256sum /opt/nmu/app/scripts/verify_backup_snapshot.py`；必须与异地拉取机 `~/Library/nmu-backup/runtime/verifier.sha256` 一致 |
| 回滚存档 | `/opt/nmu/app-before-deploy-20260808-154014.tar.gz` | `ls -t /opt/nmu/app-before-deploy-*.tar.gz \| head -1` |
| 服务 | `nmu` + `nmu-caddy` 均 active | `systemctl is-active nmu nmu-caddy` |
| 库里数据 | 1 个受试者、1 个场次、0 条云语音使用记录 | 这台机器**从未被真实使用过** |

最后一次只读核对：**2026-08-14 02:10（上海时间）**。

## 迁移头前进的部署收尾三件套（缺一必出误报）

数据库结构一升级，备份校验器就只认新结构版本。所以每次带迁移的上线，收尾必须做满三件：

1. 升库（`alembic upgrade head`）
2. **重装异地拉取机上的校验器副本**（它的支持版本是写死的）
3. **手动触发一次备份**，让最新一份快照是新结构版本产出的

少最后一件，当晚的健康检查会拿新校验器去验旧结构的快照，报一个假故障。

副作用要提前知道：**升级之后，升级前拍的所有快照都不再能被新校验器背书**，
会被移进 `legacy-unvalidated/` 等人处置。它们不是坏备份，但绝不能改名当成当前合格快照。

## 待上线增量（截至 2026-08-14）

本地分支 `feature/phase1-flow-and-evidence-20260727` 比生产多 8+ 个提交，
数据库结构从 `b8e5f2a91c07` 前进到 `b3e7c5a9d214`（两支迁移）。主要内容：
老人端一键暂停、照护员工作台与 20 题演练、自动驾驶接管就绪契约、发布证据门禁。

**这些尚未上线。** 上线记录写在下面的历史表里。

## 历史

| 日期 | 代码版本 | 结构版本 | 回滚存档 | 备注 |
| --- | --- | --- | --- | --- |
| 2026-08-08 15:40 | `9c34dcb` | `b8e5f2a91c07` | `app-before-deploy-20260808-154014.tar.gz` | 量表注册生产入口收口 |
| 2026-07-23 02:4x | `release/curated-landing-20260722` | `f9b2d6e4a801` | `app-before-cutover-20260722.tgz` | 首次公网上线 |
