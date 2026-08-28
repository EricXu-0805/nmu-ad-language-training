"""出网路径的披露闸：代码里每多一条云调用，三张对外的数据流表必须跟着多一行。

2026-08-20 上线的量表 AI 初评（`app/questionnaire_ai_draft.py` → qwen-plus）是第四条
独立出网路径，而 README、DEPLOY 与 Gate0 材料包的表整整七天只有三行。伦理、法务和
研究者正是照那张表判断"哪些数据会离开机构"的——表漏一行，他们写的同意条款就漏一类。

这条闸按 `from dashscope import` 的**出现处数**对表行数，粗但抓得住"新加了云调用忘了
改表"。加新云能力时：先改表，再让这条测试变绿。
"""
from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]

# 每处 dashscope 导入所属的模块，以及它在三张表里各自的行关键词。
# 三张表措辞不同（README/DEPLOY 写 "TTS"，Gate0 写 "语音合成"），所以按表分别给词。
EXPECTED_EGRESS = {
    "app/tts.py": {"README.md": "TTS", "DEPLOY.md": "TTS",
                   "Gate0分类会材料包.md": "语音合成"},
    "app/asr.py": {"README.md": "ASR", "DEPLOY.md": "ASR",
                   "Gate0分类会材料包.md": "语音识别"},
    "app/llm_judge.py": {"README.md": "云 LLM 初评", "DEPLOY.md": "LLM 初评",
                         "Gate0分类会材料包.md": "大模型判定"},
    "app/questionnaire_ai_draft.py": {"README.md": "量表初评",
                                      "DEPLOY.md": "量表初评",
                                      "Gate0分类会材料包.md": "大模型初评"},
}


def _modules_importing_dashscope() -> set[str]:
    found = set()
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(?:from dashscope import|import dashscope)", text, re.M):
            found.add(str(path.relative_to(ROOT)))
    return found


def test_every_module_that_calls_the_cloud_is_a_known_egress_path():
    actual = _modules_importing_dashscope()
    unknown = actual - set(EXPECTED_EGRESS)
    assert not unknown, (
        "这些模块会调用云端，但不在已披露的出网路径清单里：\n"
        + "\n".join(sorted(unknown))
        + "\n先在 README.md / DEPLOY.md / docs/handover/Gate0分类会材料包.md "
          "三张数据流表里各加一行，再把它登记进 EXPECTED_EGRESS。")
    missing = set(EXPECTED_EGRESS) - actual
    assert not missing, (
        f"清单里这些模块已经不再调用云端，表里那几行该删或改：{sorted(missing)}")


def test_the_three_public_tables_all_carry_a_row_per_egress_path():
    tables = {
        "README.md": ROOT / "README.md",
        "DEPLOY.md": ROOT / "DEPLOY.md",
        "Gate0分类会材料包.md": ROOT / "docs/handover/Gate0分类会材料包.md",
    }
    for name, path in tables.items():
        text = path.read_text(encoding="utf-8")
        rows = [line for line in text.splitlines()
                if line.lstrip().startswith("|") and line.count("|") >= 4]
        for module, keywords in EXPECTED_EGRESS.items():
            keyword = keywords[name]
            assert any(keyword in row for row in rows), (
                f"{name} 的数据流表里找不到 {module} 那一行"
                f"（按关键词 “{keyword}” 找）——出网路径必须逐条对外披露")


def test_the_questionnaire_draft_payload_carries_no_subject_identifier():
    """表里写的是"载荷不含编号/姓名/画像"——这句话必须由代码兜住。"""
    text = (ROOT / "app/questionnaire_ai_draft.py").read_text(encoding="utf-8")
    body = re.search(r"def _build_prompt\(.*?\n(?=\ndef )", text, re.S)
    assert body is not None, "_build_prompt 不见了，披露表的依据也就没了"
    prompt = body.group(0)
    for banned in ("patient_id", "patient.", "consent_person", "subject_code",
                   "asr_text", "created_at"):
        assert banned not in prompt, (
            f"_build_prompt 里出现了 {banned}：出网载荷带上了受试者标识或逐题内容，"
            "而三张对外的数据流表都写着它不带")
