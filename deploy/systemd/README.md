# 生产 systemd 单元（裸机形态，2026-08-06 起入库）

这个目录里的 `.service`、`.timer` 和两个 `apt-daily*.timer.d/` 片段是 `89.208.253.119` 上
`/etc/systemd/system/` 的版本化真相。在此之前
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
| `nmu-backup-health` | 每天 20:40（04:40） | 判备份链健不健康，并核对公网探针 30 分钟内成功过、机外探针 30 分钟内来过 | `health.state` |
| `nmu-capacity` | 每天 21:10（05:10） | 占用率、备份增长、还能撑几天 | `capacity.state` |
| `nmu-restore-drill` | 每周日 21:30（周一 05:30） | 把最新快照真恢复出来并启动应用 | `restore-drill.state` |
| `nmu-os-security` | 每周一 21:50（周二 05:50） | 数 OS 安全补丁积压 | `os-security.state` |
| `nmu-health` | 每 10 分钟，整十分（`OnCalendar=*:0/10`） | 从外面打一次公网 `/health/ready` | 只在失败时告警 |

改完单元必须现场核对，别只看 `systemctl status`：

```bash
systemctl start nmu-backup.service
tail -3 /opt/nmu/backups/backup.log     # 运维真相在这里，不在 status 字段
systemctl list-timers --all 'nmu-*' --no-pager   # 定时器看 NEXT 列，不看 active，见文末
```

改了 `/opt/nmu/caddy/Caddyfile` 用 `systemctl reload nmu-caddy`（2026-11 入库的这版单元装上之后）：
`ExecReload` 跑 `caddy reload --config` 同一份文件，走 Caddy 的 admin 接口，进程不重启
（`MainPID` 不变），新配置载入失败时旧配置照常服务。2026-10-08 窗口里单元还没有这一行，
reload 失败，只好直接调 admin 接口。

## 与本机布局绑定的两个事实

- 虚拟环境在 `/opt/nmu/venv`，**不在** `/opt/nmu/app/.venv`。`vps-backup-daily.sh`
  的解释器回退顺序是 `PYTHON_BIN` → `$APP/.venv/bin/python` → `python3`；单元不
  显式给 `PYTHON_BIN`，就会落到系统 `python3`，而系统 python3 没有 SQLAlchemy。
  2026-07-23 上线后备份连续失败十四夜，就是这条路径。`nmu-backup.service` 因此
  必须带 `Environment=PYTHON_BIN=/opt/nmu/venv/bin/python`。
- 备份 timer 写的是 UTC。机器时区是 UTC，`OnCalendar=19:30` 等于上海 03:30。
  机器时区若改成 Asia/Shanghai，这一行要跟着改，否则备份会挪到中午。

## 系统安全补丁夜间自动装（2026-10-02 入库；装到生产要 Eric 先批准这份配置）

装机起 `APT::Periodic::Unattended-Upgrade` 是 `"0"`：包列表天天刷新，补丁一个不装，
2026-08-06 到 10-02 手装了 17 轮，周一的 `nmu-os-security` 9-28 那次就是积压了 6 个包告警。
四份文件把它改成夜里自动装，只装安全补丁，不重启：

| 仓库里 | 装到 | 管什么 |
| --- | --- | --- |
| `deploy/apt/52nmu-unattended-upgrades` | `/etc/apt/apt.conf.d/` | 打开安装；不加来源（沿用 50unattended-upgrades 的 jammy、jammy-security 与两条 ESM）；不自动重启；拉黑 `linux-`、`netplan`、`libnetplan`、`grub`、`shim` |
| `deploy/needrestart/50-nmu-list-only.conf` | `/etc/needrestart/conf.d/` | needrestart 只列不重启，交互式 `apt upgrade` 也不再默认勾上 nmu、sing-box |
| `deploy/systemd/apt-daily.timer.d/50-nmu-window.conf` | `/etc/systemd/system/apt-daily.timer.d/` | 刷新列表并预下载：每天 17:30 UTC（上海 01:30） |
| `deploy/systemd/apt-daily-upgrade.timer.d/50-nmu-window.conf` | `/etc/systemd/system/apt-daily-upgrade.timer.d/` | 安装：每天 18:00 UTC（上海 02:00）起，随机推迟至多 5 分钟；`Persistent=false`，白天开机不补装 |

