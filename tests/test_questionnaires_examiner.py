"""量表电子记录：examiner_scored（ACE-III / 动物流畅性）的装载、值域、计分与 API 链。

两份数据包都是检查者当场判分的电子记录：每项存检查者录入的分值/计数，
分档换算、认知域小计、总分与界值判定只在锁定时按源表执行。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import auth, questionnaire_ai_draft
from app import db as app_db
from app.content import CONTENT_DIR, FrozenContentUnavailable
from app.db import get_session
from app.main import app
from app.models import Patient, ResearchUser
from app.questionnaires import (
    QuestionnaireValidationError,
    assert_lock_complete,
    compute_scoring,
    examiner_domain_totals,
    load_questionnaire_registry,
    validate_value_write,
)

REGISTRY = load_questionnaire_registry()
ACE = REGISTRY["ace3_v1"].definition
AFT = REGISTRY["aft_v1"].definition

# 英文原版 ACE-III 字母流畅性分档（中文版此处印错，勘误 1 待钱凯裁决）。
CHE_BINS_EXPECTED = [(18, None, 7), (14, 17, 6), (11, 13, 5), (8, 10, 4),
                     (6, 7, 3), (4, 5, 2), (2, 3, 1), (0, 1, 0)]
# 动物分档表源表逐字，无重叠。
ANIMAL_BINS_EXPECTED = [(22, None, 7), (17, 21, 6), (14, 16, 5), (11, 13, 4),
                        (9, 10, 3), (7, 8, 2), (5, 6, 1), (0, 4, 0)]


def _ace_item(item_key: str):
    assert ACE.examiner_panel is not None
    return next(item for item in ACE.examiner_panel.items if item.item_key == item_key)


def _ace_full_values(**overrides: str) -> dict[tuple[str, str], str]:
    """每项取上限：计分框记满分，两项流畅性总数记 30（≥ 最高档），重复次数记 1。"""
    assert ACE.examiner_panel is not None
    values: dict[tuple[str, str], str] = {}
    for item in ACE.examiner_panel.items:
        entry = item.entry
        if entry.kind == "count" and entry.bins:
            values[(item.item_key, "value")] = "30"
        elif entry.kind == "count":
            values[(item.item_key, "value")] = "1"
        else:
            values[(item.item_key, "value")] = str(entry.max)
    for key, value in overrides.items():
        values[(key, "value")] = value
    return values


def _aft_values(group: str, total: str, repeats: str = "0",
                irrelevant: str = "0") -> dict[tuple[str, str], str]:
    return {
        ("aft_01", "value"): group,
        ("aft_02", "value"): total,
        ("aft_03", "value"): repeats,
        ("aft_04", "value"): irrelevant,
    }


# ---------------- A. 真实数据包的逐字事实 ----------------

def test_ace3_pack_pins_form_structure():
    assert ACE.response_kind == "examiner_scored"
    assert ACE.respondent == "examiner_administered"
    assert ACE.status == "prototype"
    assert ACE.provenance.source_sha256 == (
        "e1a420fde55740aa9aba15ae2dd70ce3a0c27477ec491ae3753676893e56b067")
    panel = ACE.examiner_panel
    assert panel is not None
    assert [(d.domain_key, d.title, d.max_score) for d in panel.domains] == [
        ("attention", "注意力", 18), ("memory", "记忆力", 26),
        ("fluency", "语言流利性", 14), ("language", "语言", 26),
        ("visuospatial", "视空间", 16)]
    assert ACE.scoring is not None and ACE.scoring.kind == "examiner_sum"
    assert ACE.scoring.max_score == 100
    assert ACE.scoring.cutoff is None and ACE.scoring.stratified_cutoff is None
    assert len(panel.items) == 25
    assert sum(1 for item in panel.items if item.entry.scored) == 24
    # 唯一不计分的记录栏：三个词组的重复次数。
    unscored = [item for item in panel.items if not item.entry.scored]
    assert [(item.item_key, item.text) for item in unscored] == [("ace_04", "重复次数")]
    # 屏上顺序 = 原表顺序：流畅性两项紧跟三词回忆之后。
    assert [item.item_key for item in panel.items][5:8] == ["ace_06", "ace_07", "ace_08"]
    assert _ace_item("ace_07").domain_key == "fluency"
    assert "车" in _ace_item("ace_07").text
    assert _ace_item("ace_08").domain_key == "fluency"
    assert "动物" in _ace_item("ace_08").text


def test_ace3_fluency_bins_follow_english_original_and_record_the_erratum():
    che = _ace_item("ace_07").entry.bins
    animals = _ace_item("ace_08").entry.bins
    assert che is not None and animals is not None
    assert [(b.min, b.max, b.score) for b in che] == CHE_BINS_EXPECTED
    assert [(b.min, b.max, b.score) for b in animals] == ANIMAL_BINS_EXPECTED
    erratum = [note for note in ACE.transcription_notes if note.startswith("勘误 1")]
    assert len(erratum) == 1
    assert "3-4→1" in erratum[0] and "2-3→1" in erratum[0] and "钱凯" in erratum[0]
    assert "总数 2 记 1 分（源表记 0 分）" in erratum[0]


def test_aft_pack_pins_source_cutoffs_by_education_group():
    assert AFT.response_kind == "examiner_scored"
    assert AFT.provenance.source_sha256 == (
        "08b75798c09bdb961d32291fc0d983b6e403b0c684b8f8c4869e70db2d715d7d")
    assert AFT.scoring is not None and AFT.scoring.kind == "examiner_sum"
    assert AFT.scoring.max_score is None
    stratified = AFT.scoring.stratified_cutoff
    assert stratified is not None
    assert stratified.by_item == "aft_01"
    assert {group: (rule.operator, rule.value) if rule else None
            for group, rule in stratified.groups.items()} == {
        "初中组": ("<=", 12), "高中组": ("<=", 13), "大学组": ("<=", 14),
        "初中以下或不详": None}
    panel = AFT.examiner_panel
    assert panel is not None
    assert [(item.item_key, item.entry.kind, item.entry.scored) for item in panel.items] == [
        ("aft_01", "choice", False), ("aft_02", "count", True),
        ("aft_03", "count", False), ("aft_04", "count", False)]


# ---------------- B. 装载器对畸形定义 fail-closed ----------------

def _content_copy(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "content"
    qdir = base / "questionnaires"
    shutil.copytree(CONTENT_DIR / "questionnaires", qdir)
    return base, qdir


def _rewrite(qdir: Path, questionnaire_id: str, mutate) -> None:
    path = qdir / f"{questionnaire_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    index_path = qdir / "questionnaire_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    for entry in index["questionnaires"]:
        entry["content_sha256"] = hashlib.sha256(
            (qdir / entry["file"]).read_bytes()).hexdigest()
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def _ace_raw_item(data: dict, item_key: str) -> dict:
    return next(item for item in data["examiner_panel"]["items"] if item["item_key"] == item_key)


def test_untampered_copy_loads_as_control(tmp_path):
    base, _ = _content_copy(tmp_path)
    assert {"ace3_v1", "aft_v1"} <= set(load_questionnaire_registry(base))


def test_overlapping_bins_are_rejected(tmp_path):
    # 把源表印错的重叠档原样放进去：3-4→1 与 4-5→2 同时命中 4，装载器必须拒。
    base, qdir = _content_copy(tmp_path)

    def overlap(data):
        _ace_raw_item(data, "ace_07")["entry"]["bins"] = [
            {"min": 18, "max": None, "score": 7}, {"min": 14, "max": 17, "score": 6},
            {"min": 11, "max": 13, "score": 5}, {"min": 8, "max": 10, "score": 4},
            {"min": 6, "max": 7, "score": 3}, {"min": 4, "max": 5, "score": 2},
            {"min": 3, "max": 4, "score": 1}, {"min": 0, "max": 2, "score": 0}]
    _rewrite(qdir, "ace3_v1", overlap)
    with pytest.raises(FrozenContentUnavailable, match="连续且不重叠"):
        load_questionnaire_registry(base)


def test_domain_max_mismatch_is_rejected(tmp_path):
    base, qdir = _content_copy(tmp_path)
    _rewrite(qdir, "ace3_v1",
             lambda data: data["examiner_panel"]["domains"][0].update(max_score=17))
    with pytest.raises(FrozenContentUnavailable, match="≠ max_score"):
        load_questionnaire_registry(base)


def test_total_max_must_equal_domain_sum(tmp_path):
    base, qdir = _content_copy(tmp_path)
    _rewrite(qdir, "ace3_v1", lambda data: data["scoring"].update(max_score=99))
    with pytest.raises(FrozenContentUnavailable, match="≠ scoring.max_score"):
        load_questionnaire_registry(base)


def test_stratified_groups_must_match_the_choice_allowed_set(tmp_path):
    base, qdir = _content_copy(tmp_path)
    _rewrite(qdir, "aft_v1",
             lambda data: data["scoring"]["stratified_cutoff"]["groups"].pop("大学组"))
    with pytest.raises(FrozenContentUnavailable, match="allowed 完全一致"):
        load_questionnaire_registry(base)


def test_examiner_panel_on_a_binary_pack_is_rejected(tmp_path):
    base, qdir = _content_copy(tmp_path)
    ace = json.loads((qdir / "ace3_v1.json").read_text(encoding="utf-8"))
    _rewrite(qdir, "gds15_v1",
             lambda data: data.update(examiner_panel=ace["examiner_panel"]))
    with pytest.raises(FrozenContentUnavailable, match="binary_scored 不接受"):
        load_questionnaire_registry(base)


def test_examiner_pack_with_binary_scoring_is_rejected(tmp_path):
    base, qdir = _content_copy(tmp_path)
    gds = json.loads((qdir / "gds15_v1.json").read_text(encoding="utf-8"))
    _rewrite(qdir, "ace3_v1", lambda data: data.update(scoring=gds["scoring"]))
    with pytest.raises(FrozenContentUnavailable, match="必须是 examiner_sum"):
        load_questionnaire_registry(base)


def test_unscored_score_entry_is_rejected(tmp_path):
    base, qdir = _content_copy(tmp_path)
    _rewrite(qdir, "ace3_v1",
             lambda data: _ace_raw_item(data, "ace_01")["entry"].update(scored=False))
    with pytest.raises(FrozenContentUnavailable, match="scored 必须为 true"):
        load_questionnaire_registry(base)


# ---------------- C. 值域 ----------------

@pytest.mark.parametrize("value", ["0", "3", "5"])
def test_score_box_accepts_canonical_integers_in_range(value):
    assert validate_value_write(ACE, "ace_01", "value", value) is None


@pytest.mark.parametrize("value", ["6", "-1", "05", "1.5", "", " 3", "三", "+2"])
def test_score_box_rejects_out_of_range_and_non_canonical(value):
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        validate_value_write(ACE, "ace_01", "value", value)
    assert excinfo.value.code == "questionnaire_value_out_of_domain"
    assert str(excinfo.value) == "「注意力」第 1 题的数值必须是 0–5 之间的整数"


def test_count_box_accepts_up_to_its_max_and_choice_only_allowed_values():
    assert validate_value_write(ACE, "ace_07", "value", "60") is None
    with pytest.raises(QuestionnaireValidationError):
        validate_value_write(ACE, "ace_07", "value", "61")
    assert validate_value_write(AFT, "aft_01", "value", "初中组") is None
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        validate_value_write(AFT, "aft_01", "value", "小学组")
    assert "初中组、高中组、大学组、初中以下或不详 之一" in str(excinfo.value)
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        validate_value_write(ACE, "ace_99", "value", "1")
    assert excinfo.value.code == "questionnaire_field_unknown"
    assert validate_value_write(ACE, "ace_01", "value", None) is None


# ---------------- D. 锁定完整性 ----------------

def test_lock_lists_missing_examiner_items_with_domain_labels():
    partial = _ace_full_values()
    del partial[("ace_01", "value")]
    del partial[("ace_08", "value")]
    del partial[("ace_04", "value")]  # 不计分的记录栏同样必填
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        assert_lock_complete(ACE, partial)
    assert sorted(excinfo.value.problems) == sorted([
        "「注意力」第 1 题未评", "「注意力」第 4 题未评", "「语言流利性」第 8 题未评"])
    assert assert_lock_complete(ACE, _ace_full_values()) is None
    assert assert_lock_complete(AFT, _aft_values("初中组", "12")) is None


def test_lock_rejects_stored_value_outside_domain():
    values = _ace_full_values(ace_16="13")
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        assert_lock_complete(ACE, values)
    assert excinfo.value.problems == ["「语言」第 16 题的记录值 '13' 不在允许范围内"]


# ---------------- E. 计分 ----------------

def test_ace3_full_marks_sum_to_100_with_domain_subtotals_and_no_cutoff():
    values = _ace_full_values()
    assert examiner_domain_totals(ACE, values) == {
        "attention": 18, "memory": 26, "fluency": 14, "language": 26, "visuospatial": 16}
    assert compute_scoring(ACE, values) == {
        "computed_total": 100.0,
        "cutoff_met": None,
        "computed_flag": None,
        "scoring_rule_id": "ace3_cn2012.form_scoring.v1",
    }


def test_ace3_unscored_repeat_count_never_enters_the_total():
    assert compute_scoring(ACE, _ace_full_values(ace_04="3"))["computed_total"] == 100.0
    assert compute_scoring(ACE, _ace_full_values(ace_04="0"))["computed_total"] == 100.0


@pytest.mark.parametrize("count,points", [
    ("0", 0), ("1", 0), ("2", 1), ("3", 1), ("4", 2), ("5", 2), ("6", 3), ("7", 3),
    ("8", 4), ("10", 4), ("11", 5), ("13", 5), ("14", 6), ("17", 6), ("18", 7), ("60", 7),
])
def test_che_fluency_count_converts_by_the_english_original_table(count, points):
    totals = examiner_domain_totals(ACE, _ace_full_values(ace_07=count))
    assert totals["fluency"] == 7 + points


@pytest.mark.parametrize("count,points", [
    ("0", 0), ("4", 0), ("5", 1), ("6", 1), ("7", 2), ("8", 2), ("9", 3), ("10", 3),
    ("11", 4), ("13", 4), ("14", 5), ("16", 5), ("17", 6), ("21", 6), ("22", 7), ("60", 7),
])
def test_animal_fluency_count_converts_by_the_source_table(count, points):
    totals = examiner_domain_totals(ACE, _ace_full_values(ace_08=count))
    assert totals["fluency"] == 7 + points


def test_ace3_partial_marks_add_up_by_domain():
    values = _ace_full_values(ace_01="3", ace_06="1", ace_07="4", ace_16="9", ace_21="2")
    totals = examiner_domain_totals(ACE, values)
    assert totals == {"attention": 16, "memory": 24, "fluency": 9,
                      "language": 23, "visuospatial": 13}
    assert compute_scoring(ACE, values)["computed_total"] == 85.0


def test_ace3_scoring_refuses_a_missing_scored_item():
    values = _ace_full_values()
    del values[("ace_21", "value")]
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        compute_scoring(ACE, values)
    assert excinfo.value.code == "questionnaire_scoring_incomplete"


@pytest.mark.parametrize("group,total,met,flag", [
    ("初中组", "12", True, "得分≤界值（初中组界值 12）"),
    ("初中组", "13", False, None),
    ("高中组", "13", True, "得分≤界值（高中组界值 13）"),
    ("高中组", "14", False, None),
    ("大学组", "14", True, "得分≤界值（大学组界值 14）"),
    ("大学组", "15", False, None),
    ("初中以下或不详", "3", None, "该文化程度分组源表无界值，未判定"),
])
def test_aft_cutoff_is_stratified_by_education_group(group, total, met, flag):
    assert compute_scoring(AFT, _aft_values(group, total)) == {
        "computed_total": float(total),
        "cutoff_met": met,
        "computed_flag": flag,
        "scoring_rule_id": "aft.source_cutoffs.v1",
    }


def test_aft_unknown_group_value_is_a_validation_error_not_a_crash():
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        compute_scoring(AFT, _aft_values("小学组", "10"))
    assert excinfo.value.code == "questionnaire_scoring_incomplete"


def test_lowest_bin_must_score_zero(tmp_path):
    base, qdir = _content_copy(tmp_path)

    def lift_floor(data):
        _ace_raw_item(data, "ace_08")["entry"]["bins"] = [
            {"min": 22, "max": None, "score": 7}, {"min": 17, "max": 21, "score": 6},
            {"min": 14, "max": 16, "score": 5}, {"min": 11, "max": 13, "score": 4},
            {"min": 9, "max": 10, "score": 3}, {"min": 7, "max": 8, "score": 2},
            {"min": 0, "max": 6, "score": 1}]
    _rewrite(qdir, "ace3_v1", lift_floor)
    with pytest.raises(FrozenContentUnavailable, match="最低档必须记 0 分"):
        load_questionnaire_registry(base)


def test_aft_repeat_and_irrelevant_counts_never_enter_the_total():
    scored = compute_scoring(AFT, _aft_values("大学组", "20", repeats="5", irrelevant="9"))
    assert scored["computed_total"] == 20.0


# ---------------- F. API 全链 ----------------

PASSWORD = "examiner-pw-2026"


@pytest.fixture
def api_env(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(app_db, "engine", engine)
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(ResearchUser(
            username="research-x", display_id="RESEARCH-X",
            password_hash=auth.hash_password(PASSWORD), role="researcher"))
        session.add(Patient(
            patient_id="P-EX1", consent_status="已同意", mandarin_eligible=True,
            cloud_processing_allowed=True))
        session.commit()

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    yield engine
    app.dependency_overrides.clear()


def _client() -> TestClient:
    client = TestClient(app)
    response = client.post("/auth/login", json={
        "username": "research-x", "password": PASSWORD})
    assert response.status_code == 200, response.text
    csrf = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert csrf
    client.headers.update({"X-CSRF-Token": csrf})
    return client


def _create(client: TestClient, questionnaire_id: str) -> str:
    response = client.post(
        "/patients/P-EX1/questionnaire-records",
        json={"questionnaire_id": questionnaire_id, "phase_label": "前测"})
    assert response.status_code == 200, response.text
    return response.json()["record_id"]


def _payload(values: dict[tuple[str, str], str]) -> list[dict]:
    return [{"item_key": key, "field_key": field, "value": value}
            for (key, field), value in values.items()]


def test_ace3_chain_create_values_lock_reports_total_without_cutoff(api_env):
    client = _client()
    record_id = _create(client, "ace3_v1")
    values = _ace_full_values(ace_01="4", ace_07="4", ace_08="13", ace_16="10")
    put = client.put(f"/questionnaire-records/{record_id}/values",
                     json={"values": _payload(values)})
    assert put.status_code == 200, put.text
    assert len(put.json()["values"]) == 25
    assert {row["value_source"] for row in put.json()["values"]} == {"human_direct"}
    locked = client.post(f"/questionnaire-records/{record_id}/lock")
    assert locked.status_code == 200, locked.text
    body = locked.json()
    # 注意力 17 + 记忆 26 + 流畅性 (2+4) + 语言 24 + 视空间 16 = 89
    assert body["computed_total"] == 89.0
    assert body["cutoff_met"] is None
    assert body["computed_flag"] is None
    assert body["scoring_rule_id"] == "ace3_cn2012.form_scoring.v1"
    stored = {(row["item_key"], row["field_key"]): row["final_value"]
              for row in body["values"]}
    assert stored[("ace_07", "value")] == "4"  # 存原始总数，不存换算分


def test_ace3_value_write_rejects_out_of_range_with_domain_label(api_env):
    client = _client()
    record_id = _create(client, "ace3_v1")
    put = client.put(f"/questionnaire-records/{record_id}/values",
                     json={"values": [
                         {"item_key": "ace_16", "field_key": "value", "value": "13"}]})
    assert put.status_code == 409, put.text
    detail = put.json()["detail"]
    assert detail["code"] == "questionnaire_value_invalid"
    assert detail["problems"] == ["「语言」第 16 题的数值必须是 0–12 之间的整数"]


def test_ace3_lock_refuses_incomplete_panel_listing_domains(api_env):
    client = _client()
    record_id = _create(client, "ace3_v1")
    values = _ace_full_values()
    del values[("ace_23", "value")]
    put = client.put(f"/questionnaire-records/{record_id}/values",
                     json={"values": _payload(values)})
    assert put.status_code == 200, put.text
    lock = client.post(f"/questionnaire-records/{record_id}/lock")
    assert lock.status_code == 409, lock.text
    detail = lock.json()["detail"]
    assert detail["code"] == "questionnaire_lock_incomplete"
    assert detail["problems"] == ["「视空间」第 23 题未评"]


def test_aft_chain_locks_with_group_cutoff(api_env):
    client = _client()
    record_id = _create(client, "aft_v1")
    put = client.put(f"/questionnaire-records/{record_id}/values",
                     json={"values": _payload(_aft_values("高中组", "11", "2", "1"))})
    assert put.status_code == 200, put.text
    locked = client.post(f"/questionnaire-records/{record_id}/lock")
    assert locked.status_code == 200, locked.text
    body = locked.json()
    assert body["computed_total"] == 11.0
    assert body["cutoff_met"] is True
    assert body["computed_flag"] == "得分≤界值（高中组界值 13）"


def test_ai_draft_is_not_applicable_for_examiner_scored_packs(api_env, monkeypatch):
    def never_call(_prompt):
        raise AssertionError("examiner_scored 初评绝不该走到 LLM")

    monkeypatch.setattr(questionnaire_ai_draft, "_call_llm", never_call)
    client = _client()
    for questionnaire_id in ("ace3_v1", "aft_v1"):
        record_id = _create(client, questionnaire_id)
        drafted = client.post(f"/questionnaire-records/{record_id}/ai-draft")
        assert drafted.status_code == 200, drafted.text
        assert drafted.json()["ai_draft_status"] == "not_applicable"
        assert drafted.json()["ai_draft_engine"] is None


def test_definitions_endpoint_serves_examiner_panel_and_scoring_shapes(api_env):
    client = _client()
    catalog = client.get("/questionnaires/definitions")
    assert catalog.status_code == 200, catalog.text
    by_id = {row["definition"]["questionnaire_id"]: row["definition"]
             for row in catalog.json()["questionnaires"]}
    ace = by_id["ace3_v1"]
    assert ace["examiner_panel"]["domains"][0]["domain_key"] == "attention"
    assert ace["examiner_panel"]["items"][6]["entry"]["bins"][0] == {
        "min": 18, "max": None, "score": 7}
    assert ace["scoring"]["kind"] == "examiner_sum"
    assert by_id["gds15_v1"]["examiner_panel"] is None
    assert by_id["aft_v1"]["scoring"]["stratified_cutoff"]["groups"]["初中以下或不详"] is None


# ---------------- G. 与前端 exactKeys 解析器的键面对齐 ----------------

_FRONTEND_CONTRACT = (Path(__file__).resolve().parents[1]
                      / "web" / "src" / "console" / "questionnaires.ts")


def _frontend_key_list(name: str) -> set[str]:
    source = _FRONTEND_CONTRACT.read_text(encoding="utf-8")
    match = re.search(rf"const {name} = \[(.*?)\] as const;", source, re.S)
    assert match, f"前端契约里找不到 {name}"
    return set(re.findall(r'"([a-z_0-9]+)"', match.group(1)))


def test_serialized_definitions_match_frontend_exact_key_lists():
    """前端解析器多一键/少一键整包拒收——服务端 model_dump 出来的每个对象键面
    必须与 questionnaires.ts 的 exactKeys 清单逐个相等。回退验证:给 _ExaminerItem
    加回 `score_when: None = None`,本测试必须红(2026-08-27 真 Chrome 走查实录:
    量表目录整包被拒,五份量表一份都打不开)。"""
    keys = {
        "definition": _frontend_key_list("DEFINITION_KEYS"),
        "panel": _frontend_key_list("EXAMINER_PANEL_KEYS"),
        "domain": _frontend_key_list("EXAMINER_DOMAIN_KEYS"),
        "item": _frontend_key_list("EXAMINER_ITEM_KEYS"),
        "entry": _frontend_key_list("EXAMINER_ENTRY_KEYS"),
        "bin": _frontend_key_list("SCORE_BIN_KEYS"),
        "examiner_scoring": _frontend_key_list("EXAMINER_SCORING_KEYS"),
        "binary_scoring": _frontend_key_list("BINARY_SCORING_KEYS"),
        "cutoff": _frontend_key_list("CUTOFF_KEYS"),
        "stratified": _frontend_key_list("STRATIFIED_CUTOFF_KEYS"),
        "choice": _frontend_key_list("CHOICE_KEYS"),
    }
    for questionnaire_id, loaded in REGISTRY.items():
        dumped = loaded.definition.model_dump(mode="json")
        assert set(dumped) == keys["definition"], questionnaire_id
        scoring = dumped["scoring"]
        if scoring is not None:
            expected = keys["examiner_scoring"] if scoring["kind"] == "examiner_sum" \
                else keys["binary_scoring"]
            assert set(scoring) == expected, questionnaire_id
            if scoring.get("cutoff") is not None:
                assert set(scoring["cutoff"]) == keys["cutoff"]
            if scoring.get("stratified_cutoff") is not None:
                assert set(scoring["stratified_cutoff"]) == keys["stratified"]
                for rule in scoring["stratified_cutoff"]["groups"].values():
                    assert rule is None or set(rule) == keys["cutoff"]
        panel = dumped["examiner_panel"]
        if panel is None:
            continue
        assert set(panel) == keys["panel"], questionnaire_id
        for domain in panel["domains"]:
            assert set(domain) == keys["domain"], (questionnaire_id, domain)
        for item in panel["items"]:
            assert set(item) == keys["item"], (questionnaire_id, item["item_key"])
            assert set(item["entry"]) == keys["entry"], (questionnaire_id, item["item_key"])
            for row in item["entry"]["bins"] or []:
                assert set(row) == keys["bin"]
            if item["entry"]["choice"] is not None:
                assert set(item["entry"]["choice"]) == keys["choice"]
