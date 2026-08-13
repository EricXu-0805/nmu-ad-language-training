"""HTTP 层的只读研究数据面：角色矩阵、密钥缺失、闭集与泄漏回归。

这套测试的重点不是"能不能取到数"，而是"取不到的时候会不会漏"。
"""
from __future__ import annotations

from datetime import datetime
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import auth, content, db, repeat_intent, research_dataset
from app.db import get_session
from app.main import app
from app.models import (
    ItemEvent,
    Patient,
    ResearchUser,
    Session as TrainSession,
    TurnEvent,
)


BANK = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
PROTOCOL = content.load_autopilot_protocol(
    content.CONTENT_DIR / "autopilot_protocol_v1.json")
BANK_DIGEST = content.item_bank_definition_digest(BANK)
PROTOCOL_DIGEST = content.autopilot_protocol_definition_digest(PROTOCOL)
REPEAT_PROTOCOL = repeat_intent.active_protocol()
PASSWORD = "research-read-password-2026"
KEY = "r" * 48
KEY_ID = "nmu-test-2026"
SECRET_TEXT = "我叫王大爷住城东"


@pytest.fixture
def research_env(monkeypatch):
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("CONSOLE_PIN", "135790")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for username, display_id, role in (
            ("researcher", "RESEARCHER", "researcher"),
            ("steward", "STEWARD", "data_steward"),
            ("admin", "ADMIN", "admin"),
            ("caregiver", "CAREGIVER", "caregiver_operator"),
        ):
            session.add(ResearchUser(
                username=username, display_id=display_id,
                password_hash=auth.hash_password(PASSWORD),
                role=role, created_at=datetime.now()))
        session.add(Patient(
            patient_id="P-REAL-1", is_simulation_subject=False,
            consent_status="已同意", consent_type="本人同意",
            mandarin_eligible=True, recording_allowed=True,
            consent_person="王家属", dementia_severity="轻度"))
        session.add(Patient(
            patient_id="P-GONE-1", is_simulation_subject=False,
            consent_status="已同意", consent_type="本人同意",
            mandarin_eligible=True, recording_allowed=True,
            withdrawal_status="withdrawn"))
        train = TrainSession(
            session_id="S-REAL-1", patient_id="P-REAL-1", week_no=2,
            phase_type="正式训练", event_line="正式训练", trainer_id="RESEARCHER",
            item_bank_version_id=BANK.version_id,
            item_bank_definition_digest=BANK_DIGEST,
            autopilot_protocol_version_id=PROTOCOL["protocol_version_id"],
            autopilot_protocol_definition_digest=PROTOCOL_DIGEST,
            repeat_protocol_version_id=REPEAT_PROTOCOL.version_id,
            repeat_protocol_definition_digest=REPEAT_PROTOCOL.definition_digest,
            is_simulation=False, data_classification="research")
        session.add(train)
        session.commit()
        item = ItemEvent(session_id="S-REAL-1", item_id="SE_胡萝卜",
                         task_type="单要素", item_set_type="训练集",
                         presentation_order=1)
        session.add(item)
        session.commit()
        session.add(TurnEvent(
            item_event_id=item.id, turn_seq=1, response_role="命名",
            asr_text=SECRET_TEXT, confirmed_response_text=SECRET_TEXT,
            asr_confidence=0.9, prompt_level=0, ai_score=1.0,
            reviewed_score=1.0, score_locked=True, judge_portrait_used=False))
        session.commit()

    def override_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    yield engine
    app.dependency_overrides.clear()