两个片段在子目录里，上面那段 `deploy/systemd/*.timer` 的安装循环和上线脚本都扫不到，
只能按下面显式装。`tests/test_unattended_upgrades_config.py` 钉住这份配置的形状。

夜里会发生什么：只换盘上的文件。包自己的安装脚本会重启它自己的服务（systemd 更新会重启
systemd-networkd/resolved/journald，另有 udev、nginx、ssh），各断几秒，落在上海凌晨、
芝加哥白天；nmu、nmu-caddy、sing-box 从不被重启。所以 nmu 会一直跑着旧的
libssl/libsqlite3/zlib，直到有人重启它——这台机器没装 update-notifier，库更新不会立任何
旗子，靠下面的检查点名。会弹配置文件对话框的包（例如本机改过 `/etc/sysctl.conf` 的
procps）会被跳过，留在积压里，周一照常告警。

`os_security_check.py` 的判据（周一的 `nmu-os-security` 与 `preflight_check.py --os` 共用）：

- 安全积压 > 0 → 失败。装上这份配置之后，夜里装不了的只剩被拉黑的内核/netplan/引导链和
  会弹对话框的包：内核类走 `deploy/maintenance/kernel_netplan_window.sh` 的窗口（先跑它的
  `check` 阶段），别的手装。
- `nmu.service`、`nginx.service` 还映射着被换掉的 `.so` → 失败，原因里点名单元和库：
  挑没人练的时候 `systemctl restart nmu`（或 nginx），几秒，然后重跑检查。其余单元
  （sing-box、dbus、ssh…）只记提示，等下一个重启窗口一并换新。
- `/boot` 里有比正在跑的更新的内核 → 只提示；内核不自动装也不自动重启。
- 不是 root 跑、读不了 `/proc` → 失败，那样得出的 0 是没看。
- `--json` 多三个字段：`stale_library_units`（`{单元: [库路径]}`）、`kernel_running`、
  `kernel_newest_installed`。告警正文先列失败，再以「另注」列提示。

**维护脚本调 apt 的规矩**（夜里 18:00 起 unattended-upgrade 拿着 dpkg 锁，Eric 的窗口又常在
16:00–19:00 UTC）：每个真装包的 `apt-get` 都带 `-o DPkg::Lock::Timeout=600`，不带的会在半路
因为拿不到锁直接失败；动 apt 之前先看夜间补丁是不是正在装，正在装就停下等它结束：

```bash
case "$(systemctl show -p ActiveState --value apt-daily-upgrade.service)" in
  activating|active|deactivating) echo "夜间补丁正在装,停"; exit 1 ;;
esac
DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l \
  apt-get install -y -o DPkg::Lock::Timeout=600 <只写点名的包>
```

安装（批准之后，root；目标文件已存在的先连路径一起复制进 0700 的备份目录——两个片段
同名，平铺会互相覆盖）：

```bash
TARGETS="/etc/apt/apt.conf.d/52nmu-unattended-upgrades /etc/needrestart/conf.d/50-nmu-list-only.conf
  /etc/systemd/system/apt-daily.timer.d/50-nmu-window.conf
  /etc/systemd/system/apt-daily-upgrade.timer.d/50-nmu-window.conf"
B=/opt/nmu/os-config-before-$(date +%Y%m%d-%H%M%S); install -d -m 700 "$B"
for f in $TARGETS; do [ -e "$f" ] && cp -p --parents "$f" "$B/"; done
install -D -m 644 /opt/nmu/app/deploy/apt/52nmu-unattended-upgrades \
  /etc/apt/apt.conf.d/52nmu-unattended-upgrades
install -D -m 644 /opt/nmu/app/deploy/needrestart/50-nmu-list-only.conf \
  /etc/needrestart/conf.d/50-nmu-list-only.conf
install -D -m 644 /opt/nmu/app/deploy/systemd/apt-daily.timer.d/50-nmu-window.conf \
  /etc/systemd/system/apt-daily.timer.d/50-nmu-window.conf
install -D -m 644 /opt/nmu/app/deploy/systemd/apt-daily-upgrade.timer.d/50-nmu-window.conf \
  /etc/systemd/system/apt-daily-upgrade.timer.d/50-nmu-window.conf
systemctl daemon-reload && systemctl restart apt-daily.timer apt-daily-upgrade.timer
```

