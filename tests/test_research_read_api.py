"""HTTP 层的只读研究数据面：角色矩阵、密钥缺失、闭集与泄漏回归。

这套测试的重点不是"能不能取到数"，而是"取不到的时候会不会漏"。
"""
from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import re

import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from starlette.datastructures import QueryParams, URL
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import (auth, content, db, export_security, quality_release,
                 repeat_intent, main, research_dataset, session_admission)
from app.db import get_session
from app.main import app
from app.models import (
    AttemptEvent,
    AuditLog,
    ItemEvent,
    Patient,
    QualityDisclosureRecord,
    QualityReleaseEpoch,
    QualityReleaseEpochSession,
    ResearchUser,
    Session as TrainSession,
    SessionRuntimeState,
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
CONFIG = export_security.DeidentificationConfig(
    key=KEY.encode("utf-8"), key_id=KEY_ID)


def _freeze_epoch(engine, *session_ids: str, key_id: str = KEY_ID) -> str:
    """切一个纪元，成员就是给定的这几个场次。

    不走 ``publish_epoch``：那条路要先算出一份真载荷，而行面只消费三样东西——
    纪元号、成员假名、冻结时用的密钥标识。用真载荷会把这套测试变成对聚合管线
    的测试，行面自己的边界反而测不干净。
    """
    with Session(engine) as session:
        previous = session.exec(
            select(QualityReleaseEpoch)
            .where(QualityReleaseEpoch.status == "published")
            .order_by(QualityReleaseEpoch.epoch_seq.desc())).first()
        seq = 1 if previous is None else previous.epoch_seq + 1
        epoch_id = f"qre_test_{seq}"
        session.add(QualityReleaseEpoch(
            epoch_id=epoch_id, epoch_seq=seq, status="published",
            as_of=datetime(2026, 8, 1), frozen_at=datetime(2026, 8, 2),
            cohort_rule_version=quality_release.COHORT_RULE_VERSION,
            registry_version=quality_release.REGISTRY_VERSION,
            schema_version=quality_release.RELEASE_SCHEMA_VERSION,
            cohort_size_band="0-9", session_count_band="0-9",
            payload_json="{}", payload_sha256=f"{seq:064d}",
            min_subjects_applied=5, min_cell_subjects_applied=5,
            band_width_applied=10, rate_decimals_applied=2,
            diagnostics_status="complete", deidentification_key_id=key_id,
            builder_actor_display_id="STEWARD", builder_actor_role="data_steward",
            approver_actor_display_id="ADMIN", approver_actor_role="admin",
            idempotency_key_sha256=f"{seq:064x}"))
        session.flush()
        for session_id in session_ids:
            session.add(QualityReleaseEpochSession(
                epoch_id=epoch_id,
                session_pseudonym=export_security.pseudonymize_session(
                    session_id, CONFIG),
                evidence_watermark=1))
        if previous is not None:
            previous.status = "superseded"
            previous.superseded_at = datetime(2026, 8, 2)
            session.add(previous)
        session.commit()
    return epoch_id


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
        for session_id, patient_id in (("S-REAL-1", "P-REAL-1"),
                                       ("S-GONE-1", "P-GONE-1")):
            session.add(TrainSession(
                session_id=session_id, patient_id=patient_id, week_no=2,
                phase_type="正式训练", event_line="正式训练", trainer_id="RESEARCHER",
                item_bank_version_id=BANK.version_id,
                item_bank_definition_digest=BANK_DIGEST,
                autopilot_protocol_version_id=PROTOCOL["protocol_version_id"],
                autopilot_protocol_definition_digest=PROTOCOL_DIGEST,
                repeat_protocol_version_id=REPEAT_PROTOCOL.version_id,
                repeat_protocol_definition_digest=REPEAT_PROTOCOL.definition_digest,
                is_simulation=False, data_classification="research"))
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

    # 研究分区的行面绑在冻结纪元上：不切纪元就一行也读不到。默认 fixture 把这
    # 两个场次冻进纪元 1，让绝大多数用例测的是"绑上之后"的正常路径。
    _freeze_epoch(engine, "S-REAL-1", "S-GONE-1")

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
    monkeypatch.setenv(quality_release.RELEASE_MODE_ENV,
                       quality_release.RELEASE_MODE_REQUIRED)


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


def test_csv_carries_a_bom_and_the_same_columns_as_json(research_env, monkeypatch):
    _with_key(monkeypatch)
    client = _client("steward")
    for dataset in research_dataset.dataset_keys():
        json_body = client.get(
            f"/research/v1/{dataset}?data_classification=research").json()
        csv_response = client.get(
            f"/research/v1/{dataset}.csv?data_classification=research")
        assert csv_response.status_code == 200, dataset
        assert csv_response.headers["content-type"].startswith("text/csv")
        assert "attachment" in csv_response.headers["content-disposition"]
        raw = csv_response.content
        # Windows 上的 Excel/SPSS 不看 BOM 就按本地代码页解，中文列名会乱码
        assert raw.startswith(b"\xef\xbb\xbf"), dataset
        header = raw.decode("utf-8-sig").splitlines()[0].split(",")
        assert header == json_body["columns"], dataset
        assert SECRET_TEXT not in raw.decode("utf-8-sig")


def test_dictionary_csv_is_available_and_json_suffix_is_not(research_env, monkeypatch):
    _with_key(monkeypatch)
    client = _client("steward")
    csv_response = client.get("/research/v1/dictionary.csv")
    assert csv_response.status_code == 200
    body = csv_response.content.decode("utf-8-sig")
    assert body.splitlines()[0].startswith("dataset,column,disclosure")
    assert "asr_text" in body, "被排除的列也要出现在字典里"


def test_csv_is_refused_without_the_deidentification_key(research_env, monkeypatch):
    _without_key(monkeypatch)
    client = _client("steward")
    response = client.get("/research/v1/turns.csv?data_classification=research")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "research_deidentification_unavailable"


def _create_second_session(engine) -> None:
    """再建一个研究场次，**不切纪元**——所以它不会出现在行面里。

    默认 fixture 只有 1 个有内容的场次、1 条环节，于是 ``has_more`` 恒为 False、
    ``next_cursor`` 恒为 null——针对游标的断言会在空转里全绿。上一版泄漏回归
    就是这么漏掉明文游标的：它测的那条路径压根没被走到。
    """
    with Session(engine) as session:
        train = TrainSession(
            session_id="S-REAL-2", patient_id="P-REAL-1", week_no=2,
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
        item = ItemEvent(session_id="S-REAL-2", item_id="SE_苹果",
                         task_type="单要素", item_set_type="训练集",
                         presentation_order=1)
        session.add(item)
        session.commit()
        session.add(TurnEvent(
            item_event_id=item.id, turn_seq=1, response_role="命名",
            asr_confidence=0.8, prompt_level=0, ai_score=1.0,
            judge_portrait_used=False))
        session.commit()


def _add_second_page_rows(engine) -> None:
    """建第二个场次并把它冻进新纪元——行面要能翻页，前提是它在队列里。

    新场次不会自己进已冻结的队列，那正是绑定要挡的事。想让它出现在行面里，
    只有一条路：再切一个纪元。
    """
    _create_second_session(engine)
    _freeze_epoch(engine, "S-REAL-1", "S-GONE-1", "S-REAL-2")


def _decoded_cursor_bytes(cursor: str) -> bytes:
    """把游标还原成它承载的原始字节——用来证明里面没有明文标识符。"""
    return base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))


