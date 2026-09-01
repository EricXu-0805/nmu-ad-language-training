"""PATCH /patients/{patient_id}:非治理字段编辑 + 零关联数据时的编号更正。"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import auth, db
from app.main import app
from app.models import AuditLog, Patient, ResearchUser, Week1Profile


@pytest.fixture
def profile_client(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", eng)
    SQLModel.metadata.create_all(eng)
    client = TestClient(app)
    client.test_engine = eng
    yield client
    client.close()


def _seed_patient(client: TestClient, patient_id: str = "P-EDIT") -> None:
    assert client.post("/patients", json={
        "patient_id": patient_id,
        "is_simulation_subject": True,
        "dementia_severity": "轻度",
    }).status_code == 200


def _login(engine, username: str, role: str) -> TestClient:
    with Session(engine) as session:
        session.add(ResearchUser(
            username=username,
            display_id=f"ACTOR-{username}",
            password_hash=auth.hash_password("password1"),
            role=role,
            created_at=datetime.now(),
        ))
        session.commit()
    client = TestClient(app)
    login = client.post("/auth/login", json={
        "username": username, "password": "password1",
    })
    assert login.status_code == 200, login.text
    client.headers["X-CSRF-Token"] = client.cookies.get(auth.CSRF_COOKIE_NAME)
    return client


def test_patch_updates_fields_and_audits_names_only(profile_client):
    _seed_patient(profile_client)
    response = profile_client.patch("/patients/P-EDIT", json={
        "dementia_severity": "中度",
        "recording_allowed": True,
        "consent_person": "监护人",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dementia_severity"] == "中度"
    assert body["recording_allowed"] is True
    fetched = profile_client.get("/patients/P-EDIT").json()
    assert fetched["consent_person"] == "监护人"
    with Session(profile_client.test_engine) as s:
        entries = [row for row in s.exec(select(AuditLog)).all()
                   if row.action == "patient_profile_update"]
    assert len(entries) == 1
    assert entries[0].patient_id == "P-EDIT"
    # 审计只记字段名,绝不记字段值(称谓/角色也不进账本)。
    assert "consent_person" in entries[0].summary
    assert "监护人" not in entries[0].summary


def test_patch_rejects_governance_fields(profile_client):
    _seed_patient(profile_client)
    for payload in (
            {"cloud_processing_allowed": True},
            {"withdrawal_status": "withdrawn"},
            {"governance_revision": 3},
            {"is_simulation_subject": False},
            {"patient_id": "P-OTHER"},
    ):
        response = profile_client.patch("/patients/P-EDIT", json=payload)
        assert response.status_code == 422, (payload, response.text)


def test_consent_type_backfill_allowed_but_rewrite_rejected(profile_client):
    """P0-1 规则:补录空值允许(纠正遗漏),改写非空值拒绝(治理动作)。"""
    _seed_patient(profile_client, "P-CONSENT")
    # 建档时留空 → 补录允许。
    backfill = profile_client.patch(
        "/patients/P-CONSENT", json={"consent_type": "本人同意"})
    assert backfill.status_code == 200, backfill.text
    assert backfill.json()["consent_type"] == "本人同意"
    # 已登记后改成另一个值 → 拒绝,给出人话原因。
    rewrite = profile_client.patch(
        "/patients/P-CONSENT", json={"consent_type": "代理同意加本人赞同"})
    assert rewrite.status_code == 409, rewrite.text
    assert "本人同意" in rewrite.json()["detail"]
    assert "补录" in rewrite.json()["detail"]
    # 已登记后清空 → 同样是改写,拒绝。
    clear = profile_client.patch(
        "/patients/P-CONSENT", json={"consent_type": None})
    assert clear.status_code == 409, clear.text
    # 提交与现值相同的值是无害幂等,放行。
    same = profile_client.patch(
        "/patients/P-CONSENT", json={"consent_type": "本人同意"})
    assert same.status_code == 200, same.text
    assert profile_client.get(
        "/patients/P-CONSENT").json()["consent_type"] == "本人同意"


def test_patch_cannot_fake_withdrawal_via_consent_status(profile_client):
    # 界面下拉给的是中文值——只挡英文 "withdrawn" 会被「已撤回」绕过(对抗审查
    # 2026-08-22 坐实),否定态闭集(中英)一律 422。
    _seed_patient(profile_client)
    for denied in ("withdrawn", "已撤回", "未同意", "拒绝", "Withdrawn"):
        response = profile_client.patch(
            "/patients/P-EDIT", json={"consent_status": denied})
        assert response.status_code == 422, denied
        assert "撤回" in response.json()["detail"]


def test_denied_consent_cannot_be_washed_back_to_granted(profile_client):
    # 反方向:已登记「未同意/已撤回」的档案不能经编辑洗成「已同意」——重新同意
    # 必须走正式知情同意流程(2026-08-24 复审发现的伦理门缺口)。
    _seed_patient(profile_client)
    with Session(profile_client.test_engine) as s:
        patient = s.get(Patient, "P-EDIT")
        patient.consent_status = "未同意"
        s.add(patient)
        s.commit()
    response = profile_client.patch(
        "/patients/P-EDIT", json={"consent_status": "已同意"})
    assert response.status_code == 422
    assert "重新签署" in response.json()["detail"]


def test_withdrawn_profile_is_not_editable(profile_client):
    _seed_patient(profile_client)
    with Session(profile_client.test_engine) as s:
        patient = s.get(Patient, "P-EDIT")
        patient.withdrawal_status = "withdrawn"
        s.add(patient)
        s.commit()
    response = profile_client.patch(
        "/patients/P-EDIT", json={"dementia_severity": "中度"})
    assert response.status_code == 409


def test_rename_with_zero_linked_data_succeeds_and_audits(profile_client):
    _seed_patient(profile_client)
    response = profile_client.patch("/patients/P-EDIT", json={
        "new_patient_id": "NMU-001",
        "mandarin_eligible": True,
    })
    assert response.status_code == 200, response.text
    assert response.json()["patient_id"] == "NMU-001"
    assert profile_client.get("/patients/P-EDIT").status_code == 404
    renamed = profile_client.get("/patients/NMU-001")
    assert renamed.status_code == 200
    assert renamed.json()["mandarin_eligible"] is True
    with Session(profile_client.test_engine) as s:
        entries = [row for row in s.exec(select(AuditLog)).all()
                   if row.action == "patient_profile_update"]
    assert len(entries) == 1
    assert "renamed_from=P-EDIT" in entries[0].summary
    assert entries[0].patient_id == "NMU-001"


def test_rename_rejected_when_any_table_references_patient(profile_client):
    _seed_patient(profile_client)
    # 任意一张外键表有行都要挡:用最小的关联表证明门禁按外键图工作,
    # 不是硬编码"场次/安排"两张。
    with Session(profile_client.test_engine) as s:
        s.add(Week1Profile(patient_id="P-EDIT", zodiac="兔"))
        s.commit()
    response = profile_client.patch(
        "/patients/P-EDIT", json={"new_patient_id": "NMU-002"})
    assert response.status_code == 422
    assert "关联" in response.json()["detail"]
    # 非改名字段仍可编辑。
    still_editable = profile_client.patch(
        "/patients/P-EDIT", json={"dementia_severity": "中度"})
    assert still_editable.status_code == 200


def test_rename_conflict_and_invalid_id_are_rejected(profile_client):
    _seed_patient(profile_client, "P-EDIT")
    _seed_patient(profile_client, "P-TAKEN")
    conflict = profile_client.patch(
        "/patients/P-EDIT", json={"new_patient_id": "P-TAKEN"})
    assert conflict.status_code == 409
    assert "已被使用" in conflict.json()["detail"]
    invalid = profile_client.patch(
        "/patients/P-EDIT", json={"new_patient_id": "测试1"})
    assert invalid.status_code == 422


def test_patch_requires_training_operation_role_when_protected(
        profile_client, monkeypatch):
    _seed_patient(profile_client)
    eng = profile_client.test_engine
    researcher = _login(eng, "edit-researcher", "researcher")
    steward = _login(eng, "edit-steward", "data_steward")
    try:
        anonymous = profile_client.patch(
            "/patients/P-EDIT", json={"dementia_severity": "中度"})
        assert anonymous.status_code == 401
        assert anonymous.json()["code"] == "account_required"
        denied = steward.patch(
            "/patients/P-EDIT", json={"dementia_severity": "中度"})
        assert denied.status_code == 403
        assert denied.json()["code"] == "role_forbidden"
        allowed = researcher.patch(
            "/patients/P-EDIT", json={"dementia_severity": "中度"})
        assert allowed.status_code == 200, allowed.text
    finally:
        researcher.close()
        steward.close()


def test_covariates_editable_and_bounded(profile_client):
    """四个研究协变量(Eric 2026-08-27 拍板)从此可经档案编辑落库;越界 422 不落 500。"""
    _seed_patient(profile_client)
    ok = profile_client.patch("/patients/P-EDIT", json={
        "birth_year": 1948, "sex": "女", "education_years": 9,
        "study_arm": "训练组"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert (body["birth_year"], body["sex"],
            body["education_years"], body["study_arm"]) == (1948, "女", 9, "训练组")
    for bad, keyword in (({"birth_year": 1899}, "出生年份"),
                         ({"birth_year": 2101}, "出生年份"),
                         ({"education_years": -1}, "受教育年限"),
                         ({"education_years": 31}, "受教育年限"),
                         ({"sex": "男性"}, "性别"),
                         ({"study_arm": "x" * 65}, "分组标签")):
        response = profile_client.patch("/patients/P-EDIT", json=bad)
        assert response.status_code == 422, (bad, response.text)
        # 编辑抽屉靠这句中文人话;英文 pydantic 原文对临床研究者等于没报错。
        assert keyword in response.text, (bad, response.text)
    # 越界请求不得污染已存值。
    fetched = profile_client.get("/patients/P-EDIT").json()
    assert fetched["birth_year"] == 1948


def test_create_patient_covariates_bounded(profile_client):
    for bad in ({"sex": "M"}, {"birth_year": 1800}, {"education_years": 99}):
        response = profile_client.post("/patients", json={
            "patient_id": "P-COV-BAD", "is_simulation_subject": True, **bad})
        assert response.status_code == 422, (bad, response.text)
    ok = profile_client.post("/patients", json={
        "patient_id": "P-COV", "is_simulation_subject": True,
        "birth_year": 1950, "sex": "男", "education_years": 12,
        "study_arm": "对照组"})
    assert ok.status_code == 200, ok.text
    fetched = profile_client.get("/patients/P-COV").json()
    assert fetched["birth_year"] == 1950
    assert fetched["sex"] == "男"
