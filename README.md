# 南医大 · AI 语言沟通训练系统 — platform

轻中度痴呆老人 AI 语音机器人语言沟通训练系统。**本地部署，音频与数据不出机构、不上云端。**

口径基准（改动前先读）：
- `../模块级开发计划_开工蓝图_20260707.md` — 模块划分、共享数据契约、M0 最小闭环、构建顺序、硬约束检查表
- `../项目状态基线_开工版_20260707.md` — 已定死的开发口径
- `../会议决策补充与待办_20260706.md` — 会议拍板 + 待 PI 项

## 当前进度

**阶段0 地基（进行中）** — 已落地并全部测试通过：

| 文件 | 内容 |
|---|---|
| `app/enums.py` | 枚举注册表（冻结）：phase_type / task_type / item_set_type / cue_type / audio_status … |
| `app/scoring.py` | 评分纯函数：单要素、双要素（0.15/0.10/0.15/0.10/0.5，关系 1/0.5/0）、多要素（只算关键要素） |
| `app/audio_gate.py` | 音频删除闸门状态机（护栏1）：导出+校验(+信度复核)才准删，禁止到期盲删；撤回优先 |
| `app/judging.py` | ★画像不进判分：判分输入契约 + 运行时守卫 + 静态自检 |
| `app/models.py` | 数据表模型（SQLModel）：Patient/Session/ItemEvent/TurnEvent/Week1Profile(隔离)/ScaleResult/AudioAsset |

尚未做（下一步）：DB 接线与迁移、FastAPI 接口层、导出通道、内容/题库结构化（M5）、两端前端（阶段2）、ASR/LLM（本地、阶段1/4，可关闭）。

## 跑测试

```bash
cd platform
python3 -m pytest        # 纯逻辑测试（scoring / audio_gate / judging / enums），无需装依赖
```

接 DB/API 时：`pip install -r requirements.txt`。

## 不可触碰的硬约束（提测逐条对）

1. **★画像不进判分**：画像只喂交互（称呼/鼓励/提示措辞/拉回），绝不进判分与评分口径。`judging.py` 已在结构+运行时双重设防；`Week1Profile` 独立成表、判分侧不得 join。
2. **音频删除闸门**：原始音频导出成功+校验通过（信度样本还需人工复核完成）才准删；禁止无条件到期自动删。定时清理必须先过 `guard_time_based_purge`。
3. **人工锁定分**：研究数据以人工锁定分为准，AI 判分只作初评/辅助，永不覆盖锁定分。
4. **本地/不上云**：ASR 走本地转录（导出→本地转写→人工校对→回填），音频不出机构。
5. **最小闭环优先**：动态判分/动态提示/情绪疲劳/第1周插槽一律先做成可关闭模块，默认走降级口径。

## 技术栈

Python + FastAPI + SQLModel（SQLite 开发 / PostgreSQL 部署），前端 React（阶段2）。ASR/LLM 本地、模型选型待机构 GPU（决策19），接口按可替换设计、不写死。
