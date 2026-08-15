"""求助四态：谁能把它推到哪一态，以及没配通知通道时它能到哪。

这套测试守的是放行清单里那句话——「**不能假装工作人员已收到求助**」。
"""
from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import caregiver_service as cs
from app.models import (CaregiverHelpDisposition, CaregiverHelpRequest,
                        Patient, Session as TrainSession)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Patient(patient_id="P-1", dementia_severity="轻度",
                            mandarin_eligible=True))
        session.add(TrainSession(
            session_id="S-1", patient_id="P-1", week_no=2,
            phase_type="正式训练", event_line="正式训练", trainer_id="R1",
            item_bank_version_id="bank-v1",
            is_simulation=False, data_classification="research"))
        session.commit()
        session.add(CaregiverHelpRequest(
            request_id="chr_1", session_id="S-1", actor_id="CARE1",
            reason_code="participant_distress",
            idempotency_key_sha256="a" * 64, request_hash="b" * 64,
            runtime_revision=1))
        session.commit()
        yield session


@pytest.fixture(autouse=True)
def _no_channel(monkeypatch):
    monkeypatch.delenv(cs.HELP_NOTIFY_CHANNEL_ENV, raising=False)


def test_a_fresh_request_is_recorded_and_nothing_more(db):
    assert cs.help_state(cs.help_dispositions(db, request_id="chr_1")) == "recorded"
    view = cs.help_status_projection(db, request_id="chr_1")
    assert view["state"] == "recorded"
    assert view["reached"] == []


def test_nobody_can_claim_delivery_by_hand(db):
    """「已送达」不是人能声称的——这是这套四态存在的全部理由。"""
    with pytest.raises(cs.HelpDispositionRejected) as caught:
        cs.append_help_disposition(
            db, request_id="chr_1", state="delivered",
            actor_id="ADMIN", evidence="我说送到了")
    assert caught.value.code == "help_state_not_human_appendable"
    assert cs.help_state(cs.help_dispositions(db, request_id="chr_1")) == "recorded"


def test_without_a_configured_channel_delivery_is_structurally_unreachable(db):
    """没配通知对象时，连通道自己也写不出送达回执。

    放行清单：「求助通知给谁……必须由养老院确认后再实现」。工程只做通道，
    不替他们定通知对象——所以没定之前，这一态就该到不了。
    """
    assert cs.help_notify_channel_configured() is False
    with pytest.raises(cs.HelpDispositionRejected) as caught:
        cs.append_help_delivery_receipt(
            db, request_id="chr_1", channel_id="ward-phone", receipt="200")
    assert caught.value.code == "help_notify_channel_unconfigured"
    view = cs.help_status_projection(db, request_id="chr_1")
    assert view["notify_channel_configured"] is False
    assert view["delivery_reachable"] is False


def test_with_a_channel_configured_the_receipt_lands(db, monkeypatch):
    monkeypatch.setenv(cs.HELP_NOTIFY_CHANNEL_ENV, "ward-phone")
    cs.append_help_delivery_receipt(
        db, request_id="chr_1", channel_id="ward-phone", receipt="200 OK")
    view = cs.help_status_projection(db, request_id="chr_1")
    assert view["state"] == "delivered"
    assert view["delivery_reachable"] is True


def test_a_staff_member_who_walks_over_can_acknowledge_without_any_channel(db):
    """四态可跳过：没通道不代表没人来。

    强行要求先「已送达」才能「已接收」，会让这套东西在没配通知的机构里
    完全没用——而人确实走过来了，系统不该否认这件事。
    """
    cs.append_help_disposition(
        db, request_id="chr_1", state="acknowledged",
        actor_id="NURSE-7", evidence="现场确认")
    assert cs.help_state(cs.help_dispositions(db, request_id="chr_1")) == "acknowledged"


def test_states_are_append_only_and_each_one_happens_at_most_once(db):
    cs.append_help_disposition(
        db, request_id="chr_1", state="acknowledged",
        actor_id="NURSE-7", evidence="x")
    with pytest.raises(cs.HelpDispositionRejected) as caught:
        cs.append_help_disposition(
            db, request_id="chr_1", state="acknowledged",
            actor_id="NURSE-9", evidence="y")
    assert caught.value.code == "help_state_already_reached"

    row = cs.help_dispositions(db, request_id="chr_1")[0]
    row.actor_id = "IMPOSTOR"
    with pytest.raises(RuntimeError, match="只追加"):
        db.flush()
    db.rollback()


def test_every_disposition_is_named(db):
    for actor in ("", "   "):
        with pytest.raises(cs.HelpDispositionRejected) as caught:
            cs.append_help_disposition(
                db, request_id="chr_1", state="resolved",
                actor_id=actor, evidence="x")
        assert caught.value.code == "help_disposition_actor_required"


def test_the_ledger_stores_only_a_digest_of_the_receipt(db, monkeypatch):
    """通道回执里可能带值班人姓名与电话——只存摘要，不存正文。"""
    monkeypatch.setenv(cs.HELP_NOTIFY_CHANNEL_ENV, "ward-phone")
    secret = "已通知张护士 13800000000"
    cs.append_help_delivery_receipt(
        db, request_id="chr_1", channel_id="ward-phone", receipt=secret)
    row = db.get(CaregiverHelpDisposition,
                 cs.help_dispositions(db, request_id="chr_1")[0].disposition_id)
    assert secret not in str(row.model_dump())
    assert "张护士" not in str(row.model_dump())
    assert len(row.evidence_sha256) == 64


def test_the_projection_never_implies_delivery_that_did_not_happen(db):
    """没配通道时投影里不能出现任何暗示「已通知」的字段值。"""
    view = cs.help_status_projection(db, request_id="chr_1")
    assert view["state"] == "recorded"
    assert view["delivery_reachable"] is False
    assert not any(d["state"] == "delivered" for d in view["reached"])
