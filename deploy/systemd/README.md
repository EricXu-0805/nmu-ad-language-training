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
                       nmu-capacity.timer nmu-restore-drill.timer
```

四个定时任务（时间都写 UTC，因为机器时区是 UTC；括号里是上海时间）：

| 单元 | 何时 | 干什么 | 结论落在哪 |
| --- | --- | --- | --- |
| `nmu-backup` | 每天 19:30（03:30） | 快照 + 校验 + 原子发布 | `backup.log` |
| `nmu-backup-health` | 每天 20:40（04:40） | 判备份链健不健康 | `health.state` |
| `nmu-capacity` | 每天 21:10（05:10） | 占用率、备份增长、还能撑几天 | `capacity.state` |
| `nmu-restore-drill` | 每周日 21:30（周一 05:30） | 把最新快照真恢复出来并启动应用 | `restore-drill.state` |

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
  告警;该文件不存在则只写 launchd 日志。