`apt-daily.timer` 保留发行版的 `Persistent=true`，restart 时若今天 17:30 已过且还没刷新过，
会立刻补跑一次刷新加预下载，不装包。当场验收：

```bash
systemctl list-timers 'apt-daily*' --no-pager   # NEXT 落在 17:30 与 18:00–18:05 UTC
apt-config dump | grep -E 'Unattended-Upgrade|Automatic-Reboot|Package-Blacklist'
unattended-upgrade --dry-run --debug 2>&1 | grep -E 'Allowed origins|Initial blacklist'
```

第二天：`/var/log/unattended-upgrades/unattended-upgrades.log` 有当晚的记录，
`/var/lib/apt/periodic/upgrade-stamp` 的时间是当晚（只有成功的一轮才会更新它；失败的一轮
单元照样是绿的，周一的积压检查是兜底），`systemctl show -p ExecMainStartTimestamp nmu`
没变（证明夜里没重启 nmu），`needrestart -b -r l` 与 `os_security_check.py --json` 看还有
谁跑在旧库上。

回滚：四个文件连路径挪进一个 0700 的 `retired-*` 目录（不删），回到只刷新列表、不装包：

```bash
TARGETS="/etc/apt/apt.conf.d/52nmu-unattended-upgrades /etc/needrestart/conf.d/50-nmu-list-only.conf
  /etc/systemd/system/apt-daily.timer.d/50-nmu-window.conf
  /etc/systemd/system/apt-daily-upgrade.timer.d/50-nmu-window.conf"
R=/opt/nmu/retired-os-config-$(date +%Y%m%d-%H%M%S); install -d -m 700 "$R"
for f in $TARGETS; do [ -e "$f" ] && mkdir -p "$R$(dirname "$f")" && mv "$f" "$R$f"; done
systemctl daemon-reload && systemctl restart apt-daily.timer apt-daily-upgrade.timer
```

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
- 原因之后跟一行「下一步」,按单元写死在 `notify_ops.py` 的 `NEXT_STEPS`:os-security 那行写明
  内核元包走 `deploy/maintenance/kernel_netplan_window.sh`(先跑 `check`);netplan/grub/shim 等
  窗口脚本装不了,挑维护窗口手装、重启后跑它的 `verify-kernel`;其余先 `apt-get -s install`
  看会不会带进前两类;绝不 `apt-get full-upgrade`。health 那行分开「站点死了」与
  「`/health` 200 而 `/health/ready` 503」(库或盘);capacity 指 `capacity_reclaim.py` 只读盘点;
  备份类指交接文档 5.1。
  没登记的单元(`nmu`、`nmu-caddy`)没有这一行。