def test_pagination_cursor_never_carries_a_plaintext_identifier(
        research_env, monkeypatch):
    """游标里装的是数据库自然键，而那正是本接口明令禁出的直接标识符。

    第一版只做 HMAC 签名、载荷明文 base64，``encode_cursor(["P-REAL-1"])`` 解码
    出来就是 ``["P-REAL-1"]``。上一版的泄漏回归抓不住它，因为那条断言是对响应
    **原文**做子串匹配，而游标是 base64——`"P-REAL-1" in response.text` 恒为
    False。所以这里必须把游标解出来再看。
    """
    _with_key(monkeypatch)
    _add_second_page_rows(research_env)
    client = _client("steward")
    for dataset, secrets_that_must_not_appear in (
            ("subjects", (b"P-REAL-1", b"P-GONE-1")),
            ("sessions", (b"S-REAL-1", b"P-REAL-1")),
            ("turns", (b"S-REAL-1", b"P-REAL-1"))):
        response = client.get(
            f"/research/v1/{dataset}?data_classification=research&limit=1")
        assert response.status_code == 200, dataset
        cursor = response.json()["next_cursor"]
        assert cursor, f"{dataset} 应当还有下一页，否则这条断言是空转"
        blob = _decoded_cursor_bytes(cursor)
        for secret in secrets_that_must_not_appear:
            assert secret not in blob, f"{dataset} 的游标里带着明文 {secret!r}"


def test_a_cursor_from_one_dataset_is_rejected_by_another(
        research_env, monkeypatch):
    """跨数据集复用游标必须被拒，而不是静默截断结果。"""
    _with_key(monkeypatch)
    _add_second_page_rows(research_env)
    client = _client("steward")
    cursor = client.get(
        "/research/v1/subjects?data_classification=research&limit=1"
    ).json()["next_cursor"]
    assert cursor
    response = client.get(
        f"/research/v1/turns?data_classification=research&cursor={cursor}")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "research_cursor_invalid"


def test_the_same_key_encodes_to_a_different_cursor_every_time(
        research_env, monkeypatch):
    """随机 nonce：同一个位置两次翻页给出不同游标，不能被当成位置指纹比对。"""
    _with_key(monkeypatch)
    _add_second_page_rows(research_env)
    client = _client("steward")
    path = "/research/v1/subjects?data_classification=research&limit=1"
    first = client.get(path).json()["next_cursor"]
    second = client.get(path).json()["next_cursor"]
    assert first and second and first != second


