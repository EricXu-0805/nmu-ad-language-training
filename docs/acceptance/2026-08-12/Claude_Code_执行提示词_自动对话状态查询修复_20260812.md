# Claude Code 执行提示词：自动对话状态查询修复

> **状态：已于 2026-08-12 执行完成，请勿重跑。** 本文件保留为当时的精确执行合同。修复在本文定稿时仍是本机未提交改动；当前验收状态请看同目录的验收计划及两份本机状态说明。
> Claude 当时只处理本合同允许的 5 个功能与测试文件。其后另有独立批次补充连续接口测试，并为网页播放状态机抽出可测试的浏览器适配层、增加 6 项测试。后续批次不属于原 Claude 合同，原合同不得重跑。

## 先读

这是一个很窄的本机修复。不重构自动对话，不加 VAD，不改医疗/研究门禁，不启动服务，任务工具不连外网，不读真实数据。Claude Code 自身的最小控制面例外见下文。

仓库：

`/Users/xiaogangxu/Downloads/南医大语言沟通训练阿尔茨海默病项目/platform`

基线：

`feature/phase1-flow-and-evidence-20260727`

`c77069dad0a2c836dfdedaf66bfab923cff855dc`

## 额度和停止规则

1. 先执行 `claude auth status`。如果未登录，立即停止，只报告需要用户在本机完成登录；不索要、不读取、不传输密钥。
2. 登录后，在 Claude Code 交互会话里先打开 `/usage`。如果不能得到当前可信用量，不开始修改。
3. 对 `/usage` 显示的每个当前适用限额窗口，按“剩余 = 100% - 已用”计算，以最小剩余值为准。最小剩余不足 5% 时不开始；任何新的模型回合开始前若已不高于 4%，立即停止，为最终交接保留高于 2% 的缓冲。
4. 只用一个 Claude 会话，不开子代理，不做宽泛全库审查。

必须在以下四个时点由监督者运行 `/usage`：开始前、首次修改前、运行测试前、最终回执前。任一次无法得到可信用量，就停止并交接；不得凭估计继续。

## 强制边界

- 将 `platform/data/**` 整个目录视为不可触碰区：不得读取、打开、列出、遍历、查看元数据、统计、哈希、备份或搜索其中任何文件，包括 `app.db` 及所有 sidecar。
- 不启动后端、前端、数据库或任何长驻服务。
- 网络唯一例外是 Claude Code 本身的登录、`/usage` 查询和这一个模型会话所需的 Anthropic 控制面。任务工具不得访问任何网络；不得访问项目云端 provider、部署、生产、医院或真实患者数据。如果无法把这两类网络权限隔离，立即停止。
- 不改 migration、数据库 schema、内容题库、默认 60 个内容缺口的启动门禁。
- 不把 ASR 空文本写成“声学静音检测”；本任务不新增 VAD。
- 不 commit、不 push、不 deploy。
- 发现 HEAD 不是上述基线、下述五个允许文件已有不明修改、或修复需要超出这五个文件时，停止并报告。不得用全库 `git status`、`find` 或类似命令扫描 `data/**`。

## 已复现的问题（执行前）

在完全隔离的模拟 API 流程中，录音 `record_stopped` 后状态合法进入 `processing_attempt`，同时保留已成功的 `record` 命令。此时账号端调用：

`GET /sessions/{session_id}/autopilot/status`

执行前的错误返回：

`409 autopilot_state_invalid / 自动驾驶状态与当前命令不一致`

原因已锁定：

- `app/models.py` 的数据约束明确要求 `processing_attempt` 和 `manual_draining` 保留 `current_command_id`。
- `app/autopilot_ledger.py` 的状态语义要求它们对应 `record` 命令。
- `app/autopilot_service.py::get_autopilot_status` 却只允许 `waiting_recording` 带 `record`，将其他状态一律要求为 `None`。
- `web/src/autopilot/startControl.ts::parseAutopilotStatusReceipt` 复制了同一个错误规则。

## 允许修改的范围

仅允许根据实际需要修改：

- `app/autopilot_service.py`
- `tests/test_autopilot_service.py`
- `tests/test_autopilot_api.py`
- `web/src/autopilot/startControl.ts`
- `web/src/autopilot/startControl.test.ts`

不是每个文件都必须改；只保留证明行为所需的最小差异。

开工前只用下列方式核对基线，不遍历 `data/**`：

```text
git rev-parse HEAD
git diff --name-only -- . ':(exclude)data' ':(exclude)data/**'
git diff --cached --name-only -- . ':(exclude)data' ':(exclude)data/**'
git status --short --untracked-files=normal -- . ':(exclude)data' ':(exclude)data/**'
```

