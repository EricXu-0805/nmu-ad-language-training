# 南医大 · AI 语言沟通训练系统 — platform

轻中度痴呆老人 AI 语音机器人语言沟通训练系统。**本地部署,音频与数据不出机构、不上云端。**

口径基准(改动前先读):
- `../模块级开发计划_开工蓝图_20260707.md` — 模块划分、共享数据契约、M0 最小闭环、构建顺序、硬约束检查表
- `../项目状态基线_开工版_20260707.md` — 已定死的开发口径
- `../会议决策补充与待办_20260706.md` — 会议拍板 + 待 PI 项

## 当前进度(2026-07-10)

**后端 M0 已闭环 + 阶段1 骨架 + 阶段2 两端前端已建**,69 项 pytest 全绿:

| 层 | 内容 |
|---|---|
| 地基 | 冻结枚举 · 评分纯函数(单/双/多要素,de_total 单一事实源)· 音频删除闸门 · ★画像不进判分(结构+运行时+API 边界三重设防) |
| 数据 | SQLModel 全表(TurnEvent 每环节一行 · Week1Profile 隔离 · LiveState 跨设备状态)· **Alembic 迁移**(真机数据禁止删库重建) |
| API | FastAPI 40+ 路由:建档/建场次/会话计划/逐环节采集/AI初评/人工锁分/异常介入/量表录入/评分重建/去标识导出/音频字节+闸门/ASR/跨设备状态 |
| 音频 | 字节落本机 `data/audio/`,checksum 真 sha256 校验(篡改被拒),导出真打包,删除闸门放行才物理删 |
| ASR | M3 可插拔接口,M0=Null 引擎恒降级人工;热词自动从冻结题库生成;`ASR_ENGINE` 环境变量切换真引擎 |
| 前端 | `web/` Vite+React+TS:`/console` 操作端(建档→建场次→逐环节判分→收尾导出)+ `/patient` 老人端(大图大字/VOX录音);构建为纯静态 dist 由本服务同源托管 |
| 同步 | 双通道:BroadcastChannel(同机秒推)+ `/live/state` 服务端轮询(**内网双设备可用**) |

尚未接:真 ASR 引擎(待机构 GPU)· LLM 判分真引擎(阶段4,可关闭)。题图已回填(30 张,源=第二周训练内容 docx 内嵌插图,`web/public/img/wk2-*.webp`);多要素场景图两张因图内印有文字("动物园"/"公园")暂不可用,待内容组换图。

## 跑测试 / 开发

```bash
cd platform
./.venv/bin/python -m pytest              # 75 项(无 .venv 先: python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt)
cd web && npm run dev                     # 前端热更(另开 ./scripts/serve.sh 或 uvicorn 起后端)
```

## 部署(医院内网/科室专机,全程离线)

```bash
./scripts/serve.sh                        # 单机双窗:同机开 /console 与 /patient(localhost 麦克风可用)
INTRANET=1 ./scripts/serve.sh             # 内网双设备:0.0.0.0:8443 + 自签 TLS
                                          #   平板开 https://<本机IP>:8443/patient(首次信任证书)
                                          #   操作电脑开 https://<本机IP>:8443/console
```

- 启动自动 `alembic upgrade head`(幂等);schema 变更一律走 `alembic revision --autogenerate`,**真机严禁删库重建**。
- 内网模式必须 https:平板浏览器麦克风(getUserMedia)仅在 secure context 开放。证书/私钥在 `data/certs/`(gitignored),不外发。
- 换 PostgreSQL:改 `app/db.py` 的 URL + `alembic upgrade head`。
- **语音 SOP**:老人端页面打开后先触摸屏幕任意处一次(浏览器要求用户激活后才放行朗读;刷新后需再触摸一次);示意录音前等小语把问句读完,减少机器人声音进研究音频(朗读时刻已记入本机审计日志 `nmu:tts:log`,分析侧可剔除)。小语只用**本机**中文语音包(无则静音,绝不落到联网语音——云 TTS 意味着文本出机器,禁止)。
- **音色**:自动优先婷婷(macOS)/晓晓·慧慧(Windows)等标准普通话音色,降权 Eddy/Flo 等玩具音色,排除粤语/台湾腔优先级。macOS 想更好听:系统设置→辅助功能→朗读内容→系统声音→管理声音→下载"婷婷(高级)"。点老人端 🔊 即试听当前音色(悬停显示音色名);要钉死某音色,在老人端浏览器控制台执行 `localStorage.setItem("nmu:tts:voice", "音色名")`。备份用 `./scripts/backup.sh`(可 `BACKUP_DIR=` 指到院内加密移动盘)。

## 不可触碰的硬约束(提测逐条对)

1. **★画像不进判分**:画像只喂交互,绝不进判分与评分口径。结构(JudgeInput 无画像字段)+运行时守卫+API 边界 400+前端 oxlint 导入守卫,四层设防;`Week1Profile` 独立成表、判分侧不得 join。
2. **音频删除闸门**:导出成功+真校验通过(信度样本还需人工复核)才准删;禁止无条件到期自动删;放行才物理删字节。
3. **人工锁定分**:研究数据以人工锁定分为准,AI 判分只作初评,永不覆盖锁定分。
4. **本地/不上云**:ASR 本地转录、音频字节只落本机磁盘、前端 CSP 封死外部请求(含外部 WebSocket)。
5. **最小闭环优先**:动态判分/动态提示/第1周插槽一律可关闭模块,默认走降级口径。

## 技术栈

Python 3.14 + FastAPI + SQLModel + Alembic(SQLite 开发 / PostgreSQL 部署),前端 Vite + React + TS(纯静态产物,运行期无 node)。ASR/LLM 本地、模型选型待机构 GPU(决策19),接口按可替换设计、不写死。
