"""第1周关系建立:LLM 开放式回应生成(可插拔、恒可降级)。

边界(与 llm_judge 同一口径,但方向相反——这里**发出的是老人的自由发言**):
  · 只在受试者已授权云处理时被调用(调用方持 cloud_processing 门禁与
    serialized_subject_egress;本模块自身不做授权判定,但声明 cloud 边界);
  · 只产一句"机器人接下来说的话",**永不判分、永不写入任何评分链**;
  · 生成失败/格式可疑/内容越界 → None,调用方回落冻结回应库
    (j1 没听清 / j2 没说话 / k1 兜底),链路不断、老人永远有回应;
  · RAPPORT_REPLY 环境变量切换(默认 auto:有 DASHSCOPE_API_KEY → qwen,无 → off)。

生成文本的下游约束:它不在云 TTS 静态白名单里。发声必须走"服务端持久
utterance 行"的专用通道(main.py 的 rapport utterance TTS 端点),客户端
永远不能把自由文本递进任何合成入口。
"""
from __future__ import annotations

import json
import os
import unicodedata
from typing import Optional, Protocol


# 一句给老人听的话的硬边界:太长听不住,含槽位符/换行说明生成器没守规矩。
MAX_REPLY_CHARS = 60
_REPLY_KEYS = frozenset({"reply"})
_FORBIDDEN_SUBSTRINGS = (
    "【", "】", "{", "}", "[", "]",       # 槽位符/结构残留
    # 身份问询(姓名/年龄两问被明确排除在自由对话之外)。黑名单非穷尽,
    # 只兜常见问法;第一道约束是 prompt,伦理口径按"概率性防线"评估。
    "您叫什么", "你叫什么", "贵姓", "姓什么", "您的名字", "你的名字",
    "多大年纪", "几岁了", "多少岁", "哪年出生", "出生年",
    "身份证", "住在哪", "家住哪", "住址", "地址", "电话",
)
def validate_reply_text(raw: object) -> Optional[str]:
    """生成文本的守卫:通过返回清洗后的一句话,任何越界返回 None(回落句库)。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > MAX_REPLY_CHARS:
        return None
    # Cc 含全部 ASCII 控制符;Cf 含零宽空格/ZWJ 等格式字符——TTS 念不出、
    # 回放屏契约会整场拒收(fail-closed 且账本只追加,落一行永远修不好)。
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in text):
        return None
    if any(sub in text for sub in _FORBIDDEN_SUBSTRINGS):
        return None
    return text


def build_reply_prompt(ask: str, asr_text: str) -> str:
    return (
        "你是陪伴长者聊天的语音机器人“小语”,声音温和、语速慢。"
        "刚才你问了长者一个问题,长者回答了。请生成你接下来说的**一句**口语化回应,"
        '输出 JSON {"reply": "..."}。\n'
        f"你的问题:{ask}\n"
        f"长者的回答(语音识别转写,可能有错字):{asr_text}\n"
        "要求:\n"
        "1. 只说一句,12~30个汉字,像面对面聊天那样自然,不书面。\n"
        "2. 先接住长者说的内容(可以轻轻复述其中一个词),再给一句肯定或一个轻的开放式追问。\n"
        "3. 绝不询问姓名、年龄、住址、身份证等身份信息;绝不给医疗建议;绝不纠正或否定长者。\n"
        "4. 转写明显不通顺时按大意回应,不逐字复述;完全看不懂就说一句温和的承接话。\n"
        "5. 不用感叹号连用,不用“哇”“太棒了”这类夸张词。"
    )


class RapportReplyProvider(Protocol):
    version: str
    data_boundary: str
    provider_id: str | None

    def generate(self, ask: str, asr_text: str) -> Optional[str]: ...


class OffReplyEngine:
    """默认引擎:恒不可用 → 调用方回落冻结回应库。"""
    version = "off"
    data_boundary = "local"
    provider_id = None

    def generate(self, ask: str, asr_text: str) -> Optional[str]:
        return None


class UnknownReplyEngine(OffReplyEngine):
    """未知配置不得被当成安全的本地 off 引擎。"""
    data_boundary = "unknown"

    def __init__(self, kind: str):
        self.version = f"unknown/{kind}"


class QwenReplyEngine:
    """阿里百炼 qwen 回应生成。任何异常/格式可疑/内容越界 → None 回落句库。"""

    data_boundary = "cloud"
    provider_id = "aliyun-dashscope"

    def __init__(self, model: str):
        self._model = model
        self.version = f"dashscope/{model}"

    def available(self) -> bool:
        return bool(os.environ.get("DASHSCOPE_API_KEY"))

    def generate(self, ask: str, asr_text: str) -> Optional[str]:
        if not self.available():
            return None
        try:
            raw = self._call(build_reply_prompt(ask, asr_text))
        except Exception:
            return None
        return self._parse(raw)

    def _call(self, prompt: str) -> str | None:
        from dashscope import Generation
        resp = Generation.call(model=self._model,
                               messages=[{"role": "user", "content": prompt}],
                               result_format="message",
                               response_format={"type": "json_object"},
                               temperature=0.7,
                               request_timeout=15)  # 老人在等,等不起 SDK 默认 300s
        out = getattr(resp, "output", None)
        choices = getattr(out, "choices", None) if out is not None else None
        if not choices:
            return None
        return choices[0].message.content

    def _parse(self, raw) -> Optional[str]:
        if not raw or not isinstance(raw, str):
            return None
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict) or set(data) != _REPLY_KEYS:
            return None
        return validate_reply_text(data["reply"])


_ENGINES: dict[str, RapportReplyProvider] = {"off": OffReplyEngine()}


def register_engine(name: str, engine: RapportReplyProvider) -> None:
    """真实引擎接入点;测试亦经此注入替身。"""
    _ENGINES[name] = engine


def get_engine() -> RapportReplyProvider:
    """按 RAPPORT_REPLY 选引擎;未知配置保留 unknown 边界供调用链 fail-closed。"""
    kind = os.environ.get("RAPPORT_REPLY", "auto")
    if kind == "auto":
        kind = "qwen" if os.environ.get("DASHSCOPE_API_KEY") else "off"
    if kind == "qwen" and "qwen" not in _ENGINES:
        _ENGINES["qwen"] = QwenReplyEngine(
            os.environ.get("RAPPORT_REPLY_MODEL", "qwen-plus"))
    return _ENGINES.get(kind) or UnknownReplyEngine(kind)
