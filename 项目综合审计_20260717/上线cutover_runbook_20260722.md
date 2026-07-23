# curated 分支生产上线 · cutover runbook（2026-07-22）

> **✅ 已执行 2026-07-23 02:4x UTC** — SSH=`root@89.208.253.119`(id_ed25519)。迁移 `b2d5f8c31e07→f9b2d6e4a801`，验收全绿（health/红线404/安全头/D2标记/端口未动）。详见 独立复核文档 §9.5。唯一待补：D2 真机点穿（并入 Eric 真机验收）。

分支 `release/curated-landing-20260722`（HEAD 见 git）。裸机 systemd + 宿主 Caddy 路线。
**硬红线**：绝不碰 `nginx:80`（clash）与 `sing-box:24697`；rsync 一律 `--exclude=.env`；只操作 nmu 应用与其 Caddy loopback。

## 已完成的去风险（本机，未碰线上）
- 迁移实测：对**真实 prod 快照** `data/vps-backups/daily/20260717-152932/app.db`（head `b2d5f8c31e07`）的副本跑 `alembic upgrade head` → **干净升到 `f9b2d6e4a801`**。
- 数据风险≈0：prod `patient=0 / session=0 / audio=0`，仅 2 个账号（丁老师/eric）+ 少量 auth/audit 行。
- 前端 build 干净（167 模块）、后端 pytest exit0、前端 31/31 & 357/357。

## 需要 Eric 提供
1. **部署 SSH 访问**：部署用的 `用户@主机` + 对应私钥文件名（不是 backup 那个受限用户）。候选私钥：`id_ed25519` / `opened-prod-server.pem` / `id_rsa`。
2. **放开 harness 权限**：允许我对 `89.208.253.119` 跑 ssh/rsync（加 Bash 允许规则，或逐条批）。

## Step 0 · 只读预检（拿到 SSH 后先跑，不写任何东西）
- `whoami; hostname; systemctl is-active <nmu 单元名>`（确认应用单元名，DEPLOY/记忆记作 `nmu`/`nmu.service`）
- 确认应用路径（预期 `/opt/nmu/app`）、venv（`/opt/nmu/app/.venv`）、前端静态目录、DATABASE_URL/.env 位置
- `ss -ltnp` 确认 `:80`(clash) 与 `:24697`(sing-box) 在跑 → **仅记录，绝不动**；确认应用在 `127.0.0.1:8000`、Caddy 在 `:443`
- `df -h`（磁盘余量）、`git -C /opt/nmu/app rev-parse HEAD`（现网 ref，应≈4a72454）、`free -m`
- 确认部署机制：box 是 git clone（则 fetch+checkout）还是 rsync 落地

## Step 1 · 新鲜备份 + 验证（发布门禁）
- 触发一次每日备份：`sudo systemctl start nmu-backup`（或手动 sqlite `.backup`）
- `python3 -I scripts/verify_backup_snapshot.py verify-vps /opt/nmu/backups/daily/<最新>`（须只接受当前 head、manifest 全覆盖、SQLite/FK/DB↔audio 闭包通过）
- 看 `/opt/nmu/backups/backup.log` 审计行=成功；**没有一份验过的新备份就不往下走**

## Step 2 · 进维护窗
- `sudo systemctl stop <nmu 单元名>`（单机短暂停机，可接受；此时前端 502/维护页）

## Step 3 · 落代码 + 前端
- 代码：box 若 git → `git fetch && git checkout release/curated-landing-20260722`；否则本机 `rsync -a --exclude=.env --exclude=data/ --exclude=.venv --exclude=.git <local>/platform/ <user>@host:/opt/nmu/app/`
- 前端：本机 `cd web && npm ci && npm run build` → `rsync -a web/dist/ <user>@host:<应用静态目录>/`（1GiB 内存不在 box 上 build）
- 依赖：venv 内 `pip install -r requirements.txt`（RC 可能加了依赖，比对 lock）

## Step 4 · 迁移（已证安全）
- venv + prod DATABASE_URL：`alembic current`（应 `b2d5f8c31e07`）→ `alembic upgrade head` → `alembic current`（应 `f9b2d6e4a801`）

## Step 5 · 起服务
- `sudo systemctl start <nmu 单元名>`；`systemctl is-active` = active

## Step 6 · 验收（全绿才算上线）
- `curl -s https://89-208-253-119.sslip.io/health` → 200（**清 6 个代理变量 + NO_PROXY='*'**）
- 红线探针：`/content/item_bank_v1.json`、`/docs`、`/openapi.json` 全 **404**；安全头 CSP/HSTS/X-Frame-DENY/nosniff/`microphone=(self)` 齐全
- 迁移头：线上 DB = `f9b2d6e4a801`
- 登录：丁老师/eric 或 PIN 可登
- **D2 真机点穿**（现在可在线上验）：登录 → 准备区 → 切「快速演练」→ 建模拟档案 → 一键演练开训 → 落床旁；再验小语苏瑶声 + 按住返回
- 不碰 nginx:80 / sing-box:24697（复核仍在跑）

## Step 7 · 回滚（任一步失败）
- `sudo systemctl stop <nmu 单元名>` → 从 Step1 的新备份恢复 DB → `git checkout 4a72454`（或还原上一次 rsync 树）→ 重启
- 因无真实数据，回滚代价≈账号 2 行，可重建；不留脏状态

## D2 缺口提醒
本机只验到 tsc + 单测 + 后端契约；**真机浏览器点穿在 Step 6 补齐**。