- 标头里的主机名取 `NMU_OPS_HOST_LABEL`(`nmu-alert@.service` 里设成「NMU 生产机 box A」),
  没设才用 hostname(这台是 `green-fiber-1.localdomain`)。Mac 侧的告警不设,仍是 Mac 的 hostname。
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
   的 app.db,服务死没死它照写 ok。新增 `nmu-health.timer`,每 10 分钟从应用进程外面打一次
   公网 `/health`,覆盖「进程不应答、返 5xx」「Caddy 死了」「证书过期」。
   **「磁盘满转只读」原来看不出来**:`/health` 返回一个常量(`app/main.py` 的 `health()`),
   不碰库也不碰盘,只证明进程还在应答。2026-11 入库的单元改打 `/health/ready`:读一次库
   (SQLite 读 `sqlite_master`;`SELECT 1` 不读库文件,库写坏了照样过),
   看库目录和录音目录所在文件系统是不是只读、剩余空间低不低于 `READINESS_MIN_FREE_MB`
   (默认 2048,与 `nmu-capacity` 的 `--min-free-mb` 一致),任何一样不对返 503,正文只有
   `{"status": …}`,不说是哪一样;结论缓存 5 秒,公网打不出磁盘负载。空间低于门槛期间
   `nmu-health` 每 10 分钟失败一次、每次都告警,比 `nmu-capacity` 每天一条密得多。
   它也跑在这台机器上,机器宕了或出口断了它和它的告警一起没了,那一档归文末的异地探针。

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
成功是什么时候,超过 30 分钟即不健康;带 `--offbox-source 147.182.176.237` 数一次 nmu 访问日志里
30 分钟内有没有机外探针的 `GET /health`,一条都没有报 `offbox_probe_silent`。
六个定时器不多不少,这一项不另开定时器。所有定时器只用 `OnCalendar`:单调触发晚启动一次
就再也不触发,`Persistent=true` 对它也不起作用。`tests/test_systemd_alerting.py` 钉住这两条。

## China entry(droplet haproxy -> box A,2026-11 入库,还没装)

10-28 起国内到 box A 的 TCP 被封,11-03 起国内用户改走 `147-182-176-237.sslip.io`:解到
`openclaw-prod`(147.182.176.237,DigitalOcean 纽约,同时是 OpenClaw 的生产机),上面的 haproxy
把 443 的 TCP 原样转给 box A,TLS 仍在 box A 的 Caddy 上终止。box A 看到这条路进来的每条连接
都来自 147.182.176.237,于是:

- 按 IP 的限速、PIN 锁定把所有国内用户和扫 droplet 的扫描器算成同一个人,扫描器能把医生锁在外面;
- 上面 `nmu-backup-health --offbox-source 147.182.176.237` 数的 `GET /health` 能被国内用户喂绿。

改法:haproxy 每条连接先发一个 PROXY v2 头,box A 的 Caddy 从头里取真实客户端地址。

| 项 | 取值 |
| --- | --- |
| droplet 上 | `deploy/offbox/cn_entry/haproxy.cfg`:11-03 手装那份只多 server 行的 `send-proxy-v2` |
| box A 上 | `deploy/offbox/cn_entry/boxa-caddy-global.snippet`,加进 `/opt/nmu/caddy/Caddyfile` 的全局选项块(那份不在仓库里)。这一步是 Claude 另写的脚本,Eric 在 box A 上跑:`caddy adapt` 过了才 reload,不对就还原 |
| 谁的头算数 | 只认 147.182.176.237/32 发的头;它不带头(纽约探针、droplet 上别的直连)照旧算 droplet 的地址 |
| 别人带头 | `fallback_policy reject`:连接直接断,伪造的地址到不了 app;不带头的普通客户端不受影响 |
| 怎么验 | CI `image` 任务的 `smoke_caddy_release.py` 拿发布网关跑:允许的源带头 → app 看到头里的地址,不带头 → 看到源地址;别的源带头 → 连接断,不带头 → 照常。另用 `http_port 8081` + `auto_https disable_redirects` 的同形配置核 `servers :443` 落在 box A 的监听上 |

语义是在源码里核的:Caddy v2.11.4 与 v2.11.7 的 `modules/caddyhttp/proxyprotocol` 逐字相同。来源在
allow 里 = 有头用头、没头用对端地址;在 deny 里或落到 `fallback_policy reject` = 带头就断;默认的
`ignore` 是丢掉头里的地址、连接照常。go-proxyproto v0.12.0(本仓库 go.mod)和 v0.15.0(v2.11.7 用的)
都会让被拒的连接之后每次读都报同一个错。`timeout` 不写时 Caddy 传 0,库用自己的 10 秒(Caddy 注释
写的 5 秒不对),所以显式写 5s;超时只当作没带头,不断连接。`proxy_protocol` 必须写在 `tls` 前面:
`listener_wrappers` 里不写 `tls` 时 Caddy 把它插到最前面,PROXY 头就成了 TLS 数据。

