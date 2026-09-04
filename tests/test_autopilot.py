"""自动驾驶(M1-B)单测:协议内容闸 + 白名单模板展开 + 逐轮判类端点 + 游标反馈键流转。

红线覆盖:反馈只走"键+本地查表",游标契约仍不载文本;判类端点只读不建 turn;
协议话术缺失即校验报错(留空不兜底,不许静默哑掉)。
"""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app import content
from app.db import get_session
from app.main import _classify_with_operational_rubric, app

BANK = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
WK = content.load_week1_script(content.CONTENT_DIR / "week1_script.json")
PROTO = content.load_autopilot_protocol(content.CONTENT_DIR / "autopilot_protocol_v1.json")

BANK_VERSION = BANK.version_id


@pytest.fixture
def client():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)

    def override():
        with Session(eng) as s:
            yield s

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------- 协议内容闸 ----------------

def test_repo_protocol_passes_validation():
    assert content.validate_autopilot_protocol(PROTO) == []


def test_protocol_validation_catches_missing_lines():
    broken = {"protocol_version_id": "x", "silence_seconds": 8,
              "naming": {"success_after_cue1": {"unknown": "好"}},
              "double": {}}
    issues = content.validate_autopilot_protocol(broken)
    assert any("10 秒" in i for i in issues)
    assert any("close" in i for i in issues) and any("silence" in i for i in issues)
    assert any("success_after_cue2" in i for i in issues)
    assert any("namefix_left" in i for i in issues)


# ---------------- 白名单模板展开(红线:云端只见闭集具体句) ----------------

def test_allowlist_expands_protocol_templates_with_bank_words():
    allow = content.tts_allowlist(BANK, WK, PROTO)
    assert "很好，经过提醒您马上就想起来了，我们继续下一个。" in allow      # 无槽位句原样进
    assert "很好，您已经说出来了，这就是胡萝卜。我们继续下一个。" in allow  # 目标词展开
    assert "很好，您想起来了，这就是胡萝卜。我们继续下一个。" in allow
    assert "很好，经过提示您说出来了，这就是胡萝卜。我们继续下一个。" in allow
    d = BANK.double_element[0]
    assert f"这个我们刚刚见过，它叫{d['left_word']}。" in allow            # 双要素纠正句
    assert f"这个我们刚刚也见过，它叫{d['right_word']}。" in allow
    assert not any("【物品名】" in s for s in allow)                        # 模板原文绝不进


# ---------------- 逐轮判类端点(只读,不建 turn) ----------------

def test_judge_classify_correct_answer(client):
    exact = client.post("/judge/classify", json={"item_id": "SE_胡萝卜", "response_role": "命名",
                                                 "text": "胡萝卜"}).json()
    assert exact["answer_type"] == "正确" and exact["judge_mode"] == "规则确定式"
    assert exact["contains_target"] is True
    # 「这是胡萝卜」:垫词不改变说的是哪个词,2026-09-04 起判"正确"(以前是子串规则的
    # "部分正确");contains_target 真 → 自动驾驶算说对(表扬),这一点前后不变
    contains = client.post("/judge/classify", json={"item_id": "SE_胡萝卜", "response_role": "命名",
                                                    "text": "这是胡萝卜"}).json()
    assert contains["answer_type"] == "正确" and contains["contains_target"] is True
    # 带修饰的长句仍是子串规则的"部分正确"交人工,不抬成满分
    modified = client.post("/judge/classify", json={"item_id": "SE_胡萝卜", "response_role": "命名",
                                                    "text": "一根大胡萝卜"}).json()
    assert modified["answer_type"] == "部分正确" and modified["contains_target"] is True
    # 只说"萝卜":题库把它列进 related_but_inaccurate,所以判"上位词或相关词"而不是
    # 泛化子串规则的"部分正确"。关键不变量仍是 contains_target 假 → 按不准确升级,
    # 不当成功;两种类型在冻结分支里也都落 close。
    subword = client.post("/judge/classify", json={"item_id": "SE_胡萝卜", "response_role": "命名",
                                                   "text": "萝卜"}).json()
    assert subword["answer_type"] == "上位词或相关词" and subword["contains_target"] is False


