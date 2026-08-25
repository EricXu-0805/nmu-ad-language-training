from datetime import datetime, timedelta
import json
import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import (  # noqa: F401  register tables
    audio_store, auth, content, db as app_db, export, export_security, models,
)
from app.db import get_session
from app.enums import AudioStatus
from app.main import app


ELIGIBLE_PATIENT = {
    "consent_status": "已同意",
    "consent_type": "本人同意",
    "mandarin_eligible": True,
    "recording_allowed": True,
}


def _export_body(label: str) -> dict[str, str]:
    return {"idempotency_key": f"export-{label}-0123456789abcdef0123456789abcdef"}


@pytest.fixture
def client(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(app_db, "engine", eng)
    SQLModel.metadata.create_all(eng)

    def override():
        with Session(eng) as s:
            yield s

    app.dependency_overrides[get_session] = override
    test_client = TestClient(app)    # 不用 with：不触发 lifespan，避免动到文件库
    test_client.test_engine = eng
    yield test_client
    app.dependency_overrides.clear()


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_staff_content_bundles_are_server_routes_not_static_files(client):
    cases = (
        ("/content/item-bank-bundle", "item_bank_version_id"),
        ("/content/week1-script", "script_version_id"),
        ("/content/autopilot-protocol", "protocol_version_id"),
    )
    for path, version_key in cases:
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert response.json().get(version_key)
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["pragma"] == "no-cache"
        head = client.head(path)
        assert head.status_code == 200, path
        assert head.content == b""
        assert head.headers["cache-control"] == "private, no-store"

    # Historical filenames are deliberately not routes.  They can no longer be
    # recovered from public/dist even in loopback M0 development mode.
    for path in (
        "/content/item_bank_v1.json",
        "/content/week1_script.json",
        "/content/autopilot_protocol_v1.json",
    ):
        assert client.get(path).status_code == 404
        assert client.head(path).status_code == 404


@pytest.mark.parametrize(
    ("route", "filename", "mutate"),
    (
        (
            "/content/item-bank-bundle",
            "item_bank_v1.json",
            lambda value: (
                value["single_element"][0].__setitem__("target_word", " "),
                value["single_element"][0].__setitem__("tell_answer", "\t"),
            ),
        ),
        (
            "/content/item-bank-bundle",
            "item_bank_v1.json",
            lambda value: value["single_element"][0]
            .__setitem__("acceptable_expressions", "truthy list"),
        ),
        (
            "/content/week1-script",
            "week1_script.json",
            lambda value: value.__setitem__("sections", "truthy sections"),
        ),
        (
            "/content/week1-script",
            "week1_script.json",
            lambda value: value["sections"][2]
            .__setitem__("key", value["sections"][1]["key"]),
        ),
        (
            "/content/item-bank-bundle",
            "item_bank_v1.json",
            lambda value: value.__setitem__(
                "item_bank_definition_digest", "0" * 64
            ),
        ),
        (
            "/content/autopilot-protocol",
            "autopilot_protocol_v1.json",
            lambda value: value.__setitem__("naming", ["truthy naming"]),
        ),
        (
            "/content/autopilot-protocol",
            "autopilot_protocol_v1.json",
            lambda value: value.__setitem__(
                "autopilot_protocol_definition_digest", "0" * 64
            ),
        ),
    ),
    ids=(
        "item-bank-blank-answer",
        "item-bank-type-confusion",
        "week1-sections-type-confusion",
        "week1-duplicate-section-key",
        "item-bank-digest-mismatch",
        "autopilot-protocol-type-confusion",
        "autopilot-protocol-digest-mismatch",
    ),
)
def test_staff_content_schema_failures_are_controlled_503_without_internals(
        client, monkeypatch, tmp_path, route, filename, mutate):
    definition = json.loads(
        (content.CONTENT_DIR / filename).read_text(encoding="utf-8")
    )
    mutate(definition)
    (tmp_path / filename).write_text(
        json.dumps(definition, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(content, "CONTENT_DIR", tmp_path)

    response = client.get(route)
    assert response.status_code == 503
    assert response.json() == {"detail": {
        "code": "staff_content_bundle_unavailable",
        "message": "工作人员端冻结内容包未通过服务端校验，当前不可读取",
    }}
    assert str(tmp_path) not in response.text
    assert "ValidationError" not in response.text
    assert "Traceback" not in response.text

    head = client.head(route)
    assert head.status_code == 503
    assert head.content == b""
    assert str(tmp_path) not in head.headers.values()


@pytest.mark.parametrize(
    ("route", "filename"),
    (
        ("/content/item-bank-bundle", "item_bank_v1.json"),
        ("/content/week1-script", "week1_script.json"),
        ("/content/autopilot-protocol", "autopilot_protocol_v1.json"),
    ),
)
def test_staff_content_excessive_json_nesting_is_controlled_503_for_get_and_head(
        client, monkeypatch, tmp_path, route, filename):
    depth = 200_000
    (tmp_path / filename).write_text(
        '{"level":' * depth + "0" + "}" * depth, encoding="utf-8"
    )
    monkeypatch.setattr(content, "CONTENT_DIR", tmp_path)

    response = client.get(route)
    assert response.status_code == 503
    assert response.json() == {"detail": {
        "code": "staff_content_bundle_unavailable",
        "message": "工作人员端冻结内容包未通过服务端校验，当前不可读取",
    }}
    assert "RecursionError" not in response.text
    assert str(tmp_path) not in response.text

    head = client.head(route)
    assert head.status_code == 503
    assert head.content == b""
    assert "RecursionError" not in str(head.headers)


def test_direct_content_consumers_share_narrow_503_boundary(
        client, monkeypatch, tmp_path):
    # Seed valid state first; only the packaged definition is then corrupted.
    assert client.post("/patients", json={
        "patient_id": "P-CONTENT-BOUNDARY",
        "is_simulation_subject": True,
        **ELIGIBLE_PATIENT,
    }).status_code == 200
    valid_session = {
        "session_id": "S-CONTENT-BOUNDARY",
        "patient_id": "P-CONTENT-BOUNDARY",
        "week_no": 2,
        "phase_type": "正式训练",
        "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
        "is_simulation": True,
    }
    assert client.post("/sessions", json=valid_session).status_code == 200
    assert client.post("/patients", json={
        "patient_id": "P-CONTENT-CREATE",
        "is_simulation_subject": True,
        **ELIGIBLE_PATIENT,
    }).status_code == 200

    (tmp_path / "item_bank_v1.json").write_text(
        '{"item_bank_version_id":', encoding="utf-8"
    )
    monkeypatch.setattr(content, "CONTENT_DIR", tmp_path)
    safe = TestClient(app, raise_server_exceptions=False)
    try:
        responses = (
            safe.get("/content/item-bank"),
            safe.get("/asr/hotwords"),
            safe.get("/sessions/S-CONTENT-BOUNDARY/plan"),
            safe.post("/sessions", json={
                **valid_session,
                "session_id": "S-CONTENT-CREATE",
                "patient_id": "P-CONTENT-CREATE",
            }),
        )
        for response in responses:
            assert response.status_code == 503, response.text
            assert response.json() == {"detail": {
                "code": "frozen_content_unavailable",
                "message": "服务器冻结训练内容暂不可用，请停止当前操作并联系管理员",
            }}
            assert response.headers["cache-control"] == "private, no-store"
            assert str(tmp_path) not in response.text
            assert "ValidationError" not in response.text
            assert "Traceback" not in response.text

        # HEAD exercises the existing staff route's separate narrow boundary.
        head = safe.head("/content/item-bank-bundle")
        assert head.status_code == 503
        assert head.content == b""

        # Missing packaged files are the OSError branch of the same narrow
        # loader domain, not a generic 500 or a disclosed filesystem path.
        missing_dir = tmp_path / "missing-content"
        monkeypatch.setattr(content, "CONTENT_DIR", missing_dir)
        missing = safe.get("/content/item-bank")
        assert missing.status_code == 503
        assert missing.json()["detail"]["code"] == "frozen_content_unavailable"
        assert str(missing_dir) not in missing.text
    finally:
        safe.close()


def test_patient_with_consent_fields(client):
    body = {"patient_id": "P001", "dementia_severity": "轻度", **ELIGIBLE_PATIENT}
    r = client.post("/patients", json=body)
    assert r.status_code == 200, r.text
    assert client.get("/patients/P001").json()["consent_type"] == "本人同意"
    assert client.post("/patients", json=body).status_code == 409  # 重复


def test_session_needs_patient_and_version(client):
    sess = {"session_id": "S1", "patient_id": "P404", "week_no": 2,
            "phase_type": "正式训练", "event_line": "正式训练", "item_bank_version_id": "wk2-v1-20260707"}
    assert client.post("/sessions", json=sess).status_code == 404  # 患者不存在
    client.post("/patients", json={"patient_id": "P404", **ELIGIBLE_PATIENT,
                                    "is_simulation_subject": True})
    sess["is_simulation"] = True
    assert client.post("/sessions", json=sess).status_code == 200
    # 重复建同 id 场次 → 干净 409(曾是主键冲突 500),前端凭它走"取回续做"
    assert client.post("/sessions", json=sess).status_code == 409
    got = client.get("/sessions/S1")
    assert got.status_code == 200 and got.json()["patient_id"] == "P404"
    assert client.get("/sessions/S404").status_code == 404
    bad_context = {**sess, "session_id": "SCTX", "phase_type": "关系建立",
                   "event_line": "关系建立环节"}
    assert client.post("/sessions", json=bad_context).status_code == 422
    bad_version = {**sess, "session_id": "SVER", "item_bank_version_id": "unknown"}
    assert client.post("/sessions", json=bad_version).status_code == 409


def test_real_session_is_blocked_while_content_is_not_research_ready(
        client, monkeypatch, tmp_path):
    # 2026-08-19 起 week2 题库已冻结交付;「内容未就绪」只能用 staged 草稿
    # 副本重现——这个守门测试守的拒绝行为不因内容交付而消失。
    staged = tmp_path / "content-staged"
    shutil.copytree(content.CONTENT_DIR, staged)
    bank_path = staged / "item_bank_v1.json"
    data = json.loads(bank_path.read_text(encoding="utf-8"))
    data["qc_status"] = "draft"
    bank_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(content, "CONTENT_DIR", staged)

    assert client.post("/patients", json={
        "patient_id": "P-REAL-CONTENT", **ELIGIBLE_PATIENT,
    }).status_code == 200
    denied = client.post("/sessions", json={
        "session_id": "S-REAL-CONTENT", "patient_id": "P-REAL-CONTENT",
        "week_no": 2, "phase_type": "正式训练", "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
    })
    assert denied.status_code == 409
    assert "冻结/质控" in denied.json()["detail"]


def test_session_blocked_for_withdrawn_patient(client):
    # 撤回门禁收口在服务端:前端按钮 disabled 只是提示层,409 建档直通/API 直调都必须被拦
    with Session(client.test_engine) as db_session:
        db_session.add(models.Patient(
            patient_id="PW", withdrawal_status="withdrawn",
            governance_revision=1))
        db_session.add(models.Patient(
            patient_id="PW2", withdrawal_status="withdrawal_requested",
            governance_revision=1))
        db_session.commit()
    sess = {"session_id": "SW", "patient_id": "PW", "week_no": 2,
            "phase_type": "正式训练", "event_line": "正式训练", "item_bank_version_id": "wk2-v1-20260707"}
    r = client.post("/sessions", json=sess)
    assert r.status_code == 409 and "撤回" in r.json()["detail"]
    # 任何非空撤回状态(不只 withdrawn)一律 fail-closed
    r2 = client.post("/sessions", json={**sess, "session_id": "SW2", "patient_id": "PW2"})
    assert r2.status_code == 409


@pytest.mark.parametrize("patient, expected_fragment", [
    ({"patient_id": "P-NONE", "consent_status": "已同意", "consent_type": "本人同意",
      "mandarin_eligible": True}, "recording_allowed"),
    ({"patient_id": "P-FALSE", **ELIGIBLE_PATIENT, "recording_allowed": False},
     "recording_allowed"),
    ({"patient_id": "P-NOCONSENT", "consent_type": "本人同意",
      "mandarin_eligible": True, "recording_allowed": True}, "consent_status"),
    ({"patient_id": "P-NOLANG", **ELIGIBLE_PATIENT, "mandarin_eligible": False},
     "mandarin_eligible"),
])
def test_research_session_fail_closed_for_incomplete_eligibility(
        client, patient, expected_fragment):
    assert client.post("/patients", json=patient).status_code == 200
    response = client.post("/sessions", json={
        "session_id": f"S-{patient['patient_id']}", "patient_id": patient["patient_id"],
        "week_no": 2, "phase_type": "正式训练", "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
    })
    assert response.status_code == 409
    assert expected_fragment in response.json()["detail"]


def test_proxy_consent_requires_proxy_and_assent(client):
    base = {"consent_status": "已同意", "consent_type": "代理同意加本人赞同",
            "mandarin_eligible": True, "recording_allowed": True}
    client.post("/patients", json={"patient_id": "P-PROXY-BAD", **base,
                                    "proxy_consent": True, "assent_obtained": False})
    session = {"session_id": "S-PROXY-BAD", "patient_id": "P-PROXY-BAD", "week_no": 2,
               "phase_type": "正式训练", "event_line": "正式训练",
               "item_bank_version_id": "wk2-v1-20260707"}
    denied = client.post("/sessions", json=session)
    assert denied.status_code == 409 and "assent_obtained" in denied.json()["detail"]

    client.post("/patients", json={"patient_id": "P-PROXY-OK", **base,
                                    "proxy_consent": True, "assent_obtained": True})
    allowed = client.post("/sessions", json={**session, "session_id": "S-PROXY-OK",
                                              "patient_id": "P-PROXY-OK"})
    # 代理同意 + 本人赞同齐备后放行(2026-08-19 内容冻结交付,真实场次可建),
    # 反证上面的 409 拦的确是 assent 而非别的门。
    assert allowed.status_code == 200, allowed.text


def test_simulation_session_and_audio_are_explicit_and_server_derived(client, monkeypatch):
    monkeypatch.delenv("ALLOW_SIMULATION_DATA", raising=False)
    client.post("/patients", json={"patient_id": "P-SIM", "is_simulation_subject": True})
    session = {"session_id": "S-SIM", "patient_id": "P-SIM", "week_no": 2,
               "phase_type": "正式训练", "event_line": "正式训练",
               "item_bank_version_id": "wk2-v1-20260707", "is_simulation": True}
    assert client.post("/sessions", json=session).status_code == 409
    monkeypatch.setenv("ALLOW_SIMULATION_DATA", "1")
    created = client.post("/sessions", json=session)
    assert created.status_code == 200 and created.json()["is_simulation"] is True
    assert created.json()["data_classification"] == "simulation"

    # 普通档案和任何明确拒绝都不能利用模拟开关绕过。
    client.post("/patients", json={"patient_id": "P-NOT-SIM"})
    denied_plain = client.post("/sessions", json={
        **session, "session_id": "S-NOT-SIM", "patient_id": "P-NOT-SIM",
    })
    assert denied_plain.status_code == 409 and "is_simulation_subject" in denied_plain.json()["detail"]
    client.post("/patients", json={"patient_id": "P-SIM-DENIED",
                                    "is_simulation_subject": True,
                                    "consent_status": "拒绝",
                                    "recording_allowed": False})
    denied_explicit = client.post("/sessions", json={
        **session, "session_id": "S-SIM-DENIED", "patient_id": "P-SIM-DENIED",
    })
    assert denied_explicit.status_code == 409
    assert "consent_status" in denied_explicit.json()["detail"]
    assert "recording_allowed=false" in denied_explicit.json()["detail"]

    # 音频不需重复声明模拟；服务端从场次派生并持久化。
    audio = client.post("/audio", json={"raw_audio_id": "sim-a", "session_id": "S-SIM"})
    assert audio.status_code == 200 and audio.json()["is_simulation"] is True
    assert audio.json()["data_classification"] == "simulation"
    assert client.post("/audio", json={"raw_audio_id": "sim-conflict", "session_id": "S-SIM",
                                        "is_simulation": False}).status_code == 409


def test_sessionless_audio_requires_explicit_enabled_simulation(client, monkeypatch):
    monkeypatch.delenv("ALLOW_SIMULATION_DATA", raising=False)
    assert client.post("/audio", json={"raw_audio_id": "unbound-implicit"}).status_code == 409
    assert client.post("/audio", json={"raw_audio_id": "unbound-disabled",
                                        "is_simulation": True}).status_code == 409
    monkeypatch.setenv("ALLOW_SIMULATION_DATA", "1")
    created = client.post("/audio", json={"raw_audio_id": "unbound-sim",
                                           "is_simulation": True})
    assert created.status_code == 200 and created.json()["is_simulation"] is True


def test_legacy_unknown_session_is_visible_but_blocked_from_new_processing(client):
    _mk_session(client, sid="S-LEGACY", pid="P-LEGACY")
    with Session(client.test_engine) as db_session:
        session = db_session.get(models.Session, "S-LEGACY")
        session.data_classification = "legacy_unknown"
        db_session.add(session)
        db_session.commit()
    fetched = client.get("/sessions/S-LEGACY")
    assert fetched.status_code == 200
    assert fetched.json()["data_classification"] == "legacy_unknown"
    denied = client.post("/sessions/S-LEGACY/recording-authorization")
    assert denied.status_code == 409
    assert "legacy_unknown" in denied.json()["detail"]


def test_item_bank_endpoint(client):
    # 2026-08-21 交互数据包交付后的现实:week2 题库 20单+10双+2多、qc frozen、
    # 零 error 零 warning、全部开放环节有 operational_rubrics;第2-8周交互
    # 数据包按周登记,10双×5 + 2多×4 = 58 个题位全部由数据包驱动,78/78 可自动执行。
    d = client.get("/content/item-bank").json()
    assert d["single_count"] == 20 and d["double_count"] == 10
    assert d["multi_count"] == 2 and d["supported_training_weeks"] == [2]
    assert d["structured_training_weeks"] == [2, 3, 4, 5, 6, 7, 8]
    assert d["qc_status"] == "frozen" and d["ready_for_research"] is True
    assert d["operational_autopilot_ready"] is True
    assert len(d["item_bank_definition_digest"]) == 64
    assert d["autopilot_protocol_version_id"] == "autopilot-v2-20260821"
    assert len(d["autopilot_protocol_definition_digest"]) == 64
    assert d["protocol_validation_issues"] == []
    assert d["selector_validation_issues"] == []
    assert d["autopilot_admission_validation_issue"] is None
    assert d["operational_position_count"] == 78
    assert d["unsupported_operational_position_count"] == 0
    assert d["unsupported_operational_positions"] == []
    assert d["unsupported_operational_position_counts_by_code"] == {
        "source_field_unavailable": 0,
        "operational_rubric_unavailable": 0,
        "operational_protocol_unavailable": 0,
        "interaction_package_unavailable": 0,
    }
    assert d["unsupported_operational_position_gaps"] == []
    assert d["source_protocol_position_count"] == 78
    assert d["source_unstructured_position_count"] == 0
    assert d["source_unstructured_positions"] == []
    assert d["delivery_unsupported_position_count"] == 0
    assert d["source_document_sha256"] == (
        "b3310b61bdc6afb437cbc05785bd6f4e1f6c30dd53ad0999eb2c0fea10c3891a"
    )
    assert d["draft_revision"] == "2026-08-19.1"
    # The rubric-only QC list remains separately available and must not be
    # mistaken for the stronger full-plan automatic protocol scan; with all
    # open-ended rubrics delivered it is now empty.
    assert d["unsupported_operational_rubrics"] == []
    assert d["errors"] == []
    assert d["warnings"] == []


def test_item_bank_endpoint_serves_requested_week(client):
    # 2026-08-25 Eric 实测:第 3 周场次在训练台整屏 fail-closed「题库版本不一致:
    # 计划=wk3-v1-20260819 后端=wk2-v1-20260707」——本端点曾写死第 2 周,
    # 训练台按场次周次取数就永远对不上。周参数必须真的换周。
    from app import content as content_module
    for week in (3, 8):
        expected = content_module.load_item_bank_for_week(week)
        d = client.get(f"/content/item-bank?week={week}").json()
        assert d["version_id"] == expected.version_id, week
        assert d["supported_training_weeks"] == [week]
        assert d["operational_position_count"] == 78
        assert d["unsupported_operational_position_count"] == 0
        assert d["operational_autopilot_ready"] is True
        # 逐周汇总与默认周语义不随请求周漂移。
        assert d["structured_training_weeks"] == [2, 3, 4, 5, 6, 7, 8]
    # 默认(不带参数)保持第 2 周就绪探针语义,见 test_item_bank_endpoint。
    assert client.get("/content/item-bank").json()["version_id"].startswith("wk2-")
    # 越界周 fail-closed。
    assert client.get("/content/item-bank?week=1").status_code == 422
    assert client.get("/content/item-bank?week=9").status_code == 422


def test_score_double_all_correct_is_100(client):
    items = [{"item_id": "d1", "left_name": 1, "left_function": 1,
              "right_name": 1, "right_function": 1, "relation": 1}]
    d = client.post("/score/double", json=items).json()
    assert d["weekly_de_score_percentile"] == 100.0


def test_score_single_validation_error(client):
    bad = [{"item_id": "x", "final_correct": 1, "spontaneous_correct": 1, "prompt_level": 2}]
    assert client.post("/score/single", json=bad).status_code == 422  # 自发正确却有提示


def test_judge_rejects_portrait_at_boundary(client):
    r = client.post("/judge/build-input", json={
        "item_id": "d1", "task_type": "单要素", "target_word": "锚", "zodiac": "属牛"})
    assert r.status_code == 400
    assert "画像" in r.json()["detail"]


def test_judge_valid_prefers_confirmed(client):
    r = client.post("/judge/build-input", json={
        "item_id": "d1", "task_type": "单要素", "target_word": "胡萝卜",
        "asr_text": "红萝波", "confirmed_response_text": "红萝卜"})
    assert r.status_code == 200
    assert r.json()["resolved_text"] == "红萝卜"


def _export_one_audio_for_delete_binding(
        client, *, session_id: str, patient_id: str,
        raw_audio_id: str) -> dict:
    _mk_session(client, sid=session_id, pid=patient_id)
    registered = client.post("/audio", json={
        "raw_audio_id": raw_audio_id,
        "session_id": session_id,
    })
    assert registered.status_code == 200, registered.text
    uploaded = client.put(
        f"/audio/{raw_audio_id}/blob",
        content=b"\x1a\x45\xdf\xa3authoritative-audio",
        headers={"content-type": "audio/webm"},
    )
    assert uploaded.status_code == 200, uploaded.text
    _mark_completed(client, session_id)
    _login_admin(client)
    exported = client.post(
        f"/sessions/{session_id}/export",
        json=_export_body(session_id.casefold()),
    )
    assert exported.status_code == 200, exported.text
    return exported.json()


def test_audio_gate_blocks_premature_delete(client, monkeypatch):
    _mk_session(client, sid="SA1", pid="PA1")
    client.post("/audio", json={"raw_audio_id": "a1", "session_id": "SA1"})
    # ★环境开关先放行:端点把 ENABLE_AUDIO_DELETE 检查放在闸门之前,若开关关着,
    # 下面每个 409 都来自"物理删除默认禁用"而非闸门——闸门断言全部变成空断言,
    # 端点绕过 request_delete 的回归将测不出来。
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    r = client.delete("/audio/a1")
    assert r.status_code == 409 and "导出" in r.json()["detail"]  # 未导出不能删(真闸门拒绝)
    client.put("/audio/a1/blob", content=b"\x1a\x45\xdf\xa3fake-audio",
               headers={"content-type": "audio/webm"})     # 字节落库+登记 sha256
    assert client.post("/audio/a1/export").status_code == 409      # 禁止脱离场次盲翻状态
    _mark_completed(client, "SA1")
    _login_admin(client)
    intent = _export_body("sa1")
    exported_response = client.post("/sessions/SA1/export", json=intent)
    assert exported_response.status_code == 200, exported_response.text
    ex = exported_response.json()
    assert ex["audio_touched"] == [export_security.pseudonymize_audio("a1")]
    assert ex["status"] == "published"
    assert all(not path.startswith("/") for path in ex["files"])
    assert all(not artifact["relative_path"].startswith("/")
               for artifact in ex["artifacts"])
    assert str(export.EXPORT_DIR) not in exported_response.text
    assert "EXPORT-ADMIN" not in exported_response.text
    replay = client.post("/sessions/SA1/export", json=intent)
    assert replay.status_code == 200
    assert replay.json()["batch_id"] == ex["batch_id"]
    readback = client.get(f"/exports/{ex['batch_id']}")
    assert readback.status_code == 200
    assert readback.json()["artifacts"] == ex["artifacts"]
    with Session(client.test_engine) as db_session:
        batch_row = db_session.get(models.ExportBatch, ex["batch_id"])
        assert batch_row.actor_display_id == "EXPORT-ADMIN"
        assert batch_row.actor_role == "admin"
        export_audit = db_session.exec(select(models.AuditLog).where(
            models.AuditLog.action == "data_export")).first()
        assert export_audit is not None
        assert export_audit.patient_id is None
        assert export_audit.session_id is None
        assert "SA1" not in export_audit.summary
    assert client.put("/audio/a1/blob", content=b"\x1a\x45\xdf\xa3overwrite",
                      headers={"content-type": "audio/webm"}).status_code == 409
    r = client.delete("/audio/a1")
    assert r.status_code == 409 and "校验" in r.json()["detail"]  # 未校验不能删(真闸门拒绝)
    audio_store.find_blob("a1").write_bytes(b"source-was-changed") # 校验不得回头拿采集源文件冒充
    assert client.post("/audio/a1/checksum").status_code == 200   # 真校验通过
    # 闸门全绿但环境开关关闭:仍默认禁删——独立于闸门的第二道保险
    monkeypatch.delenv("ENABLE_AUDIO_DELETE")
    r = client.delete("/audio/a1")
    assert r.status_code == 409 and "默认禁用" in r.json()["detail"]
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    r = client.delete("/audio/a1")
    assert r.status_code == 200 and r.json()["bytes_deleted"] is True  # 放行才物理删字节
    assert client.get("/audio/a1/blob").status_code == 404        # 字节确已删除


def test_audio_delete_rejects_unlisted_matching_controlled_copy(
        client, monkeypatch):
    """An extra blob cannot borrow an unrelated valid batch as delete proof."""
    exported = _export_one_audio_for_delete_binding(
        client,
        session_id="S-UNLISTED-AUDIO",
        patient_id="P-UNLISTED-AUDIO",
        raw_audio_id="listed-audio",
    )
    extra_bytes = b"\x1a\x45\xdf\xa3unlisted-controlled-copy"
    source_path, source_checksum = audio_store.save_blob(
        "unlisted-audio", extra_bytes, "audio/webm")
    audio_code = export_security.pseudonymize_audio("unlisted-audio")
    controlled_copy = (
        export.CONTROLLED_AUDIO_DIR
        / "simulation" / exported["batch_id"] / "audio"
        / f"{audio_code}.webm"
    )
    controlled_copy.write_bytes(extra_bytes)
    controlled_copy.chmod(0o600)
    with Session(client.test_engine) as db_session:
        db_session.add(models.AudioAssetRow(
            raw_audio_id="unlisted-audio",
            session_id="S-UNLISTED-AUDIO",
            is_simulation=True,
            data_classification="simulation",
            audio_format="webm",
            status=AudioStatus.deletable,
            checksum=source_checksum,
            byte_count=len(extra_bytes),
            export_batch_id=exported["batch_id"],
        ))
        db_session.commit()

    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    denied = client.delete("/audio/unlisted-audio")

    assert denied.status_code == 409
    assert denied.json()["detail"]["code"] == (
        "controlled_export_artifact_unverified")
    assert audio_store.find_blob("unlisted-audio") == source_path
    assert source_path.read_bytes() == extra_bytes
    assert controlled_copy.read_bytes() == extra_bytes
    with Session(client.test_engine) as db_session:
        stored = db_session.get(models.AudioAssetRow, "unlisted-audio")
        assert stored is not None
        assert stored.status == AudioStatus.deletable
        assert stored.delete_gate_passed is False


@pytest.mark.parametrize(
    "mismatch",
    [
        "metadata_missing",
        "artifact_wrong_kind",
        "artifact_wrong_path",
        "artifact_wrong_hash",
        "artifact_duplicate",
    ],
)
def test_audio_delete_rejects_export_projection_mismatch_without_mutation(
        client, monkeypatch, mismatch):
    exported = _export_one_audio_for_delete_binding(
        client,
        session_id="S-EXPORT-MISMATCH",
        patient_id="P-EXPORT-MISMATCH",
        raw_audio_id="mismatch-audio",
    )
    checksum_result = client.post("/audio/mismatch-audio/checksum")
    assert checksum_result.status_code == 200, checksum_result.text
    source_path = audio_store.find_blob("mismatch-audio")
    assert source_path is not None
    source_bytes = source_path.read_bytes()
    audio_code = export_security.pseudonymize_audio("mismatch-audio")
    controlled = [
        artifact for artifact in exported["artifacts"]
        if artifact["kind"] == "controlled_audio"
        and artifact["relative_path"].endswith(f"/{audio_code}.webm")
    ]
    assert len(controlled) == 1
    target_path = controlled[0]["relative_path"]
    real_get_export_batch_result = export.get_export_batch_result

    def mismatched_result(db_session, batch_id, *, write_dir=None):
        result = real_get_export_batch_result(
            db_session, batch_id, write_dir=write_dir)
        projected = {
            **result,
            "audio_touched": list(result["audio_touched"]),
            "artifacts": [dict(artifact) for artifact in result["artifacts"]],
        }
        if mismatch == "metadata_missing":
            projected["audio_touched"].remove(audio_code)
            return projected
        target = next(
            artifact for artifact in projected["artifacts"]
            if artifact["relative_path"] == target_path)
        if mismatch == "artifact_wrong_kind":
            target["kind"] = "csv"
        elif mismatch == "artifact_wrong_path":
            target["relative_path"] = (
                f"simulation/{batch_id}/wrong/{audio_code}.webm")
        elif mismatch == "artifact_wrong_hash":
            target["sha256"] = "0" * 64
        elif mismatch == "artifact_duplicate":
            projected["artifacts"].append(dict(target))
        return projected

    monkeypatch.setattr(
        export, "get_export_batch_result", mismatched_result)
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    denied = client.delete("/audio/mismatch-audio")

    assert denied.status_code == 409
    assert denied.json()["detail"]["code"] == (
        "controlled_export_artifact_unverified")
    assert source_path.read_bytes() == source_bytes
    with Session(client.test_engine) as db_session:
        stored = db_session.get(models.AudioAssetRow, "mismatch-audio")
        assert stored is not None
        assert stored.status == AudioStatus.deletable
        assert stored.delete_gate_passed is False


def test_audio_checksum_requires_real_bytes(client):
    _mk_session(client, sid="SNB", pid="PNB")
    client.post("/audio", json={"raw_audio_id": "nb", "session_id": "SNB"})
    _mark_completed(client, "SNB")
    _login_admin(client)
    client.post("/sessions/SNB/export", json=_export_body("snb"))
    assert client.post("/audio/nb/checksum").status_code == 409   # 无字节禁止盲翻状态


def test_legacy_export_without_authoritative_ledger_cannot_advance_delete_gate(
        client, monkeypatch):
    """A legacy directory/checksum must never substitute for ExportBatch receipts."""
    _login_admin(client)
    deletable_path, deletable_checksum = audio_store.save_blob(
        "legacy-deletable-no-ledger",
        b"\x1a\x45\xdf\xa3legacy-deletable", "audio/webm")
    deleted_path, deleted_checksum = audio_store.save_blob(
        "legacy-deleted-no-ledger",
        b"\x1a\x45\xdf\xa3legacy-logically-deleted", "audio/webm")
    with Session(client.test_engine) as db_session:
        db_session.add(models.AudioAssetRow(
            raw_audio_id="legacy-export-no-ledger",
            status=AudioStatus.exported,
            export_batch_id="EXP-legacy-no-ledger",
            checksum="a" * 64,
            is_simulation=True,
            data_classification="simulation",
        ))
        db_session.add(models.AudioAssetRow(
            raw_audio_id="legacy-review-no-ledger",
            status=AudioStatus.checksum_verified,
            is_reliability_sample=True,
            export_batch_id="EXP-legacy-review-no-ledger",
            checksum="b" * 64,
            is_simulation=True,
            data_classification="simulation",
        ))
        db_session.add(models.AudioAssetRow(
            raw_audio_id="legacy-deletable-no-ledger",
            status=AudioStatus.deletable,
            export_batch_id="EXP-legacy-deletable-no-ledger",
            checksum=deletable_checksum,
            audio_format=deletable_path.suffix.lstrip("."),
            is_simulation=True,
            data_classification="simulation",
        ))
        db_session.add(models.AudioAssetRow(
            raw_audio_id="legacy-deleted-no-ledger",
            status=AudioStatus.deleted,
            export_batch_id="EXP-legacy-deleted-no-ledger",
            checksum=deleted_checksum,
            audio_format=deleted_path.suffix.lstrip("."),
            delete_gate_passed=True,
            is_simulation=True,
            data_classification="simulation",
        ))
        db_session.commit()

    blocked = client.post("/audio/legacy-export-no-ledger/checksum")

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "legacy_export_batch_unverified"
    with Session(client.test_engine) as db_session:
        stored = db_session.get(models.AudioAssetRow, "legacy-export-no-ledger")
        assert stored is not None
        assert stored.status == AudioStatus.exported
        assert stored.delete_gate_passed is False

    review = client.post("/audio/legacy-review-no-ledger/reliability-review")
    assert review.status_code == 409
    assert review.json()["detail"]["code"] == "legacy_export_batch_unverified"
    with Session(client.test_engine) as db_session:
        stored = db_session.get(models.AudioAssetRow, "legacy-review-no-ledger")
        assert stored is not None
        assert stored.status == AudioStatus.checksum_verified
        assert stored.delete_gate_passed is False

    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    deleted = client.delete("/audio/legacy-export-no-ledger")
    assert deleted.status_code == 409
    legacy_deletable = client.delete("/audio/legacy-deletable-no-ledger")
    assert legacy_deletable.status_code == 409
    assert legacy_deletable.json()["detail"]["code"] == (
        "legacy_export_batch_unverified")
    assert audio_store.find_blob("legacy-deletable-no-ledger") is not None
    legacy_deleted = client.delete("/audio/legacy-deleted-no-ledger")
    assert legacy_deleted.status_code == 409
    assert legacy_deleted.json()["detail"]["code"] == (
        "legacy_export_batch_unverified")
    assert audio_store.find_blob("legacy-deleted-no-ledger") is not None
    with Session(client.test_engine) as db_session:
        stored = db_session.get(models.AudioAssetRow, "legacy-export-no-ledger")
        assert stored is not None
        assert stored.status == AudioStatus.exported
        assert stored.delete_gate_passed is False
        deletable = db_session.get(
            models.AudioAssetRow, "legacy-deletable-no-ledger")
        assert deletable is not None
        assert deletable.status == AudioStatus.deletable
        assert deletable.delete_gate_passed is False
        logically_deleted = db_session.get(
            models.AudioAssetRow, "legacy-deleted-no-ledger")
        assert logically_deleted is not None
        assert logically_deleted.status == AudioStatus.deleted
        assert logically_deleted.delete_gate_passed is True


def test_audio_blob_roundtrip_and_asr_degraded(client, monkeypatch):
    monkeypatch.setenv("ALLOW_SIMULATION_DATA", "1")
    client.post("/audio", json={"raw_audio_id": "rt", "is_simulation": True})
    up = client.put("/audio/rt/blob", content=b"\x1a\x45\xdf\xa3webm-ish",
                    headers={"content-type": "audio/webm"}).json()
    assert up["format"] == "webm" and len(up["checksum"]) == 64
    assert up["idempotent"] is False
    repeated = client.put("/audio/rt/blob", content=b"\x1a\x45\xdf\xa3webm-ish",
                          headers={"content-type": "audio/webm"}).json()
    assert repeated["idempotent"] is True
    assert client.post("/asr/transcribe/rt").status_code == 409


def test_recording_explicitly_denied_blocks_audio_registration(client):
    client.post("/patients", json={"patient_id": "PNOREC", **ELIGIBLE_PATIENT,
                                    "recording_allowed": False})
    denied = client.post("/sessions", json={
        "session_id": "SNOREC", "patient_id": "PNOREC", "week_no": 2,
        "phase_type": "正式训练", "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707"})
    assert denied.status_code == 409
    assert "recording_allowed" in denied.json()["detail"]


def test_audio_registration_and_upload_revalidate_current_patient_state(client):
    _mk_session(client, sid="S-REVALIDATE", pid="P-REVALIDATE")
    registered = client.post("/audio", json={
        "raw_audio_id": "revalidate-upload", "session_id": "S-REVALIDATE",
    })
    assert registered.status_code == 200

    with Session(client.test_engine) as db_session:
        patient = db_session.get(models.Patient, "P-REVALIDATE")
        patient.recording_allowed = False
        db_session.add(patient)
        db_session.commit()
    upload = client.put("/audio/revalidate-upload/blob", content=b"must-not-be-saved")
    assert upload.status_code == 409 and "recording_allowed" in upload.json()["detail"]
    assert audio_store.find_blob("revalidate-upload") is None

    with Session(client.test_engine) as db_session:
        patient = db_session.get(models.Patient, "P-REVALIDATE")
        patient.recording_allowed = True
        patient.withdrawal_status = "withdrawn"
        db_session.add(patient)
        db_session.commit()
    registration = client.post("/audio", json={
        "raw_audio_id": "after-withdrawal", "session_id": "S-REVALIDATE",
    })
    assert registration.status_code == 409 and "撤回" in registration.json()["detail"]


def test_audio_rejects_session_whose_patient_row_is_missing(client):
    with Session(client.test_engine) as db_session:
        db_session.add(models.Session(
            session_id="S-ORPHAN", patient_id="P-MISSING", week_no=2,
            phase_type="正式训练", event_line="正式训练",
            item_bank_version_id="wk2-v1-20260707",
        ))
        db_session.commit()
    response = client.post("/audio", json={
        "raw_audio_id": "orphan-audio", "session_id": "S-ORPHAN",
    })
    assert response.status_code == 409 and "缺少受试者档案" in response.json()["detail"]


def test_asr_hotwords_from_frozen_bank(client):
    d = client.get("/asr/hotwords").json()
    assert d["engine"] == "null-0"
    assert "胡萝卜" in d["hotwords"] and "斧子" in d["hotwords"] and "树" in d["hotwords"]
    assert "属牛" in d["hotwords"] or "牛" in d["hotwords"]       # 属相闭表并入


def test_audio_reliability_needs_review_before_delete(client, monkeypatch):
    monkeypatch.setenv("DEIDENTIFICATION_KEY", "test-only-deidentification-key-32-bytes")
    monkeypatch.setenv("DEIDENTIFICATION_KEY_ID", "test-key")
    _mk_session(client, sid="SREL", pid="PREL")
    client.post("/audio", json={"raw_audio_id": "rel", "session_id": "SREL",
                                "is_reliability_sample": True})
    client.put("/audio/rel/blob", content=b"\x1a\x45\xdf\xa3rel-bytes",
               headers={"content-type": "audio/webm"})
    _mark_completed(client, "SREL")
    _login_admin(client)
    exported = client.post("/sessions/SREL/export", json=_export_body("srel"))
    assert exported.status_code == 200, exported.text
    checksummed = client.post("/audio/rel/checksum")
    assert checksummed.status_code == 200, checksummed.text
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")  # 先放行开关,让 409 真正来自信度闸门
    r = client.delete("/audio/rel")
    assert r.status_code == 409 and "信度" in r.json()["detail"]  # 信度样本须先人工复核
    reviewed = client.post("/audio/rel/reliability-review")
    assert reviewed.status_code == 200, reviewed.text
    deleted = client.delete("/audio/rel")
    assert deleted.status_code == 200, deleted.text
    with Session(client.test_engine) as db_session:
        actions = {row.action for row in db_session.exec(select(models.AuditLog))}
    assert {"audio_checksum_verified", "audio_reliability_review", "audio_delete"} <= actions


def test_withdrawn_audio_cannot_advance_checksum_or_reliability_review(
        client, monkeypatch):
    monkeypatch.setenv(
        "DEIDENTIFICATION_KEY", "test-only-deidentification-key-32-bytes")
    monkeypatch.setenv("DEIDENTIFICATION_KEY_ID", "test-key")
    _mk_session(client, sid="S-WITHDRAW-GATES", pid="P-WITHDRAW-GATES")
    for raw_audio_id, reliability in (
        ("withdrawn-checksum", False),
        ("withdrawn-review", True),
    ):
        registered = client.post("/audio", json={
            "raw_audio_id": raw_audio_id,
            "session_id": "S-WITHDRAW-GATES",
            "is_reliability_sample": reliability,
        })
        assert registered.status_code == 200, registered.text
        uploaded = client.put(
            f"/audio/{raw_audio_id}/blob",
            content=b"\x1a\x45\xdf\xa3" + raw_audio_id.encode("ascii"),
            headers={"content-type": "audio/webm"},
        )
        assert uploaded.status_code == 200, uploaded.text
    _mark_completed(client, "S-WITHDRAW-GATES")
    _login_admin(client)
    exported = client.post(
        "/sessions/S-WITHDRAW-GATES/export",
        json=_export_body("withdraw-gates"),
    )
    assert exported.status_code == 200, exported.text
    reviewed_prerequisite = client.post("/audio/withdrawn-review/checksum")
    assert reviewed_prerequisite.status_code == 200, reviewed_prerequisite.text

    with Session(client.test_engine) as db_session:
        patient = db_session.get(models.Patient, "P-WITHDRAW-GATES")
        patient.withdrawal_status = "withdrawn"
        patient.consent_status = "withdrawn"
        patient.secondary_use_allowed = False
        db_session.add(patient)
        for raw_audio_id in ("withdrawn-checksum", "withdrawn-review"):
            row = db_session.get(models.AudioAssetRow, raw_audio_id)
            # Cover both independent terminal signals: the review row proves
            # a non-empty isolation status alone is sufficient to fail closed.
            row.withdrawn = raw_audio_id == "withdrawn-checksum"
            row.withdrawal_status = "isolated_by_subject_withdrawal"
            db_session.add(row)
        db_session.commit()

    checksum = client.post("/audio/withdrawn-checksum/checksum")
    review = client.post("/audio/withdrawn-review/reliability-review")
    assert checksum.status_code == 409 and (
        "撤回" in checksum.text or "失效" in checksum.text)
    assert review.status_code == 409 and "撤回" in review.text
    monkeypatch.setenv("ENABLE_AUDIO_DELETE", "1")
    wrong_channel = client.delete("/audio/withdrawn-checksum")
    assert wrong_channel.status_code == 409
    assert wrong_channel.json()["detail"]["code"] == (
        "audio_withdrawal_delete_source_required")
    assert audio_store.find_blob("withdrawn-checksum") is not None
    with Session(client.test_engine) as db_session:
        assert db_session.get(
            models.AudioAssetRow, "withdrawn-checksum").status.value == "exported"
        assert db_session.get(
            models.AudioAssetRow, "withdrawn-review").status.value == "checksum_verified"


def _mk_session(client, sid="SM0", pid="PM0"):
    client.post("/patients", json={"patient_id": pid, **ELIGIBLE_PATIENT,
                                    "is_simulation_subject": True,
                                    "secondary_use_allowed": True})
    client.post("/sessions", json={"session_id": sid, "patient_id": pid, "week_no": 2,
                                   "phase_type": "正式训练", "event_line": "正式训练",
                                   "item_bank_version_id": "wk2-v1-20260707",
                                   "is_simulation": True,
                                   "trainer_id": "CONFIRMATION-REVIEWER"})


def _open_review_window(client, session_id: str) -> None:
    """Unit-test shortcut; lifecycle tests exercise the real finish endpoint."""
    with Session(client.test_engine) as db_session:
        runtime = db_session.get(models.SessionRuntimeState, session_id)
        if runtime is None:
            runtime = models.SessionRuntimeState(session_id=session_id)
        runtime.status = "intervention_completed"
        runtime.intervention_completed_at = datetime.now()
        runtime.revision = max(runtime.revision, 1)
        db_session.add(runtime)
        db_session.commit()


def _login_confirmation_reviewer(client) -> None:
    with Session(client.test_engine) as db_session:
        db_session.add(models.ResearchUser(
            username="confirmation-reviewer",
            display_id="CONFIRMATION-REVIEWER",
            password_hash=auth.hash_password("password-2026"),
            role="researcher",
            created_at=datetime.now(),
        ))
        db_session.commit()
    response = client.post("/auth/login", json={
        "username": "confirmation-reviewer", "password": "password-2026",
    })
    assert response.status_code == 200, response.text
    csrf = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert csrf
    client.headers.update({"X-CSRF-Token": csrf})


def _mark_completed(client, session_id: str) -> None:
    with Session(client.test_engine) as db_session:
        train_session = db_session.get(models.Session, session_id)
        assert train_session is not None
        intervention_completed_at = datetime(2026, 7, 19, 9, 0, 0)
        completed_at = intervention_completed_at + timedelta(minutes=5)
        actor = "TEST-COMPLETER"
        runtime = db_session.get(models.SessionRuntimeState, session_id)
        if runtime is None:
            runtime = models.SessionRuntimeState(session_id=session_id)
        runtime.status = "completed"
        runtime.revision = max(runtime.revision, 1)
        runtime.intervention_completed_at = intervention_completed_at
        runtime.completed_at = completed_at
        runtime.ended_by = actor
        runtime.end_reason = "completion_gate_passed"
        db_session.add(runtime)
        if db_session.get(models.SessionOutcomeSummary, session_id) is None:
            db_session.add(models.SessionOutcomeSummary(
                session_id=session_id,
                schema_version="session-outcome-summary.v1",
                generator_version="test-export-closeout.v1",
                item_bank_version_id=train_session.item_bank_version_id,
                is_simulation=train_session.is_simulation,
                data_classification=train_session.data_classification,
                expected_turns=0,
                matched_turns=0,
                completed_attempt_turns=0,
                audio_evidenced_turns=0,
                total_attempts=0,
                completed_attempts=0,
                needs_review_attempts=0,
                technical_failure_attempts=0,
                prompt_level_0_count=0,
                prompt_level_1_count=0,
                prompt_level_2_count=0,
                prompt_level_3_count=0,
                technical_pause_count=0,
                researcher_takeover_count=0,
                source_digest="a" * 64,
                generated_at=intervention_completed_at - timedelta(seconds=1),
            ))
        if db_session.get(models.SessionCloseoutReport, session_id) is None:
            db_session.add(models.SessionCloseoutReport(
                session_id=session_id,
                schema_version="session-closeout.v1",
                status="no_additional_observation",
                revision=2,
                last_idempotency_key="test-export-closeout",
                last_request_hash="b" * 64,
                created_by=actor,
                created_at=intervention_completed_at + timedelta(minutes=1),
                updated_by=actor,
                updated_at=completed_at,
                locked_by=actor,
                locked_at=completed_at,
            ))
        db_session.commit()


def _login_admin(client) -> None:
    with Session(client.test_engine) as db_session:
        db_session.add(models.ResearchUser(
            username="export-admin", display_id="EXPORT-ADMIN",
            password_hash=auth.hash_password("password1"), role="admin"))
        db_session.commit()
    assert client.post("/auth/login", json={
        "username": "export-admin", "password": "password1",
    }).status_code == 200
    token = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert token
    client.headers.update({"X-CSRF-Token": token})


def _login_role(client, role: str, username: str) -> None:
    with Session(client.test_engine) as db_session:
        db_session.add(models.ResearchUser(
            username=username, display_id=username.upper(),
            password_hash=auth.hash_password("password1"), role=role))
        db_session.commit()
    assert client.post("/auth/login", json={
        "username": username, "password": "password1",
    }).status_code == 200
    token = client.cookies.get(auth.CSRF_COOKIE_NAME)
    assert token
    client.headers.update({"X-CSRF-Token": token})


def test_audio_upload_is_limited_validated_idempotent_and_pause_safe(client):
    _mk_session(client, sid="S-UPLOAD", pid="P-UPLOAD")
    assert client.post("/audio", json={
        "raw_audio_id": "pending-before-pause", "session_id": "S-UPLOAD",
        "turn_key": "SE_锚#1",
    }).status_code == 200
    assert client.post("/sessions/S-UPLOAD/pause").status_code == 200
    assert client.post("/audio", json={
        "raw_audio_id": "new-during-pause", "session_id": "S-UPLOAD",
        "turn_key": "SE_锚#1",
    }).status_code == 409

    payload = b"\x1a\x45\xdf\xa3paused-flush"
    first = client.put("/audio/pending-before-pause/blob", content=payload,
                       headers={"content-type": "audio/webm"})
    assert first.status_code == 200 and first.json()["idempotent"] is False
    repeat = client.put("/audio/pending-before-pause/blob", content=payload,
                        headers={"content-type": "audio/webm"})
    assert repeat.status_code == 200 and repeat.json()["idempotent"] is True
    assert client.put("/audio/pending-before-pause/blob",
                      content=b"\x1a\x45\xdf\xa3different",
                      headers={"content-type": "audio/webm"}).status_code == 409
    assert audio_store.find_blob("pending-before-pause").read_bytes() == payload

    assert client.post("/sessions/S-UPLOAD/resume").status_code == 200
    for raw_id in ("bad-mime", "bad-magic", "too-large", "checksum-gap"):
        assert client.post("/audio", json={
            "raw_audio_id": raw_id, "session_id": "S-UPLOAD",
            "turn_key": "SE_锚#1",
        }).status_code == 200
    assert client.put("/audio/bad-mime/blob", content=payload,
                      headers={"content-type": "application/octet-stream"}).status_code == 415
    assert client.put("/audio/bad-magic/blob", content=b"not-webm",
                      headers={"content-type": "audio/webm"}).status_code == 415
    assert client.put("/audio/too-large/blob", content=payload, headers={
        "content-type": "audio/webm", "content-length": str(64 * 1024 * 1024 + 1),
    }).status_code == 413
    original = b"\x1a\x45\xdf\xa3original"
    assert client.put("/audio/checksum-gap/blob", content=original,
                      headers={"content-type": "audio/webm"}).status_code == 200
    audio_store.find_blob("checksum-gap").unlink()
    assert client.put("/audio/checksum-gap/blob",
                      content=b"\x1a\x45\xdf\xa3replacement",
                      headers={"content-type": "audio/webm"}).status_code == 409
    assert audio_store.find_blob("checksum-gap") is None


def test_audio_reads_recheck_identity_classification_and_withdrawal(client):
    _mk_session(client, sid="S-READ", pid="P-READ")
    assert client.post("/audio", json={
        "raw_audio_id": "read-audio", "session_id": "S-READ",
        "turn_key": "SE_锚#1",
    }).status_code == 200
    payload = b"\x1a\x45\xdf\xa3read-audio"
    assert client.put("/audio/read-audio/blob", content=payload,
                      headers={"content-type": "audio/webm"}).status_code == 200
    _login_admin(client)
    assert client.get("/audio/read-audio").status_code == 200
    blob = client.get("/audio/read-audio/blob")
    assert blob.status_code == 200 and blob.content == payload
    assert blob.headers["cache-control"] == "private, no-store"
    assert client.get("/audio/read-audio/blob", headers={"range": "bytes=0-3"}).status_code == 416

    with Session(client.test_engine) as db_session:
        patient = db_session.get(models.Patient, "P-READ")
        patient.withdrawal_status = "withdrawn"
        db_session.add(patient)
        db_session.commit()
    assert client.get("/audio/read-audio").status_code == 409
    assert client.get("/audio/read-audio/blob").status_code == 409

    with Session(client.test_engine) as db_session:
        patient = db_session.get(models.Patient, "P-READ")
        patient.withdrawal_status = None
        patient.recording_allowed = False
        session = db_session.get(models.Session, "S-READ")
        session.data_classification = "legacy_unknown"
        db_session.add(patient)
        db_session.add(session)
        db_session.commit()
    assert client.get("/audio/read-audio").status_code == 409
    assert client.get("/audio/read-audio/blob").status_code == 409


def test_export_requires_completed_secondary_use_and_privileged_account(client):
    _mk_session(client, sid="S-EXPORT-GATE", pid="P-EXPORT-GATE")
    _login_role(client, "researcher", "plain-researcher")
    assert client.post("/sessions/S-EXPORT-GATE/export", json=_export_body("researcher-gate")).status_code == 403
    client.post("/auth/logout")
    _login_admin(client)
    assert client.post("/sessions/S-EXPORT-GATE/export", json=_export_body("incomplete-gate")).status_code == 409
    _mark_completed(client, "S-EXPORT-GATE")
    assert client.post("/sessions/S-EXPORT-GATE/export?deidentify=false", json=_export_body("identified-gate")).status_code == 403
    with Session(client.test_engine) as db_session:
        patient = db_session.get(models.Patient, "P-EXPORT-GATE")
        patient.secondary_use_allowed = False
        db_session.add(patient)
        db_session.commit()
    assert client.post("/sessions/S-EXPORT-GATE/export", json=_export_body("secondary-gate")).status_code == 409


def _authoritative_turn(client, session_id: str, item_event_id: int, item_id: str,
                        *, turn_seq: int = 1, response_role: str = "命名",
                        asr_text: str = "锚", prompt_level: int = 0,
                        answer_type: str = "正确", score: float | None = 1.0):
    raw_audio_id = f"aud-{session_id}-{item_event_id}-{turn_seq}"
    turn_key = f"{item_id}#{turn_seq}"
    assert client.post("/audio", json={
        "raw_audio_id": raw_audio_id, "session_id": session_id,
        "turn_key": turn_key,
    }).status_code == 200
    assert client.put(f"/audio/{raw_audio_id}/blob",
                      content=b"\x1a\x45\xdf\xa3test-audio",
                      headers={"content-type": "audio/webm"}).status_code == 200
    with Session(client.test_engine) as db_session:
        attempt = models.AttemptEvent(
            session_id=session_id, item_id=item_id, turn_seq=turn_seq,
            response_role=response_role, attempt_seq=1, raw_audio_id=raw_audio_id,
            prompt_level=prompt_level, asr_text=asr_text, asr_confidence=.9,
            asr_engine_version="test-asr", operational_answer_type=answer_type,
            operational_score=score, operational_needs_review=False,
            judge_mode="规则确定式", judge_engine_version="rule-test",
            processing_status="completed", is_simulation=True,
        )
        db_session.add(attempt)
        db_session.commit()
    return client.post(f"/items/{item_event_id}/turns", json={
        "turn_seq": turn_seq, "response_role": response_role,
        "raw_audio_id": raw_audio_id,
    })


def test_session_plan_expands_turns(client):
    _mk_session(client)
    d = client.get("/sessions/SM0/plan", params={"week_no": 2, "event_line": "正式训练"}).json()
    assert d["total_items"] == 32 and d["total_turns"] == 20 + 10 * 5 + 2 * 4


def test_session_plan_uses_persisted_context_and_fails_closed(client):
    _mk_session(client, sid="SP2", pid="PP2")
    assert client.get("/sessions/SP2/plan").status_code == 200
    assert client.get("/sessions/SP2/plan", params={"week_no": 3}).status_code == 409
    assert client.get("/sessions/SP2/plan", params={"event_line": "基线测评窗"}).status_code == 409

    blocked = client.post("/sessions", json={"session_id": "SP3", "patient_id": "PP2", "week_no": 3,
                                             "phase_type": "正式训练", "event_line": "正式训练",
                                             "item_bank_version_id": "wk2-v1-20260707"})
    assert blocked.status_code == 409

    client.post("/sessions", json={"session_id": "SP1", "patient_id": "PP2", "week_no": 1,
                                   "phase_type": "关系建立", "event_line": "关系建立环节",
                                   "item_bank_version_id": "wk2-v1-20260707",
                                   "is_simulation": True})
    p1 = client.get("/sessions/SP1/plan").json()
    assert p1["total_items"] == 0 and p1["total_turns"] == 0


def test_m0_end_to_end_single_item(client):
    """建档→建场次→建题→录环节→改写→AI初评→人工锁分→重建评分→去标识导出。"""
    _mk_session(client)
    ie = client.post("/sessions/SM0/items",
                     json={"item_id": "SE_锚", "task_type": "单要素"}).json()
    te = _authoritative_turn(
        client, "SM0", ie["id"], "SE_锚", asr_text="毛",
        answer_type="其他错误", score=0.0).json()
    # 已由 attempt 权威收口，旧 ai-judge 重试不得再次调用模型。
    j = client.post(f"/turns/{te['id']}/ai-judge").json()
    assert j["ai_answer_type"] == "其他错误" and j["source_attempt_id"] is not None
    _open_review_window(client, "SM0")
    _login_confirmation_reviewer(client)
    # 人工改写为 confirmed（不覆盖 asr_text 原文）
    c = client.patch(f"/turns/{te['id']}/confirm", json={
        "confirmed_response_text": "锚", "expected_revision": 0,
        "idempotency_key": "test-api-confirm-m0-0001",
    }).json()
    assert c["asr_text"] == "毛" and c["confirmed_response_text"] == "锚"
    # 人工锁分
    lk = client.patch(f"/turns/{te['id']}/lock",
                      json={"reviewer_id": "R1", "element_value": 1, "prompt_level": 0}).json()
    assert lk["score_locked"] is True
    assert client.patch(f"/turns/{te['id']}/lock",
                        json={"reviewer_id": "R1", "element_value": 0}).status_code == 409  # 不可重复锁
    # 锁后不得改 confirmed
    assert client.patch(f"/turns/{te['id']}/confirm",
                        json={"confirmed_response_text": "别的", "expected_revision": 1,
                              "idempotency_key": "test-api-confirm-m0-0002"}).status_code == 409
    # 重建评分
    sc = client.get("/sessions/SM0/scores").json()
    assert sc["single"]["naming_accuracy"] == 1.0
    # 去标识导出：不带直接标识
    _mark_completed(client, "SM0")
    _login_admin(client)
    ex = client.post("/sessions/SM0/export", json=_export_body("sm0")).json()
    assert ex["deidentified"] is True and ex["sheet_counts"]["turns"] == 1


def test_confirmed_response_is_trimmed_and_cannot_be_blank(client):
    _mk_session(client, sid="S-CONFIRM", pid="P-CONFIRM")
    item = client.post("/sessions/S-CONFIRM/items", json={
        "item_id": "SE_锚", "task_type": "单要素",
    }).json()
    turn = _authoritative_turn(
        client, "S-CONFIRM", item["id"], "SE_锚").json()
    assert client.patch(f"/turns/{turn['id']}/confirm", json={
        "confirmed_response_text": " \n\t ",
        "expected_revision": 0,
        "idempotency_key": "test-api-confirm-blank-0001",
    }).status_code == 422
    _open_review_window(client, "S-CONFIRM")
    _login_confirmation_reviewer(client)
    confirmed = client.patch(f"/turns/{turn['id']}/confirm", json={
        "confirmed_response_text": "  锚  ",
        "expected_revision": 0,
        "idempotency_key": "test-api-confirm-trim-0001",
    })
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmed_response_text"] == "锚"


def test_lock_requires_confirmed_formal_valid_context(client):
    _mk_session(client, sid="SLK", pid="PLK")
    ie = client.post("/sessions/SLK/items",
                     json={"item_id": "SE_锚", "task_type": "单要素"}).json()
    te = _authoritative_turn(client, "SLK", ie["id"], "SE_锚").json()
    _open_review_window(client, "SLK")
    _login_confirmation_reviewer(client)
    endpoint = f"/turns/{te['id']}/lock"
    assert client.patch(endpoint, json={"reviewer_id": "R1", "element_value": 1}).status_code == 409
    client.patch(f"/turns/{te['id']}/confirm", json={
        "confirmed_response_text": "锚", "expected_revision": 0,
        "idempotency_key": "test-api-confirm-lock-0001",
    })
    assert client.patch(endpoint, json={"reviewer_id": "R1", "element_value": 2}).status_code == 422
    assert client.patch(endpoint, json={"reviewer_id": "R1", "element_value": 1,
                                        "prompt_level": 4}).status_code == 422
    prompt_conflict = client.patch(
        endpoint, json={"element_value": 1, "prompt_level": 1})
    assert prompt_conflict.status_code == 409
    assert prompt_conflict.json()["detail"]["code"] == \
        "research_review_prompt_evidence_mismatch"
    score_conflict = client.patch(
        endpoint, json={"element_value": 1, "reviewed_score": 0})
    assert score_conflict.status_code == 409
    assert score_conflict.json()["detail"]["code"] == \
        "research_review_score_contract_mismatch"
    authenticated_lock = client.patch(
        endpoint, json={"element_value": 1})
    assert authenticated_lock.status_code == 200
    assert authenticated_lock.json()["reviewer_id"] == "CONFIRMATION-REVIEWER"

    bad_role = client.post(f"/items/{ie['id']}/turns",
                           json={"turn_seq": 2, "response_role": "命名", "prompt_level": 0})
    assert bad_role.status_code == 409

    client.post("/sessions", json={"session_id": "SLK1", "patient_id": "PLK", "week_no": 1,
                                   "phase_type": "关系建立", "event_line": "关系建立环节",
                                   "item_bank_version_id": "wk2-v1-20260707",
                                   "is_simulation": True,
                                   "trainer_id": "CONFIRMATION-REVIEWER"})
    w1_item = client.post("/sessions/SLK1/items",
                          json={"item_id": "SE_锚", "task_type": "单要素"}).json()
    w1_turn = client.post(f"/items/{w1_item['id']}/turns",
                          json={"turn_seq": 1, "response_role": "命名", "prompt_level": 0})
    assert w1_turn.status_code == 409


def test_loopback_m0_lock_identity_is_server_owned(client):
    _mk_session(client, sid="S-M0-LOCK", pid="P-M0-LOCK")
    item = client.post("/sessions/S-M0-LOCK/items", json={
        "item_id": "SE_锚", "task_type": "单要素",
    }).json()
    turn = _authoritative_turn(
        client, "S-M0-LOCK", item["id"], "SE_锚").json()
    _open_review_window(client, "S-M0-LOCK")
    # 正式确认接口要求具名账号；本测试只隔离验证 lock 自身的 M0 身份来源。
    with Session(client.test_engine) as db_session:
        stored = db_session.get(models.TurnEvent, turn["id"])
        assert stored is not None
        stored.confirmed_response_text = "锚"
        db_session.add(stored)
        db_session.commit()

    locked = client.patch(f"/turns/{turn['id']}/lock", json={
        "reviewer_id": "FORGED-LOCAL-REVIEWER",
        "element_value": 1,
    })

    assert locked.status_code == 200, locked.text
    assert locked.json()["reviewer_id"] == "LOCAL-M0"
    assert locked.json()["reviewed_score"] == locked.json()["element_value"] == 1


def test_read_only_session_recovery_endpoints(client):
    _mk_session(client, sid="SREC", pid="PREC")
    ie = client.post("/sessions/SREC/items",
                     json={"item_id": "SE_锚", "task_type": "单要素"}).json()
    _authoritative_turn(client, "SREC", ie["id"], "SE_锚")
    client.post("/audio", json={"raw_audio_id": "arec", "session_id": "SREC"})
    client.post("/sessions/SREC/abnormal", json={"abnormal_type": "环境噪声"})

    sessions = client.get("/patients/PREC/sessions").json()
    assert [row["session_id"] for row in sessions] == ["SREC"]
    journal = client.get("/sessions/SREC/journal").json()
    assert journal["session"]["session_id"] == "SREC"
    assert len(journal["items"]) == len(journal["turns"]) == 1
    assert journal["audios"][0]["raw_audio_id"] == "arec"
    assert journal["abnormal"][0]["abnormal_type"] == "环境噪声"


def test_ai_judge_no_deterministic_target_is_human_only(client):
    _mk_session(client)
    ie = client.post("/sessions/SM0/items",
                     json={"item_id": "DE_斧子+树", "task_type": "双要素"}).json()
    te = _authoritative_turn(
        client, "SM0", ie["id"], "DE_斧子+树", turn_seq=5,
        response_role="关系识别", asr_text="用斧子砍树",
        answer_type="有回答", score=None).json()
    j = client.post(f"/turns/{te['id']}/ai-judge").json()
    assert j["ai_answer_type"] == "有回答" and j["source_attempt_id"] is not None


def test_legacy_scale_list_and_migration_export_integration(client):
    client.post("/patients", json={"patient_id": "PS1", **ELIGIBLE_PATIENT,
                                    "is_simulation_subject": True,
                                    "secondary_use_allowed": True})
    r = client.post("/patients/PS1/scales",
                    json={"phase_type": "前测", "scale_name": "CETI", "score": 42.0, "assessor_id": "A1"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "scale_protocol_not_frozen"
    # 历史自由行只由迁移/夹具直接种入；产品 API 永远不能再创建。
    with Session(client.test_engine) as db_session:
        db_session.add(models.ScaleResult(
            patient_id="PS1", phase_type="前测", scale_name="CETI",
            score=42.0, assessor_id="LEGACY-A1"))
        db_session.commit()
    lst = client.get("/patients/PS1/scales").json()
    assert len(lst) == 1 and lst[0]["score"] == 42.0
    # 未建档患者 → 404
    assert client.post("/patients/P404x/scales",
                       json={"phase_type": "前测", "scale_name": "CETI"}).status_code == 404
    # 旧自由名称/分数不得冒充正式结局；只进迁移用途的隔离未验证表。
    client.post("/sessions", json={"session_id": "SPS1", "patient_id": "PS1", "week_no": 2,
                                   "phase_type": "正式训练", "event_line": "正式训练",
                                   "item_bank_version_id": "wk2-v1-20260707",
                                   "is_simulation": True})
    _mark_completed(client, "SPS1")
    _login_admin(client)
    ex = client.post("/sessions/SPS1/export", json=_export_body("sps1")).json()
    assert ex["sheet_counts"]["scales"] == 0
    assert ex["sheet_counts"]["legacy_unverified_scales"] == 1


def test_live_state_roundtrip_and_session_reset(client):
    assert client.get("/live/state").json()["seq"] == 0            # 初始为空
    _mk_session(client, sid="S1", pid="P-LIVE")
    assert client.post("/sessions", json={
        "session_id": "S2", "patient_id": "P-LIVE", "week_no": 2,
        "phase_type": "正式训练", "event_line": "正式训练",
        "item_bank_version_id": "wk2-v1-20260707",
        "is_simulation": True,
    }).status_code == 200
    hs1 = client.put("/live/state", json={"kind": "session",
                          "payload": {"sessionId": "S1", "weekNo": 2, "mode": "task", "wseq": 1}})
    cur = client.put("/live/state", json={"kind": "cursor",
                          "payload": {"sessionId": "S1", "screen": "record", "itemIdx": 3,
                                      "turnIdx": 0, "responseRole": "命名", "cueLevel": 0,
                                      "recording": "armed", "wseq": 0}})
    assert client.post("/audio", json={"raw_audio_id": "a9", "session_id": "S1",
                                       "turn_key": "SE_树#1"}).status_code == 200
    upload = client.put("/audio/a9/blob", content=b"\x1a\x45\xdf\xa3live-audio",
                        headers={"content-type": "audio/webm"})
    assert upload.status_code == 200
    client.put("/live/state", json={"kind": "audioSaved",
                                    "payload": {"rawAudioId": "a9", "durationSeconds": 1.2,
                                                "byteCount": upload.json()["bytes"],
                                                "checksum": upload.json()["checksum"],
                                                "containsDirectIdentifier": False,
                                                "turnKey": "SE_树#1",
                                                "sessionId": "S1"}})
    client.put("/live/state", json={"kind": "patientRec",
                                    "payload": {"active": True, "turnKey": "SE_树#1", "sessionId": "S1"}})
    d = client.get("/live/state").json()
    assert d["seq"] == 4 and d["cursor"]["itemIdx"] == 3
    assert d["session"]["wseq"] == hs1.json()["wseq"] < cur.json()["wseq"] == d["cursor"]["wseq"]
    assert "audioSaved" not in d and "patientRec" not in d          # 患者最小读口不泄露研究回报
    console = client.get("/live/console-state").json()
    assert console["audioSaved"]["rawAudioId"] == "a9"
    assert console["patientRec"]["active"] is True
    # 新场次握手 → 旧游标/录音回报/麦克风上报清空(防老人端串场)
    client.put("/live/state", json={"kind": "session",
                                    "payload": {"sessionId": "S2", "weekNo": 2, "mode": "task"}})
    d2 = client.get("/live/state").json()
    assert d2["session"]["sessionId"] == "S2" and d2["cursor"] is None
    console2 = client.get("/live/console-state").json()
    assert console2["audioSaved"] is None and console2["patientRec"] is None
    assert d2["seq"] == 5                                           # seq 只增不回卷
    assert client.put("/live/state", json={"kind": "bogus", "payload": {}}).status_code == 422


def test_tts_speak_degraded_and_validation(client, monkeypatch):
    import app.tts as tts_mod
    monkeypatch.setenv("TTS_ENGINE", "null")
    monkeypatch.setattr(tts_mod, "_engine", None)          # 复位懒加载单例,强制吃到 env
    assert client.post("/tts/speak", json={"text": ""}).status_code == 422
    assert client.post("/tts/speak", json={"text": "x" * 501}).status_code == 422
    assert client.get("/tts/speak", params={"text": "你好"}).status_code == 405
    r = client.post("/tts/speak", json={"text": "你好"})
    assert r.status_code == 204 and r.headers["x-tts-engine"] == "null-0"  # 降级→前端回退系统语音
    monkeypatch.setattr(tts_mod, "_engine", None)


def test_tts_piper_synthesis_and_cache(client, tmp_path, monkeypatch):
    import app.tts as tts_mod
    if not tts_mod.PiperTtsEngine(tts_mod.DEFAULT_VOICE).available():
        pytest.skip("本机没有可用的 piper(缺模型或缺包;部署机可选)")
    monkeypatch.delenv("TTS_ENGINE", raising=False)
    monkeypatch.setattr(tts_mod, "_engine", None)
    monkeypatch.setattr(tts_mod, "CACHE_DIR", tmp_path / "tts-cache")
    r = client.post("/tts/speak", json={"text": "这是测试"})
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    assert r.headers["x-tts-cache"] == "miss"
    r2 = client.post("/tts/speak", json={"text": "这是测试"})
    assert r2.headers["x-tts-cache"] == "hit" and r2.content == r.content  # 同句只合成一次
    monkeypatch.setattr(tts_mod, "_engine", None)


def test_piper_model_without_piper_package_reports_null_not_piper(client, tmp_path, monkeypatch):
    """有模型文件、没装 piper 包:引擎名要报 null,不能报 piper。

    降级链把"最后尝试过的引擎"写进 X-Tts-Engine 和 TtsServeEvidence。
    available() 若只看模型文件,一台没装包的机器会给每次 204 都盖上
    "piper/…"的章——一个从未运行、也不可能运行的引擎。
    """
    import importlib.util

    import app.tts as tts_mod

    voice = tmp_path / "zh_CN-huayan-medium.onnx"
    voice.write_bytes(b"not a real onnx model")     # 只要求存在,不会被加载
    monkeypatch.setenv("TTS_VOICE_PATH", str(voice))
    monkeypatch.delenv("TTS_ENGINE", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(tts_mod, "_engine", None)
    monkeypatch.setattr(tts_mod, "_fallback_piper", None)
    monkeypatch.setattr(tts_mod, "CACHE_DIR", tmp_path / "tts-cache")

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **kw:
                        None if name == "piper" else real_find_spec(name, *a, **kw))

    assert not tts_mod.PiperTtsEngine(voice).available()
    assert isinstance(tts_mod.get_engine(), tts_mod.NullTtsEngine)
    r = client.post("/tts/speak", json={"text": "这是测试"})
    assert r.status_code == 204
    assert r.headers["x-tts-engine"] == tts_mod.NullTtsEngine.version


def test_phase_aware_cue_intervention_flagged(client):
    _mk_session(client)
    ev = client.post("/sessions/SM0/abnormal",
                     json={"intervention_type": "代说物品名"}).json()
    # 正式训练周代说物品名 → 线索性介入 + 影响判分有效性
    assert ev["abnormal_type"] == "线索性介入" and ev["affects_scoring_validity"] is True