HEAD 必须精确匹配基线；两条 `git diff --name-only` 和排除 `data/**` 后的全仓 status 必须为空。该 pathspec 不得下探、列出或读取 `data/**`。若输出非空，立即停止，不自行删除、恢复或覆盖用户文件。

## 必须实现的语义

后端和前端共享前两列的 status↔kind 对应；第三列的命令生命周期只由持有完整 RuntimeCommand 的后端校验：

| 服务器状态 | 前后端共享的当前命令类型 | 仅后端校验的合法命令生命周期 |
|---|---|---|
| `waiting_tts` | `tts` | `pending` / `started` |
| `waiting_recording` | `record` | `pending` / `started` |
| `processing_attempt` | `record` | `succeeded` |
| `manual_draining` | `record` | `pending` / `started` |
| 其他所有合法状态 | `null` / `None` | 无当前命令 |

仍要 fail-closed：任何错误 kind、错误 command state、错误 session、错误 scope/generation、缺失必要命令或本应为空却带命令，都必须拒绝。

同时保持前后端已有的所有权语义：

- `autonomous` 所有权持续穿越 `processing_attempt`，不得因本修复解锁人工控制或推进计划。
- `manual_draining` 是 `autonomous + server_owned=true + record(pending/started)` 的安全收麦持久态，不是已释放的 `manual` 状态。
- `autonomous + idle` 必须拒绝。
- `manual` 只允许 `paused` / `scope_completed` / `failed`，且当前命令必须为空。
- 现有服务测试中把 `waiting_tts` 直接改成 `manual` 后仍接受的断言，与安全释放契约冲突；应改为断言该组合被拒绝，不得为保住旧测试而放宽前端。

校验责任不得混淆：

- 后端持有完整 RuntimeCommand，负责校验 command state、session、scope、control generation、runner generation 和 current id。
- 前端的最小 status receipt 不含上述完整字段，只负责校验回执形状、mode/server-owned 和 status↔kind；不得声称它在本地重做了后端命令证明。

## 回归测试

至少要证明：

1. 完整走到 `record_stopped -> processing_attempt`后，账号端 status 返回 200，状态为 `processing_attempt`，命令类型为 `record`，不泄露命令载荷。
2. 后端会接受合法的 `manual_draining + record(pending/started)`，并继续拒绝 `manual_draining + tts/null/succeeded`。`manual_draining` 目前没有已确认的生产写入者，所以只测合法持久态契约，不得宣称该路径已可执行。
3. 后端拒绝 `processing_attempt + tts/null`，也拒绝 `processing_attempt + record(pending/started)`，只接受已完成录音命令的 `record(succeeded)`。
4. 前端会接受 `processing_attempt/manual_draining + record`，并拒绝与状态不一致的 `tts/null`；它不接收 command state，不得伪造这一层校验。
5. 原有 `waiting_tts/waiting_recording`、禁用、暂停、完成、失败和人工接管规则不变；增加可通过正常 fixture 构造的错 kind/state/scope/generation 负例。session/current-id 绑定继续由既有复合外键和服务检查覆盖；不得用 raw SQL、关闭外键或伪造坏库行来补测试。

建议先让新测试在旧代码上精确失败，再修正生产规则并跑绿。

## 本地验证

仅运行相关小测试：

```text
DATABASE_URL=sqlite:///:memory: PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q <你新增/修改的后端测试节点>
node --test web/src/autopilot/startControl.test.ts
git diff --check -- app/autopilot_service.py tests/test_autopilot_service.py tests/test_autopilot_api.py web/src/autopilot/startControl.ts web/src/autopilot/startControl.test.ts
git diff --name-only -- . ':(exclude)data' ':(exclude)data/**'
git diff --cached --name-only -- . ':(exclude)data' ':(exclude)data/**'
git status --short --untracked-files=normal -- . ':(exclude)data' ':(exclude)data/**'
```

收尾时，非暂存差异和排除 `data/**` 后的 status 中出现的路径都必须是上述五个允许文件的子集，暂存差异必须为空。任何越界路径都立即停止，不自行删除或恢复。

如果测试命令需要下载依赖、网络、服务或真实数据，不要执行，立即停止。

## 交付回执

请用简短中文返回：

1. 改了什么；
2. 为什么这是最小修复；
3. 新增测试在修复前如何失败；
4. 修复后哪些测试通过；
5. 还没证明什么（真浏览器、真麦克风、云端 ASR/TTS、医院/正式研究均不得宣称已验证）；
6. 当前 `/usage` 估计还剩多少，是否确认保留至少约 2%。
