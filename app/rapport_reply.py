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
import re
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
# 2026-09-04 起属相/兴趣/活动三问进云:属相之后最顺口的追问就是「哪一年的」「高寿」,
# 老人一答就是出生年份/年龄——正是被排除在云外的两问。上面的子串黑名单兜不住这些
# 变体(复核实测「您是哪一年的？」「您今年高寿？」「几零年生的呀？」「比我大多少呀？」
# 全部放行),这里再加一道按模式的兜底;命中即回落句库,误伤的代价只是少一句现编。
_IDENTITY_PROBE_RE = re.compile(
    r"哪一?年|几零年|几几年|出生|生于|生日|年纪|年龄|高寿|贵庚|周岁|虚岁|岁数"
    r"|多大|多少岁|几岁|比我大|比我小|生的[呀啊吧呢吗?？]"
    r"|怎么称呼|尊姓|大名|全名|家住|住在|住哪|哪个小区|门牌|手机号|身份证"
)
MAX_ROUNDS_DEFAULT = 2
MAX_ROUNDS_CEILING = 5


def max_rounds() -> int:
    """每问最多聊几轮(RAPPORT_MAX_ROUNDS,默认 2,夹在 1..5)。"""
    raw = (os.environ.get("RAPPORT_MAX_ROUNDS") or "").strip()
    try:
        value = int(raw) if raw else MAX_ROUNDS_DEFAULT
    except ValueError:
        value = MAX_ROUNDS_DEFAULT
    return min(MAX_ROUNDS_CEILING, max(1, value))


# 末轮之后不再开麦:任何把话头递回老人的句子都不许出现——问号只是最显眼的一种,
# 「再讲讲…吧」「说说您的家人」「您常去那儿吗。」把问号换成句号一样是追问。
_INVITING_TAILS = ("吗", "呢", "吧", "么")
_INVITING_PHRASES = (
    "讲讲", "说说", "聊聊", "谈谈", "再多讲", "再多说", "跟我讲", "跟我说",
    "告诉我", "您接着", "你接着", "接着说", "再说说", "多说两句", "多讲两句",
)


def invites_reply(text: str) -> bool:
    if "？" in text or "?" in text:
        return True
    body = text.rstrip("。！!…、,，.\u3000 ")
    if body.endswith(_INVITING_TAILS):
        return True
    return any(phrase in text for phrase in _INVITING_PHRASES)


def validate_reply_text(raw: object, *, final: bool = False) -> Optional[str]:
    """生成文本的守卫:通过返回清洗后的一句话,任何越界返回 None(回落句库)。

    final=True(本问最后一轮):只许收束,不许再向老人抛问题——那一轮之后不再开麦,
    留一个悬着的问号等于让老人对着空气说话。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > MAX_REPLY_CHARS:
        return None
    if final and invites_reply(text):
        return None
    # Cc 含全部 ASCII 控制符;Cf 含零宽空格/ZWJ 等格式字符——TTS 念不出、
    # 回放屏契约会整场拒收(fail-closed 且账本只追加,落一行永远修不好)。
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in text):
        return None
    if any(sub in text for sub in _FORBIDDEN_SUBSTRINGS):
        return None
    if _IDENTITY_PROBE_RE.search(text):
        return None
    return text


History = tuple[tuple[Optional[str], str], ...]   # ((老人说, 机器人说), ...) 按轮次


def build_reply_prompt(ask: str, asr_text: str, history: History = (),
                       round_no: int = 1, max_rounds_value: int = 1) -> str:
    final = round_no >= max_rounds_value
    lines = [
        "你是陪伴长者聊天的语音机器人“小语”,声音温和、语速慢。"
        "你问了长者一个问题,你们围绕这个问题聊了几句。请生成你接下来说的**一句**口语化回应,"
        '输出 JSON {"reply": "..."}。',
        f"你的问题:{ask}",
    ]
    if history:
        lines.append("前几轮对话(按先后):")
        for i, (elder, robot) in enumerate(history, 1):
            lines.append(f"  第{i}轮 长者:{elder if elder else '(没听清)'}")
            lines.append(f"  第{i}轮 你:{robot}")
    lines.append(f"长者最新的回答(语音识别转写,可能有错字):{asr_text}")
    lines.append(f"这是本问的第{round_no}轮,最多{max_rounds_value}轮。")
    lines.append("要求:")
    lines.append("1. 只说一句,12~30个汉字,像面对面聊天那样自然,不书面;不要重复你前几轮说过的话。")
    if final:
        lines.append("2. 这是最后一轮:先接住长者刚说的内容,再给一句温暖的肯定或收束。"
                     "**不要再提任何问题,句尾不能是问号**——这句之后不再开麦。")
    else:
        lines.append("2. 先接住长者说的内容(可以轻轻复述其中一个词),再给一个轻的开放式追问,让长者能接着说。")
    lines.append("3. 绝不询问姓名、年龄、出生年份、住址、身份证等身份信息——长者说了属相也不许"
                 "追问或推算是哪一年生的、多大年纪;绝不给医疗建议;绝不纠正或否定长者。")
    lines.append("4. 转写明显不通顺时按大意回应,不逐字复述;完全看不懂就说一句温和的承接话。")
    lines.append("5. 不用感叹号连用,不用“哇”“太棒了”这类夸张词。")
    return "\n".join(lines)


class RapportReplyProvider(Protocol):
    version: str
    data_boundary: str
    provider_id: str | None

    def generate(self, ask: str, asr_text: str, history: History = (),
                 round_no: int = 1, max_rounds_value: int = 1) -> Optional[str]: ...


class OffReplyEngine:
    """默认引擎:恒不可用 → 调用方回落冻结回应库。"""
    version = "off"
    data_boundary = "local"
    provider_id = None

    def generate(self, ask: str, asr_text: str, history: History = (),
                 round_no: int = 1, max_rounds_value: int = 1) -> Optional[str]:
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

    def generate(self, ask: str, asr_text: str, history: History = (),
                 round_no: int = 1, max_rounds_value: int = 1) -> Optional[str]:
        if not self.available():
            return None
        try:
            raw = self._call(build_reply_prompt(
                ask, asr_text, history, round_no, max_rounds_value))
        except Exception:
            return None
        return self._parse(raw, final=round_no >= max_rounds_value)

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

    def _parse(self, raw, *, final: bool = False) -> Optional[str]:
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
        return validate_reply_text(data["reply"], final=final)


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
