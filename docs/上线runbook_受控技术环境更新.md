# 上线 runbook：受控技术环境更新（仅限当前这台无真实数据的服务器）

> **这份 runbook 不是正式发布流程。** 它只适用于当前这台**没有任何真实受试者数据**的
> 技术验证服务器（库里 1 个虚构受试者、1 个场次、0 条云语音使用记录）。
> 正式发布路径见 `DEPLOY.md` 第 3、8、9 章（不可变镜像 + 候选数据卷 + 蓝绿切换）。
>
> **为什么要单独写这一份**：`scripts/deploy_baremetal.sh` 已在 2026-08-12 被有意掏空
> （`exit 64`），而这台机器上没有 Docker、只有 1 GiB 内存。装 Docker 会改动转发与
> iptables 规则，按 `DEPLOY.md` §9.0 自己的要求必须另开维护窗口复核 SSH 与云安全组——
> 那不是无人值守该做的事。所以这次更新走**逐条有记录的人工步骤**，而不是新造一个
> 自动化入口：新入口一定会被后来的人当成"正式发布路径"复用，一份写明边界的 runbook 不会。
>
> **待裁决**：裸机到底算不算过渡期的正式发布路径，`DEPLOY.md` §12.3（只接受不可变镜像）
> 与 `pyproject.toml`（"下限 = 两条运行路径里最低的那个"）至今互相矛盾，没有人裁决过。
> 这件事必须由负责人定，见本文末尾。

## 0. 前置条件（任一不满足就停）

- [ ] 本地工作树干净，HEAD 已推送到 origin，GitHub CI 三个作业在**同一个 SHA** 上全绿
- [ ] `scripts/ci_gate.sh` 六关全绿
- [ ] 备份链健康：`backup.log` 最后一行是 30 小时内的 `ok`；`health.state` / `capacity.state` 正常
- [ ] 异地 `conflicts/` 为空
- [ ] 目标机空闲磁盘 > (2 × 单份快照大小 + 1 GiB)，可用内存 > 400 MiB
- [ ] **已在旧结构版本上做过一次恢复演练并留证**（见第 1 节）——这是回滚锚，不做不许往下走

## 1. 先做恢复演练（在**旧**结构版本上）

这一步多数人会跳过，而它恰恰是让后面每一步都可回滚的原因。

```bash
# 演练会临时再起一个应用实例；1 GiB 内存下必须在主服务停掉的窗口里做
systemctl stop nmu nmu-backup.timer
/opt/nmu/venv/bin/python /opt/nmu/app/scripts/restore_drill.py --keep
systemctl start nmu nmu-backup.timer
curl -sS -o /dev/null -w '%{http_code}\n' https://<站点>/health   # 期望 200
```

记录并留档：旧代码版本、旧结构版本、**旧校验器的 sha256**、用的哪份快照、演练结果。
**演练任一步失败 → 不得继续。一台自己恢复不了的机器不能接受结构升级。**

## 2. 停定时器（记住：停 = 推迟，不是取消）

五个单元全部 `Persistent=true`。重新启用的那一刻，错过的那次会**立刻补跑**。
所以在重新启用之前，下面五样必须已经互相一致：

1. 部署树源码（含 `scripts/verify_backup_snapshot.py`）
2. 数据库结构版本
3. 异地拉取机上的校验器副本
4. 服务器上最新一份快照（必须由**新**结构版本产出）
5. 异地已归档的最新一份（新结构、已验证、不在 `legacy-unvalidated/`）

```bash
systemctl stop nmu-backup.timer nmu-backup-health.timer \
                nmu-capacity.timer nmu-restore-drill.timer
```

## 3. 停写快照（不可逆点标记）

```bash
systemctl stop nmu
systemctl start nmu-backup.service          # 用【旧】校验器拍最后一份
tail -3 /opt/nmu/backups/backup.log         # 必须 ok
```

在异地拉取机上确认这份已经**验证通过**落盘，再继续。

## 4. 同步代码

硬要求，一条都不能少：

- **不带 `--delete`**
- `.env` 与 `data/` 显式排除
- 同步完成后，部署树里 `scripts/verify_backup_snapshot.py` 的 sha256 必须等于本次发布
  版本上的值（否则第 8 步的快照会被判成不可验证）

```bash
rsync -a --exclude='.env' --exclude='data/' --exclude='.git/' \
      <本地仓库>/ root@<服务器>:/opt/nmu/app/
ssh root@<服务器> 'sha256sum /opt/nmu/app/scripts/verify_backup_snapshot.py'
# 与本地 `git show <发布SHA>:scripts/verify_backup_snapshot.py | shasum -a 256` 对比
```

## 5. 先查依赖锁有没有变

```bash
git diff <旧SHA>..<新SHA> -- requirements-deploy.lock.txt
```

- **没变 → 绝不碰 venv。** 这是这台机器上最危险的一步，`DEPLOY.md` §12.3 也明令禁止在
  生产机上 `pip install` 或重建 venv。