def test_related_but_inaccurate_is_wired_and_never_counts_as_success(client):
    """题库字段叫 related_but_inaccurate;判分输入侧的字段名是 upper_terms。

    之前读的是题库里根本不存在的 upper_terms,"蔬菜"一路掉到未识别 → unknown 分支,
    冻结的 close 一级提示永远选不中。
    """
    close = client.post("/judge/classify", json={
        "item_id": "SE_胡萝卜", "response_role": "命名", "text": "蔬菜"}).json()
    assert close["answer_type"] == "上位词或相关词"      # → _failed_response_path 的 close
    assert close["matched_on"] == "upper"
    assert close["contains_target"] is False            # 相关但不准确,绝不算说对
    assert close["ai_score"] == 0.5 and close["needs_review"] is True
    assert close["truth_scope"] == "operational_only"

    # 保护:显式拒答仍是拒答,不得被相关词表吸走(→ unknown 分支)。
    refusal = client.post("/judge/classify", json={
        "item_id": "SE_胡萝卜", "response_role": "命名", "text": "不知道"}).json()
    assert refusal["answer_type"] == "拒答" and refusal["matched_on"] == "refusal"
    assert refusal["contains_target"] is False

    # 保护:表外的无关回答仍是未识别(→ unknown 分支),没有被放宽。
    unknown = client.post("/judge/classify", json={
        "item_id": "SE_胡萝卜", "response_role": "命名", "text": "汽车"}).json()
    assert unknown["answer_type"] == "未识别" and unknown["matched_on"] is None

    # 保护:空转写仍是沉默(→ silence 分支)。
    silence = client.post("/judge/classify", json={
        "item_id": "SE_胡萝卜", "response_role": "命名", "text": ""}).json()
    assert silence["answer_type"] == "沉默" and silence["matched_on"] == "silence"


def test_frozen_related_term_is_decided_before_the_llm_is_consulted():
    """精确命中冻结相关词表的这一格由确定式规则定,不交给 LLM 赌。

    生产 Qwen 对"蔬菜"这类回答并不稳定;判成正确会把 close 一级提示换成表扬,
    判成未识别会换成 unknown 分支的提示——两种都改了老人真正听到的话。
    """
    from app.enums import AnswerType
    from app.llm_judge import LlmJudgement
    from app.main import _classify_operational

    class _WrongEngine:
        version = "stub-judge-v1"
        data_boundary = "local"
        provider_id = None

        def __init__(self):
            self.calls = 0

        def judge(self, _ji):
            self.calls += 1
            return LlmJudgement(answer_type=AnswerType.正确, ai_score=1.0,
                                ai_needs_review=False, reason="stub")

    engine = _WrongEngine()
    result = _classify_operational(
        item_id="SE_胡萝卜", response_role="命名", text="蔬菜",
        bank=BANK, llm_engine=engine, cloud_llm_allowed=True)
    assert engine.calls == 0                              # 根本没问 LLM
    assert result["judge_mode"] == "规则确定式"
    assert result["answer_type"] == "上位词或相关词"
    assert result["contains_target"] is False

    # 对照:表外回答仍然照常走 LLM,这条旁路没有把整个 LLM 通道关掉。
    other = _classify_operational(
        item_id="SE_胡萝卜", response_role="命名", text="一个红色的东西",
        bank=BANK, llm_engine=engine, cloud_llm_allowed=True)
    assert engine.calls == 1 and other["judge_mode"] == "LLM辅助"


def test_contextless_judge_route_never_initializes_cloud_engine(client, monkeypatch):
    def forbidden_engine():
        raise AssertionError("/judge/classify 不得触发 LLM engine")

    monkeypatch.setattr("app.main.llm_judge.get_engine", forbidden_engine)
    response = client.post("/judge/classify", json={
        "item_id": "SE_胡萝卜", "response_role": "命名", "text": "胡萝卜",
    })
    assert response.status_code == 200
    assert response.json()["judge_mode"] == "规则确定式"


