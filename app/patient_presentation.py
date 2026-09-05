"""受试者端当前呈现内容的最小投影。

这是浏览器自动驾驶尚未被服务端状态机完全替代前的过渡边界。它只从
服务端已验证的当前游标生成“此刻已被下发”的一条线索/反馈/话术；
不向配对设备下发整份题库、答案表、后续提示或其他问题。
"""
from __future__ import annotations

import re

from . import rapport_reply
from typing import Any


FEEDBACK_KEYS = frozenset({
    "self", "cued1_unknown", "cued1_close", "cued1_silence",
    "cued2", "namefix_l", "namefix_r",
})

# 设备引用只表示冻结计划中的位置，不含目标词，也不是 secret。
# 必须与 capability 已绑定的 session 一起解析，单独的 ref 没有全局身份。
_ITEM_REF = re.compile(r"^itm-([0-9]{4})$")
_TURN_REF = re.compile(r"^(itm-[0-9]{4})#([1-9][0-9]*)$")


def item_ref(item_idx: int) -> str:
    if isinstance(item_idx, bool) or item_idx < 0 or item_idx >= 9_999:
        raise ValueError("受试者题位超出 opaque ref 范围")
    return f"itm-{item_idx + 1:04d}"


def item_index_from_ref(value: str) -> int:
    match = _ITEM_REF.fullmatch(value)
    if match is None:
        raise ValueError("设备 item_ref 格式非法")
    position = int(match.group(1))
    if position < 1:
        raise ValueError("设备 item_ref 题位非法")
    return position - 1


def turn_ref(item_idx: int, turn_seq: int) -> str:
    if isinstance(turn_seq, bool) or turn_seq < 1:
        raise ValueError("设备 turn_seq 非法")
    return f"{item_ref(item_idx)}#{turn_seq}"