def _client(username: str) -> TestClient:
    client = TestClient(app)
    response = client.post("/auth/login",
                           json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers.update({"X-CSRF-Token": client.cookies.get(auth.CSRF_COOKIE_NAME)})
    return client


def _with_key(monkeypatch):
    monkeypatch.setenv("DEIDENTIFICATION_KEY", KEY)
    monkeypatch.setenv("DEIDENTIFICATION_KEY_ID", KEY_ID)


def _without_key(monkeypatch):
    monkeypatch.delenv("DEIDENTIFICATION_KEY", raising=False)
    monkeypatch.delenv("DEIDENTIFICATION_KEY_ID", raising=False)


PATHS = ("/research/v1/meta", "/research/v1/dictionary",
         "/research/v1/subjects?data_classification=research")


def test_anonymous_and_wrong_roles_never_reach_the_research_surface(
        research_env, monkeypatch):
    _with_key(monkeypatch)
    anonymous = TestClient(app)
    for path in PATHS:
        assert anonymous.get(path).status_code == 401, path
    for username in ("researcher", "caregiver"):
        client = _client(username)
        for path in PATHS:
            assert client.get(path).status_code == 403, f"{username} {path}"
    for username in ("steward", "admin"):
        client = _client(username)
        for path in PATHS:
            assert client.get(path).status_code == 200, f"{username} {path}"


def test_missing_key_keeps_meta_readable_but_returns_zero_rows(
        research_env, monkeypatch):
    _without_key(monkeypatch)
    client = _client("steward")
    meta = client.get("/research/v1/meta")
    assert meta.status_code == 200
    body = meta.json()
    assert body["deidentification"]["configured"] is False
    assert body["datasets"] == []

    for dataset in research_dataset.dataset_keys():
        response = client.get(
            f"/research/v1/{dataset}?data_classification=research")
        assert response.status_code == 503, dataset
        detail = response.json()["detail"]
        assert detail["code"] == "research_deidentification_unavailable"
        # 绝不降级成明文，也不返回部分结果
        assert "rows" not in response.text
        assert "P-REAL-1" not in response.text
        assert KEY not in response.text


def test_rows_are_pseudonymous_and_carry_no_text_or_absolute_time(
        research_env, monkeypatch):
    _with_key(monkeypatch)
    client = _client("steward")
    for dataset in research_dataset.dataset_keys():
        response = client.get(
            f"/research/v1/{dataset}?data_classification=research")
        assert response.status_code == 200, dataset
        payload = response.json()
        assert payload["schema_version"] == research_dataset.SCHEMA_VERSION
        assert payload["pseudonym_key_id"] == KEY_ID
        expected = list(research_dataset.published_columns(
            research_dataset.dataset_for(dataset)))
        assert payload["columns"] == expected
        for row in payload["rows"]:
            assert list(row) == expected, "行必须是闭集，多一个键都不行"
        raw = response.text
        assert "P-REAL-1" not in raw, "真实研究编号绝不出接口"
        assert "S-REAL-1" not in raw, "真实场次号绝不出接口"
        assert SECRET_TEXT not in raw, "作答文本绝不出接口"
        assert "王家属" not in raw, "知情同意签署人绝不出接口"
        assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", raw), \
            "绝对时间戳绝不出接口"
        assert response.headers["Cache-Control"] == "private, no-store"


def test_withdrawn_subjects_leave_a_tombstone_instead_of_vanishing(
        research_env, monkeypatch):
    _with_key(monkeypatch)
    client = _client("steward")
    subjects = client.get(
        "/research/v1/subjects?data_classification=research").json()
    # 撤回的人仍然出现在分母里，只是标记为已撤回——两次拉取之差不该泄露谁撤回了
    assert subjects["row_count"] == 2
    assert sorted(row["withdrawn"] for row in subjects["rows"]) == [False, True]


def test_unknown_dataset_query_and_limit_are_refused_with_stable_codes(
        research_env, monkeypatch):
    _with_key(monkeypatch)
    client = _client("steward")

    unknown = client.get("/research/v1/nope?data_classification=research")
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "research_dataset_unknown"

    # 契约没实现的维度不许静默接受
    extra = client.get(
        "/research/v1/subjects?data_classification=research&group_by=week")
    assert extra.status_code == 422
    assert extra.json()["detail"]["code"] == "research_query_invalid"

    # data_classification 必填、无默认
    assert client.get("/research/v1/subjects").status_code == 422

    for bad in (0, -1, 100000):
        response = client.get(
            f"/research/v1/subjects?data_classification=research&limit={bad}")
        assert response.status_code == 422, bad
        assert response.json()["detail"]["code"] == "research_limit_invalid"


def test_a_tampered_cursor_is_refused_rather_than_silently_restarting(
        research_env, monkeypatch):
    _with_key(monkeypatch)
    client = _client("steward")
    response = client.get(
        "/research/v1/subjects?data_classification=research&cursor=bogus")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "research_cursor_invalid"


def test_dictionary_documents_the_excluded_columns(research_env, monkeypatch):
    _with_key(monkeypatch)
    client = _client("steward")
    body = client.get("/research/v1/dictionary").json()
    excluded = {(row["dataset"], row["column"]) for row in body["columns"]
                if not row["published"]}
    assert ("turns", "asr_text") in excluded
    assert ("subjects", "patient_id") in excluded
    assert ("sessions", "training_date") in excluded
