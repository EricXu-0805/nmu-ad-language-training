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
| `nmu-backup-health` | 每天 20:40（04:40） | 判备份链健不健康，并核对公网探针 30 分钟内成功过 | `health.state` |
| `nmu-capacity` | 每天 21:10（05:10） | 占用率、备份增长、还能撑几天 | `capacity.state` |
| `nmu-restore-drill` | 每周日 21:30（周一 05:30） | 把最新快照真恢复出来并启动应用 | `restore-drill.state` |
| `nmu-os-security` | 每周一 21:50（周二 05:50） | 数 OS 安全补丁积压 | `os-security.state` |
| `nmu-health` | 每 10 分钟，整十分（`OnCalendar=*:0/10`） | 从外面打一次公网 `/health` | 只在失败时告警 |

改完单元必须现场核对，别只看 `systemctl status`：

```bash
systemctl start nmu-backup.service
tail -3 /opt/nmu/backups/backup.log     # 运维真相在这里，不在 status 字段
systemctl list-timers --all 'nmu-*' --no-pager   # 定时器看 NEXT 列，不看 active，见文末
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
python3 跑 `scripts/notify_ops.py --unit <单元名>`,把主机名、失败原因和这一轮的
journal 推到 Discord(`Eric Hub > #daily`,Heartbeat 同频道)。失败才响,成功永远安静。

- 失败原因排在消息最前面(取前 600 字):`nmu-os-security` / `nmu-restore-drill` /
  `nmu-capacity` / `nmu-backup-health` 取各自的 state 文件,`nmu-backup` 取
  `backup.log` 最后一行,`nmu-health` 取这一轮 curl 自己的输出。2026-09-28 那次
  os-security 告警里一个待装的包都没有:结论整份重定向进了 state 文件,journal 里只剩
  systemd 自己那几行。state 文件不是这一轮写的(脚本在写结论之前就挂了),消息里会标出来。
- 日志只取失败的这一轮(`systemctl show -p InvocationID` → `journalctl
  _SYSTEMD_INVOCATION_ID=…`);拿不到 InvocationID 才退回 `-n 12`,并注明可能混有上一轮。
- 发送失败隔 30 秒再试一次;两次都失败,`nmu-alert@` 自己退非零进 failed,
  `systemctl --failed` 里看得见。
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

## 定时器：唯一入口、维护重启、怎么验收（2026-10-02 起）

2026-09-30 重启后 `nmu-health.timer` 晚于开机 5 分钟才被启动。它当时只有
`OnBootSec=5min` + `OnUnitActiveSec=10min` 两条单调触发：systemd 249 丢掉已过期的
开机截止点,`OnUnitActiveSec` 在新一次开机里又没有基准,定时器停在 `SubState=elapsed`、
没有下一次触发时间,`systemctl status` 却一直显示 `active`。公网探针就此停摆,
没有任何东西因此失败,也就没有任何告警。

- **`nmu-backup.timer` 是唯一的备份入口。** `/etc/cron.d`、`cron.daily`、`cron.hourly`、
  `cron.weekly` 里不许有 `nmu-*` 条目,手工补跑也走 `systemctl start nmu-backup.service`。
  cron 那份不带单元里的 `PYTHON_BIN`(上文十四夜那条路径),失败了也不触发 `OnFailure`。
- **维护重启不得停用定时器。** 重启前不要 `disable` / `mask` 任何 `nmu-*.timer`,也不要
  打算重启后再手工一个个起回来:它们是 `enabled` 的,开机时随 `timers.target` 自己起来。
  停用不会让任何东西失败、也没有告警,忘了恢复就是无声停摆。重启后按下一条验收。
- **验收读 `SubState` 和 `NEXT`,不读 `ActiveState`。**

```bash
systemctl list-timers --all 'nmu-*' --no-pager    # 每一行 NEXT 都要有时间,不能是 n/a
for t in $(systemctl list-unit-files --no-legend 'nmu-*.timer' | awk '{print $1}'); do
  systemctl show "$t" -p Id -p SubState -p NextElapseUSecRealtime -p UnitFileState
done    # 每个都要 SubState=waiting、NextElapseUSecRealtime 非空、UnitFileState=enabled
```

处置:被停用的 `systemctl enable --now <timer>`;停在 elapsed 的、或刚换过 `.timer` 文件的,
`systemctl daemon-reload` 之后再 `systemctl restart <timer>`。做完按上面两条命令再验一遍。

对应的自动检查:`preflight_check.py --os` 的「无 nmu-* cron 条目」查第一条;
「定时器在排程」就是上面第二段命令,被停用、被停止或停在 elapsed 的定时器都会被点名;
`nmu-backup-health` 每天带 `--probe-unit nmu-health.service` 问一次公网探针最近一次
成功是什么时候,超过 30 分钟即不健康。所有定时器只用 `OnCalendar`:单调触发晚启动一次
就再也不触发,`Persistent=true` 对它也不起作用。`tests/test_systemd_alerting.py` 钉住这两条。
