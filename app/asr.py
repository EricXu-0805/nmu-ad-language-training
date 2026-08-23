"""ASR 接口(可插拔、可关闭)——云端优先,恒可降级人工转写。

口径(2026-07-22 更新,Eric 确认备案已完成):患者音频可发云端转写。
- DashScopeAsrEngine:阿里百炼 qwen3-asr-flash(ASR_CLOUD_MODEL),**上下文偏置**——
  题库目标词/可接受表达/左右词整包喂进 system 文本,识别自动向这些词倾斜
  (这是工程偏置能力,不代表已验证老人、方言或含糊回答的准确率)。按 2026-07-22
  官方模型说明,单段上限为 5 分钟/10MB；本项目仍以更短的床旁单次回答为设计目标。
- NullAsrEngine:恒返回 None → 操作端人工转写降级,判分链不断。
ASR_ENGINE 选择(默认 auto:有 DASHSCOPE_API_KEY → dashscope,无 → null)。
云端失败(断网/欠费/超格式)一律降级 null 口径,不 fail-hard。
转写结果是三态,不是两态:有文本 / 明确成功但无转写文本("") / 技术失败(None)。
""  只表示运营层"本轮无转写",不等于已证明声学静音。
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from .content import ItemBank

# 云转写的临时文件放 data/ 自有 scratch 目录(不进系统 /tmp):患者音频副本必须
# 留在音频治理边界内;进程被杀的残留由 cleanup_scratch()(lifespan 启动时)清扫。
SCRATCH_DIR = Path(__file__).resolve().parent.parent / "data" / "asr-scratch"


def cleanup_scratch() -> None:
    try:
        for p in SCRATCH_DIR.glob("asr-*"):
            p.unlink(missing_ok=True)
    except OSError:
        pass


@dataclass(frozen=True)
class AsrResult:
    # None = 技术失败(引擎不可用/异常/非 200/响应结构损坏) → 人工转写降级;
    # ""   = provider 明确返回 200、转写结构合法、但没有给出任何转写文本。
    #        这只是**运营层"本轮无转写"**,不是已证明声学静音:音量过低、设备
    #        问题、模型漏识都会落到这里。运行决策据此走冻结的 operational 沉默
    #        分支;研究结论不得据此推断老人没说话。
    # 两者绝不能折成同一个值:折了之后本轮无转写会被当成技术失败暂停整场,
    # 冻结协议里那句"我们慢慢来"永远播不出来。
    asr_text: str | None
    asr_confidence: float | None
    engine_version: str
    hotword_hit: bool = False


@dataclass(frozen=True)
class _AsrCall:
    """provider 单次调用的结构化结果:把"调用成功"与"拿到转写文本"分开。

    ok=True 的门槛是**明确的** 200 + 合法转写结构;缺 status_code、缺 choices、
    空转写段列表、段里没有显式 string text、混入坏段——一律 ok=False 技术失败。
    ok=True 且 text="" 只表示这次调用没有给出任何转写文本。
    """
    ok: bool
    text: str = ""


class AsrProvider(Protocol):
    version: str
    data_boundary: str
    provider_id: str | None

    def transcribe(self, audio_bytes: bytes, hotwords: Sequence[str]) -> AsrResult: ...


class NullAsrEngine:
    """占位引擎:恒降级。保证『动态能力关闭即走降级口径』这条铁律从第一天就可跑。"""
    version = "null-0"
    data_boundary = "local"
    provider_id = None

    def transcribe(self, audio_bytes: bytes, hotwords: Sequence[str]) -> AsrResult:
        return AsrResult(asr_text=None, asr_confidence=None, engine_version=self.version)


def _sniff_ext(audio_bytes: bytes) -> str:
    """按魔数猜容器扩展名(百炼按扩展名识别格式)。MediaRecorder 默认 webm/opus。"""
    if audio_bytes[:4] == b"\x1aE\xdf\xa3":
        return ".webm"
    if audio_bytes[:4] == b"OggS":
        return ".ogg"
    if audio_bytes[:4] == b"RIFF":
        return ".wav"
    return ".webm"


class DashScopeAsrEngine:
    """qwen3-asr-flash 录音文件识别。context=热词整包(任意背景文本即可偏置)。"""

    data_boundary = "cloud"
    provider_id = "aliyun-dashscope"

    def __init__(self, model: str):
        self._model = model
        self.version = f"dashscope/{model}"

    def available(self) -> bool:
        return bool(os.environ.get("DASHSCOPE_API_KEY"))

    def transcribe(self, audio_bytes: bytes, hotwords: Sequence[str]) -> AsrResult:
        if not audio_bytes or not self.available():
            return AsrResult(None, None, self.version)
        context = "、".join(dict.fromkeys(w for w in hotwords if w))
        try:
            call = self._call(audio_bytes, context)
        except Exception:
            return AsrResult(None, None, self.version)
        if not call.ok:
            return AsrResult(None, None, self.version)
        if not call.text:
            # 明确 200 + 合法转写结构 + 没有转写文本 = 运营层"本轮无转写"。
            # 这不是"已证明老人没出声";判类链只把它当运行决策依据。
            return AsrResult(asr_text="", asr_confidence=None,
                             engine_version=self.version)
        hit = any(w and w in call.text for w in hotwords)
        return AsrResult(asr_text=call.text, asr_confidence=None,
                         engine_version=self.version, hotword_hit=hit)

    def _call(self, audio_bytes: bytes, context: str) -> _AsrCall:
        from dashscope import MultiModalConversation
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        f = tempfile.NamedTemporaryFile(dir=SCRATCH_DIR, prefix="asr-",
                                        suffix=_sniff_ext(audio_bytes), delete=False)
        tmp = Path(f.name)
        try:                                # write 也在 try 内:写半截失败同样要删副本
            with f:
                f.write(audio_bytes)
            messages = []
            if context:
                messages.append({"role": "system", "content": [{"text": context}]})
            messages.append({"role": "user", "content": [{"audio": tmp.as_uri()}]})
            resp = MultiModalConversation.call(
                model=self._model, messages=messages, result_format="message",
                request_timeout=30,
                # itn 关:数字保持汉字口语形态,与题库可接受表达的匹配口径一致
                asr_options={"enable_lid": False, "enable_itn": False})
            # 只有**明确的** 200 才算调用成功;缺这个字段说明响应不是我们认识的
            # 那个形状,不能默认放行。
            if getattr(resp, "status_code", None) != 200:
                return _AsrCall(ok=False)
            out = getattr(resp, "output", None)
            choices = getattr(out, "choices", None) if out is not None else None
            if not choices:
                return _AsrCall(ok=False)
            content = choices[0].message.content
            if isinstance(content, list):
                # 空段列表 = 真实 DashScope 对无语音音频的正常应答(2026-08-23
                # 生产直调实证:静音与纯音调都返回 200 + content=[]),按"本轮
                # 无转写"处理;判成技术失败会把沉默映射成 asr_degraded 安全暂停。
                if not content:
                    return _AsrCall(ok=True, text="")
                # 缺 text、text 不是字符串、混进非 dict 的段——损坏结构,fail closed。
                parts = []
                for segment in content:
                    if not isinstance(segment, dict):
                        return _AsrCall(ok=False)
                    piece = segment.get("text")
                    if not isinstance(piece, str):
                        return _AsrCall(ok=False)
                    parts.append(piece)
                text = "".join(parts)
            elif isinstance(content, str):
                text = content
            else:
                # content 既不是分段列表也不是字符串 = 响应结构损坏,按技术失败处理。
                return _AsrCall(ok=False)
            return _AsrCall(ok=True, text=text.strip())
        finally:
            tmp.unlink(missing_ok=True)


_ENGINES: dict[str, AsrProvider] = {"null": NullAsrEngine()}


class UnknownAsrEngine(NullAsrEngine):
    """未知配置不能伪装成本地降级；调用链会按 unknown fail-closed。"""
    data_boundary = "unknown"

    def __init__(self, kind: str):
        self.version = f"unknown/{kind}"


def get_engine() -> AsrProvider:
    """按 ASR_ENGINE 选引擎(默认 auto:有 Key 走云,无 Key 降级 null)。
    已知但不可用的云引擎降级 null；未知配置保留 unknown 边界供调用链 fail-closed。"""
    kind = os.environ.get("ASR_ENGINE", "auto")
    if kind == "auto":
        kind = "dashscope" if os.environ.get("DASHSCOPE_API_KEY") else "null"
    if kind == "dashscope" and "dashscope" not in _ENGINES:
        _ENGINES["dashscope"] = DashScopeAsrEngine(
            os.environ.get("ASR_CLOUD_MODEL", "qwen3-asr-flash"))
    eng = _ENGINES.get(kind)
    if eng is None:
        return UnknownAsrEngine(kind)
    if isinstance(eng, DashScopeAsrEngine) and not eng.available():
        return _ENGINES["null"]
    return eng


def build_hotwords(bank: ItemBank, week1_script: dict | None = None) -> list[str]:
    """M 组热词:单要素目标词+可接受表达、双要素左右词;week1 传入则附属相闭表。去重保序。"""
    out: list[str] = []
    seen: set[str] = set()

    def add(w) -> None:
        if w and isinstance(w, str) and w not in seen:
            seen.add(w)
            out.append(w)

    for it in bank.single_element:
        add(it.get("target_word"))
        for x in it.get("acceptable_expressions") or []:
            add(x)
    for it in bank.double_element:
        add(it.get("left_word"))
        add(it.get("right_word"))
    if week1_script:
        for z in week1_script.get("zodiac_closed_list") or []:
            add(z)
    return out