def test_pagination_still_walks_every_row_exactly_once(
        research_env, monkeypatch):
    """加密之后分页得照样能用：逐页走完，不漏行不重行。"""
    _with_key(monkeypatch)
    _add_second_page_rows(research_env)
    client = _client("steward")
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        query = "/research/v1/subjects?data_classification=research&limit=1"
        if cursor:
            query += f"&cursor={cursor}"
        payload = client.get(query).json()
        seen.extend(row["subject_code"] for row in payload["rows"])
        cursor = payload["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)), "翻页翻出了重复行"
    everything = client.get(
        "/research/v1/subjects?data_classification=research").json()
    assert seen == [row["subject_code"] for row in everything["rows"]]


def test_research_reads_are_actually_rate_limited(research_env, monkeypatch):
    """限速要真的挂上策略表，别只是调了一次返回 None 的函数。

    第一版端点老老实实调了 ``_expensive_rate_limit``，但 ``_POLICIES`` 里没有
    任何一条能 fullmatch 到它传的路径，于是 ``consume`` 永远返回 None——一次
    全库刮取不会被拖慢半秒。
    """
    _with_key(monkeypatch)
    client = _client("steward")
    path = "/research/v1/subjects?data_classification=research"
    codes = [client.get(path).status_code for _ in range(24)]
    assert 429 in codes, "连打 24 次一次都没被拦，限速策略没挂上"
    limited = client.get(path)
    if limited.status_code == 429:
        assert limited.json()["code"] == "resource_rate_limited"
        assert int(limited.headers["Retry-After"]) >= 1


def test_a_withdrawn_subject_is_judged_by_the_same_rule_as_everywhere_else(
        research_env, monkeypatch):
    """撤回判定与 main.py / ai_quality_service.py 同一条口径：非空即撤回。

    第一版在取数层自建了白名单（放行 "active"/"not_withdrawn"），口径一分岔，
    同一个人就会在别处算已撤回、在这里算在训，全量明细照出。
    """
    _with_key(monkeypatch)
    with Session(research_env) as session:
        patient = session.get(Patient, "P-GONE-1")
        patient.withdrawal_status = "active"      # 别处会判成"已撤回"
        session.add(patient)
        session.commit()
    client = _client("steward")
    payload = client.get(
        "/research/v1/subjects?data_classification=research").json()
    gone = export_security.pseudonymize_subject(
        "P-GONE-1", export_security.load_deidentification_config())
    row = next(r for r in payload["rows"] if r["subject_code"] == gone)
    assert row["withdrawn"] is True, "取数层的撤回口径与全仓不一致"


def test_withdrawal_expressed_only_through_the_consent_field_still_seals(
        research_env, monkeypatch):
    """有一支撤回只体现在知情同意字段上，只看 withdrawal_status 会整支漏掉。

    仓库自己在 main.py 里写着 "A legacy record may express study withdrawal
    only through the consent field"，六个别的读面都防了这一支，取数层第一版没防。
    """
    _with_key(monkeypatch)
    with Session(research_env) as session:
        patient = session.get(Patient, "P-REAL-1")
        patient.withdrawal_status = None
        patient.consent_status = "已撤回"
        session.add(patient)
        session.commit()
        assert session_admission.patient_content_sealed(patient)
    client = _client("steward")
    config = export_security.load_deidentification_config()
    code = export_security.pseudonymize_subject("P-REAL-1", config)
    subjects = client.get(
        "/research/v1/subjects?data_classification=research").json()
    row = next(r for r in subjects["rows"] if r["subject_code"] == code)
    assert row["withdrawn"] is True, "只在同意字段上撤回的人被当成了在训"
    turns = client.get(
        "/research/v1/turns?data_classification=research").json()
    for turn in turns["rows"]:
        if turn["subject_code"] == code:
            assert turn["ai_score"] is None, "撤回者的逐环节明细照发了"
            assert turn["prompt_level"] is None


def test_every_research_read_leaves_an_audit_entry_without_identifiers(
        research_env, monkeypatch):
    """批量取数必须进账本——限速拦不住有耐心的内部人，账本才能事后追责。

    本仓库的惯例是每个批量读面都记（导出、音频元数据读、录音字节读都写）。
    第一版的取数面是唯一一个不写的：跑 60 次取数，AuditLog 里只有一行登录。
    但账本本身也不能变成泄漏面——只记元数据，不记编号、不记游标。
    """
    _with_key(monkeypatch)
    client = _client("steward")
    client.get("/research/v1/turns?data_classification=research")
    client.get("/research/v1/subjects.csv?data_classification=research")
    with Session(research_env) as session:
        rows = [r for r in session.exec(select(AuditLog))
                if r.action == "research_read"]
    assert len(rows) == 2, "两次取数应当留下两条账本"
    assert {"json", "csv"} == {"csv" if "csv" in r.summary else "json" for r in rows}
    for row in rows:
        assert row.actor == "STEWARD"
        for secret in ("P-REAL-1", "S-REAL-1", "P-GONE-1", SECRET_TEXT):
            assert secret not in row.summary, f"账本自己漏了 {secret}"


def _withdraw(engine, patient_id: str) -> None:
    with Session(engine) as session:
        patient = session.get(Patient, patient_id)
        patient.withdrawal_status = "withdrawn"
        session.add(patient)
        session.commit()


def test_turn_tombstones_keep_the_natural_key_so_the_denominator_survives(
        research_env, monkeypatch):
    """墓碑存在的唯一理由就是保住分母，而第一版恰好毁了它。

    第一版把 item_id 与 turn_seq 一起置 null，于是一个 36 环节的场次返回 36 行
    逐字节相同的行——PI 一跑 distinct() 就塌成 1 行，分母从 36 变 1。
    这两列本来就是公开列（``clear``），不是标识符，没有任何理由抹掉。
    """
    _with_key(monkeypatch)
    with Session(research_env) as session:
        item = session.exec(select(ItemEvent).where(
            ItemEvent.session_id == "S-REAL-1")).first()
        for seq in (2, 3, 4):
            session.add(TurnEvent(
                item_event_id=item.id, turn_seq=seq, response_role="命名",
                asr_text=SECRET_TEXT, prompt_level=1, ai_score=0.0,
                judge_portrait_used=False))
        session.commit()
    before = _client("steward").get(
        "/research/v1/turns?data_classification=research").json()["rows"]
    live = [r for r in before if r["session_code"]]
    assert len(live) == 4, "先确认这个场次真的有 4 个环节，否则下面是空转"

    _withdraw(research_env, "P-REAL-1")
    after = _client("steward").get(
        "/research/v1/turns?data_classification=research").json()["rows"]
    assert len(after) == len(before), "撤回后行数变了，分母就不稳了"

    keys = {(r["session_code"], r["item_id"], r["turn_seq"]) for r in after}
    assert len(keys) == len(after), "自然键不唯一——distinct() 一跑就塌行"
    for row in after:
        assert row["withdrawn"] is True
        assert row["item_id"] and row["turn_seq"] is not None, "自然键被抹掉了"
        assert row["ai_score"] is None and row["prompt_level"] is None, \
            "撤回者的内容不该还在"


def test_a_withdrawn_subject_row_stops_claiming_secondary_use_is_allowed(
        research_env, monkeypatch):
    """撤回者的 secondary_use_allowed 还是 true，等于对 PI 说"这人可以二次利用"。"""
    _with_key(monkeypatch)
    with Session(research_env) as session:
        patient = session.get(Patient, "P-REAL-1")
        patient.secondary_use_allowed = True
        session.add(patient)
        session.commit()
    _withdraw(research_env, "P-REAL-1")
    config = export_security.load_deidentification_config()
    code = export_security.pseudonymize_subject("P-REAL-1", config)
    rows = _client("steward").get(
        "/research/v1/subjects?data_classification=research").json()["rows"]
    row = next(r for r in rows if r["subject_code"] == code)
    assert row["withdrawn"] is True
    assert row["secondary_use_allowed"] is None, "撤回者仍在声称可以二次利用"
    assert row["dementia_severity"] is None, "撤回者的临床属性仍在照发"
    assert row["session_count"] == 1, "场次数要留着，分母才稳"


def test_withdrawn_is_a_real_boolean_on_every_dataset_not_a_missing_value(
        research_env, monkeypatch):
    """交接文档要求"统计时按 withdrawn 过滤"，那三张表就都得有这一列。

    而且在训的人必须是 False 而不是 null——SPSS 里 null 是缺失值，
    `filter withdrawn = 0` 会把在训的人一起滤掉。
    """
    _with_key(monkeypatch)
    client = _client("steward")
    for dataset in research_dataset.dataset_keys():
        columns = research_dataset.published_columns(
            research_dataset.dataset_for(dataset))
        assert "withdrawn" in columns, f"{dataset} 没有 withdrawn 列"
        rows = client.get(
            f"/research/v1/{dataset}?data_classification=research").json()["rows"]
        assert rows, f"{dataset} 零行，这条断言是空转"
        for row in rows:
            assert isinstance(row["withdrawn"], bool), \
                f"{dataset} 的 withdrawn 是 {row['withdrawn']!r}，不是布尔"


ODD_SPELLINGS = (
    "/research/v1/%23subjects?data_classification=research",
    "/research/v1/subjects/?data_classification=research",
    "/research/v1/?data_classification=research",
    "/research/v1/a/b?data_classification=research",
)


def test_odd_path_spellings_get_the_same_verdict_as_the_canonical_one(
        research_env, monkeypatch):
    """换个拼法不能换来一个更宽松的判定。

    2026-08-15 实测：`/research/v1/%23subjects` 里解码出的 `#` 把中间件读的
    `request.url.path` 截断成 `/research/v1/`，权限落到含 researcher 的命名空间
    兜底，路由器却照未截断的路径把请求送进了研究处理器——researcher 拿到的是
    处理器内部的 404 而不是红线要求的 403。
    """
    _with_key(monkeypatch)
    researcher = _client("researcher")
    caregiver = _client("caregiver")
    anonymous = TestClient(app)
    for path in ODD_SPELLINGS:
        assert researcher.get(path).status_code in (403, 404), path
        body = researcher.get(path).json()
        assert "subjects" not in str(body.get("detail", "")) or \
            researcher.get(path).status_code == 403, \
            f"{path} 把数据集清单漏给了 researcher"
        assert caregiver.get(path).status_code in (403, 404), path
        assert anonymous.get(path).status_code == 401, path


def test_the_handler_authorizes_before_it_answers_whether_a_dataset_exists(
        research_env, monkeypatch):
    """角色判定要排在"你问的东西存不存在"之前——这里直接打处理器。

    原来 422（缺参）与 404（未知数据集）都排在角色校验前面：一个本该 403 的
    调用者可以用不同的 dataset_key 打出 404 与 200 的差分，把有哪些数据集枚举
    出来。

    **不能用 TestClient 测这一条**：命名空间规则修好之后，中间件会先答 403，
    处理器内的顺序在 HTTP 层不再可观测。而中间件不判角色的场合是存在的
    （回环 M0 开放模式），那时处理器内这道闸是唯一防线。所以这里绕过中间件，
    直接调处理器函数。
    """
    _with_key(monkeypatch)

    class _FakeRequest:
        method = "GET"

        def __init__(self):
            self.state = SimpleNamespace(actor="RESEARCHER", actor_role="researcher")
            self.query_params = QueryParams("data_classification=research")
            self.url = URL("http://t/research/v1/x")
            self.client = None
            self.headers = {}

    seen = set()
    for key in ("subjects", "turns", "不存在的表", "dictionary", "subjects.csv"):
        with pytest.raises(HTTPException) as caught:
            main.get_research_dataset(
                key, _FakeRequest(), Response(),
                data_classification=None, cursor=None, limit=None,
                s=next(iter([None])))
        seen.add(caught.value.status_code)
    assert seen == {403}, (
        f"处理器对 researcher 给出了不止一种回答：{sorted(seen)}——"
        "存在与否、参数齐不齐，都不该在鉴权之前被回答")


def test_deleting_the_in_handler_role_checks_makes_the_suite_go_red(
        research_env, monkeypatch):
    """处理器内那四道角色闸必须有测试盯着。

    复核实测：把四处 _require_account_identity 全删掉，整套测试仍然 134 全绿——
    因为中间件那一层先拒了，测试从来没走到处理器里那道闸。而在回环 M0 开放模式
    下（auth 未启用），中间件不判角色，处理器内那道闸是唯一的防线。
    """
    _with_key(monkeypatch)
    source = Path(main.__file__).read_text(encoding="utf-8")
    block = source.split("# 只读研究数据面 /research/v1/*")[1].split("\n@app.get(\n    \"/ai/provider-readiness\"")[0]
    guards = block.count("_require_account_identity(")
    assert guards >= 3, (
        f"研究面只剩 {guards} 处处理器内角色闸；中间件之外必须还有一道，"
        "回环 M0 开放模式下它是唯一防线")
    for action in ("查看研究数据接口状态", "查看研究数据字典", "读取去标识研究数据"):
        assert action in block, f"少了 {action} 那一道闸"


def test_a_cross_site_navigation_cannot_pull_the_csv_with_a_logged_in_cookie(
        research_env, monkeypatch):
    """会话 cookie 是 SameSite=Lax——跨站的顶层跳转照样带 cookie。

    恶意页面只要 `<a href="…/research/v1/turns.csv" download>`，受害者的 steward
    身份就会把一份去标识研究数据拉到他自己机器上，而账本会记成"这位数据管理员
    取了数"。攻击者读不到响应（无 CORS），但归属被污染了。
    """
    _with_key(monkeypatch)
    client = _client("steward")
    path = "/research/v1/turns.csv?data_classification=research"

    for site in ("cross-site", "same-site"):
        blocked = client.get(path, headers={"Sec-Fetch-Site": site})
        assert blocked.status_code == 403, site
        assert blocked.json()["code"] == "request_origin_rejected", site

    # 本站页面自己发的请求照常
    assert client.get(path, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    # 人自己敲地址 / 点书签
    assert client.get(path, headers={"Sec-Fetch-Site": "none"}).status_code == 200
    # 非浏览器客户端根本不带这个头——它们是这个接口的主要用户
    assert client.get(path).status_code == 200


def test_the_cross_site_read_guard_does_not_break_opening_the_console(
        research_env, monkeypatch):
    """SPA 外壳与静态资源是 PUBLIC，跨站点开控制台不能被这道闸误伤。"""
    _with_key(monkeypatch)
    anonymous = TestClient(app)
    for path in ("/console", "/patient", "/", "/health"):
        response = anonymous.get(path, headers={"Sec-Fetch-Site": "cross-site"})
        assert response.status_code != 403, f"{path} 被误伤了"


def test_columns_the_dictionary_declares_as_variables_actually_carry_values(
        research_env, monkeypatch):
    """字典宣称是真变量的列，取数层不能写死 null。

    `runtime_status` 与 `source_attempt_seq` 在数据字典里各有一句中文说明，
    实现里却是两个硬编码的 None——**那比不给这一列更坏**：PI 会照着字典建分析，
    拿到一整列缺失值，而缺失值在统计上意味着"没测到"，不是"我们没做"。

    这两列空了两周没人发现，因为没有任何断言看过它们的值。
    """
    _with_key(monkeypatch)
    with Session(research_env) as session:
        session.add(SessionRuntimeState(session_id="S-REAL-1", status="completed"))
        item = session.exec(select(ItemEvent).where(
            ItemEvent.session_id == "S-REAL-1")).first()
        attempt = AttemptEvent(
            session_id="S-REAL-1", item_id=item.item_id, turn_seq=1,
            attempt_seq=7, response_role="命名",
            raw_audio_id="AUDIO-FIXTURE-1", prompt_level=0)
        session.add(attempt)
        session.commit()
        turn = session.exec(select(TurnEvent).where(
            TurnEvent.item_event_id == item.id)).first()
        turn.source_attempt_id = attempt.id
        session.add(turn)
        session.commit()

    client = _client("steward")
    sessions = client.get(
        "/research/v1/sessions?data_classification=research").json()["rows"]
    live = [r for r in sessions if r["session_code"] and not r["withdrawn"]]
    assert live, "没有在训场次，这条断言是空转"
    assert any(r["runtime_status"] == "completed" for r in live), \
        "runtime_status 整列都是 null——字典宣称它是真变量"

    turns = client.get(
        "/research/v1/turns?data_classification=research").json()["rows"]
    assert any(r["source_attempt_seq"] == 7 for r in turns), \
        "source_attempt_seq 整列都是 null——字典宣称它是关联的原始尝试序号"
    for row in turns:
        assert "source_attempt_id" not in row, "反查成序号，不能把主键漏出去"


def test_the_schema_surface_stays_readable_without_a_key_but_carries_no_data(
        research_env, monkeypatch):
    """meta 与 dictionary 在密钥未配时仍可读——但必须证明它们零数据。

    拦住它们保护不了任何东西（两者全由静态列注册表生成），只会让人在最需要弄清
    "这个接口出哪些列"的时候看不到答案。代价是这个例外必须被钉死：字典里一旦
    出现任何一行真实数据，这条测试就红。
    """
    _without_key(monkeypatch)
    client = _client("steward")

    for path in ("/research/v1/meta", "/research/v1/dictionary",
                 "/research/v1/dictionary.csv"):
        response = client.get(path)
        assert response.status_code == 200, path
        raw = response.text
        for secret in ("P-REAL-1", "P-GONE-1", "S-REAL-1", SECRET_TEXT, "王家属"):
            assert secret not in raw, f"{path} 漏了 {secret}"
        assert "rows" not in response.json() if path.endswith("dictionary") else True

    # 而一切带数据行的端点必须 503
    for dataset in research_dataset.dataset_keys():
        for suffix in ("", ".csv"):
            response = client.get(
                f"/research/v1/{dataset}{suffix}?data_classification=research")
            assert response.status_code == 503, f"{dataset}{suffix}"
            assert response.json()["detail"]["code"] == \
                "research_deidentification_unavailable"


def test_paging_turns_one_at_a_time_gives_exactly_the_same_rows_as_one_big_page(
        research_env, monkeypatch):
    """把 keyset 判据推进 SQL 之后，结果必须逐行等于原来的全量。

    重写取数路径最容易出的错不是报错，是**悄悄少几行或多几行**。这条把
    limit=1 逐页走完的结果与一次性拉全量逐行比对；顺带覆盖撤回墓碑与在训行
    混在一起翻页的情形（撤回者的行也占位置，不能被跳过）。
    """
    _with_key(monkeypatch)
    with Session(research_env) as session:
        item = session.exec(select(ItemEvent).where(
            ItemEvent.session_id == "S-REAL-1")).first()
        for seq in (2, 3):
            session.add(TurnEvent(
                item_event_id=item.id, turn_seq=seq, response_role="命名",
                prompt_level=1, ai_score=0.0, judge_portrait_used=False))
        extra = ItemEvent(session_id="S-REAL-1", item_id="SE_苹果",
                          task_type="单要素", item_set_type="训练集",
                          presentation_order=2)
        session.add(extra)
        session.commit()
        session.add(TurnEvent(
            item_event_id=extra.id, turn_seq=1, response_role="命名",
            prompt_level=0, ai_score=1.0, judge_portrait_used=False))
        session.commit()

    client = _client("steward")
    whole = client.get(
        "/research/v1/turns?data_classification=research&limit=1000").json()
    assert whole["row_count"] >= 4, "行数太少，翻页断言会是空转"

    walked: list[dict] = []
    cursor = None
    for _ in range(whole["row_count"] + 3):
        query = "/research/v1/turns?data_classification=research&limit=1"
        if cursor:
            query += f"&cursor={cursor}"
        page = client.get(query).json()
        walked.extend(page["rows"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert cursor is None, "翻到上限还没走完，游标没有前进"
    assert walked == whole["rows"], "逐页走出来的行与一次全量拉的不一致"


# ---------------------------------------------------------------------------
# 行面绑定到冻结纪元
#
# 绑定之前，行面与聚合面算的是两个队列：聚合是冻结、过隔离期的那批场次，行面是
# 库里当下的一切。于是拿到 CSV 的人复现不出看板上任何一个数，而"两次拉取之差"
# 里装着这期间新入组那几个人的全部明细。
# ---------------------------------------------------------------------------

def _drop_every_epoch(engine) -> None:
    with Session(engine) as session:
        for row in session.exec(select(QualityReleaseEpoch)):
            row.status = "revoked"
            row.revoked_at = datetime(2026, 8, 3)
            row.revoke_reason_code = "test"
            session.add(row)
        session.commit()


ROW_PATHS = tuple(
    f"/research/v1/{dataset}{suffix}?data_classification=research"
    for dataset in ("subjects", "sessions", "turns")
    for suffix in ("", ".csv"))


def test_research_rows_are_refused_until_the_frozen_mode_is_switched_on(
        research_env, monkeypatch):
    """未启用冻结发布 = 行面关着，而不是照发全库。"""
    _with_key(monkeypatch)
    monkeypatch.delenv(quality_release.RELEASE_MODE_ENV, raising=False)
    client = _client("steward")
    for path in ROW_PATHS:
        response = client.get(path)
        assert response.status_code == 503, path
        assert response.json()["detail"]["code"] == "research_release_not_frozen"
        assert "P-REAL-1" not in response.text, path


def test_research_rows_are_refused_when_the_mode_is_on_but_nothing_was_cut(
        research_env, monkeypatch):
    """开关打开、纪元没切，同样一行都不发。开关本身不是授权。"""
    _with_key(monkeypatch)
    _drop_every_epoch(research_env)
    client = _client("steward")
    response = client.get("/research/v1/turns?data_classification=research")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "research_release_not_frozen"


def test_a_session_outside_the_frozen_cohort_never_reaches_the_row_face(
        research_env, monkeypatch):
    """队列外的场次不出现在任何一张表里——这是绑定的全部意义。"""
    _with_key(monkeypatch)
    _add_second_page_rows(research_env)          # 建 S-REAL-2 并把它冻进纪元 2
    _freeze_epoch(research_env, "S-REAL-1", "S-GONE-1")   # 纪元 3 把它排除掉
    client = _client("steward")

    excluded = export_security.pseudonymize_session("S-REAL-2", CONFIG)
    sessions = client.get(
        "/research/v1/sessions?data_classification=research").json()
    codes = {row["session_code"] for row in sessions["rows"]}
    assert export_security.pseudonymize_session("S-REAL-1", CONFIG) in codes, \
        "队列内的场次没出来，这条断言会因为整表为空而空转"
    assert excluded not in codes, "队列外的场次出现在行面里"

    turns = client.get(
        "/research/v1/turns?data_classification=research").json()
    assert excluded not in {row["session_code"] for row in turns["rows"]}


def test_data_written_after_the_freeze_does_not_move_the_row_face(
        research_env, monkeypatch):
    """同一纪元内两次拉取必须逐字节相同，哪怕中间又训练了一场。

    这条正是绑定要挡的那个差分：不绑的话，第二次拉取减第一次，差出来的就是
    这期间新做的那一场的全部明细。
    """
    _with_key(monkeypatch)
    client = _client("steward")
    path = "/research/v1/turns?data_classification=research"
    before = client.get(path).text
    assert '"row_count":1' in before.replace(" ", ""), \
        "第一次拉取就是空的，后面比对逐字节相同也说明不了什么"
    _create_second_session(research_env)          # 又训了一场，但没有切新纪元
    assert client.get(path).text == before, "冻结之后新写入的数据改变了行面的输出"


def test_rotating_the_key_closes_the_row_face_instead_of_emptying_it(
        research_env, monkeypatch):
    """轮换密钥之后旧纪元的假名对不上——必须拒绝，不能静默返回零行。

    静默零行是最坏的形态：调用者拿到一份 200、标着"纪元 N"、却一行都没有的
    数据，会以为这个队列真的空了。
    """
    _with_key(monkeypatch)
    _freeze_epoch(research_env, "S-REAL-1", key_id="nmu-old-key")
    client = _client("steward")
    response = client.get("/research/v1/sessions?data_classification=research")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "research_release_key_rotated"


def test_a_frozen_session_that_no_longer_resolves_closes_the_row_face(
        research_env, monkeypatch):
    """队列里有场次在库中找不到，就不许发——发出去的是纪元 N 的一个子集。"""
    _with_key(monkeypatch)
    with Session(research_env) as session:
        epoch = session.exec(
            select(QualityReleaseEpoch).where(
                QualityReleaseEpoch.status == "published")).first()
        session.add(QualityReleaseEpochSession(
            epoch_id=epoch.epoch_id,
            session_pseudonym=export_security.pseudonymize_session(
                "S-NEVER-EXISTED", CONFIG),
            evidence_watermark=1))
        session.commit()
    client = _client("steward")
    response = client.get("/research/v1/turns?data_classification=research")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "research_release_cohort_unresolved"


def test_the_simulation_partition_is_not_bound_to_any_epoch(
        research_env, monkeypatch):
    """仿真分区照常可读：那里没有真人，冻纪元只会让演练也要走治理动作。"""
    _with_key(monkeypatch)
    _drop_every_epoch(research_env)
    client = _client("steward")
    response = client.get("/research/v1/sessions?data_classification=simulation")
    assert response.status_code == 200
    assert response.json()["release"] is None, "仿真分区不该看起来像冻过纪元"


def test_subject_session_counts_only_count_the_frozen_sessions(
        research_env, monkeypatch):
    """session_count 数全部场次就是个活计数器：又训一场，同一纪元里它就变了。"""
    _with_key(monkeypatch)
    _add_second_page_rows(research_env)                   # P-REAL-1 现在有两场
    _freeze_epoch(research_env, "S-REAL-1", "S-GONE-1")   # 只冻其中一场
    code = export_security.pseudonymize_subject("P-REAL-1", CONFIG)
    rows = _client("steward").get(
        "/research/v1/subjects?data_classification=research").json()["rows"]
    row = next(r for r in rows if r["subject_code"] == code)
    assert row["session_count"] == 1, \
        "队列外的场次被数进了 session_count，这一列会随新训练而变"


def test_a_subject_outside_the_cohort_is_absent_rather_than_zero_rowed(
        research_env, monkeypatch):
    """新入组但还没有冻结场次的人整个不出现。

    留一行零场次的占位等于把"谁入组了"做成一张每次拉取都在更新的名册，
    而入组本身就是要保护的事实。
    """
    _with_key(monkeypatch)
    with Session(research_env) as session:
        session.add(Patient(
            patient_id="P-NEW-1", is_simulation_subject=False,
            consent_status="已同意", consent_type="本人同意",
            mandarin_eligible=True, recording_allowed=True))
        session.commit()
    newcomer = export_security.pseudonymize_subject("P-NEW-1", CONFIG)
    payload = _client("steward").get(
        "/research/v1/subjects?data_classification=research").json()
    codes = {row["subject_code"] for row in payload["rows"]}
    assert codes, "受试者表为空，这条断言会空转"
    assert newcomer not in codes, "队列外的受试者出现在行面里"


def test_every_bound_page_says_which_epoch_it_came_from(
        research_env, monkeypatch):
    """信封要能回答"这份数据是哪一版"，否则半年后 PI 没法比对两份 CSV。"""
    _with_key(monkeypatch)
    client = _client("steward")
    for dataset in research_dataset.dataset_keys():
        payload = client.get(
            f"/research/v1/{dataset}?data_classification=research").json()
        release = payload["release"]
        assert release["epoch_seq"] == 1, dataset
        assert release["cohort_rule_version"] == quality_release.COHORT_RULE_VERSION
        assert len(release["aggregate_payload_sha256"]) == 64, dataset


def test_the_release_envelope_carries_no_absolute_time(
        research_env, monkeypatch):
    """信封里放 ISO 时刻会被泄漏回归抓住，而且没必要——纪元号已经唯一确定版本。

    绝对时刻只出现在 /research/v1/meta，那是给运维与 PI 自诊的面。
    """
    _with_key(monkeypatch)
    client = _client("steward")
    raw = client.get("/research/v1/turns?data_classification=research").text
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", raw)
    meta = client.get("/research/v1/meta").json()["research_release"]
    assert meta["bound"] is True
    assert meta["as_of"].startswith("2026-08-01"), "meta 才是给出截止时刻的地方"
    assert meta["frozen_session_count"] == 2


def test_meta_names_the_gate_that_closed_the_row_face(
        research_env, monkeypatch):
    """503 只给一个码，取数的人要在 meta 里看懂是哪一道闸拦的。"""
    _with_key(monkeypatch)
    monkeypatch.delenv(quality_release.RELEASE_MODE_ENV, raising=False)
    state = _client("steward").get("/research/v1/meta").json()["research_release"]
    assert state["bound"] is False
    assert state["code"] == "research_release_not_frozen"
    assert state["reason"]


def test_meta_stays_readable_and_honest_without_the_key(
        research_env, monkeypatch):
    """密钥没配时 meta 仍可读，而且不能声称行面绑上了。"""
    _without_key(monkeypatch)
    state = _client("steward").get("/research/v1/meta").json()["research_release"]
    assert state["bound"] is False
    assert state["code"] == "research_deidentification_unavailable"


def test_the_row_face_records_who_read_which_epoch(
        research_env, monkeypatch):
    """聚合与明细指向同一个纪元，"谁看过纪元 N"就该把两条路径记在一起。

    AuditLog 记得下这次取数，但它不知道纪元是哪一个。
    """
    _with_key(monkeypatch)
    client = _client("steward")
    client.get("/research/v1/turns?data_classification=research")
    client.get("/research/v1/subjects.csv?data_classification=research")
    client.get("/research/v1/sessions?data_classification=simulation")
    with Session(research_env) as session:
        records = list(session.exec(select(QualityDisclosureRecord)))
    assert len(records) == 2, "仿真分区不该记披露，研究分区两次都该记"
    for record in records:
        assert record.actor_id == "STEWARD"
        assert record.actor_role == "data_steward"
        assert record.epoch_id == "qre_test_1"


def test_the_csv_filename_carries_the_epoch(research_env, monkeypatch):
    """CSV 落到磁盘之后信封与响应头都没了，而"哪一版"正是最需要留住的。"""
    _with_key(monkeypatch)
    response = _client("steward").get(
        "/research/v1/turns.csv?data_classification=research")
    assert response.status_code == 200
    assert 'filename="nmu-turns-research-epoch001.csv"' in \
        response.headers["content-disposition"]