- **变了 → 停下来交回负责人。** 那意味着要在受控构建环境重出锁、SBOM 和扫描，
  不能在生产机上临场做。

## 6. 升结构版本

```bash
cd /opt/nmu/app
/opt/nmu/venv/bin/python -I scripts/check_database_head.py
# 期望退出码 78（database_revision_not_head）——这是升级前的正确状态，
# 同时证明这道闸是活的。如果它退 0，说明库已经是目标版本，不要重复升。

/opt/nmu/venv/bin/python -m alembic upgrade head
/opt/nmu/venv/bin/python -I scripts/check_database_head.py   # 期望退出码 0
```

## 7. 起服并做完整预检

```bash
systemctl start nmu
curl -sS -o /dev/null -w '%{http_code}\n' https://<站点>/health    # 200

/opt/nmu/venv/bin/python scripts/preflight_check.py --require-all \
  --db /opt/nmu/app/data/app.db \
  --backup-root /opt/nmu/backups \
  --lock /opt/nmu/app/requirements-deploy.lock.txt \
  --os --base-url https://<站点>
```

三个必须知道的用法细节：

- **必须给 `--require-all`**：不给它时，"没提供的检查"算跳过而不算失败——
  一个 flag 都不传也会绿。
- **必须用生产 venv 的解释器跑**：`--lock` 比对的是**跑这个脚本的解释器**里装了什么。
- **永远不要加 `--release`**。那一档里的"正式发布批准"是**硬编码失败**的，
  等的是具名养老院、PI、伦理/隐私、法规和运维负责人的批准。这是设计，不要去"修好"它。

**任一项 FAIL 或 SKIP → 按第 10 节回滚。**

## 8. 立刻拍一份新结构的快照

```bash
systemctl start nmu-backup.service
tail -3 /opt/nmu/backups/backup.log     # 必须 ok
```

**这一步失败 = 新校验器与新结构对不上 → 在任何定时器触发之前立刻回滚。**

## 9. 重装异地校验器，再拉一次

```bash
# 在异地拉取机（Mac）上，用本次发布版本重跑安装器
./scripts/install-macos-offsite-pull.sh
cat ~/Library/nmu-backup/runtime/verifier.sha256    # 应等于仓库里该文件的 sha256
~/Library/nmu-backup/run-pull.sh
tail -3 ~/Library/nmu-backup/offsite/pull.log       # 终态应为 ok
ls ~/Library/nmu-backup/offsite/daily | tail -1     # 应是第 8 步那份新快照
```

**要提前知道的代价**：结构一升级，升级之前拍的所有快照就不再能被新校验器背书，
会被移进 `legacy-unvalidated/`。它们不是坏备份，但**绝不能改名当成当前合格快照**。
这次升级会让现有的全部历史快照一次性进入这个状态——这是设计，不是故障。

## 10. 回滚（第 6 步之后任一失败）

1. 停 `nmu` 和所有写入者。
2. 把第 3 步那份停写快照恢复到**新目录**。
3. 把**旧代码树和旧校验器一起**放回——它们必须作为**一个单元**移动，
   否则新校验器会拒绝旧结构的快照。
4. 重启，用**旧**校验器复核旧结构版本、完整性和核心表行数。
5. **绝不执行 `alembic downgrade`**（本次涉及的迁移的降级会 drop 表）。
   **绝不**逐文件覆盖 live `data/`。
6. 保留失败现场当证据，不要清理。

## 11. 收尾

- [ ] 重新启用第 2 步停掉的四个定时器
- [ ] 观察一次夜间备份 + 一次健康检查周期
- [ ] **更新 `docs/RELEASE_STATE.md`**（代码版本、结构版本、校验器指纹、回滚存档、日期）
- [ ] 在提交信息与交接材料里如实写明：**这是受控技术环境更新，不构成任何外部批准**

## 12. 必须交回负责人的裁决

裸机到底算不算过渡期的正式发布路径？三条事实摆在这里：

1. 这台机器 1 GiB 内存，而 `DEPLOY.md` §9.0 自己要求候选容器限 384 MiB；
2. `DEPLOY.md` §12.3（唯一发布形态是不可变镜像）与 `pyproject.toml` 的注释
   （"下限 = 两条运行路径里最低的那个"）**直接矛盾且从未被裁决**；
3. Docker 首次启动会改动转发与 iptables，失手就是失联。

两个选项，二选一：

- **A**：认可裸机为过渡期正式路径 → 相应修订 `DEPLOY.md` §12.3，并补一份裸机版的
  发布合同（发布单元是什么、证据怎么绑定、回滚怎么定义）。
- **B**：维持只认不可变镜像 → 那么在养老院试点之前必须先扩容或换机器。

**开发者不能替负责人做这个二选一。** 在它被裁决之前，本 runbook 只用于
"这台没有真实数据的技术验证服务器"，不得推广为正式发布流程。