def test_judge_classify_open_role_fails_closed_without_frozen_rubric(
        client, monkeypatch, tmp_path):
    """2026-08-19 起 week2 开放环节 rubric 已全部冻结交付；「缺 rubric → 409」
    只能靠 staged 副本重现——这条 fail-closed 行为不因内容交付而消失。"""
    import json
    import shutil

    d = BANK.double_element[0]
    staged = tmp_path / "content-staged"
    shutil.copytree(content.CONTENT_DIR, staged)
    bank_path = staged / "item_bank_v1.json"
    data = json.loads(bank_path.read_text(encoding="utf-8"))
    removed = False
    for item in data["double_element"]:
        if item["item_id"] == d["item_id"]:
            del item["operational_rubrics"]["关系识别"]
            removed = True
    assert removed, f"staged 副本没找到 {d['item_id']}:关系识别 的 rubric"
    bank_path.write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(content, "CONTENT_DIR", staged)

    r = client.post("/judge/classify", json={"item_id": d["item_id"], "response_role": "关系识别",
                                             "text": "随便说说"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "operational_rubric_unavailable"

    silent = client.post("/judge/classify", json={
        "item_id": d["item_id"], "response_role": "关系识别", "text": "  ",
    })
    assert silent.status_code == 409


def test_versioned_open_answer_rubric_requires_actual_content_match():
    rubric = {
        "rubric_version": "clinical-v1",
        "decision_policy": "all_required_concepts",
        "required_concepts": ["杯子", "喝水"],
        "cues": {"1": "轻提示", "2": "明确提示"},
        "tell_answer": "用杯子喝水",
    }
    correct = _classify_with_operational_rubric(rubric, "这个杯子是拿来喝水的")
    partial = _classify_with_operational_rubric(rubric, "杯子")
    wrong = _classify_with_operational_rubric(rubric, "随便说说")
    silent = _classify_with_operational_rubric(rubric, " ")
    assert correct["answer_type"] == "正确" and correct["ai_score"] == 1.0
    assert partial["answer_type"] == "部分正确" and partial["ai_score"] == 0.5
    assert wrong["answer_type"] == "未识别" and wrong["ai_score"] == 0.0
    assert silent["answer_type"] == "沉默" and silent["matched_on"] == "silence"
    assert all(row["truth_scope"] == "operational_only"
               for row in (correct, partial, wrong, silent))


def test_judge_classify_unknown_item_404_and_extra_field_422(client):
    assert client.post("/judge/classify", json={"item_id": "SE_不存在", "text": "x"}).status_code == 404
    r = client.post("/judge/classify", json={"item_id": "SE_胡萝卜", "text": "x",
                                             "patient_name": "张三"})
    assert r.status_code == 422                       # extra=forbid:画像/患者字段结构性进不来


def test_judge_classify_writes_nothing(client):
    _seed(client)
    client.post("/judge/classify", json={"item_id": "SE_胡萝卜", "text": "胡萝卜"})
    journal = client.get("/sessions/S-AP-1/journal").json()
    assert journal["items"] == []                     # 判类不建 item/turn


# ---------------- 游标反馈键(不载文本,老人端免 PIN 读口可见) ----------------

def _seed(client):
    assert client.post("/patients", json={
        "patient_id": "P-AP", "consent_status": "已同意", "consent_type": "本人同意",
        "mandarin_eligible": True, "recording_allowed": True,
        "is_simulation_subject": True,
    }).status_code == 200
    assert client.post("/sessions", json={
        "session_id": "S-AP-1", "patient_id": "P-AP", "week_no": 2,
        "phase_type": "正式训练", "event_line": "正式训练",
        "item_bank_version_id": BANK_VERSION,
        "is_simulation": True,
    }).status_code == 200
    assert client.put("/live/state", json={"kind": "session", "payload": {
        "sessionId": "S-AP-1", "weekNo": 2, "eventLine": "正式训练",
        "mode": "task", "itemBankVersionId": BANK_VERSION, "wseq": 10,
    }}).status_code == 200


def test_cursor_fb_fields_roundtrip_to_patient(client):
    _seed(client)
    r = client.put("/live/state", json={"kind": "cursor", "payload": {
        "sessionId": "S-AP-1", "screen": "present", "itemIdx": 1, "turnIdx": 0,
        "responseRole": "命名", "cueLevel": 0, "recording": "idle",
        "fbKey": "cued2", "fbItemId": "SE_胡萝卜", "fbSeq": 3, "wseq": 11,
    }})
    assert r.status_code == 200, r.text
    cur = client.get("/live/state").json()["cursor"]
    assert cur["fbKey"] == "cued2" and "fbItemId" not in cur and cur["fbSeq"] == 3
    # canonical 反馈题指针只对账号端投影，不进入受试设备快照。
    assert client.get("/live/console-state").json()["cursor"]["fbItemId"] == "SE_胡萝卜"


def test_cursor_rejects_unknown_fb_key_and_free_text(client):
    _seed(client)
    bad_key = client.put("/live/state", json={"kind": "cursor", "payload": {
        "sessionId": "S-AP-1", "screen": "present", "itemIdx": 0, "turnIdx": 0,
        "responseRole": "命名", "cueLevel": 0, "recording": "idle",
        "fbKey": "自由文本反馈", "wseq": 12,
    }})
    assert bad_key.status_code == 422                 # 键是枚举:游标永远载不了话术文本
    free_text = client.put("/live/state", json={"kind": "cursor", "payload": {
        "sessionId": "S-AP-1", "screen": "present", "itemIdx": 0, "turnIdx": 0,
        "responseRole": "命名", "cueLevel": 0, "recording": "idle",
        "fbText": "你答对了", "wseq": 13,
    }})
    assert free_text.status_code == 422               # extra=forbid


# ── 2026-09-04 钱凯:「回答正确但 AI 判定为 0 分」──────────────────────────────
# 生产 8/31~9/4 完成的 196 次作答里 49 次(25%)被云判分判成「重复」0 分,49 次全部
# 含目标词(「窗帘，窗帘。」「嗯，茶杯，茶杯。」「花花」)。自动带练按 contains_target
# 推进,流程没走错;错的是研究者屏上的 AI 初评与复核时的初评建议。两道闸:整句就是
# 目标词→确定式规则定、不问 LLM;老人把目标词当完整的词说了出来而初评却是 0 分类型
# →改判部分正确待复核,但仍记 LLM 辅助+引擎版本+原初评理由(出网记录不消失)。

class _ZeroVerdictEngine:
    version = "fake-zero-1"
    data_boundary = "local"
    provider_id = None

    def __init__(self, verdict: str = "重复"):
        from app.enums import AnswerType
        self.verdict = AnswerType(verdict)
        self.calls = 0

    def judge(self, _ji):
        from app.llm_judge import LlmJudgement
        self.calls += 1
        return LlmJudgement(answer_type=self.verdict, ai_score=0.0,
                            ai_needs_review=False, reason="完全复述目标词两次")


def _legal(result) -> bool:
    from app.autopilot_service import legacy_judgement_result_is_legal
    return legacy_judgement_result_is_legal(result)


def test_target_said_twice_never_reaches_the_llm():
    from app.main import _classify_operational
    engine = _ZeroVerdictEngine()
    for text in ("胡萝卜，胡萝卜。", "嗯，胡萝卜。", "胡萝卜胡萝卜胡萝卜。", "这是胡萝卜。",
                 "一个胡萝卜。", "胡萝，卜。"):
        result = _classify_operational(
            item_id="SE_胡萝卜", response_role="命名", text=text,
            bank=BANK, llm_engine=engine, cloud_llm_allowed=True)
        assert engine.calls == 0, text
        assert result["judge_mode"] == "规则确定式", text
        assert (result["answer_type"], result["ai_score"], result["matched_on"]) == (
            "正确", 1.0, "target"), text
        assert result["contains_target"] is True and result["needs_review"] is False
        assert _legal(result), text   # 「胡萝，卜」:contains 按归一化文本算,契约不撞
    # 被标点拆开又带垫词:分段后不是整词,走子串分支并问 LLM;契约仍合法(contains 归一化为真)
    split = _classify_operational(
        item_id="SE_胡萝卜", response_role="命名", text="嗯，胡萝，卜。",
        bank=BANK, allow_llm=False)
    assert (split["answer_type"], split["matched_on"], split["contains_target"]) == ("部分正确", "substring", True)
    assert _legal(split)


@pytest.mark.parametrize("verdict", ["重复", "偏题", "未识别"])
def test_llm_zero_verdict_contradicting_spoken_target_is_reverdicted(verdict):
    from app.main import _classify_operational
    engine = _ZeroVerdictEngine(verdict)
    # 含目标词但还有别的字:要问 LLM;它答 0 分类型与「老人说出了目标词」矛盾 →
    # 改判部分正确待复核,仍记 LLM 辅助、留引擎版本、原初评进理由(出网记录不消失)。
    result = _classify_operational(
        item_id="SE_胡萝卜", response_role="命名", text="大胡萝卜，这是大胡萝卜。",
        bank=BANK, llm_engine=engine, cloud_llm_allowed=True)
    assert engine.calls == 1
    assert result["judge_mode"] == "LLM辅助" and result["judge_engine_version"] == "fake-zero-1"
    assert (result["answer_type"], result["ai_score"], result["needs_review"]) == ("部分正确", 0.5, True)
    assert result["matched_on"] is None and result["contains_target"] is True
    assert result["judge_reason"].startswith("含目标词「胡萝卜」，初评「" + verdict + "」不采信")
    assert "完全复述目标词两次" in result["judge_reason"]
    assert _legal(result)


def test_llm_zero_verdict_without_target_is_still_trusted():
    from app.main import _classify_operational
    engine = _ZeroVerdictEngine()
    result = _classify_operational(
        item_id="SE_胡萝卜", response_role="命名", text="一个红色的东西",
        bank=BANK, llm_engine=engine, cloud_llm_allowed=True)
    assert engine.calls == 1
    assert result["judge_mode"] == "LLM辅助" and result["answer_type"] == "重复"
    assert result["contains_target"] is False and result["judge_reason"] == "完全复述目标词两次"


@pytest.mark.parametrize("week,item,role,text,verdict", [
    (4, "DE_书包+书", "右命名", "书包。", "重复"),     # 照搬本题另一个要素=临床的重复
    (3, "DE_书架+书", "右命名", "书架", "重复"),
    (5, "DE_手+手套", "右命名", "手套，手套。", "重复"),
    (6, "SE_鱼", "命名", "鲸鱼。", "偏题"),           # 单字目标词藏在别的词里=别的东西
    (4, "SE_花", "命名", "花生。", "偏题"),
    (4, "SE_窗帘", "命名", "不是窗帘。", "偏题"),     # 否定
])
def test_single_char_or_negated_target_keeps_llm_zero_verdict(week, item, role, text, verdict):
    """「花瓶」里的「花」、「书架」里的「书」不算说出了目标词;LLM 判偏题/重复是对的,
    不改判(复核 2026-09-04)。contains_target 仍按字面子串——那是推进契约,不在此改。"""
    from app.main import _classify_operational
    engine = _ZeroVerdictEngine(verdict)
    result = _classify_operational(
        item_id=item, response_role=role, text=text,
        bank=content.load_item_bank_for_week(week), llm_engine=engine, cloud_llm_allowed=True)
    assert engine.calls == 1, (item, text)
    assert result["judge_mode"] == "LLM辅助" and result["answer_type"] == verdict, (item, text)
    assert result["judge_reason"] == "完全复述目标词两次"
    assert _legal(result)


@pytest.mark.parametrize("week,item,role,text", [
    (4, "DE_窗户+窗帘", "左命名", "窗帘，窗帘。"),   # 生产原样(9/4 钱凯截图那一题的双要素版)
    (4, "DE_窗户+窗帘", "右命名", "嗯，窗户。"),
    (2, "DE_狐狸+鸡", "右命名", "鸡鸡。"),          # 单字目标词叠音
    (7, "SE_门", "命名", "对，门。"),                # 单字目标词 + 独立的单字垫词段
])
def test_de_roles_and_single_char_targets_are_decided_by_rules_and_stay_legal(week, item, role, text):
    from app.main import _classify_operational
    engine = _ZeroVerdictEngine()
    result = _classify_operational(
        item_id=item, response_role=role, text=text,
        bank=content.load_item_bank_for_week(week), llm_engine=engine, cloud_llm_allowed=True)
    assert engine.calls == 0, (item, text)
    assert (result["judge_mode"], result["answer_type"], result["matched_on"]) == (
        "规则确定式", "正确", "target"), (item, text)
    assert result["contains_target"] is True and _legal(result)


def test_single_char_compound_is_not_the_target():
    """「对门」不是「门」:单字目标词旁的单字垫词不剥,走子串分支交人工。"""
    from app.main import _classify_operational
    engine = _ZeroVerdictEngine("偏题")
    result = _classify_operational(
        item_id="SE_门", response_role="命名", text="对门。",
        bank=content.load_item_bank_for_week(7), llm_engine=engine, cloud_llm_allowed=True)
    assert engine.calls == 1
    # LLM 的偏题保留(「对门」不算说出了「门」)
    assert result["judge_mode"] == "LLM辅助" and result["answer_type"] == "偏题"
    rule_only = _classify_operational(
        item_id="SE_门", response_role="命名", text="对门。",
        bank=content.load_item_bank_for_week(7), allow_llm=False)
    assert (rule_only["answer_type"], rule_only["matched_on"]) == ("部分正确", "substring")


def test_acceptable_expression_only_text_through_classify():
    """SE_刀子 的可接受表达是「刀」:「嗯，刀，刀。」由规则定正确;「一把刀」带修饰
    走子串分支部分正确(以前漏了可接受表达,落成未识别 0 分而 contains_target 却为真)。"""
    from app.main import _classify_operational
    bank3 = content.load_item_bank_for_week(3)
    engine = _ZeroVerdictEngine()
    only = _classify_operational(item_id="SE_刀子", response_role="命名", text="嗯，刀，刀。",
                                 bank=bank3, llm_engine=engine, cloud_llm_allowed=True)
    assert engine.calls == 0 and (only["answer_type"], only["matched_on"]) == ("正确", "acceptable")
    assert only["contains_target"] is True and _legal(only)
    # 「一把刀」:量词短语就是那个词,同样由规则定
    phrase = _classify_operational(item_id="SE_刀子", response_role="命名", text="一把刀。",
                                   bank=bank3, llm_engine=engine, cloud_llm_allowed=True)
    assert engine.calls == 0 and (phrase["answer_type"], phrase["matched_on"]) == ("正确", "acceptable")
    # 「小刀」:带修饰的可接受表达走子串分支部分正确(以前漏了可接受表达,落成未识别 0 分而
    # contains_target 却为真);单字「刀」藏在「小刀」里不算整词,LLM 的偏题保留
    partial = _classify_operational(item_id="SE_刀子", response_role="命名", text="小刀。",
                                    bank=bank3, allow_llm=False)
    assert (partial["answer_type"], partial["matched_on"]) == ("部分正确", "substring")
    assert partial["contains_target"] is True and _legal(partial)
    engine2 = _ZeroVerdictEngine("偏题")
    kept = _classify_operational(item_id="SE_刀子", response_role="命名", text="小刀。",
                                 bank=bank3, llm_engine=engine2, cloud_llm_allowed=True)
    assert engine2.calls == 1 and kept["answer_type"] == "偏题" and kept["judge_mode"] == "LLM辅助"
    # 多字可接受表达带修饰时初评矛盾才改判:用「刀子」本身
    engine3 = _ZeroVerdictEngine("偏题")
    reverdicted = _classify_operational(item_id="SE_刀子", response_role="命名", text="一把小刀子。",
                                        bank=bank3, llm_engine=engine3, cloud_llm_allowed=True)
    assert engine3.calls == 1 and reverdicted["answer_type"] == "部分正确"
    assert reverdicted["needs_review"] is True and reverdicted["judge_mode"] == "LLM辅助" and _legal(reverdicted)


def test_dialect_repeat_is_decided_by_rules_with_review():
    """方言俗名连说两遍:规则定正确(留复核),不问 LLM。题库今天没有方言行,用改写的题。"""
    import copy
    from app.main import _classify_operational
    bank = copy.deepcopy(BANK)
    item = next(it for it in bank.single_element if it["item_id"] == "SE_胡萝卜")
    item["dialect_synonyms"] = ["红萝卜"]
    engine = _ZeroVerdictEngine()
    result = _classify_operational(item_id="SE_胡萝卜", response_role="命名", text="红萝卜，红萝卜。",
                                   bank=bank, llm_engine=engine, cloud_llm_allowed=True)
    assert engine.calls == 0
    assert (result["answer_type"], result["matched_on"], result["needs_review"]) == ("正确", "dialect", True)
    assert _legal(result)
