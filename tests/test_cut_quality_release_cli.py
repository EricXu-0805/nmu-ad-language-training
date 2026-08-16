"""切纪元命令行工具：它拒绝的那些情况，才是它存在的理由。

这个工具全研究期只跑几次，所以每一次都必须是「要么完整地切成，要么一个字节
都不写」。下面每条测试都先把出错的场景摆出来，再断言它拒绝且 DB 没动。
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import ai_quality_service, db, export_security, quality_release
from app.models import (
    ConsentType,
    Patient,
    QualityReleaseEpoch,
    Session as TrainSession,
    SessionRuntimeState,
)


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "cut_quality_release", ROOT / "scripts" / "cut_quality_release.py")
assert _SPEC is not None and _SPEC.loader is not None
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)

NOW = datetime(2026, 8, 16, 3, 0, 0)
BUILDER = ["--builder", "STEWARD-A", "--builder-role", "data_steward"]


@pytest.fixture
def wired(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setenv(export_security.DEIDENTIFICATION_KEY_ENV, "k" * 48)
    monkeypatch.setenv(export_security.DEIDENTIFICATION_KEY_ID_ENV, "test-key-1")
    monkeypatch.setenv(ai_quality_service.RESEARCH_MIN_SUBJECTS_ENV, "5")
    monkeypatch.setenv(quality_release.MIN_CELL_SUBJECTS_ENV, "5")
    monkeypatch.setenv(quality_release.BAND_WIDTH_ENV, "10")
    monkeypatch.setenv(quality_release.RATE_DECIMALS_ENV, "2")
    return engine


def _seed(engine, *, status: str, settled_days_ago: int = 30,
          session_id: str = "S-1") -> None:
    with Session(engine) as s:
        s.add(Patient(
            patient_id=f"P-{session_id}", is_simulation_subject=False,
            consent_status="已同意", consent_type=ConsentType.本人同意,
            recording_allowed=True, mandarin_eligible=True))
        s.add(TrainSession(
            session_id=session_id, patient_id=f"P-{session_id}",
            training_date=date.today(), week_no=2, phase_type="正式训练",
            event_line="正式训练", trainer_id="T-1",
            item_bank_version_id="bank-v1", is_simulation=False,
            data_classification="research"))
        s.add(SessionRuntimeState(
            session_id=session_id, status=status, revision=1,
            updated_at=NOW - timedelta(days=settled_days_ago)))
        s.commit()


def _epochs(engine) -> list[QualityReleaseEpoch]:
    with Session(engine) as s:
        return list(s.exec(select(QualityReleaseEpoch)))


def test_without_the_deidentification_key_nothing_is_computed_or_written(
        wired, monkeypatch, capsys):
    monkeypatch.delenv(export_security.DEIDENTIFICATION_KEY_ENV, raising=False)

    assert cli.main(["--propose", *BUILDER]) == 1
    assert "deidentification_key_unconfigured" in capsys.readouterr().err
    assert _epochs(wired) == []


def test_an_open_bedside_session_blocks_the_cut(wired, capsys):
    _seed(wired, status="active")

    assert cli.main(["--propose", *BUILDER]) == 1
    assert "release_bedside_session_open" in capsys.readouterr().err
    assert _epochs(wired) == []


@pytest.mark.parametrize("missing", [
    quality_release.MIN_CELL_SUBJECTS_ENV,
    quality_release.BAND_WIDTH_ENV,
    quality_release.RATE_DECIMALS_ENV,
    ai_quality_service.RESEARCH_MIN_SUBJECTS_ENV,
])
def test_an_unset_threshold_refuses_rather_than_falling_back(
        wired, monkeypatch, capsys, missing):
    _seed(wired, status="completed")
    monkeypatch.delenv(missing, raising=False)

    assert cli.main(["--propose", *BUILDER]) == 1
    assert "unconfigured" in capsys.readouterr().err
    assert _epochs(wired) == []


def test_propose_writes_nothing_at_all(wired, capsys):
    _seed(wired, status="completed")

    cli.main(["--propose", *BUILDER])

    assert _epochs(wired) == [], "--propose 只算不写"


def test_approve_refuses_when_the_payload_moved_between_the_two_phases(
        wired, capsys, monkeypatch):
    """两阶段之间库里动过就不许发。

    这里把装载荷整个换掉，因为要走到 sha 比对那一行，前面每一道闸都得先过；
    用真数据把它们全喂饱是另一件事，那件事由 `test_quality_release_epoch`
    覆盖。换掉之后 sha 比对是这条测试里唯一还在起作用的判据。
    """
    monkeypatch.setattr(
        cli, "_build",
        lambda _s, _as_of: ({"rows": [{
            "release": {"cohort_size_band": "30-39",
                        "session_count_band": "240-249"},
            "diagnostics": {"status": "complete"},
            "operational": {}, "research_truth": {},
        }]}, {"S-1": 240}, "a" * 64))
    _seed(wired, status="completed")

    code = cli.main([
        "--approve", *BUILDER, "--approver", "ADMIN-B",
        "--approver-role", "admin", "--expect-sha256", "0" * 64,
        "--idempotency-key", "cut-0001"])

    assert code == 1
    assert "release_payload_moved" in capsys.readouterr().err
    assert _epochs(wired) == []


def test_a_session_without_a_bound_content_definition_is_refused(
        wired, capsys):
    """题库绑定验不过就不切——冻一份内容来源不明的聚合，比不冻更糟。"""
    _seed(wired, status="completed")

    assert cli.main(["--propose", *BUILDER]) == 1
    assert "release_cohort_no_bound_definition" in capsys.readouterr().err
    assert _epochs(wired) == []


def test_one_person_cannot_be_both_builder_and_approver(wired):
    with pytest.raises(SystemExit) as caught:
        cli.main([
            "--approve", *BUILDER, "--approver", "STEWARD-A",
            "--approver-role", "admin", "--expect-sha256", "0" * 64,
            "--idempotency-key", "cut-0001"])

    assert caught.value.code == 2
    assert _epochs(wired) == []


def test_approve_needs_every_one_of_the_four_second_phase_arguments(wired):
    for extra in (
        ["--approver", "ADMIN-B"],
        ["--approver", "ADMIN-B", "--approver-role", "admin"],
        ["--approver", "ADMIN-B", "--approver-role", "admin",
         "--expect-sha256", "0" * 64],
    ):
        with pytest.raises(SystemExit) as caught:
            cli.main(["--approve", *BUILDER, *extra])
        assert caught.value.code == 2


def test_the_refusal_output_carries_no_subject_or_session_identifier(
        wired, capsys):
    _seed(wired, status="active", session_id="S-VERY-DISTINCTIVE")

    cli.main(["--propose", *BUILDER])

    captured = capsys.readouterr()
    assert "S-VERY-DISTINCTIVE" not in captured.out + captured.err
    assert "P-S-VERY-DISTINCTIVE" not in captured.out + captured.err


def test_an_empty_cohort_is_refused_instead_of_publishing_an_empty_release(
        wired, capsys):
    # 一个合格场次都没有：不能发一份"全零"的聚合出去，那和"这批人什么都没做"
    # 是两句不同的话。
    assert cli.main(["--propose", *BUILDER]) == 1
    assert "release_cohort_empty" in capsys.readouterr().err
    assert _epochs(wired) == []


def test_a_session_still_inside_the_quarantine_window_is_not_frozen_in(
        wired, capsys):
    _seed(wired, status="completed", settled_days_ago=3)

    assert cli.main([
        "--propose", *BUILDER,
        "--as-of", NOW.isoformat()]) == 1
    assert "release_cohort_empty" in capsys.readouterr().err


def _stub_build(monkeypatch, payload: dict) -> None:
    """让装载荷这一步成功，好让后面那几道闸真的被走到。

    直接喂真数据要先把题库绑定、证据投影全部满足——那件事由
    `test_quality_release_epoch` 覆盖。这里替掉的是 `build_payload`
    而不是 `_build`，因为登记表自检就在 `_build` 里，替掉它等于把要验的
    东西一起绕过去。
    """
    monkeypatch.setattr(
        quality_release, "build_payload",
        lambda _s, _cohort, **_kwargs: (payload, {"S-1": 240}))


def _publishable() -> dict:
    return {
        "rows": [{
            "release": {"cohort_size_band": "30-39",
                        "session_count_band": "240-249",
                        "registry_version": quality_release.REGISTRY_VERSION,
                        "cohort_rule_version": quality_release.COHORT_RULE_VERSION},
            "diagnostics": {"status": "complete", "reason_counts": None},
            "operational": {}, "research_truth": {},
            "visibility_scope": "frozen_release_cohort",
            "suppression": {"status": "released", "reason": None,
                            "minimum_distinct_subjects": 5,
                            "distinct_subjects": None},
            "coverage": {}, "dimensions": {"data_classification": "research"},
        }],
        "schema_version": quality_release.RELEASE_SCHEMA_VERSION,
        "generated_at": "2026-08-16T00:00:00Z",
        "privacy": {"aggregation_only": True,
                    "contains_patient_identifiers": False,
                    "contains_audio": False, "contains_transcripts": False},
    }


def test_propose_still_writes_nothing_when_the_build_actually_succeeds(
        wired, monkeypatch, capsys):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())

    assert cli.main(["--propose", *BUILDER]) == 0
    assert "payload_sha256" in capsys.readouterr().out
    assert _epochs(wired) == [], "--propose 只算不写"


def test_an_unregistered_field_in_the_payload_stops_the_cut(
        wired, monkeypatch, capsys):
    """登记表是"默认抑制"的实现方式：没在册的字段一出现就整份不发。"""
    smuggled = _publishable()
    smuggled["rows"][0]["operational"]["exact_subject_count"] = 30
    _seed(wired, status="completed")
    _stub_build(monkeypatch, smuggled)

    assert cli.main(["--propose", *BUILDER]) == 1
    captured = capsys.readouterr().err
    assert "release_registry_incomplete" in captured
    assert "exact_subject_count" in captured
    assert _epochs(wired) == []