def resolve_task_turn_ref(plan: Any, value: str) -> tuple[str, int, int]:
    """Resolve a session-contextual device ref to the canonical frozen turn key."""
    match = _TURN_REF.fullmatch(value)
    if match is None:
        raise ValueError("设备 turn ref 格式非法")
    item_idx = item_index_from_ref(match.group(1))
    turn_seq = int(match.group(2))
    items = getattr(plan, "items", None)
    if not isinstance(items, (list, tuple)) or item_idx >= len(items):
        raise ValueError("设备 turn ref 超出场次冻结计划")
    item = items[item_idx]
    turns = getattr(item, "turns", None)
    if not isinstance(turns, (list, tuple)):
        raise ValueError("场次冻结计划缺少环节")
    if not any(getattr(turn, "turn_seq", None) == turn_seq for turn in turns):
        raise ValueError("设备 turn ref 不属于该题冻结环节")
    return f"{item.item_id}#{turn_seq}", item_idx, turn_seq


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required_text(value: Any, label: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise ValueError(f"当前呈现缺少{label}")
    return text


def _fill(template: Any, word: Any, label: str) -> str:
    text = _required_text(template, label)
    target = _required_text(word, "目标词")
    if "【物品名】" not in text:
        raise ValueError(f"{label}缺少【物品名】槽位")
    return text.replace("【物品名】", target)


def resolve_task_texts(
    *,
    item_id: str,
    task_type: str,
    display: dict,
    response_role: str,
    cue_level: int,
    protocol: dict,
    feedback_key: str | None,
    feedback_item_id: str | None,
) -> tuple[str | None, str | None]:
    """只返回当前游标已授权显示/朗读的文本。

    反馈指针必须精确指向当前题；任何跨题或结构错位都失败关闭，
    不会借机查找或返回其他题的内容。
    """
    if cue_level not in {0, 1, 2, 3}:
        raise ValueError("当前提示等级非法")

    cue_text: str | None = None
    if cue_level > 0 and task_type == "单要素":
        if cue_level in {1, 2}:
            cues = display.get("cues")
            cue_text = _required_text(
                cues.get(str(cue_level)) if isinstance(cues, dict) else None,
                f"第{cue_level}级线索",
            )
        else:
            cue_text = _required_text(display.get("tell_answer"), "告知答案话术")
    elif cue_level > 0 and task_type == "双要素":
        field = {
            "左作用": "left_function_cue",
            "右作用": "right_function_cue",
            "关系识别": "relation_cue",
        }.get(response_role)
        # 双要素命名纠正由 feedback_key 下发，本身没有 cue 文本。
        if field is not None:
            cue_text = _required_text(display.get(field), f"{response_role}线索")

    if feedback_key is None:
        if feedback_item_id is not None:
            raise ValueError("反馈题指针缺少 feedback_key")
        return cue_text, None
    if feedback_key not in FEEDBACK_KEYS:
        raise ValueError("未知反馈键")
    if feedback_item_id != item_id:
        raise ValueError("反馈指针不属于当前题")

    naming = protocol.get("naming")
    naming = naming if isinstance(naming, dict) else {}
    cue1 = naming.get("success_after_cue1")
    cue1 = cue1 if isinstance(cue1, dict) else {}
    double = protocol.get("double")
    double = double if isinstance(double, dict) else {}

    if feedback_key == "self":
        if task_type != "单要素":
            raise ValueError("自发正确反馈只适用于单要素命名")
        feedback_text = _required_text(display.get("success_line"), "自发正确反馈")
    elif feedback_key == "cued1_unknown":
        if task_type != "单要素":
            raise ValueError("命名提示反馈只适用于单要素")
        feedback_text = _required_text(cue1.get("unknown"), "第1次提示后反馈")
    elif feedback_key in {"cued1_close", "cued1_silence"}:
        if task_type != "单要素":
            raise ValueError("命名提示反馈只适用于单要素")
        branch = "close" if feedback_key == "cued1_close" else "silence"
        feedback_text = _fill(cue1.get(branch), display.get("target_word"), "第1次提示后反馈")
    elif feedback_key == "cued2":
        if task_type != "单要素":
            raise ValueError("命名提示反馈只适用于单要素")
        feedback_text = _fill(
            naming.get("success_after_cue2"), display.get("target_word"),
            "第2次提示后反馈",
        )
    else:
        if task_type != "双要素":
            raise ValueError("双要素命名纠正只适用于双要素")
        side = "left" if feedback_key == "namefix_l" else "right"
        feedback_text = _fill(
            double.get(f"namefix_{side}"), display.get(f"{side}_word"),
            f"{side}命名纠正反馈",
        )
    return cue_text, feedback_text


RAPPORT_TURN_PREFIX = "关系建立·"
# 自我介绍节里回答本身就是直接身份信息的问位(脚本槽位名):这两问的录音整段标记含
# 直接标识、永不进云。属相/兴趣/活动不在其列(2026-09-04 Eric 拍板放行进云)。
RAPPORT_IDENTITY_SLOT_FIELDS = frozenset({"preferred_appellation", "age"})


def rapport_turn_key(section_key: str, question_idx: int | None = None) -> str:
    """第1周录音的 turn_key。带问位=「关系建立·<节>#<问>」(2026-09-04 起设备开麦那一刻
    锁存问位,服务端据此按问放行自动回应);不带问位是旧版按节绑定的键,只读兼容。"""
    if question_idx is None:
        return f"{RAPPORT_TURN_PREFIX}{section_key}"
    return f"{RAPPORT_TURN_PREFIX}{section_key}#{question_idx}"


def parse_rapport_turn_key(turn_key: str | None) -> tuple[str, int | None] | None:
    """「关系建立·<节>[#<问>]」→ (节, 问|None);不是第1周键返回 None。"""
    if not isinstance(turn_key, str) or not turn_key.startswith(RAPPORT_TURN_PREFIX):
        return None
    body = turn_key[len(RAPPORT_TURN_PREFIX):]
    if not body:
        return None
    section, sep, idx = body.partition("#")
    if not sep:
        return section, None
    if not section or not idx.isdigit():
        return None
    return section, int(idx)


def rapport_allowed_turn_keys(script: dict) -> frozenset[str]:
    """冻结脚本允许的全部第1周录音键:每节的节级键(旧版/无问节)+ 每一问的问级键。"""
    keys: set[str] = set()
    for section in script.get("sections", []) or []:
        if not isinstance(section, dict) or not section.get("key"):
            continue
        key = str(section["key"])
        keys.add(rapport_turn_key(key))
        questions = section.get("questions") or []
        for idx in range(max(1, len(questions))):
            keys.add(rapport_turn_key(key, idx))
    return frozenset(keys)


def rapport_identity_question_indices(script: dict, section_key: str) -> frozenset[int]:
    section = next((row for row in script.get("sections", []) or []
                    if isinstance(row, dict) and row.get("key") == section_key), None)
    if section is None:
        return frozenset()
    return frozenset(
        idx for idx, question in enumerate(section.get("questions") or [])
        if isinstance(question, dict)
        and question.get("slot_field") in RAPPORT_IDENTITY_SLOT_FIELDS)


def rapport_turn_requires_identity_flag(script: dict, turn_key: str | None) -> bool:
    """这段录音是否必须整段标记含直接标识:自我介绍节的姓名/年龄两问;旧版按节绑定的
    自我介绍录音分不清是哪一问,一律要求(与撤回/导出红线同口径)。"""
    parsed = parse_rapport_turn_key(turn_key)
    if parsed is None:
        return False
    section, idx = parsed
    identity = rapport_identity_question_indices(script, section)
    if not identity:
        return False
    return idx is None or idx in identity


def rapport_reply_allowed_here(bank: dict, section_key: str, question_idx: int) -> bool:
    """这一问是否开放选句。姓名/年龄那两问永远不在里面——回答是直接身份信息。"""
    for row in bank.get("applies_to") or []:
        if (isinstance(row, dict) and row.get("section_key") == section_key
                and row.get("question_idx") == question_idx):
            return True
    return False


def rapport_round_limit(bank: dict, section_key: str, question_idx: int) -> int:
    """这一问最多聊几轮:全局上限与回应库里该问位的 max_rounds 取小。

    属相一问是 1:老人答完属相,机器人只接一句、不追问——属相之后最顺口的追问就是
    「哪一年的」「高寿」,老人一答就是被排除在云外的出生年份/年龄(2026-09-05 Eric 拍板)。
    """
    limit = rapport_reply.max_rounds()
    for row in bank.get("applies_to") or []:
        if (isinstance(row, dict) and row.get("section_key") == section_key
                and row.get("question_idx") == question_idx):
            cap = row.get("max_rounds")
            if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1:
                return min(limit, cap)
            return limit
    return limit


def rapport_bank_reply_line(bank: dict, reply_id: str,
                            section_key: str, question_idx: int) -> str | None:
    """回应库里那一句；这一问不开放选句、或者 id 不在闭集里，一律 None。"""
    if not rapport_reply_allowed_here(bank, section_key, question_idx):
        return None
    for row in bank.get("replies") or []:
        if isinstance(row, dict) and row.get("id") == reply_id:
            text = row.get("text")
            return text if isinstance(text, str) and text.strip() else None
    return None


def rapport_bank_invites_more(bank: dict, reply_id: str | None) -> bool:
    """这句是不是把话头递回老人(冻结句库自带的 invites_more)。

    多轮对话据此决定说完要不要再开麦:收束句(「那我们聊点别的吧」)之后开麦,
    老人会收到互相矛盾的指令。
    """
    if reply_id is None:
        return False
    for row in bank.get("replies") or []:
        if isinstance(row, dict) and row.get("id") == reply_id:
            return bool(row.get("invites_more"))
    return False


def rapport_reply_line(script: dict, section_key: str, question_idx: int) -> str | None:
    """当前一问在冻结脚本里写好的回应句；脚本没写就是 None（不代拟）。"""
    sections = script.get("sections")
    if not isinstance(sections, list):
        return None
    section = next((row for row in sections
                    if isinstance(row, dict) and row.get("key") == section_key), None)
    if section is None or section.get("speaker") != "机器人":
        return None
    questions = section.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    if question_idx < 0 or question_idx >= len(questions):
        return None
    question = questions[question_idx]
    if not isinstance(question, dict):
        return None
    reply = question.get("success")
    if not isinstance(reply, str) or not reply.strip():
        return None
    return _reply_without_unfilled_slots(script, reply)


_SLOT_IN_LINE = re.compile(r"【([^】]+)】")


def _reply_without_unfilled_slots(script: dict, reply: str) -> str | None:
    """回应句里的槽位要填老人刚说的话；还没有可填的值时改说脚本写的备用句。

    冻结脚本给每个槽位都写了 fallback_line，正是为这一刻准备的。带槽位的模板
    本身不在云 TTS 白名单里（实例化后含老人自述内容=患者数据），照原样下发会把
    「【老人所说的兴趣】」这七个字念给老人听。
    """
    slot_names = _SLOT_IN_LINE.findall(reply)
    if not slot_names:
        return reply
    slots = script.get("slots")
    if not isinstance(slots, dict):
        return None
    fallbacks = {
        (slots.get(name) or {}).get("fallback_line") for name in slot_names
    }
    if len(fallbacks) != 1:
        return None
    fallback = fallbacks.pop()
    return fallback if isinstance(fallback, str) and fallback.strip() else None


def resolve_rapport_text(
    script: dict,
    *,
    section_key: str,
    question_idx: int,
    beat: str = "ask",
    reply_id: str | None = None,
    reply_bank: dict | None = None,
) -> tuple[str, str | None]:
    """投影第1周当前一拍；不返回其他节、其他问或画像槽位。

    beat="ask" 是问句，beat="reply" 是老人答完后机器人说的那句。回应句只能来自
    冻结脚本的 success 字段——脚本没写就拒绝，绝不由代码代拟一句给老人听。
    """
    sections = script.get("sections")
    if not isinstance(sections, list):
        raise ValueError("关系建立脚本缺少 sections")
    section = next((row for row in sections
                    if isinstance(row, dict) and row.get("key") == section_key), None)
    if section is None:
        raise ValueError("当前 sectionKey 不在冻结关系建立脚本中")
    speaker = _required_text(section.get("speaker"), "话术说话人")
    if speaker not in {"机器人", "研究者"}:
        raise ValueError("当前关系建立话术的说话人未被允许")
    if beat not in {"ask", "reply"}:
        raise ValueError("当前关系建立话拍未被允许")
    if beat == "reply":
        if speaker != "机器人":
            raise ValueError("研究者节没有机器人回应句")
        if reply_id is not None:
            if reply_bank is None:
                raise ValueError("回应库未就绪，拒绝按 id 投影回应句")
            reply = rapport_bank_reply_line(
                reply_bank, reply_id, section_key, question_idx)
            if reply is None:
                raise ValueError("当前一问不接受这条回应句")
            return speaker, reply
        reply = rapport_reply_line(script, section_key, question_idx)
        if reply is None:
            raise ValueError("冻结关系建立脚本没有为当前一问写回应句")
        return speaker, reply
    questions = section.get("questions")
    if isinstance(questions, list) and questions:
        if question_idx < 0 or question_idx >= len(questions):
            raise ValueError("当前 questionIdx 超出冻结关系建立脚本")
        question = questions[question_idx]
        if not isinstance(question, dict):
            raise ValueError("当前关系建立问题结构非法")
        text = _required_text(question.get("ask"), "当前关系建立问句")
    else:
        if question_idx != 0:
            raise ValueError("无问题列表的节只允许 questionIdx=0")
        text = _required_text(section.get("line"), "当前关系建立话术") \
            if speaker == "机器人" else None
    # 研究者节只当面说，老人端固定显示中性欢迎语，不下发 note。
    return speaker, text if speaker == "机器人" else None