顺序,两头都要对:

1. 先改 box A:加 snippet、reload、核过。haproxy 这时还不带头,droplet 的连接按「没带头」照常接。
2. 再在 Mac 上跑 `install_cn_entry.sh`。预检先经 droplet 自己的公网 IP 打一次 `/build-id.txt` 当基线,
   再从 droplet 直连 box A,发一个源地址是 TEST-NET-1 的 v2 头,走 TLS(验证书)打 `/build-id.txt`,
   哪个不是 200 就停,haproxy 不动;过了才换配置、`systemctl reload`(不 restart)、等 haproxy 从
   reloading 落定成 active、经 droplet 核对,不通自动还原。
3. 回滚反过来:先 `--rollback` 写回 11-03 那份,再去 box A 删 snippet。先删 snippet 的话 haproxy 还在
   发头,box A 把头当成 TLS 数据,国内入口当场全断。只有 `--rollback` 打印「现在可以去 box A 删 snippet」
   才删:文件已经是 11-03 那份时它照样 reload、核对一遍,reload 失败会把文件换回仓库那份,跟跑着的一致。

```bash
bash deploy/offbox/cn_entry/install_cn_entry.sh --plan        # 只列步骤和顺序,不连机器
bash deploy/offbox/cn_entry/install_cn_entry.sh --preflight   # 只读;box A 还没加 snippet 时停在最后一项,这是对的
bash deploy/offbox/cn_entry/install_cn_entry.sh               # 预检 → 换配置、reload、核对
bash deploy/offbox/cn_entry/install_cn_entry.sh --verify      # 只读核对
bash deploy/offbox/cn_entry/install_cn_entry.sh --rollback    # 先回滚这里,再去 box A 删 snippet
```

预检认的「11-03 那份」是 11-03 手装时记下的指令(注释行当时没记),原文就是 `install_cn_entry.sh`
里 `PRE_PROXY` 那段;比的时候去掉注释、压掉多余空白。装着的那份原样的 sha256 当时没记,预检会打印出来。

它的边界:

- box A 的 Caddyfile 不在仓库里,`servers :443` 只在站点不写 `bind`、HTTPS 端口用默认 443 时对得上;
  那份改了监听写法要重核。
- 只管 TCP。HTTP/3 走 UDP,不经过 haproxy。reload 之前建立的连接在断开之前仍不带头,照旧算 droplet
  (空闲 10 分钟会断,一直在用的可能更久;没设 `hard-stop-after`)。
- 纽约探针仍从 droplet 直连 box A、不带头,`--offbox-source 147.182.176.237` 从此只数得到它和 droplet
  上别的直连。
- `allow 147.182.176.237/32` 信的是 openclaw-prod 上的每一个进程,不只是 haproxy:OpenClaw 的 agent、
  cron,任何能在那台机器上连 box A:443 的东西都能在头里填任意源地址,绕开按 IP 的限速、PIN 锁定,
  也能把某个医生的地址锁死。改之前整台 droplet 只占一个 IP 桶;从此那台机器被拿下,这层限速就失效。
- box A 的 89.208.253.119 写在 `deploy/offbox/cn_entry/haproxy.cfg`、`install_cn_entry.sh` 和
  `tests/test_cn_entry.py` 里;droplet 的 147.182.176.237 除这三份外还写在 `boxa-caddy-global.snippet`
  和 `scripts/smoke_caddy_release.py` 里(smoke 只有 droplet 的地址)。IP 切换脚本的清单要加上。

## 异地探针(2026-10 起,跑在 OpenClaw VPS 上)

