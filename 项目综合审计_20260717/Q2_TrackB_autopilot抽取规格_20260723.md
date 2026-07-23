# Q2 Track B — 自动驾驶 P0A 加固 + 并发锁序 抽取规格（待 Eric 拍板后单独一轮执行）

> **✅ 已执行 2026-07-23** — Eric"弄吧，这个弄完我一起来审核"后单独一轮落地。分支 `feature/q2-autopilot-hardening-20260723` commit `e8267fa`，**未部署**。做法=从干净 Track A 基线往上逐 hunk 加（非从脏 HEAD 往下删）。硬 grep 门禁 0 内核 / autopilot **215 passed** / 全量 pytest exit0 / 单头 f9b2d6e4a801 / 新增 7 负向测试。6-agent 对抗验证=**0 代码缺陷**；唯一锁序发现（technical-pause 重排 vs 两 attempt helper）**被独立反驳 not-a-bug**（单 worker + SQLite + 进程级 `_LIVE_WRITE_LOCK`，ABBA 结构不可达）。**关键偏差**：本规格 §「要抽取的」#2（service 外部停止内联命令匹配加强）实际 **DROP** 了——基线 `_command_matches_control_state` 已含 id/session/scope==P0A/双 generation 匹配，01cf27f 内联版在 P0A 分支上没多加东西，只留了两处 `db.expire(state)`。详见独立复核文档 §11。**仍待 Eric 真机自动驾驶验收**（步骤 7，只有他能做）。

> Track A（AI 质量看板）已落 `feature/q2-quality-autopilot-20260723`。Track B 是 30h 审计里**唯一延后的高风险手术**：把 `01cf27f` 里真有价值的自动驾驶失败闭合 + 并发锁序加固择出来，剥掉缠绕的 task_contract 内核。**因它是老人端安全攸关的自动驾驶代码、且"去内核后的围栏"是 Codex 从没测过的新组合，建议单独一轮、配真机自动驾驶测试再落。**

## 为什么单独一轮（不塞进 Track A）
- **交织度高**：`01cf27f` 对三个文件的改动是"保留逻辑 + task_contract 内核"逐行交织——`autopilot_ledger.py` 6 个混合 hunk、`main.py` 4 个、`autopilot_service.py` 2 个（共 ~12 个混合 hunk 需逐行取舍）。不是能 cherry-pick 的。
- **新组合无原厂测试覆盖**：这些 P0A 围栏在 `01cf27f` 里是为 task_contract scope 设计的；剥掉 scope 后的"围栏 without scope"是 Codex 没跑过的组合，`01cf27f` 自带测试假设 scope 存在，只能手挑用例 → 覆盖信心下降。
- **安全攸关 + 时序/并发敏感**：自动驾驶决定老人端看到/听到什么、何时推进；锁序改动是跨 worker 死锁/半围栏暴露防护。手术错一行可能误驱动一场老人干预，且未必被测试抓到。

## 要抽取的（KEEP，全部 kernel-free、对现有列可用）
1. **autopilot_ledger.py P0A 失败闭合围栏**：`verify_terminal_tts_ack`（规范 tts_ended ACK、`media_ended=True`、字节级重编码核对）、`RECORD_STOP_REASONS` + `record_stopped` 规范 stop_reason、成功时间戳早于终态 ACK 的守卫、`record==command_seq+1`（TTS 后）+ 同 capability/device-hash 绑定、`_verify_ack_binding` 的 scope+mode（陈旧 epoch/降级）复检。**读的列已在 HEAD 存在**（command_seq/scope_key/issued_capability_token_hash/issued_device_id_hash/issued_at/succeeded_at）。
2. **autopilot_service.py**：`db.expire`-after-fence（flush 后重读 `SessionAutopilotState`）+ 外部停止内联命令匹配加强（id 非空、session_id 匹配、scope_key 匹配）。
3. **main.py 并发锁序加固**：abort/pause/technical_pause 的取锁顺序 + `_abort_runtime_snapshot_decision`（跨 worker 死锁 + 半围栏暴露防护）。
4. **（可选）audio 字节上限接线**：`main.py:3720/3877/5413` 各 ~1 行传 receipt 一致的 cap（如 `row.byte_count`）。**注意**：cap 不对会在老人录音活路误拒 `AudioBlobTooLarge`，必须 receipt 尺寸 + 专门测试。

## 必须剥掉的（DROP，逐行）
- `autopilot_ledger.py`：`TASK_CONTRACT_SCOPE_KEY`、`AUTONOMOUS_SCOPE_KEYS`、`require_autonomous_scope_key`（放行该 scope）、`validate_runtime_tts/record` 的 scope dispatch、`generic_identity` 分支（读 RuntimeCommand 上的 task_contract_* 属性）。
- scope CHECK 收窄到 HEAD 的 `p0a_sim_first_single_v1`，**不**放行 `task_contract_sim_v1`。
- `main.py`：`_SESSION_INTERNAL_FIELDS`/`_session_public_projection`/response_model_exclude/create_session 写 `task_contract_bundle_id=None` 这套会话投影脚手架（HEAD 已用普通 model_dump）。
- 任何 `task_contract_*` 列/表/迁移引用。

## 执行步骤（单独分支 `feature/q2-autopilot-hardening-20260723`，基于 Track A 分支）
1. 逐文件把 pure-keep hunk 应用；对 12 个混合 hunk 逐行手编，只留 KEEP 行、删 DROP 行。
2. **硬门禁 grep**（必须全 0）：`git grep -E 'task_contract|AUTONOMOUS_SCOPE_KEYS|TASK_CONTRACT_SCOPE_KEY|generic_identity|_session_public_projection' -- 'app/*.py'`。
3. scope 守卫全部 pin 到 `P0A_SCOPE_KEY`/`p0a_sim_first_single_v1`。
4. 端口 `01cf27f` 测试里**非 task_contract** 的用例：media_ended ACK、stop_reason 集合成员、record==command_seq+1、同设备绑定、`_verify_ack_binding` scope/mode 降级。
5. 跑**全量自动驾驶套件**：`test_autopilot.py`/`test_autopilot_api.py`/`test_autopilot_contract.py`/`test_autopilot_data_layer.py`/`test_autopilot_positions.py`/`test_autopilot_service.py` + 全量 pytest。
6. **对抗复核**（Ultracode）：独立 agent 复查有无内核污染 + 围栏逻辑正确性。
7. **真机自动驾驶验收**（关键，只有 Eric 能做）：真设备跑一场，验 TTS ACK 时序 / 录音围栏 / 外部停止 / 暂停恢复 不被这套加固弄坏。
8. 全绿 + 真机过 → 合入；任一不过 → 回退，不留半成品。

## 风险登记
- 混合 hunk 逐行手编 → 易误带一个 task_contract 属性读（会对 curated schema AttributeError）→ 靠步骤 2 grep 门禁兜底。
- 围栏 without scope 无原厂测试 → 靠手挑用例 + 真机；这就是为什么要真机验收。
- 锁序改动并发敏感 → 靠全量自动驾驶套件 + 真机暂停/恢复/中止。
