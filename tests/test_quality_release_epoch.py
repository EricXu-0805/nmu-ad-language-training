"""冻结发布纪元：读一次和读第二次之间，攻击者能拿到多少新信息。

这一层的全部主张是"零"。所以下面的测试不是在验功能，是在验那个零：
两次读之间往库里加数据、改环境变量、让人撤回，返回的字节必须一模一样。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import ai_quality_service, export_security, quality_release
from app.ai_quality_metrics import (
    AttemptQualityEvidence,
    QualityDimensions,
    TurnQualityEvidence,
)
from app.models import (
    ConsentType,
    Patient,
    QualityDisclosureRecord,
    QualityReleaseEpoch,
    QualityReleaseEpochSession,
    Session as TrainSession,
    SessionRuntimeState,
)
from app.quality_release import ReleaseRefused, ReleaseThresholds


THRESHOLDS = ReleaseThresholds(
    min_subjects=5, min_cell_subjects=5, band_width=10, rate_decimals=2,
    entry_quarantine_days=14)
NOW = datetime(2026, 8, 16, 3, 0, 0)


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def deidentified(monkeypatch):
    monkeypatch.setenv(
        export_security.DEIDENTIFICATION_KEY_ENV, "k" * 48)
    monkeypatch.setenv(
        export_security.DEIDENTIFICATION_KEY_ID_ENV, "test-key-1")


@pytest.fixture
def readable(monkeypatch):
    monkeypatch.setenv(
        quality_release.RELEASE_MODE_ENV, quality_release.RELEASE_MODE_REQUIRED)
    monkeypatch.setenv(
        quality_release.READER_ROLES_ENV, "data_steward,admin")
    monkeypatch.setenv(
        ai_quality_service.RESEARCH_MIN_SUBJECTS_ENV, "5")


def _payload(*, cohort_band: str = "30-39") -> dict:
    released = ai_quality_service._released_payload(
        data_classification="research", visibility_scope="all_sessions",
        generated_at=datetime(2026, 8, 16, tzinfo=timezone.utc),
        threshold=ai_quality_service._Threshold("configured", 5),
        distinct_patients=30, visible_sessions=240, included_sessions=240,
        source_turns=16800, evidence=[], restricted_sessions=0,
        classification_bad_sessions=0, protocol_binding_invalid_sessions=0,
        structural_invalid_evidence_records=0, lineage_invalid_turns=0)
    return quality_release._apply_disclosure_control(
        released, distinct_subjects=30, reviewed_subjects=22,
        included_sessions=240, thresholds=THRESHOLDS)


def _publish(db: Session, *, key: str = "cut-0001") -> QualityReleaseEpoch:
    epoch = quality_release.publish_epoch(
        db, payload=_payload(), watermarks={"S-1": 240, "S-2": 240},
        as_of=NOW, thresholds=THRESHOLDS,
        builder=("STEWARD-A", "data_steward"),
        approver=("ADMIN-B", "admin"), idempotency_key=key, now=NOW)
    db.commit()
    return epoch


def _evidence(count: int) -> list[TurnQualityEvidence]:
    """一批分母非零的证据，好让抑制产生可观测的差别。

    上面那份空证据的 fixture 里率本来就是 None（分母为 0），拿它去验抑制
    等于什么都没验——这正是本仓两天内栽过五次的那类空转。
    """
    return [
        TurnQualityEvidence(
            dimensions=QualityDimensions(data_classification="research"),
            eligible=True,
            # 每 11 条塞一个 level 3。有了它，"0..2 之和"与
            # "prompt_level_known_attempts" 才是两个不同的数——否则换分母
            # 一点区别都没有，那条断言就是空转。
            attempts=(AttemptQualityEvidence(
                prompt_level=3 if index % 11 == 0 else index % 3,
                processing_status="completed",
                latency_ms=100 + index),),
            audio_evidenced=True, ai_attempted=True, ai_judged=True,
            ai_predicted_correct=index % 4 != 0,
            asr_reviewed=True, asr_corrected=index % 5 == 0,
            human_truth_locked=True, human_truth_correct=index % 3 != 0,
            technical_pause_count=1 if index % 7 == 0 else 0,
            researcher_takeover_count=0)
        for index in range(count)
    ]


def _payload_with_rates(*, reviewed_subjects: int) -> dict:
    released = ai_quality_service._released_payload(
        data_classification="research", visibility_scope="all_sessions",
        generated_at=datetime(2026, 8, 16, tzinfo=timezone.utc),
        threshold=ai_quality_service._Threshold("configured", 5),
        distinct_patients=30, visible_sessions=240, included_sessions=240,
        source_turns=700, evidence=_evidence(700), restricted_sessions=0,
        classification_bad_sessions=0, protocol_binding_invalid_sessions=0,
        structural_invalid_evidence_records=0, lineage_invalid_turns=0)
    return quality_release._apply_disclosure_control(
        released, distinct_subjects=30, reviewed_subjects=reviewed_subjects,
        included_sessions=240, thresholds=THRESHOLDS)


def test_rates_really_compute_when_the_contributor_count_clears_the_threshold():
    row = _payload_with_rates(reviewed_subjects=22)["rows"][0]

    # 先证明这条路径真的走得到——否则下一条测试就是空转。
    assert row["research_truth"]["agreement_rate"] is not None
    assert row["operational"]["asr_manual_correction_rate"] is not None
    assert row["research_truth"]["reviewed_decisions_band"] is not None


def test_too_few_reviewers_nulls_the_rates_that_would_otherwise_be_published():
    row = _payload_with_rates(reviewed_subjects=3)["rows"][0]

    assert row["research_truth"] == {
        "agreement_rate": None,
        "false_positive_rate": None,
        "false_negative_rate": None,
        "reviewed_decisions_band": None,
    }
    # 连接闭包：复核格被抑制，同一批人支撑的队列侧比率也必须跟着灭。
    assert all(value is None for key, value in row["operational"].items()
               if key.endswith("_rate"))


def test_rates_never_publish_more_decimals_than_the_frozen_setting():
    row = _payload_with_rates(reviewed_subjects=22)["rows"][0]
    published = [value for value in
                 (*row["research_truth"].values(), *row["operational"].values())
                 if isinstance(value, float)]

    assert published, "一个率都没发，这条断言会空转"
    for value in published:
        assert value == round(value, THRESHOLDS.rate_decimals)


def test_the_prompt_escalation_denominator_cannot_be_used_to_solve_level_three():
    """level 3 的计数被服务层显式置成 None（床旁没有留存回执，报 0 是假陈述）。

    分母若用 prompt_level_known_attempts，减法就能把它解出来——所以分母只用
    0..2 三档之和。这条测试钉住那个选择。
    """
    row = _payload_with_rates(reviewed_subjects=22)["rows"][0]

    assert "prompt_level_known_attempts" not in row["operational"]
    assert row["coverage"]["prompt_level_known_attempts"] is None

    # 700 条里每 11 条一个 level 3 = 64 条；0..2 三档共 636 条。
    # 升级（level 1、2）在这 636 条里的占比才是该发的那个数。
    escalated = sum(1 for index in range(700)
                    if index % 11 != 0 and index % 3 in (1, 2))
    base = sum(1 for index in range(700) if index % 11 != 0)
    assert row["operational"]["prompt_escalation_rate"] == (
        int(escalated / base * 100) / 100)
    # 若分母误用 prompt_level_known_attempts（含 level 3），得到的是另一个数——
    # 两者之差就把被显式置成 None 的 level 3 计数解了出来。
    assert row["operational"]["prompt_escalation_rate"] != (
        int(escalated / 700 * 100) / 100)


def test_every_leaf_of_a_real_payload_is_registered_so_new_ones_default_shut():
    assert quality_release.registry_problems(_payload()) == []


def test_an_unregistered_leaf_is_caught_rather_than_published_silently():
    smuggled = _payload()
    smuggled["rows"][0]["operational"]["exact_subject_count"] = 30

    assert quality_release.registry_problems(smuggled) == [
        "rows/0/operational/exact_subject_count"]


def test_reading_twice_returns_the_identical_bytes_including_generated_at(
        engine, deidentified, readable):
    with Session(engine) as db:
        _publish(db)
        first = quality_release.serve(db, actor_id="A", actor_role="admin")
        db.commit()
        second = quality_release.serve(db, actor_id="A", actor_role="admin")
        db.commit()

    assert quality_release.canonical_bytes(first) == (
        quality_release.canonical_bytes(second))
    assert first["generated_at"] == second["generated_at"]


def test_new_sessions_landing_between_two_reads_move_nothing(
        engine, deidentified, readable):
    with Session(engine) as db:
        _publish(db)
        before = quality_release.serve(db, actor_id="A", actor_role="admin")
        db.commit()
        db.add(Patient(patient_id="P-NEW", is_simulation_subject=False,
                       consent_status="已同意", recording_allowed=True))
        db.add(TrainSession(
            session_id="S-NEW", patient_id="P-NEW", training_date=date.today(),
            week_no=2, phase_type="正式训练", event_line="正式训练",
            trainer_id="T-1", item_bank_version_id="bank-v1",
            is_simulation=False, data_classification="research"))
        db.commit()
        after = quality_release.serve(db, actor_id="A", actor_role="admin")
        db.commit()

    assert quality_release.canonical_bytes(before) == (
        quality_release.canonical_bytes(after))


def test_serving_reads_no_live_table_so_withdrawal_is_not_a_one_person_probe(
        engine, deidentified, readable, monkeypatch):
    """读路径查撤回闸看着更"及时"，实际是一个 1 Hz 的单人探针。"""
    def forbidden(*_args, **_kwargs):
        raise AssertionError("冻结复读不得触碰任何活表")

    with Session(engine) as db:
        _publish(db)
        monkeypatch.setattr(ai_quality_service, "_visible_sessions", forbidden)
        monkeypatch.setattr(ai_quality_service, "_preproject_sessions", forbidden)
        monkeypatch.setattr(
            ai_quality_service, "_begin_stable_read_snapshot", forbidden)
        monkeypatch.setattr(quality_release, "derive_cohort", forbidden)
        monkeypatch.setattr(quality_release, "build_payload", forbidden)

        served = quality_release.serve(db, actor_id="A", actor_role="admin")
        db.commit()

    assert served["rows"][0]["visibility_scope"] == "frozen_release_cohort"


def test_nobody_reads_it_until_someone_named_says_who_may(
        engine, deidentified, monkeypatch):
    monkeypatch.setenv(
        quality_release.RELEASE_MODE_ENV, quality_release.RELEASE_MODE_REQUIRED)
    monkeypatch.delenv(quality_release.READER_ROLES_ENV, raising=False)
    with Session(engine) as db:
        _publish(db)
        with pytest.raises(ReleaseRefused) as caught:
            quality_release.serve(db, actor_id="A", actor_role="admin")

    assert caught.value.code == "research_release_reader_not_authorized"


@pytest.mark.parametrize("roles", ["", "  ", "researcher,superuser", "admin ,"])
def test_a_misspelt_role_list_shuts_the_whole_thing_not_just_that_name(
        monkeypatch, roles):
    monkeypatch.setenv(quality_release.READER_ROLES_ENV, roles)
    allowed = quality_release.authorized_reader_roles()
    assert "superuser" not in allowed
    if "superuser" in roles:
        assert allowed == frozenset(), "拼错一个名字不该只是静默过滤掉它"


def test_a_role_outside_the_configured_list_is_refused(
        engine, deidentified, readable):
    with Session(engine) as db:
        _publish(db)
        with pytest.raises(ReleaseRefused) as caught:
            quality_release.serve(db, actor_id="R", actor_role="researcher")

    assert caught.value.code == "research_release_reader_not_authorized"


def test_without_release_mode_there_is_nothing_to_read(
        engine, deidentified, monkeypatch):
    monkeypatch.setenv(quality_release.READER_ROLES_ENV, "admin")
    monkeypatch.delenv(quality_release.RELEASE_MODE_ENV, raising=False)
    with Session(engine) as db:
        _publish(db)
        with pytest.raises(ReleaseRefused) as caught:
            quality_release.serve(db, actor_id="A", actor_role="admin")

    assert caught.value.code == "research_release_not_frozen"


def test_before_any_cut_the_partition_is_refused_not_computed_on_the_fly(
        engine, deidentified, readable):
    with Session(engine) as db:
        with pytest.raises(ReleaseRefused) as caught:
            quality_release.serve(db, actor_id="A", actor_role="admin")

    assert caught.value.code == "research_release_not_frozen"


def test_raising_the_threshold_after_the_cut_shuts_the_old_epoch(
        engine, deidentified, readable, monkeypatch):
    with Session(engine) as db:
        _publish(db)
        monkeypatch.setenv(
            ai_quality_service.RESEARCH_MIN_SUBJECTS_ENV, "8")
        with pytest.raises(ReleaseRefused) as caught:
            quality_release.serve(db, actor_id="A", actor_role="admin")

    assert caught.value.code == "research_release_threshold_raised"


def test_loosening_the_threshold_afterwards_still_reports_what_was_frozen(
        engine, deidentified, readable, monkeypatch):
    with Session(engine) as db:
        _publish(db)
        monkeypatch.setenv(
            ai_quality_service.RESEARCH_MIN_SUBJECTS_ENV, "2")
        served = quality_release.serve(db, actor_id="A", actor_role="admin")
        db.commit()

    assert served["rows"][0]["suppression"]["minimum_distinct_subjects"] == 5


def test_a_tampered_frozen_payload_is_refused_rather_than_served(
        engine, deidentified, readable):
    with Session(engine) as db:
        epoch = _publish(db)
        payload = json.loads(epoch.payload_json)
        payload["rows"][0]["operational"]["pause_rate"] = 0.99
        # 绕开只追加守卫直接改存储层，模拟持有 DB 写权的人。
        db.exec(  # type: ignore[call-overload]
            QualityReleaseEpoch.__table__.update()
            .where(QualityReleaseEpoch.__table__.c.epoch_id == epoch.epoch_id)
            .values(payload_json=quality_release.canonical_bytes(
                payload).decode("utf-8")))
        db.commit()
        with pytest.raises(ReleaseRefused) as caught:
            quality_release.serve(db, actor_id="A", actor_role="admin")

    assert caught.value.code == "research_release_payload_corrupt"


def test_every_read_is_named_in_the_disclosure_ledger(
        engine, deidentified, readable):
    with Session(engine) as db:
        _publish(db)
        quality_release.serve(db, actor_id="STEWARD-A", actor_role="data_steward")
        db.commit()
        quality_release.serve(db, actor_id="ADMIN-B", actor_role="admin")
        db.commit()
        rows = list(db.exec(select(QualityDisclosureRecord)))

    assert sorted(row.actor_id for row in rows) == ["ADMIN-B", "STEWARD-A"]
    assert {row.actor_role for row in rows} == {"data_steward", "admin"}


def test_the_ledger_does_not_depend_on_the_caller_committing(
        engine, deidentified, readable):
    """账本必须自己落库。

    上一版是往调用方会话里 add + flush 就完了。这条测试的上一版紧跟着写了句
    ``db.commit()``，于是全绿；而 HTTP 上 ``get_session`` 从头到尾不 commit，
    读接口本来也没有别的写——每一行都在请求结束时随会话一起回滚。交接文档写着
    "每一次读取都往只追加的账本里写一行"，实际上账本一行都没有。

    所以这里**故意回滚调用方的事务**，再从另一个会话里查。
    """
    with Session(engine) as db:
        _publish(db)
        db.commit()
        quality_release.serve(db, actor_id="STEWARD-A", actor_role="data_steward")
        db.rollback()

    with Session(engine) as other:
        rows = list(other.exec(select(QualityDisclosureRecord)))
    assert [row.actor_id for row in rows] == ["STEWARD-A"], \
        "调用方回滚之后账本空了——那正是 HTTP 上每一次读取的实际情形"


def test_the_frozen_cohort_stores_pseudonyms_not_session_ids(
        engine, deidentified):
    with Session(engine) as db:
        _publish(db)
        rows = list(db.exec(select(QualityReleaseEpochSession)))

    assert len(rows) == 2
    serialized = json.dumps([row.model_dump() for row in rows],
                            ensure_ascii=False, default=str)
    assert "S-1" not in serialized and "S-2" not in serialized
    assert all(row.session_pseudonym.startswith("SESS") for row in rows)


def test_the_frozen_content_cannot_be_edited_only_its_lifecycle(
        engine, deidentified):
    with Session(engine) as db:
        epoch = _publish(db)
        epoch.payload_json = "{}"
        with pytest.raises(RuntimeError, match="冻结内容不可改"):
            db.commit()
    with Session(engine) as db:
        stored = db.exec(select(QualityReleaseEpoch)).one()
        stored.status = "revoked"
        stored.revoked_at = NOW
        db.commit()  # 生命周期字段可以动，不抛。
        assert db.exec(select(QualityReleaseEpoch)).one().status == "revoked"


def test_a_second_cut_supersedes_the_first_and_becomes_what_is_served(
        engine, deidentified, readable):
    with Session(engine) as db:
        first = _publish(db, key="cut-0001")
        second = _publish(db, key="cut-0002")
        assert (first.epoch_seq, second.epoch_seq) == (1, 2)
        assert quality_release.current_epoch(db).epoch_id == second.epoch_id
        db.refresh(first)
        assert first.status == "superseded"


def test_the_disclosure_ledger_and_frozen_cohort_are_append_only(
        engine, deidentified):
    with Session(engine) as db:
        _publish(db)
        row = db.exec(select(QualityReleaseEpochSession)).first()
        row.evidence_watermark = 1
        with pytest.raises(RuntimeError, match="冻结的队列构成"):
            db.commit()


def _seed_session(db: Session, session_id: str, *, status: str,
                  settled_days_ago: int) -> None:
    patient_id = f"P-{session_id}"
    db.add(Patient(patient_id=patient_id, is_simulation_subject=False,
                   consent_status="已同意", consent_type=ConsentType.本人同意,
                   recording_allowed=True, mandarin_eligible=True))
    db.add(TrainSession(
        session_id=session_id, patient_id=patient_id,
        training_date=date.today(), week_no=2, phase_type="正式训练",
        event_line="正式训练", trainer_id="T-1",
        item_bank_version_id="bank-v1", is_simulation=False,
        data_classification="research"))
    db.add(SessionRuntimeState(
        session_id=session_id, status=status, revision=1,
        updated_at=NOW - timedelta(days=settled_days_ago)))


def test_the_cohort_takes_only_settled_sessions_past_the_quarantine(engine):
    with Session(engine) as db:
        _seed_session(db, "S-OLD", status="completed", settled_days_ago=30)
        _seed_session(db, "S-FRESH", status="completed", settled_days_ago=3)
        _seed_session(db, "S-RUNNING", status="active", settled_days_ago=30)
        _seed_session(db, "S-PAUSED", status="paused", settled_days_ago=30)
        db.commit()

        cohort = quality_release.derive_cohort(
            db, as_of=NOW, quarantine_days=14)

    assert [row.session_id for row in cohort] == ["S-OLD"]


def test_there_is_no_way_to_hand_pick_who_is_in_the_cohort():
    """leave-one-out：切两次、第二次少一个人，两版之差就是那个人。

    唯一自洽的挡法是让队列没有可选的入口——`derive_cohort` 只接受 as_of 与
    隔离期，两个都是可复算的标量。
    """
    import inspect

    parameters = set(
        inspect.signature(quality_release.derive_cohort).parameters)

    assert parameters == {"s", "as_of", "quarantine_days"}
