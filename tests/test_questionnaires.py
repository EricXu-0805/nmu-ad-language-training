"""量表电子记录（原型道）：装载器 fail-closed / 值域与锁定 / API 集成。

覆盖三份临床提供的原型量表（SFACS/GDS-15/NPI-Q）的字节钉装载、
作答值域与锁定完整性合同、GDS-15 源表计分逐字规则，以及
create→values→ai-draft→lock 的 HTTP 全链与锁定后的 ORM 不可变层。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import auth, questionnaire_ai_draft
from app import db as app_db
from app.content import CONTENT_DIR, FrozenContentUnavailable
from app.db import get_session
from app.main import app
from app.models import (
    AuditLog,
    Patient,
    QuestionnaireItemValue,
    QuestionnaireRecord,
    ResearchUser,
)
from app.questionnaire_ai_draft import DraftItem, DraftOutcome
from app.questionnaires import (
    QuestionnaireValidationError,
    assert_lock_complete,
    compute_scoring,
    load_questionnaire_registry,
    validate_value_write,
)


REGISTRY = load_questionnaire_registry()
SFACS = REGISTRY["sfacs_v1"].definition
GDS = REGISTRY["gds15_v1"].definition
NPIQ = REGISTRY["npiq_v1"].definition

# GDS-15 源表计分说明的逐字事实：1，5，7，11 答“否”记 1 分，其余答“是”记 1 分。
GDS_REVERSED_KEYS = {"gds_01", "gds_05", "gds_07", "gds_11"}


# ---------------- A. 装载器 fail-closed ----------------

def _questionnaire_content_copy(tmp_path: Path) -> tuple[Path, Path]:
    """把真实 content/questionnaires 拷到临时 content 根，供逐项破坏。"""
    base = tmp_path / "content"
    qdir = base / "questionnaires"
    shutil.copytree(CONTENT_DIR / "questionnaires", qdir)
    return base, qdir


def _rewrite_package(qdir: Path, questionnaire_id: str, mutate) -> None:
    path = qdir / f"{questionnaire_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _repin_index(qdir: Path) -> None:
    """重算索引哈希，让字节钉通过——用于证明拒绝来自 schema 而不是哈希。"""
    index_path = qdir / "questionnaire_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    for entry in index["questionnaires"]:
        entry["content_sha256"] = hashlib.sha256(
            (qdir / entry["file"]).read_bytes()).hexdigest()
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def test_real_content_packages_load_with_expected_item_counts():
    registry = load_questionnaire_registry()
    assert set(registry) == {"sfacs_v1", "gds15_v1", "npiq_v1", "ace3_v1", "aft_v1"}
    assert {qid: len(loaded.definition.all_items())
            for qid, loaded in registry.items()} == {
        "sfacs_v1": 21, "gds15_v1": 15, "npiq_v1": 12, "ace3_v1": 25, "aft_v1": 4,
    }
    for loaded in registry.values():
        assert loaded.definition.status == "prototype"
        assert len(loaded.content_sha256) == 64


def test_untampered_copy_is_a_loadable_control(tmp_path):
    # 对照组：拷贝本身可装载。后面的拒绝测试红，才归因于各自的破坏。
    base, _ = _questionnaire_content_copy(tmp_path)
    assert set(load_questionnaire_registry(base)) == {
        "sfacs_v1", "gds15_v1", "npiq_v1", "ace3_v1", "aft_v1"}


def test_tampered_package_bytes_are_rejected_by_the_index_byte_pin(tmp_path):
    base, qdir = _questionnaire_content_copy(tmp_path)
    _rewrite_package(
        qdir, "sfacs_v1",
        lambda data: data.update(instruction=data["instruction"] + "。"))
    # 故意不重钉索引：单文件静默漂移必须被字节钉拒绝。
    with pytest.raises(FrozenContentUnavailable, match="字节钉拒绝装载"):
        load_questionnaire_registry(base)


def test_non_prototype_status_is_rejected_even_with_matching_hash(tmp_path):
    base, qdir = _questionnaire_content_copy(tmp_path)
    _rewrite_package(
        qdir, "gds15_v1", lambda data: data.update(status="final"))
    _repin_index(qdir)
    with pytest.raises(FrozenContentUnavailable, match="prototype"):
        load_questionnaire_registry(base)


def test_binary_scored_package_missing_scoring_is_rejected(tmp_path):
    base, qdir = _questionnaire_content_copy(tmp_path)
    _rewrite_package(qdir, "gds15_v1", lambda data: data.update(scoring=None))
    _repin_index(qdir)
    with pytest.raises(FrozenContentUnavailable,
                       match="binary_scored 需要 value_field/items/scoring"):
        load_questionnaire_registry(base)


def test_ordinal_sections_package_with_scoring_is_rejected(tmp_path):
    base, qdir = _questionnaire_content_copy(tmp_path)
    borrowed_scoring = json.loads(
        (qdir / "gds15_v1.json").read_text(encoding="utf-8"))["scoring"]
    _rewrite_package(
        qdir, "sfacs_v1", lambda data: data.update(scoring=borrowed_scoring))
    _repin_index(qdir)
    with pytest.raises(FrozenContentUnavailable,
                       match="ordinal_sections 源表未定义计分"):
        load_questionnaire_registry(base)


def test_symptom_triplet_package_with_scoring_is_rejected(tmp_path):
    base, qdir = _questionnaire_content_copy(tmp_path)
    borrowed_scoring = json.loads(
        (qdir / "gds15_v1.json").read_text(encoding="utf-8"))["scoring"]
    _rewrite_package(
        qdir, "npiq_v1", lambda data: data.update(scoring=borrowed_scoring))
    _repin_index(qdir)
    with pytest.raises(FrozenContentUnavailable,
                       match="symptom_triplet 源表未定义计分"):
        load_questionnaire_registry(base)


def test_package_questionnaire_id_must_match_the_index_entry(tmp_path):
    base, qdir = _questionnaire_content_copy(tmp_path)
    _rewrite_package(
        qdir, "sfacs_v1", lambda data: data.update(questionnaire_id="sfacs_v2"))
    _repin_index(qdir)  # 哈希对上了，仍要按 id 漂移拒绝
    with pytest.raises(FrozenContentUnavailable,
                       match="questionnaire_id 与索引不一致"):
        load_questionnaire_registry(base)


# ---------------- B. 值域 / 锁定完整性 / GDS-15 计分 ----------------

def _gds_all(answer: str) -> dict[tuple[str, str], str]:
    return {(item.item_key, "value"): answer for item in GDS.all_items()}


def _npiq_all_absent() -> dict[tuple[str, str], str]:
    return {(item.item_key, "present"): "无" for item in NPIQ.all_items()}


def test_validate_value_write_rejects_out_of_domain_values():
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        validate_value_write(GDS, "gds_01", "value", "也许")
    assert excinfo.value.code == "questionnaire_value_out_of_domain"
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        validate_value_write(NPIQ, "npiq_01", "severity", "4")
    assert excinfo.value.code == "questionnaire_value_out_of_domain"


def test_validate_value_write_rejects_unknown_item_and_field_keys():
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        validate_value_write(GDS, "gds_99", "value", "是")
    assert excinfo.value.code == "questionnaire_field_unknown"
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        validate_value_write(NPIQ, "npiq_01", "value", "有")
    assert excinfo.value.code == "questionnaire_field_unknown"


def test_validate_value_write_accepts_none_as_clearing_any_known_slot():
    assert validate_value_write(GDS, "gds_01", "value", None) is None
    assert validate_value_write(NPIQ, "npiq_01", "severity", None) is None
    assert validate_value_write(SFACS, "sfacs_01", "value", None) is None


def test_lock_incomplete_lists_every_missing_slot():
    partial = _gds_all("是")
    del partial[("gds_03", "value")]
    del partial[("gds_12", "value")]
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        assert_lock_complete(GDS, partial)
    assert excinfo.value.code == "questionnaire_lock_incomplete"
    assert sorted(excinfo.value.problems) == sorted([
        "第 3 题未作答", "第 12 题未作答"])

    # 全空作答：SFACS 必填面 = 21 条目值 + 2 节 × 4 要素 = 29 槽位，缺项要列全。
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        assert_lock_complete(SFACS, {})
    assert len(excinfo.value.problems) == 29
    assert "第 1 题未作答" in excinfo.value.problems
    assert "「一、简单社交沟通」的「准确程度」未评" in excinfo.value.problems


def test_npiq_absent_symptom_with_severity_is_a_contradiction():
    final = _npiq_all_absent()
    final[("npiq_01", "severity")] = "2"
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        assert_lock_complete(NPIQ, final)
    assert excinfo.value.problems == [
        "第 1 题记为“无”却带严重度/频率——先清除再锁定"]


def test_npiq_present_symptom_missing_frequency_or_severity_is_rejected():
    missing_frequency = _npiq_all_absent()
    missing_frequency[("npiq_02", "present")] = "有"
    missing_frequency[("npiq_02", "severity")] = "1"
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        assert_lock_complete(NPIQ, missing_frequency)
    assert excinfo.value.problems == ["第 2 题记为“有”但缺频率"]

    missing_severity = _npiq_all_absent()
    missing_severity[("npiq_03", "present")] = "有"
    missing_severity[("npiq_03", "frequency")] = "4"
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        assert_lock_complete(NPIQ, missing_severity)
    assert excinfo.value.problems == ["第 3 题记为“有”但缺严重度"]

    complete = _npiq_all_absent()
    complete[("npiq_04", "present")] = "有"
    complete[("npiq_04", "severity")] = "3"
    complete[("npiq_04", "frequency")] = "1"
    assert assert_lock_complete(NPIQ, complete) is None


def test_lock_rejects_answer_keys_outside_the_definition():
    final = _npiq_all_absent()
    final[("npiq_99", "present")] = "无"
    with pytest.raises(QuestionnaireValidationError) as excinfo:
        assert_lock_complete(NPIQ, final)
    assert excinfo.value.problems == ["出现了定义之外的作答记录 (npiq_99, present)，请联系管理员核查"]


def test_gds15_reverse_items_are_pinned_verbatim_to_the_source_rule():
    score_when = {item.item_key: item.score_when for item in GDS.all_items()}
    assert score_when == {
        key: ("否" if key in GDS_REVERSED_KEYS else "是")
        for key in score_when
    }
    assert GDS.scoring is not None
    assert GDS.scoring.cutoff_value == 8
    assert GDS.scoring.cutoff_operator == ">="
    assert GDS.scoring.cutoff_label == "有抑郁症状"


def test_gds15_all_yes_scores_eleven_and_meets_the_cutoff():
    final = _gds_all("是")
    assert assert_lock_complete(GDS, final) is None
    assert compute_scoring(GDS, final) == {
        "computed_total": 11.0,
        "cutoff_met": True,
        "computed_flag": "有抑郁症状",
        "scoring_rule_id": "gds15.source_scoring.v1",
    }


def test_gds15_all_no_scores_four_below_cutoff_with_no_invented_flag():
    final = _gds_all("否")
    assert assert_lock_complete(GDS, final) is None
    assert compute_scoring(GDS, final) == {
        "computed_total": 4.0,
        "cutoff_met": False,
        "computed_flag": None,  # 源表只定义了达界标签，未达界不发明标签
        "scoring_rule_id": "gds15.source_scoring.v1",
    }


def test_unscored_questionnaires_never_invent_totals():
    assert compute_scoring(SFACS, {}) is None
    assert compute_scoring(NPIQ, {}) is None


# ---------------- C. API 集成（真实 app + TestClient） ----------------

PASSWORD = "questionnaire-pw-2026"


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
        for username, display_id, role in (
            ("research-a", "RESEARCH-A", "researcher"),
            ("steward", "STEWARD", "data_steward"),
        ):
            session.add(ResearchUser(
                username=username,
                display_id=display_id,
                password_hash=auth.hash_password(PASSWORD),
                role=role,
            ))
        session.add(Patient(
            patient_id="P-Q1", consent_status="已同意", mandarin_eligible=True))
        session.add(Patient(
            patient_id="P-WD", consent_status="已同意",
            withdrawal_status="withdrawn_all"))
        session.commit()

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    yield engine
    app.dependency_overrides.clear()


def _client(username: str) -> TestClient:
    client = TestClient(app)
    response = client.post("/auth/login", json={
        "username": username,
        "password": PASSWORD,
    })
    assert response.status_code == 200, response.text
    csrf = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert csrf
    client.headers.update({"X-CSRF-Token": csrf})
    return client


def _create_record(client: TestClient, patient_id: str,
                   questionnaire_id: str, phase: str = "前测") -> dict:
    response = client.post(
        f"/patients/{patient_id}/questionnaire-records",
        json={"questionnaire_id": questionnaire_id, "phase_label": phase})
    assert response.status_code == 200, response.text
    return response.json()


def _gds_values_payload(answer: str) -> list[dict]:
    return [
        {"item_key": item.item_key, "field_key": "value", "value": answer}
        for item in GDS.all_items()
    ]


def _lock_gds_record(client: TestClient, answer: str) -> dict:
    record = _create_record(client, "P-Q1", "gds15_v1")
    record_id = record["record_id"]
    put = client.put(f"/questionnaire-records/{record_id}/values",
                     json={"values": _gds_values_payload(answer)})
    assert put.status_code == 200, put.text
    locked = client.post(f"/questionnaire-records/{record_id}/lock")
    assert locked.status_code == 200, locked.text
    return locked.json()


def test_anonymous_definitions_read_is_rejected_and_leaks_no_item_text(api_env):
    # 「新 GET 路由默认匿名公开」陷阱的覆盖测试（access_policy 对未知 GET 的
    # 默认是 PUBLIC）。验收依据 = 2026-08-20 四次逐层拆防线实测：
    # (1) 只摘处理器 _require_account_identity → 仍绿（中间件按显式 _route
    #     规则 401 account_required）；
    # (2) 只从 _API_ROOTS 摘 questionnaires/questionnaire-records → 仍绿
    #     （显式规则与处理器鉴权都在——任务书设想的「摘 _API_ROOTS 即红」
    #     对本路径不成立，_API_ROOTS 只是无显式规则时的 fail-closed 兜底）；
    # (3) 显式规则 + _API_ROOTS + 处理器鉴权全摘 → 匿名拿到 200 与逐题词，
    #     本测试红——断言不空转；
    # (4) 只留处理器鉴权 → 403，仍绿。
    # 即匿名拒绝有两道独立防线（中间件策略 + 处理器具名闸），两道全失守
    # 本测试才红；逐路由分类的常驻钉在 tests/test_access_policy.py。
    client = TestClient(app)
    response = client.get("/questionnaires/definitions")
    assert response.status_code in (401, 403), response.text
    # 逐题词永不进匿名响应
    assert "gds_01" not in response.text
    assert "score_when" not in response.text

    records = client.get("/patients/P-Q1/questionnaire-records")
    assert records.status_code in (401, 403), records.text


def test_data_steward_can_read_but_cannot_write(api_env):
    steward = _client("steward")
    catalog = steward.get("/questionnaires/definitions")
    assert catalog.status_code == 200, catalog.text
    assert [row["definition"]["questionnaire_id"]
            for row in catalog.json()["questionnaires"]] == [
        "ace3_v1", "aft_v1", "gds15_v1", "npiq_v1", "sfacs_v1"]

    listing = steward.get("/patients/P-Q1/questionnaire-records")
    assert listing.status_code == 200, listing.text

    create = steward.post(
        "/patients/P-Q1/questionnaire-records",
        json={"questionnaire_id": "gds15_v1", "phase_label": "前测"})
    assert create.status_code == 403, create.text
    put = steward.put(
        "/questionnaire-records/qr_any/values",
        json={"values": [
            {"item_key": "gds_01", "field_key": "value", "value": "是"}]})
    assert put.status_code == 403, put.text
    draft = steward.post("/questionnaire-records/qr_any/ai-draft")
    assert draft.status_code == 403, draft.text
    lock = steward.post("/questionnaire-records/qr_any/lock")
    assert lock.status_code == 403, lock.text


def test_researcher_full_chain_create_values_lock(api_env):
    researcher = _client("research-a")
    created = _create_record(researcher, "P-Q1", "gds15_v1")
    assert created["schema_version"] == "questionnaire-record.v1"
    assert created["status"] == "draft"
    assert created["values"] == []
    assert created["definition_sha256"] == REGISTRY["gds15_v1"].content_sha256
    record_id = created["record_id"]

    put = researcher.put(
        f"/questionnaire-records/{record_id}/values",
        json={"values": _gds_values_payload("是")})
    assert put.status_code == 200, put.text
    draft_body = put.json()
    assert len(draft_body["values"]) == 15
    assert {value["value_source"] for value in draft_body["values"]} == {
        "human_direct"}
    assert {value["final_value"] for value in draft_body["values"]} == {"是"}

    locked = researcher.post(f"/questionnaire-records/{record_id}/lock")
    assert locked.status_code == 200, locked.text
    locked_body = locked.json()
    assert locked_body["status"] == "locked"
    assert locked_body["locked_by"] == "RESEARCH-A"
    assert locked_body["locked_at"] is not None
    assert locked_body["computed_total"] == 11.0
    assert locked_body["cutoff_met"] is True
    assert locked_body["computed_flag"] == "有抑郁症状"
    assert locked_body["scoring_rule_id"] == "gds15.source_scoring.v1"
    assert len(locked_body["values"]) == 15

    listing = researcher.get("/patients/P-Q1/questionnaire-records")
    assert listing.status_code == 200, listing.text
    records = listing.json()["records"]
    assert [record["record_id"] for record in records] == [record_id]
    assert records[0]["status"] == "locked"
    assert len(records[0]["values"]) == 15

    with Session(api_env) as session:
        audit_rows = list(session.exec(select(AuditLog)))
    actions = [row.action for row in audit_rows]
    assert "questionnaire_record" in actions
    assert "questionnaire_lock" in actions
    lock_rows = [row for row in audit_rows
                 if row.action == "questionnaire_lock"]
    assert len(lock_rows) == 1
    assert lock_rows[0].patient_id == "P-Q1"
    assert lock_rows[0].actor == "RESEARCH-A"


def test_locked_record_rejects_values_ai_draft_and_relock(api_env):
    researcher = _client("research-a")
    locked_body = _lock_gds_record(researcher, "否")
    # 全“否”方向在 API 层复核一次源表计分：只有 1/5/7/11 计分。
    assert locked_body["computed_total"] == 4.0
    assert locked_body["cutoff_met"] is False
    assert locked_body["computed_flag"] is None
    record_id = locked_body["record_id"]

    put = researcher.put(
        f"/questionnaire-records/{record_id}/values",
        json={"values": [
            {"item_key": "gds_01", "field_key": "value", "value": "是"}]})
    assert put.status_code == 409, put.text
    assert put.json()["detail"]["code"] == "questionnaire_record_locked"

    draft = researcher.post(f"/questionnaire-records/{record_id}/ai-draft")
    assert draft.status_code == 409, draft.text
    assert draft.json()["detail"]["code"] == "questionnaire_record_locked"

    relock = researcher.post(f"/questionnaire-records/{record_id}/lock")
    assert relock.status_code == 409, relock.text
    assert relock.json()["detail"]["code"] == "questionnaire_record_locked"


def test_lock_refuses_incomplete_answers_with_the_full_missing_list(api_env):
    researcher = _client("research-a")
    record = _create_record(researcher, "P-Q1", "gds15_v1")
    record_id = record["record_id"]
    put = researcher.put(
        f"/questionnaire-records/{record_id}/values",
        json={"values": [
            {"item_key": "gds_01", "field_key": "value", "value": "是"}]})
    assert put.status_code == 200, put.text

    lock = researcher.post(f"/questionnaire-records/{record_id}/lock")
    assert lock.status_code == 409, lock.text
    detail = lock.json()["detail"]
    assert detail["code"] == "questionnaire_lock_incomplete"
    assert len(detail["problems"]) == 14
    assert "第 2 题未作答" in detail["problems"]


def test_out_of_domain_value_write_is_rejected_with_problems(api_env):
    researcher = _client("research-a")
    record = _create_record(researcher, "P-Q1", "gds15_v1")
    put = researcher.put(
        f"/questionnaire-records/{record['record_id']}/values",
        json={"values": [
            {"item_key": "gds_01", "field_key": "value", "value": "也许"}]})
    assert put.status_code == 409, put.text
    detail = put.json()["detail"]
    assert detail["code"] == "questionnaire_value_invalid"
    assert len(detail["problems"]) == 1


def test_withdrawn_subject_is_refused_for_reads_and_writes(api_env):
    researcher = _client("research-a")
    create = researcher.post(
        "/patients/P-WD/questionnaire-records",
        json={"questionnaire_id": "gds15_v1", "phase_label": "前测"})
    assert create.status_code == 409, create.text
    assert "撤回" in create.text

    listing = researcher.get("/patients/P-WD/questionnaire-records")
    assert listing.status_code == 409, listing.text


def test_unknown_questionnaire_is_a_409_naming_the_registered_catalog(api_env):
    researcher = _client("research-a")
    create = researcher.post(
        "/patients/P-Q1/questionnaire-records",
        json={"questionnaire_id": "mmse_v1", "phase_label": "前测"})
    assert create.status_code == 409, create.text
    detail = create.json()["detail"]
    assert detail["code"] == "questionnaire_unknown"
    assert detail["registered"] == [
        "ace3_v1", "aft_v1", "gds15_v1", "npiq_v1", "sfacs_v1"]


def test_value_source_tracks_ai_accept_override_and_human_direct(api_env,
                                                                 monkeypatch):
    researcher = _client("research-a")
    record = _create_record(researcher, "P-Q1", "sfacs_v1")
    record_id = record["record_id"]

    def fixed_draft(_s, _patient, definition):
        assert definition.questionnaire_id == "sfacs_v1"
        return DraftOutcome(status="generated", engine="test/fixed-draft.v1",
                            items={
                                "sfacs_01": DraftItem(value="7", rationale="固定草稿"),
                                "sfacs_02": DraftItem(value="5", rationale="固定草稿"),
                            })

    monkeypatch.setattr(questionnaire_ai_draft, "generate_draft", fixed_draft)
    drafted = researcher.post(f"/questionnaire-records/{record_id}/ai-draft")
    assert drafted.status_code == 200, drafted.text
    drafted_body = drafted.json()
    assert drafted_body["ai_draft_status"] == "generated"
    assert drafted_body["ai_draft_engine"] == "test/fixed-draft.v1"
    slots = {(value["item_key"], value["field_key"]): value
             for value in drafted_body["values"]}
    assert slots[("sfacs_01", "value")]["ai_draft_value"] == "7"
    assert slots[("sfacs_01", "value")]["final_value"] is None

    put = researcher.put(
        f"/questionnaire-records/{record_id}/values",
        json={"values": [
            {"item_key": "sfacs_01", "field_key": "value", "value": "7"},
            {"item_key": "sfacs_02", "field_key": "value", "value": "4"},
            {"item_key": "sfacs_03", "field_key": "value", "value": "N"},
        ]})
    assert put.status_code == 200, put.text
    slots = {(value["item_key"], value["field_key"]): value
             for value in put.json()["values"]}
    assert slots[("sfacs_01", "value")]["value_source"] == "ai_accepted"
    assert slots[("sfacs_02", "value")]["value_source"] == "ai_overridden"
    assert slots[("sfacs_02", "value")]["ai_draft_value"] == "5"  # 草稿留档
    assert slots[("sfacs_03", "value")]["value_source"] == "human_direct"
    assert slots[("sfacs_03", "value")]["ai_draft_value"] is None

    cleared = researcher.put(
        f"/questionnaire-records/{record_id}/values",
        json={"values": [
            {"item_key": "sfacs_01", "field_key": "value", "value": None}]})
    assert cleared.status_code == 200, cleared.text
    slots = {(value["item_key"], value["field_key"]): value
             for value in cleared.json()["values"]}
    assert slots[("sfacs_01", "value")]["final_value"] is None
    assert slots[("sfacs_01", "value")]["value_source"] is None


def test_ai_draft_is_not_applicable_for_gds15_without_touching_the_llm(
        api_env, monkeypatch):
    def never_call(_prompt):
        raise AssertionError("gds15 初评绝不该走到 LLM")

    monkeypatch.setattr(questionnaire_ai_draft, "_call_llm", never_call)
    researcher = _client("research-a")
    record = _create_record(researcher, "P-Q1", "gds15_v1")
    drafted = researcher.post(
        f"/questionnaire-records/{record['record_id']}/ai-draft")
    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    # never_call 若被触发，generate_draft 会把异常吞成 failed——这一断言就会红。
    assert body["ai_draft_status"] == "not_applicable"
    assert body["ai_draft_engine"] is None


def test_ai_draft_without_cloud_authorization_never_reaches_the_network(
        api_env, monkeypatch):
    # conftest 已摘 DASHSCOPE_API_KEY，这里显式再钉一次并记录出网调用。
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(
        questionnaire_ai_draft, "_call_llm",
        lambda prompt: calls.append(prompt) or None)
    researcher = _client("research-a")
    # P-Q1 没有 cloud_processing_allowed=True：sfacs 可初评但授权闸必须先拒。
    record = _create_record(researcher, "P-Q1", "sfacs_v1")
    drafted = researcher.post(
        f"/questionnaire-records/{record['record_id']}/ai-draft")
    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    assert body["ai_draft_status"] == "unavailable_not_authorized"
    assert body["ai_draft_engine"] is None
    assert calls == []


def test_locked_record_is_immutable_and_undeletable_at_the_orm_layer(api_env):
    researcher = _client("research-a")
    record_id = _lock_gds_record(researcher, "是")["record_id"]

    with Session(api_env) as session:
        record = session.get(QuestionnaireRecord, record_id)
        record.note = "锁定后篡改"
        with pytest.raises(RuntimeError, match="锁定后不可变"):
            session.commit()
        session.rollback()

    with Session(api_env) as session:
        record = session.get(QuestionnaireRecord, record_id)
        session.delete(record)
        with pytest.raises(RuntimeError, match="QuestionnaireRecord 永不删除"):
            session.commit()
        session.rollback()

    with Session(api_env) as session:
        value = session.exec(select(QuestionnaireItemValue).where(
            QuestionnaireItemValue.record_id == record_id)).first()
        assert value is not None
        session.delete(value)
        with pytest.raises(RuntimeError,
                           match="QuestionnaireItemValue 永不删除"):
            session.commit()
        session.rollback()


def test_patient_registration_rejects_non_ascii_research_code(api_env):
    """建档口与训练安排契约同宽:中文/空格编号 422 且给中文指引。

    回退验证:把 create_patient 的 re.fullmatch 闸删掉,本测试必须变红
    (2026-08-21 首测实录:『测试1』建档成功却永远排不了训练)。
    """
    client = _client("research-a")
    denied = client.post("/patients", json={
        "patient_id": "测试1", "name": "x"})
    assert denied.status_code == 422, denied.text
    assert "字母、数字" in denied.text
    accepted = client.post("/patients", json={
        "patient_id": "TEST-OK-01", "name": "x"})
    assert accepted.status_code == 200, accepted.text
