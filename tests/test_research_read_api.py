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

from app import (auth, content, db, export_security, repeat_intent,
                 main, research_dataset, session_admission)
from app.db import get_session
from app.main import app
from app.models import (
    AuditLog,
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


def _add_second_page_rows(engine) -> None:
    """让三张表都真的有第二页。

    默认 fixture 只有 1 个场次、1 条环节，于是 ``has_more`` 恒为 False、
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
