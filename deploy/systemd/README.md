# 生产 systemd 单元（裸机形态，2026-08-06 起入库）

这四个文件是 `89.208.253.119` 上 `/etc/systemd/system/` 的版本化真相。在此之前
仓库里没有生产单元，只能靠备份快照里的副本反推裸机配置——审计 §11.5 第 7 条
「部署可证明性」说的就是这件事。

安装：

```bash
for unit in deploy/systemd/*.service deploy/systemd/*.timer; do
  install -m 644 "$unit" "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload
systemctl enable --now nmu-backup.timer nmu-backup-health.timer \
                       nmu-capacity.timer nmu-restore-drill.timer \
                       nmu-os-security.timer nmu-health.timer
```

定时任务（时间都写 UTC，因为机器时区是 UTC；括号里是上海时间）：

| 单元 | 何时 | 干什么 | 结论落在哪 |
| --- | --- | --- | --- |
| `nmu-backup` | 每天 19:30（03:30） | 快照 + 校验 + 原子发布 | `backup.log` |
| `nmu-backup-health` | 每天 20:40（04:40） | 判备份链健不健康 | `health.state` |
| `nmu-capacity` | 每天 21:10（05:10） | 占用率、备份增长、还能撑几天 | `capacity.state` |
| `nmu-restore-drill` | 每周日 21:30（周一 05:30） | 把最新快照真恢复出来并启动应用 | `restore-drill.state` |
| `nmu-os-security` | 每周一 21:50（周二 05:50） | 数 OS 安全补丁积压 | `os-security.state` |
| `nmu-health` | 每 10 分钟 | 从外面打一次公网 `/health` | 只在失败时告警 |

改完单元必须现场核对，别只看 `systemctl status`：

```bash
systemctl start nmu-backup.service
tail -3 /opt/nmu/backups/backup.log     # 运维真相在这里，不在 status 字段
```

## 与本机布局绑定的两个事实

- 虚拟环境在 `/opt/nmu/venv`，**不在** `/opt/nmu/app/.venv`。`vps-backup-daily.sh`
  的解释器回退顺序是 `PYTHON_BIN` → `$APP/.venv/bin/python` → `python3`；单元不
  显式给 `PYTHON_BIN`，就会落到系统 `python3`，而系统 python3 没有 SQLAlchemy。
  2026-07-23 上线后备份连续失败十四夜，就是这条路径。`nmu-backup.service` 因此
  必须带 `Environment=PYTHON_BIN=/opt/nmu/venv/bin/python`。
- 备份 timer 写的是 UTC。机器时区是 UTC，`OnCalendar=19:30` 等于上海 03:30。
  机器时区若改成 Asia/Shanghai，这一行要跟着改，否则备份会挪到中午。

## 失败告警（2026-08-07 起）

所有 `nmu-*.service`（含 `nmu.service`/`nmu-caddy.service` 本体）都带
`OnFailure=nmu-alert@%N.service`：单元进入 failed 时,`nmu-alert@.service` 用系统
python3 跑 `scripts/notify_ops.py --unit <单元名>`,把主机名 + journal 尾部推到
Discord(`Eric Hub > #daily`,Heartbeat 同频道)。失败才响,成功永远安静。

- webhook 只存在 `/opt/nmu/ops-alert.env`(0600,一行 `NMU_OPS_WEBHOOK=…`),不进
  仓库、不进单元正文;换频道=换这个文件,不动代码。
- Mac 侧对应物:`install-macos-offsite-pull.sh` 生成的 `run-pull.sh` 在拉取失败
  (或退出码 3=异地卷超软配额)时读 `~/Library/nmu-backup/ops-webhook.env` 发同款
  告警;该文件不存在则只写 launchd 日志。消息只报**本轮**写进 pull.log 的行
  (本轮 FAIL 条数 + 第一条 FAIL + 收尾行)。原来用 `tail -5`,2026-08-26 链断那次
  端出来的全是 8-22 起每晚一模一样的稳态 held 项,真因排在第 1 行、被挤出了消息。

### 三个曾经存在的死角（2026-08-27 修）

1. **`Restart=always` 的单元原来永远进不了 failed。** systemd 默认
   `StartLimitIntervalSec=10` / `StartLimitBurst=5`,而 `RestartSec=3` 在 10 秒窗口里
   最多落 4 次尝试——够不到 burst,单元不落 failed,`OnFailure` 一次都不会触发。
   一棵只同步了一半的树会无限重启,而 Discord 全程安静、`backup.log` 照常 ok。
   现在 `nmu.service` 与 `nmu-caddy.service` 显式写 `StartLimitIntervalSec=120` +
   `StartLimitBurst=5`。**代价**:落 failed 之后不再自动拉起——但 3 秒一循环的服务
   本来就已经不在服务了。
2. **`nmu-os-security.service` 用 `sh -c '… || true'`,退出码恒 0**,它自己那行
   OnFailure 是死的。补丁积压攒到 106 个那次就是这么攒起来的。现在保留退出码,
   `chmod` 从 `ExecStartPost` 挪进命令内部(失败时 ExecStartPost 不执行,state 权限会松)。
3. **「进程活着但站点不服务」这一整档没有任何检查。** 备份脚本直接 sqlite3 打开磁盘上
   的 app.db,服务死没死它照写 ok。新增 `nmu-health.timer`,每 10 分钟从外面打一次
   公网 `/health`,一条覆盖「返 500」「Caddy 死了」「证书过期」「磁盘满转只读」。

`tests/test_systemd_alerting.py` 把这三条钉住:每个单元必须有 OnFailure、
`Restart=always` 必须配得上 burst、单元里不许出现 `|| true`、必须存在公网探针单元。