`nmu-health` 从本机打回本机的公网地址,机器宕了、机房出口断了,它和它的 `OnFailure`
告警一起没了。`deploy/offbox/` 补这一档:探针跑在 Eric 的 OpenClaw VPS(`openclaw-prod`,
DigitalOcean 纽约)。这台 NMU 机器上不装任何东西,只是每 10 分钟多收一个来自
`147.182.176.237` 的 `GET /health`(重试时最多 3 个),上线脚本的空闲检查不数它。

| 项 | 取值 |
| --- | --- |
| 在哪跑 | `openclaw-prod` 的 user crontab 一行 `4-59/10`(UTC 每小时 04/14/…/54 分,避开 watchdog 与本机探针),套 `flock -n`;文件在 `~/.openclaw/workspace/nmu-probe/`(0700) |
| 一轮怎么判 | 最多试 3 次,每次最多 15 秒,间隔 20 秒;HTTP 200 且正文是 `/health` 的应答才算通(SPA 外壳也回 200) |
| 不算 NMU 的 | 三次都不通时先探 Discord API,它也没应答 = 纽约这头的问题,只记账、不计数 |
| DNS | 名字解析失败再钉 IP(`--resolve`,Host 与 SNI 不变)探一次:钉 IP 就通记 `dns`,照样算挂,用户靠的就是这个域名 |
| 何时报 | 连续 2 轮失败报一次 DOWN 并 @Eric(10-22 分钟发现);恢复报一次 RECOVERED,带中断时长;中间不重复。近 30 天最长一次停机 4 分 51 秒,上线与重启不会触发 |
| 报到哪 | `#daily-digest`,用 `OPENCLAW_DISCORD_DAILY_WEBHOOK`(本机告警的 `/opt/nmu/ops-alert.env` 当初是从同一个值复制的),署名「NMU 异地探针」,正文写明「从纽约看」;发不出去的下一轮补发,DOWN 一直没发出去就恢复的合成一条补报 |
| 它自己停了 | OpenClaw 22:00 CT 的 Heartbeat 多一行「NMU 异地探针」:超过 25 分钟没跑 WARN,DOWN 中 ERROR,24 小时内有未到阈值的失败 WARN |

安装、核对、回滚都由 Eric 在 Mac 上跑(写 crontab、真发 Discord 消息,agent 过不了):

```bash
bash deploy/offbox/install_offbox_probe.sh --plan        # 只列步骤,不连机器
bash deploy/offbox/install_offbox_probe.sh               # 预检 → 推送 → 零消息自测 → 核频道 → 装 crontab
bash deploy/offbox/install_offbox_probe.sh --verify      # 下一个 :x4 之后,零消息核对
bash deploy/offbox/install_offbox_probe.sh --heartbeat   # 把 check_nmu_probe 插进 heartbeat.py
```

预检要求 crontab 与 `heartbeat.py` 的 sha256 等于 2026-10-02 量到的基线,不符就列差异停下,
不在别人改过的文件上动手。`--test-alert` 只有显式给了才真发一条测试消息。

维护要停站超过 10 分钟,先静默。文件里是 epoch 秒,只认未来 24 小时以内的值,不是整数
一律不认,所以忘了删也最多管 24 小时:

```bash
ssh eric@147.182.176.237 'date -u -d "+90 min" +%s > ~/.openclaw/workspace/nmu-probe/silence-until'
```

静默只压「新的 DOWN」:在窗口里开始又结束的故障一条都不发,过了窗口还挂着照报。
看状态:`ssh eric@147.182.176.237 '/usr/bin/python3 -B ~/.openclaw/workspace/nmu-probe/nmu_offbox_probe.py --status'`。
回滚:`crontab ~/.openclaw/backups/crontab.before-nmu-probe-<时间戳>`;插过 Heartbeat 的再
`cp -p heartbeat.py.before-nmu-probe-<时间戳> heartbeat.py`;最后 `gio trash ~/.openclaw/workspace/nmu-probe`。

它的边界:

- 只从纽约一个点看。美国这侧的线路问题可能误报;国内用户到南京那一段(GFW、CN2)出问题它看不到。
- 与 `openclaw-prod` 同生共死:那台机器挂了,探针和 Heartbeat 一起没了。2026-11 入库的
  `nmu-backup-health` 每天 20:40 UTC 数一次「30 分钟内有没有 147.182.176.237 的 GET /health」,
  没有就告警;一天只查一次,最晚晚一天发现,其余时间仍只能靠 22:00 CT 那条 Heartbeat 没来。
- 它打的是 `/health`,只证明进程在应答;库和盘归本机 `nmu-health` 打的 `/health/ready`。
- x-feed 在 14:00、17:30 UTC 把那台单核机器占满约 7 分钟;三次重试加两轮门槛应能吸收,
  饿死超过 10 分钟仍可能误报一次 DOWN。
- 四条告警共用一个 webhook(Heartbeat、watchdog、本机告警的副本、这个探针):轮换
  `OPENCLAW_DISCORD_DAILY_WEBHOOK` 时,`/opt/nmu/ops-alert.env` 与 Mac 的
  `~/Library/nmu-backup/ops-webhook.env` 要一起换,否则会一起哑掉。

## 国内探针(2026-11 入库,11-03 装在 openclaw-prod)

10-28 起国内到 box A 整个 IP 的 TCP 都被封了。`nmu-health`、上面的纽约探针、Heartbeat 的
`check_bwg_nodes_live` 都从美国看,一直是绿的,是代理坏了才发现。`deploy/offbox/nmu_china_probe.py`
从国内看:每小时请 Globalping 的 6 个国内探针连一次 TCP 443,状态变了才发 Discord。
11-03 装进 `openclaw-prod` 的 user crontab(:17),目标是国内入口 droplet 的 IP
`147.182.176.237`(haproxy 把 443 原样转给 box A)。加 `--https-host` 之后每轮再用同一批探针
GET 一次 `https://<域名>/build-id.txt`,报「TCP 通但 HTTPS 不通」;这个参数 2026-11 入库,
crontab 里加不加由 Eric 定。

| 项 | 取值 |
| --- | --- |
| 一轮怎么判 | 一次 Globalping 测量(ping、TCP 443、3 个包、country=CN、6 个探针),只送 `--target`;算数的探针 ≥4 个、分属 ≥2 个 ASN 才下结论:全通 up,全不通 down,其余 partial |
| HTTPS(`--https-host`) | TCP 那次建成就再测一次 http:protocol=HTTPS、GET `/build-id.txt`,`locations` 填 TCP 那次的测量 id(同一批探针);每个探针要 HTTP 200 且 `tls.authorized=true`。这一轮 TCP 是 up 才算数:全通 up,有不通的就是「TCP 通但 HTTPS 不通」,消息里写每个探针不通的原因(连不上、超时或 TLS 断了 / DNS 解析失败 / 证书没验过 / HTTP 码不是 200)和对应的可能原因 |
| 为什么不打 `/health` | PROXY protocol 上线前,经 droplet 来的请求在 box A 上都是 `147.182.176.237`;`nmu-backup-health` 的机外死人开关数的正是这个 IP 的 `GET /health`,国内探针打它会把开关喂绿。`/build-id.txt` 是静态文件,发布的空闲闸本来就不数它 |
| 何时报 | 新结论连续 2 轮(1-2 小时)才改状态;改到 down 或 partial 报一次,比上次报的更坏才 @Eric;回到 up 报一次,带中断时长;中间不重复。HTTPS 那条线同样规则、自己一份状态(`china-state.json` 的 `https`),同一轮 TCP 的消息先发 |
| 不算数的 | Globalping 回 429/4xx/5xx、连不上、90 秒没测完、算数的探针不够:记 `unevaluable`,不改状态,不据此报新状态;之前已确认、因静默没报的那条照样补报。HTTPS 那次也一样,TCP 那一轮不是 up 时 HTTPS 那条线也记 `unevaluable` |
| 额度 | 匿名每个出口 IP 每小时 250 个测试(出处写在文件头);一轮 6 个,不到 3%;加 `--https-host` 一轮 12 个,不到 5%,仍留十倍余量。文档的数没在 droplet 上量过,每轮把 `X-RateLimit-Remaining/Limit` 记进 `china.log` 的 `quota=`(HTTPS 那次是 `https_quota=`) |
| 静默与文件 | 与纽约探针同一个目录,共用 `silence-until`,上面那条静默命令两边一起管;自己的是 `china-state.json`、`china.log`(0600) |
| 升级 | 不加 `--https-host`,请求、输出、日志、状态文件与加这个参数之前逐字节一样;11-03 以来的 `china-state.json`(version 1)照常读,加上参数后的第一轮 HTTPS 正常就什么都不发 |

