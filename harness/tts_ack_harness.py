"""HARNESS ONLY —— exact demo20 TTS started/ended ACK 的本地回环验收入口。

**这不是生产入口，永远不得用于真实受试者或公网部署。**
默认的 `app.main` 完全不受影响：本模块不被 app.* 任何模块导入，只有显式运行
`python -m harness.tts_ack_harness` 才可能生效；所有重定向与注入都只作用于本进程。

它解决的唯一工程缺口：当前 canonical Week 2 源协议仍有 60 个交付缺口，
`/autopilot/start` 对默认整计划必须继续返回
`autopilot_plan_not_fully_supported`。本入口不再替换或裁剪 canonical 题库；
它在临时 root 内用生产 VisitPlan create→approve→start 账本，精确绑定
`week2-single20-demo-v1`，让浏览器在不放宽任何门禁的前提下走生产
/console、/patient 与 autopilot/TTS/ACK 状态机。这 20 题只是模拟演练范围，
不代表源协议 80 个环节已完成。

它明确不做的事：不代发 ACK、不伪造 Audio 事件、不新增诊断端点、不改任何生产文件。
最终 ACK / RuntimeCommand / TtsServeEvidence 由验收者只读查临时 SQLite。

--------------------------------------------------------------------------
运行手册（顺序不可颠倒；不要用 scripts/serve.sh，它会迁移所选库、读钥匙串、写 cert）
--------------------------------------------------------------------------
    export NMU_HARNESS_ROOT="$(mktemp -d)"   # 必须由调用者先建；本入口不代建 root
    chmod 700 "$NMU_HARNESS_ROOT"
    export DATABASE_URL="sqlite:///$NMU_HARNESS_ROOT/harness.db"
    export AUDIO_DIR="$NMU_HARNESS_ROOT/audio"
    export NMU_HARNESS_TTS_CACHE_DIR="$NMU_HARNESS_ROOT/tts-cache"
    export ALLOW_SIMULATION_DATA=1 ENABLE_AUTOPILOT_P0A_SIMULATION=1 REQUIRE_AUTH=1
    export CONSOLE_PIN=...            # 6–32 位数字，临时 PIN，用完即弃，不写进任何文件
    export NMU_HARNESS_PASSWORD=...   # 临时管理员口令，同上

    # 1) 先把临时库迁到 head（绝不碰 platform/data/app.db）
    ./.venv/bin/python -m harness.tts_ack_harness --migrate-and-seed
    # 2) 再起服务；监听地址由本命令固定 127.0.0.1
    ./.venv/bin/python -m harness.tts_ack_harness --serve --port 8811

`--migrate-and-seed` 会：alembic upgrade head → 建临时 admin → 建模拟受试者 →
走生产 /visit-plans create→approve→start 原子建场（不绕任何门禁），打印 session_id。
`--serve` 起 uvicorn 前会再校验一遍环境，并在启动 lifespan 里由生产代码校验
"账号 + CONSOLE_PIN 缺一不可"。

TTS 两种模式（NMU_HARNESS_TTS_MODE）：
- synthetic（默认）：确定性非零 WAV + 确定性本地 ASR，**即使父 shell 里已经 export
  了 DASHSCOPE_API_KEY，本进程也零云**——进入任何 app import 之前，harness 会把
  DASHSCOPE_API_KEY 与 PROVIDER_READINESS_FINGERPRINT_KEY 从本进程环境里摘掉，并
  钉死 TTS_ENGINE=null / ASR_ENGINE=null / LLM_JUDGE=off，然后才注入两个确定性替身。
  这样 provider readiness 的合成探针（TTS→ASR）是真的跑通的，既不外呼也不造 ready 行。
- production：接生产 DashScope 云链路联调。必须同时 `export TTS_ENGINE=qwen-tts
  DASHSCOPE_API_KEY=... PROVIDER_READINESS_FINGERPRINT_KEY=...`（后者是 readiness
  凭据指纹密钥，必须与 provider key 不同、够长、无空白；缺它真实 readiness 在外呼
  之前就判失败），并把 `TTS_VOICE_PATH` 指到 root 内一个不存在的路径，这样云失败时
  piper 降级层拿不到仓库里的 onnx，链路只可能是 DashScope 或明确降级，不会静默变成
  本地音。判据：TtsServeEvidence 必须是 served、cache_hit=false、引擎版本为 DashScope；
  任一项不满足即判失败，不得改判为通过。
  这只是把链路接到云上，**不等于真机验收本身**——真机结论由外部走查与证据注记判定。
白名单红线在两种模式下不是同一回事，别混着说：
- production：每一句上云文本都由 tts 合成循环里那道 `eng.cloud and not
  cloud_text_allowed(text) → 跳过` 拦着，红线真正在守；generic tts.speak 与
  exact tts.speak_autopilot 共用这一份守卫，两条路都拦得住。
- synthetic：替身引擎 cloud=False，合成循环压根不会去查 allowlist——因为这条路径
  上没有任何文本出网，红线无事可守。唯一仍无条件查 allowlist 的是 readiness 探针
  （provider_readiness 对固定探针话术的检查），两种模式都走。
"harness 不新增任何训练文本"是独立成立的另一件事：demo20 profile
只按不可变位置清单选择 canonical 题库里已冻结的 20 题，所有话术仍由生产
allowlist 与行级内容合同审核。

当前可自动复现的证据是：新建临时库、生产 VisitPlan 三步账本、exact demo20
解析为 20 个位置，且生产 `/autopilot/start` 只签发第一条 TTS。升级后的
20 题全流程真实浏览器证据必须用新 marker 重跑；2026-07-31 的旧单题记录
不能当作当前 demo20 验收。

harness 进程的每个响应都带
`X-NMU-Test-Harness: demo20-profile-tts-ack-v1`。**不改写 HTML
正文**——重建响应会把重复的 Set-Cookie 合并掉，登录与设备配对会因此坏掉，一个视觉
横幅不值得这个协议风险。截图上的"20 题本机模拟、非正式研究"标识由外部证据注记承担。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import math
import os
import re
import stat
import sys
import wave
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_DATA_DIR = PLATFORM_ROOT / "data"

MARKER_HEADER = "X-NMU-Test-Harness"
MARKER_VALUE = "demo20-profile-tts-ack-v1"

ROOT_ENV = "NMU_HARNESS_ROOT"
TTS_CACHE_ENV = "NMU_HARNESS_TTS_CACHE_DIR"
TTS_MODE_ENV = "NMU_HARNESS_TTS_MODE"
PASSWORD_ENV = "NMU_HARNESS_PASSWORD"
ACTOR_ENV = "NMU_HARNESS_ACTOR"
FINGERPRINT_KEY_ENV = "PROVIDER_READINESS_FINGERPRINT_KEY"

HARNESS_PATIENT_ID = "SYN-T5-P001"
DEFAULT_ACTOR = "syn-t5-admin"
TTS_MODES = ("synthetic", "production")
CANONICAL_BANK_PATH = PLATFORM_ROOT / "content" / "item_bank_v1.json"
DEMO_PROFILE_VERSION = "week2-single20-demo-v1"
_PIN_RE = re.compile(r"[0-9]{6,32}")


class HarnessConfigError(RuntimeError):
    """环境不满足隔离要求。抛出时一定还没有 import 过 app.db，默认库不会被触碰。"""


@dataclass(frozen=True)
class HarnessConfig:
    root: Path
    database_url: str
    database_file: Path
    audio_dir: Path
    tts_cache_dir: Path
    asr_scratch_dir: Path
    export_dir: Path
    controlled_export_dir: Path
    tts_mode: str
    piper_voice_path: Path
    actor_username: str
    actor_password: str

    def scrubbed(self) -> dict[str, str]:
        """可以安全打印的字段；口令与 PIN 永不出现。"""
        return {
            "root": str(self.root),
            "database_url": self.database_url,
            "audio_dir": str(self.audio_dir),
            "tts_cache_dir": str(self.tts_cache_dir),
            "tts_mode": self.tts_mode,
            "piper_voice_path": str(self.piper_voice_path),
            "actor": self.actor_username,
        }


# ----------------------------------------------------------------------------
# fail-closed 环境校验：纯路径运算，不 import 任何 app.* / sqlalchemy
# ----------------------------------------------------------------------------
def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise HarnessConfigError(f"缺少必需的 harness 环境变量 {name}")
    return value


def _absolute(raw: str, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise HarnessConfigError(f"{label} 必须是绝对路径")
    return Path(os.path.normpath(str(path)))


def _inside_root(
    path: Path,
    root: Path,
    label: str,
    lexical_roots: tuple[Path, ...] | None = None,
) -> Path:
    """把一条路径收进私有 root，且**先词法后文件系统**。

    次序是安全要求，不是风格：传进来的可能就是生产默认库路径，那条路径上连一次
    lstat/resolve 都不该发生——边界是零触达，不是零写入。所以第一步纯字符串判断，
    词法上落在 root 之外立刻拒绝，期间不做任何文件系统调用。

    只有词法上确实在私有 root 内，才允许探文件系统：从 root 往下逐段拒绝符号链接，
    否则 root 里预置一条软链就能把写入外逸；最后 resolve 再复核一次仍在 root 内。

    lexical_roots 允许同时接受"声明的 root"和"resolve 后的 root"——macOS 上
    mktemp -d 给的是 /var/folders/…，resolve 后是 /private/var/folders/…，运行手册
    里 $NMU_HARNESS_ROOT/audio 这种写法必须仍然成立。
    """
    lexical = Path(os.path.normpath(str(path)))
    candidates = tuple(lexical_roots) if lexical_roots else (root,)
    if lexical in candidates:
        raise HarnessConfigError(f"{label} 不能就是 harness root 本身")
    base = next((c for c in candidates if lexical.is_relative_to(c)), None)
    if base is None:
        raise HarnessConfigError(f"{label} 必须落在 {ROOT_ENV} 之内")

    current = base
    for part in lexical.relative_to(base).parts:
        current = current / part
        if current.is_symlink():
            raise HarnessConfigError(f"{label} 路径上不得是符号链接：{part}")

    resolved = lexical.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise HarnessConfigError(f"{label} 必须落在 {ROOT_ENV} 之内")
    return resolved


def _sqlite_file(url: str) -> Path:
    if not url.startswith("sqlite:///"):
        raise HarnessConfigError("DATABASE_URL 必须是 SQLite（sqlite:///绝对路径）")
    if "?" in url:
        raise HarnessConfigError("DATABASE_URL 不接受查询参数")
    raw = url[len("sqlite:///"):]
    if not raw.startswith("/"):
        raise HarnessConfigError("DATABASE_URL 的 SQLite 路径必须是绝对路径")
    if raw.startswith("/:memory:") or raw.lstrip("/").startswith("file:"):
        raise HarnessConfigError("DATABASE_URL 必须指向 harness root 内的真实文件")
    return _absolute(raw, "DATABASE_URL 的 SQLite 路径")


def _flag(env: Mapping[str, str], name: str) -> None:
    if (env.get(name) or "").strip() != "1":
        raise HarnessConfigError(f"{name} 必须显式设为 1")


def resolve_config(env: Mapping[str, str] | None = None) -> HarnessConfig:
    """校验隔离前提；任一项不满足即固定失败，且不触碰默认 DB。"""
    env = os.environ if env is None else env

    declared_root = _absolute(_required(env, ROOT_ENV), ROOT_ENV)
    if declared_root.is_symlink():
        raise HarnessConfigError(f"{ROOT_ENV} 不得是符号链接")
    root = declared_root.resolve()
    if root == Path(root.anchor):
        raise HarnessConfigError(f"{ROOT_ENV} 不能是文件系统根")
    if root == PLATFORM_ROOT or root.is_relative_to(PLATFORM_ROOT):
        raise HarnessConfigError(f"{ROOT_ENV} 不得落在仓库目录内")
    # root 必须是调用者预先 mktemp -d 出来的真实私有目录：本入口不代建，
    # 也不接受任何 group/world 可达的目录——临时库里有合成场次与 PIN 绑定证据。
    if not root.is_dir():
        raise HarnessConfigError(f"{ROOT_ENV} 必须是调用者预先创建的真实目录")
    if stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise HarnessConfigError(f"{ROOT_ENV} 不得对同组或其他用户可达（chmod 700）")

    lexical_roots = (root, declared_root) if declared_root != root else (root,)

    database_url = _required(env, "DATABASE_URL")
    database_file = _inside_root(
        _sqlite_file(database_url), root, "DATABASE_URL", lexical_roots)
    audio_dir = _inside_root(
        _absolute(_required(env, "AUDIO_DIR"), "AUDIO_DIR"), root, "AUDIO_DIR",
        lexical_roots)
    tts_cache_dir = _inside_root(
        _absolute(_required(env, TTS_CACHE_ENV), TTS_CACHE_ENV), root,
        TTS_CACHE_ENV, lexical_roots)

    # 这三个是 harness 自选的固定名字，同样要 canonicalize：root 里预置一条
    # asr-scratch → /somewhere 的软链，否则会把患者音频临时副本写到 root 之外。
    asr_scratch_dir = _inside_root(root / "asr-scratch", root, "asr scratch 目录")
    export_dir = _inside_root(root / "exports", root, "导出目录")
    controlled_export_dir = _inside_root(
        root / "controlled-audio-exports", root, "受控音频导出目录")

    for produced in (database_file, audio_dir, tts_cache_dir, asr_scratch_dir,
                     export_dir, controlled_export_dir):
        if produced.is_relative_to(PRODUCTION_DATA_DIR):
            raise HarnessConfigError("harness 路径不得落在生产 data/ 目录内")

    _flag(env, "ALLOW_SIMULATION_DATA")
    _flag(env, "ENABLE_AUTOPILOT_P0A_SIMULATION")
    _flag(env, "REQUIRE_AUTH")
    # 与生产 auth.console_pin 同一口径，但必须在 import app 之前就判死，
    # 否则非法 PIN 要等到 lifespan 才炸，那时默认库路径已经被引擎解析过。
    pin = env.get("CONSOLE_PIN") or ""
    if not _PIN_RE.fullmatch(pin):
        raise HarnessConfigError("CONSOLE_PIN 必须是 6–32 位 ASCII 数字")

    password = env.get(PASSWORD_ENV) or ""
    if len(password) < 8:
        raise HarnessConfigError(f"{PASSWORD_ENV} 必须显式设置且至少 8 位")

    tts_mode = (env.get(TTS_MODE_ENV) or "synthetic").strip()
    if tts_mode not in TTS_MODES:
        raise HarnessConfigError(f"{TTS_MODE_ENV} 只能是 {'/'.join(TTS_MODES)}")

    # 真机模式必须钉死云引擎：auto 会在缺 Key 时静默落 piper，piper 又会去读仓库
    # 里的 onnx——那样验收到的"朗读"根本不是 DashScope，证据就白取了。
    piper_voice_path = root / "no-piper-voice.onnx"
    if tts_mode == "production":
        if (env.get("TTS_ENGINE") or "").strip() != "qwen-tts":
            raise HarnessConfigError("production 模式必须显式设 TTS_ENGINE=qwen-tts")
        if not (env.get("DASHSCOPE_API_KEY") or "").strip():
            raise HarnessConfigError("production 模式必须提供 DASHSCOPE_API_KEY")
        # 配了 provider key 却没配指纹密钥，真实 readiness 会在外呼之前就判失败。
        # 这里只查"有没有"；完整最小合同由 assert_production_fingerprint_contract()
        # 在 app.db 被导入之前用生产判据强制，不在此复制一份。
        if not (env.get(FINGERPRINT_KEY_ENV) or "").strip():
            raise HarnessConfigError(
                f"production 模式必须提供 {FINGERPRINT_KEY_ENV}")
        declared_voice = env.get("TTS_VOICE_PATH")
        if declared_voice:
            piper_voice_path = _inside_root(
                _absolute(declared_voice, "TTS_VOICE_PATH"), root, "TTS_VOICE_PATH",
                lexical_roots)
        if piper_voice_path.exists():
            raise HarnessConfigError(
                "TTS_VOICE_PATH 必须指向 harness root 内一个不存在的路径，"
                "以关闭 piper 降级层")

    return HarnessConfig(
        root=root,
        database_url=database_url,
        database_file=database_file,
        audio_dir=audio_dir,
        tts_cache_dir=tts_cache_dir,
        asr_scratch_dir=asr_scratch_dir,
        export_dir=export_dir,
        controlled_export_dir=controlled_export_dir,
        tts_mode=tts_mode,
        piper_voice_path=piper_voice_path,
        actor_username=(env.get(ACTOR_ENV) or DEFAULT_ACTOR).strip() or DEFAULT_ACTOR,
        actor_password=password,
    )


# ----------------------------------------------------------------------------
# 合成 TTS（训练内容仍为 canonical + exact profile）
# ----------------------------------------------------------------------------
def deterministic_wav(text: str) -> bytes:
    """同一句话永远得到同一段非零 WAV；确定性、可复现、离线。"""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    freq = 220 + (digest[0] << 8 | digest[1]) % 440
    rate = 8000
    frames = bytearray()
    for i in range(int(rate * 0.6)):
        sample = int(11000 * math.sin(2 * math.pi * freq * i / rate))
        frames += int(sample).to_bytes(2, "little", signed=True)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(frames))
    return buffer.getvalue()


class DeterministicHarnessTtsEngine:
    """本地确定性引擎：cloud=False，文本不出机器。仅 synthetic 模式启用。"""

    cloud = False
    cache_params = "harness-synthetic-v1"
    version = "harness-synthetic/1"

    def available(self) -> bool:
        return True

    def synthesize(self, text: str) -> bytes | None:
        return deterministic_wav(text)


def canonical_bank_loader(original_loader, bank):
    """只替换 canonical 题库这一条路径的 loader。

    无条件吞掉所有路径会让勘误比对、导出核验、迁移校验也读到合成题库——那些调用
    本来就应该读它们各自指定的文件。
    """
    canonical = CANONICAL_BANK_PATH.resolve()

    def _load_item_bank(path):
        try:
            is_canonical = Path(path).resolve() == canonical
        except (OSError, RuntimeError, ValueError):
            is_canonical = False
        return bank if is_canonical else original_loader(path)

    return _load_item_bank


class DeterministicHarnessAsrEngine:
    """本地确定性 ASR。

    provider readiness 的合成探针是 TTS→ASR 串联的：只换 TTS 不换 ASR，探针必然
    停在 asr_text_empty，浏览器根本开不了场。这里补齐 ASR 侧，让探针**真的跑通**，
    而不是绕过 readiness 或直接往库里塞一行 ready。音频不出机器。
    """

    cloud = False
    data_boundary = "local"
    provider_id = None
    version = "harness-synthetic-asr/1"

    def transcribe(self, audio_bytes: bytes, hotwords):
        from app.asr import AsrResult

        first = next((w for w in hotwords if isinstance(w, str) and w.strip()), None)
        text = first.strip() if first else "harness-synthetic-transcript"
        return AsrResult(
            asr_text=text,
            asr_confidence=1.0,
            engine_version=self.version,
            hotword_hit=first is not None,
        )


# ----------------------------------------------------------------------------
# 进程内重定向（只在本进程；生产 import app.main 完全不变）
# ----------------------------------------------------------------------------
def _assert_process_database_binding(config: HarnessConfig) -> None:
    """进入 alembic / app 之前，确认**进程环境**真的指向 harness 库。

    resolve_config 接受任意 mapping。如果调用者用 mapping 校验通过，却没有把
    DATABASE_URL 写回 os.environ，接下来 alembic env.py 和 app.db 都会按进程环境
    解析——那就直接落到默认库上了。这条缝只能在导入之前焊死，导入之后引擎已经建好。

    纯字符串与路径比较，不导入任何 app 模块。
    """
    live = os.environ.get("DATABASE_URL") or ""
    if live != config.database_url:
        raise HarnessConfigError(
            "进程环境 DATABASE_URL 与 harness 配置不一致；resolve_config 校验的是"
            "传入的 mapping，导入 app 之前必须把同一个值写回 os.environ")
    if _sqlite_file(live).resolve() != config.database_file:
        raise HarnessConfigError(
            "进程环境 DATABASE_URL 解析出的库文件不是 harness 库")


def prepare_synthetic_process(config: HarnessConfig) -> None:
    """synthetic 模式：把本进程钉成"根本没有云"，必须早于任何 app import。

    只换 TTS/ASR 引擎是不够的。父 shell 里若已经 export 了 DASHSCOPE_API_KEY，
    llm_judge 的 auto 选择会挑到 qwen，readiness 探针就可能真的外呼；而"有 key 却
    没有 fingerprint 密钥"又会让 synthetic 探针必然失败。两个后果都不能接受。

    所以这里直接把凭据从本进程环境里摘掉，并显式钉死三个引擎开关。install() 之后
    再注入确定性 TTS/ASR 替身。production 模式不做这些。
    """
    if config.tts_mode != "synthetic":
        return
    for name in ("DASHSCOPE_API_KEY", FINGERPRINT_KEY_ENV):
        os.environ.pop(name, None)
    os.environ["TTS_ENGINE"] = "null"
    os.environ["ASR_ENGINE"] = "null"
    os.environ["LLM_JUDGE"] = "off"


def _secret_env_digest(provider_readiness) -> str:
    """凭据环境的内存安全摘要：只留 sha256，绝不保存或打印任何原值。

    覆盖生产 helper 当前的 _NON_REUSABLE_SECRET_ENV_NAMES（其中已含 CONSOLE_PIN）
    加上指纹密钥本身。名单跟着生产走，harness 不另立一份。
    """
    names = tuple(provider_readiness._NON_REUSABLE_SECRET_ENV_NAMES)  # noqa: SLF001
    hasher = hashlib.sha256(b"nmu-harness-secret-env-digest-v1\x00")
    for name in sorted(set(names + (FINGERPRINT_KEY_ENV, "DASHSCOPE_API_KEY"))):
        value = os.environ.get(name)
        hasher.update(name.encode("utf-8") + b"\x00")
        hasher.update(b"\x01" if value is None else b"\x02")
        hasher.update(hashlib.sha256((value or "").encode("utf-8")).digest())
    return hasher.hexdigest()


_fingerprint_contract_digest: str | None = None


def assert_production_fingerprint_contract() -> None:
    """production 模式的指纹密钥完整合同，必须在 app.db 被导入之前判死。

    resolve_config 只查"有没有"。真正的最小合同（长度、空白、复用 provider key）
    直接复用生产判据 provider_readiness._fingerprint_key_failure_code，harness 不
    复制一份——复制出来的第二份迟早和生产走偏。

    为了让"在 app.db 之前"这句话可验证而不是口号：首次调用先断言 app.db 还没进
    sys.modules，只导入 provider_readiness，再断言这次导入没有把 app.db 带进来。
    synthetic 模式不走这里，因此根本不会导入它。

    通过后记的是**凭据环境的摘要**，不是一个 bool。migrate() 之后 install()/seed()
    再调用，只有摘要完全相同才算幂等跳过；凭据在中途被换掉而 app.db 已经导入时，
    旧验证一律作废并 fail-closed——不能拿上一份凭据的结论替这一份背书。
    """
    global _fingerprint_contract_digest
    first_time = _fingerprint_contract_digest is None
    if first_time and "app.db" in sys.modules:
        raise HarnessConfigError("指纹合同预检必须发生在 app.db 被导入之前")

    already_loaded = "app.provider_readiness" in sys.modules
    from app import provider_readiness

    if first_time and not already_loaded and "app.db" in sys.modules:
        raise HarnessConfigError("导入 provider_readiness 时带入了 app.db，预检次序失效")

    digest = _secret_env_digest(provider_readiness)
    if _fingerprint_contract_digest is not None:
        if digest == _fingerprint_contract_digest:
            return
        if "app.db" in sys.modules:
            raise HarnessConfigError(
                "凭据环境在预检通过之后发生变化，且 app.db 已导入；"
                "旧验证不能沿用，请在干净进程里重新启动 harness")

    failure = provider_readiness._fingerprint_key_failure_code(  # noqa: SLF001
        os.environ.get("DASHSCOPE_API_KEY") or "",
        os.environ.get(FINGERPRINT_KEY_ENV))
    if failure is not None:
        _fingerprint_contract_digest = None
        raise HarnessConfigError(
            f"{FINGERPRINT_KEY_ENV} 不满足生产最小合同：{failure}")
    _fingerprint_contract_digest = digest


def _assert_imported_engine_binding(config: HarnessConfig, db) -> None:
    """已经建好的生产引擎必须真的绑在 harness 库上。

    _pre_app_import_guard 只看 os.environ，那只能拦住"还没导入"的情形。进程里若已经
    存在一个绑到别的库的引擎，事后把环境改对也救不回来——引擎在 app.db 导入的那一刻
    就按当时的 DATABASE_URL 建好了。任何会真的开 Session 的入口都得再查这一遍。

    判定纯词法，不做任何文件系统调用：这里拿到的 bound 是**别人**的路径，极端情况下
    就是生产默认库，那条路径连一次 lstat/resolve 都不该挨上。可接受的写法有两种——
    resolve 后的 harness 库路径，和 DATABASE_URL 里原样声明的那一条（macOS 上
    mktemp -d 给 /var/folders/…，resolve 后是 /private/var/folders/…，两者都算数）。
    """
    bound = db.engine.url.database
    if bound is None:
        raise HarnessConfigError(
            "已导入的生产引擎未绑定 harness 库；DATABASE_URL 必须在导入 app 之前设置")
    lexical = Path(os.path.normpath(str(bound)))
    if lexical not in (config.database_file, _sqlite_file(config.database_url)):
        raise HarnessConfigError(
            "已导入的生产引擎未绑定 harness 库；DATABASE_URL 必须在导入 app 之前设置")


def _pre_app_import_guard(config: HarnessConfig) -> None:
    """migrate / install / seed 的统一前置闸，必须早于任何 app 或 alembic import。"""
    _assert_process_database_binding(config)
    if config.tts_mode == "synthetic":
        prepare_synthetic_process(config)
    else:
        assert_production_fingerprint_contract()


def install(config: HarnessConfig):
    """导入生产 app 后，只重定向可变存储与本地引擎。

    canonical 题库 loader 保持生产原样；demo20 范围只能由 seed()
    经生产 VisitPlan 账本绑定，不允许再用进程级题库替身进入。
    """
    # main() 已经先过一遍；这一次是保护"直接调用 install()"的路径。
    _pre_app_import_guard(config)

    # 先只导入 app.db 并验绑定：引擎错了就不该继续 import app.main，
    # 更不该动任何 app 全局。
    from app import db

    _assert_imported_engine_binding(config, db)

    from app import asr, audio_store, export, tts
    from app.main import app

    for directory in (config.audio_dir, config.tts_cache_dir, config.asr_scratch_dir,
                      config.export_dir, config.controlled_export_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    audio_store.AUDIO_DIR = config.audio_dir
    tts.CACHE_DIR = config.tts_cache_dir
    asr.SCRATCH_DIR = config.asr_scratch_dir
    export.EXPORT_DIR = config.export_dir
    export.CONTROLLED_AUDIO_DIR = config.controlled_export_dir

    if config.tts_mode == "synthetic":
        tts._engine = None
        tts.get_engine = DeterministicHarnessTtsEngine
        # exact 自动驾驶通道现在只认 Qwen，synthetic 验收必须显式注入同一个
        # 确定性引擎；不注入的话 synthetic 场次会正确地 204 而不是出声。
        tts.get_autopilot_engine = DeterministicHarnessTtsEngine
        asr.get_engine = DeterministicHarnessAsrEngine
    else:
        # 关掉 piper 降级层：云失败必须表现为明确降级，不能悄悄读仓库里的 onnx。
        os.environ["TTS_VOICE_PATH"] = str(config.piper_voice_path)
        tts.DEFAULT_VOICE = config.piper_voice_path
        tts._fallback_piper = None
        tts._engine = None

    app.add_middleware(harness_marker_middleware_class())
    return app


def harness_marker_middleware_class():
    from starlette.middleware.base import BaseHTTPMiddleware

    class _Middleware(BaseHTTPMiddleware):
        """harness-only：每个响应加一个固定标记头，响应体原样透传。

        不改写 HTML 正文。重建响应会把重复的 Set-Cookie 合并掉，登录/设备配对
        就此坏掉——一个视觉横幅不值得承担这个协议风险。截图上的"合成验收"标识
        由外部证据注记承担。
        """

        async def dispatch(self, request, call_next):
            response = await call_next(request)
            response.headers[MARKER_HEADER] = MARKER_VALUE
            return response

    return _Middleware


# ----------------------------------------------------------------------------
# 合成账号 / 受试者 / 场次（全部走生产准入路径，不直建场次）
# ----------------------------------------------------------------------------
def migrate(config: HarnessConfig) -> None:
    # 必须早于 alembic import：alembic/env.py 会导入 app.db。
    _pre_app_import_guard(config)
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(PLATFORM_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", config.database_url)
    command.upgrade(cfg, "head")


def seed(config: HarnessConfig) -> dict[str, str]:
    """建临时 admin + 模拟受试者，并用生产账本建 exact demo20 场。"""
    # 直接调用 seed() 的路径同样要过闸，不能靠"调用者一定先调过 install"。
    _pre_app_import_guard(config)

    # 同样先只导入 app.db 验绑定。进程里可能已经有一个绑到别的库的引擎，
    # 环境对不代表引擎对；验不过就一行不读、一行不写地退出。
    from app import db

    _assert_imported_engine_binding(config, db)

    from sqlmodel import Session, select

    from app import auth, autopilot_plan_profiles, visit_plan_service
    from app.models import Patient, ResearchUser, VisitPlan
    from app.models import Session as TrainSession
    from app.visit_plan_contract import VisitPlanCreateIn, VisitPlanMutationIn

    with Session(db.engine) as session:
        display_id = auth.validate_display_id(config.actor_username)
        if session.get(ResearchUser, config.actor_username) is None:
            session.add(ResearchUser(
                username=config.actor_username,
                display_id=display_id,
                password_hash=auth.hash_password(config.actor_password),
                role="admin",
                created_at=datetime.now(),
            ))
        if session.get(Patient, HARNESS_PATIENT_ID) is None:
            session.add(Patient(
                patient_id=HARNESS_PATIENT_ID,
                is_simulation_subject=True,
                consent_status="已同意",
                recording_allowed=True,
            ))
        session.commit()

        existing = session.exec(select(TrainSession).where(
            TrainSession.patient_id == HARNESS_PATIENT_ID,
        )).first()
        if existing is not None:
            if existing.autopilot_profile_version_id != DEMO_PROFILE_VERSION:
                raise HarnessConfigError(
                    "harness root 内已有非 exact demo20 场次，拒绝静默复用")
            existing_plan = (
                session.get(VisitPlan, existing.visit_plan_id)
                if existing.visit_plan_id is not None else None)
            if existing_plan is None:
                raise HarnessConfigError("exact demo20 场次缺少 VisitPlan 账本")
            visit_plan_service.assert_started_profile_command_chain(
                session, existing_plan, existing)
            return {
                "session_id": existing.session_id,
                "plan_id": existing.visit_plan_id or "",
                "profile_version_id": existing.autopilot_profile_version_id,
                "reused": "1",
            }

        if (autopilot_plan_profiles.WEEK2_SINGLE20_DEMO_VERSION
                != DEMO_PROFILE_VERSION):
            raise HarnessConfigError("demo20 profile 版本常量与生产 registry 不一致")

        suffix = hashlib.sha256(
            f"{config.root}|{display_id}".encode()).hexdigest()[:16]
        receipt = visit_plan_service.create_plan(
            session,
            body=VisitPlanCreateIn(
                idempotency_key=f"harness-create-{suffix}",
                patient_id=HARNESS_PATIENT_ID,
                scheduled_date=date.today(),
                week_no=2,
                phase_type="正式训练",
                event_line="正式训练",
                autopilot_profile_version_id=DEMO_PROFILE_VERSION,
            ),
            actor_id=display_id,
        )
        plan_id = receipt.plan_id
        receipt = visit_plan_service.approve_plan(
            session, plan_id=plan_id,
            body=VisitPlanMutationIn(
                idempotency_key=f"harness-approve-{suffix}",
                expected_revision=receipt.revision),
            actor_id=display_id)
        visit_plan_service.start_plan(
            session, plan_id=plan_id,
            body=VisitPlanMutationIn(
                idempotency_key=f"harness-start-{suffix}",
                expected_revision=receipt.revision),
            actor_id=display_id)
        session.commit()
        started = session.exec(select(TrainSession).where(
            TrainSession.visit_plan_id == plan_id,
        )).first()
        if started is None:
            raise HarnessConfigError("VisitPlan 开场后未生成 exact demo20 场次")
        started_plan = session.get(VisitPlan, plan_id)
        if started_plan is None:
            raise HarnessConfigError("VisitPlan 开场后账本丢失")
        visit_plan_service.assert_started_profile_command_chain(
            session, started_plan, started)
        return {
            "session_id": started.session_id,
            "plan_id": plan_id,
            "profile_version_id": started.autopilot_profile_version_id or "",
            "reused": "0",
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness.tts_ack_harness",
        description=(
            "HARNESS ONLY —— exact 20 题本机模拟 TTS/ACK 验收入口；"
            "canonical 题库不会被替换，禁止用于生产。"))
    parser.add_argument(
        "--migrate-and-seed", action="store_true",
        help="迁移临时库，并用生产账本建立 week2-single20-demo-v1 场次")
    parser.add_argument(
        "--serve", action="store_true",
        help="仅在 127.0.0.1 启动 exact demo20 验收服务")
    parser.add_argument(
        "--port", type=int, default=8811,
        help="本机回环端口（默认 8811）")
    args = parser.parse_args(argv)
    if not (args.migrate_and_seed or args.serve):
        parser.error("必须显式选择 --migrate-and-seed 或 --serve")

    try:
        config = resolve_config(os.environ)
    except HarnessConfigError as exc:
        print(f"harness 环境校验失败：{exc}", file=sys.stderr)
        return 2

    # 必须早于 migrate（alembic 会导入 app.db）与 install。
    try:
        _pre_app_import_guard(config)
    except HarnessConfigError as exc:
        print(f"harness 环境校验失败：{exc}", file=sys.stderr)
        return 2

    if args.migrate_and_seed:
        migrate(config)

    app = install(config)
    print(f"harness 配置：{config.scrubbed()}", file=sys.stderr)

    if args.migrate_and_seed:
        print(f"harness 场次：{seed(config)}", file=sys.stderr)
    if args.serve:
        import uvicorn

        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
