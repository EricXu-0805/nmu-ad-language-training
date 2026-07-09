"""枚举注册表（M6 拥有，冻结）——全项目共用取值，各模块不得本地私造。

对应共享数据契约里的枚举字段。改动须走版本化：枚举一旦冻结，只增不改语义。
"""
from __future__ import annotations
from enum import Enum


class PhaseType(str, Enum):
    """phase_type：显式绑定到具体测评/训练事件，不由 week_no 单独推导。"""
    关系建立 = "关系建立"
    基线测评 = "基线测评"
    正式训练 = "正式训练"
    前测 = "前测"
    后测 = "后测"
    随访 = "随访"
    探针测评 = "探针测评"


class EventLine(str, Enum):
    """同一周可并行的两条事件线（第1周：关系建立 + 基线测评窗）。"""
    关系建立环节 = "关系建立环节"
    基线测评窗 = "基线测评窗"
    正式训练 = "正式训练"


class TaskType(str, Enum):
    单要素 = "单要素"
    双要素 = "双要素"
    多要素 = "多要素"
    关系建立 = "关系建立"


class ItemSetType(str, Enum):
    训练集 = "训练集"
    探针集 = "探针集"
    画像采集 = "画像采集"


class AnswerType(str, Enum):
    """LLM/规则判分的回答类型（判分链）。沉默/拒答由交互状态机产生，不属判分回答类型。"""
    正确 = "正确"
    部分正确 = "部分正确"
    上位词或相关词 = "上位词或相关词"
    偏题 = "偏题"
    重复 = "重复"
    未识别 = "未识别"


class CueType(str, Enum):
    语义 = "语义"
    视觉 = "视觉"
    语音 = "语音"
    排除式 = "排除式"
    答案告知 = "答案告知"


class PromptLevel(int, Enum):
    自发正确 = 0
    轻度语义提示 = 1
    明确提示或语音线索 = 2
    告知答案 = 3


class InterventionType(str, Enum):
    """§13.1 允许介入。研究者代说物品名/称呼在关系建立周属允许、正式训练周属违规。"""
    操作设备 = "操作设备"
    安抚情绪 = "安抚情绪"
    帮助听清任务 = "帮助听清任务"
    重复系统已说题目 = "重复系统已说题目"
    修正ASR转写 = "修正ASR转写"
    暂停或结束训练 = "暂停或结束训练"
    代说物品名 = "代说物品名"          # 关系建立周允许；正式训练周→记为异常·线索性介入
    代说称呼 = "代说称呼"


class AbnormalType(str, Enum):
    拒绝继续 = "拒绝继续"
    明显疲劳 = "明显疲劳"
    长时间沉默 = "长时间沉默"
    激越烦躁 = "激越烦躁"
    情绪低落 = "情绪低落"
    线索性介入 = "线索性介入"        # 正式训练周的越界介入
    环境噪声 = "环境噪声"
    设备或网络中断 = "设备或网络中断"
    ASR严重错误 = "ASR严重错误"
    中途结束 = "中途结束"


class AudioStatus(str, Enum):
    recorded = "recorded"
    exported = "exported"
    checksum_verified = "checksum_verified"
    reliability_review_done = "reliability_review_done"
    deletable = "deletable"
    deleted = "deleted"


class ConsentType(str, Enum):
    本人同意 = "本人同意"
    代理同意加本人赞同 = "代理同意加本人赞同"


class JudgingMode(str, Enum):
    """判分模式：M0 只有 规则确定式；LLM 为阶段4 可关闭增强。"""
    规则确定式 = "规则确定式"
    LLM辅助 = "LLM辅助"


# 冻结契约：测试据此校验关键枚举成员齐全，防止后续误删/改名。
FROZEN_MEMBERS = {
    "PhaseType": {"关系建立", "基线测评", "正式训练", "前测", "后测", "随访", "探针测评"},
    "TaskType": {"单要素", "双要素", "多要素", "关系建立"},
    "ItemSetType": {"训练集", "探针集", "画像采集"},
    "CueType": {"语义", "视觉", "语音", "排除式", "答案告知"},
    "AudioStatus": {"recorded", "exported", "checksum_verified",
                    "reliability_review_done", "deletable", "deleted"},
}