不联网看它会发什么,以及 Eric 批了之后从 Mac 真测一次(不发 Discord、不写状态;加
`--https-host` 用掉 12 个测试额度,只测 box A 自己的 IP 是 6 个,10-29 复测时还封着):

```bash
python3 -B deploy/offbox/nmu_china_probe.py --plan --target 147.182.176.237 --https-host 147-182-176-237.sslip.io
python3 -B deploy/offbox/nmu_china_probe.py --check --target 147.182.176.237 --https-host 147-182-176-237.sslip.io
python3 -B deploy/offbox/nmu_china_probe.py --check --target 89.208.253.119
```

`--target` 用 IP,直接回答「这个 IP 从国内连不连得上」;`--https-host` 用域名,把国内解析
sslip.io、按 SNI 过滤、droplet → box A 那一段和证书都走一遍。

11-03 装的是这样(以 droplet 上 `crontab -l` 为准):

1. `nmu_china_probe.py` 放在 `openclaw-prod` 的 `~/.openclaw/workspace/nmu-probe/`。它 import
   同目录的 `nmu_offbox_probe.py`(发 Discord、落盘、静默都用那一份),所以要用 `python3 -B` 起,
   不能加 `-I`。
2. user crontab 一行(UTC 每小时 :17,避开纽约探针的 :x4 和 watchdog 的 :00/:05)。加上
   `--https-host` 是这样:
   `17 * * * * /usr/bin/flock -n /home/eric/.openclaw/workspace/nmu-probe/.china.lock /usr/bin/python3 -B /home/eric/.openclaw/workspace/nmu-probe/nmu_china_probe.py --target 147.182.176.237 --https-host 147-182-176-237.sslip.io >> /home/eric/.openclaw/workspace/nmu-probe/china-cron.log 2>&1`。
   改这行前先把新的 `nmu_china_probe.py` 推上去(旧版不认 `--https-host`,argparse 报错退出,
   那一轮整个不测);`install_offbox_probe.sh` 不认这一行,它的 crontab 基线预检之后会对不上,要一起改。
3. 目标和域名都写在这行 crontab 里,不在仓库文件里:IP 切换脚本的清单要加上它。
4. Heartbeat 还没有对应的一行。要的话照 `check_nmu_probe` 写一个读 `china-state.json` 的;
   没有它,探针自己停了不会有任何消息。共用 `OPENCLAW_DISCORD_DAILY_WEBHOOK` 的就是五条。

它的边界:

- Globalping 的国内探针多在阿里、腾讯这类机房,家宽和手机网络只有少数几个;机房连得上不等于
  江苏电信家宽连得上。
- 不加 `--https-host` 只测 TCP 握手:droplet 的 haproxy 照样接 TCP,所以 droplet → box A 断了、
  域名被按 SNI 封了它都看不见。加了也只打 `/build-id.txt`,不打 `/health`、`/health/ready`,
  库和盘仍归本机 `nmu-health`。
- TCP 那一轮不是全通(partial)时 HTTPS 不单独判:partial 那条已经报了,但同时发生的 SNI 封锁
  要等 TCP 回到全通才会报出来。
- 靠 Globalping 这个外部服务。它挂了或改了额度,这边只会一直 `unevaluable`,不会报警;
  `--status` 里的 `unevaluable_streak` 看得见(HTTPS 那条线在第三行)。
