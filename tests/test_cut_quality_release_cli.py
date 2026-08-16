"""切纪元命令行工具：它拒绝的那些情况，才是它存在的理由。

这个工具全研究期只跑几次，所以每一次都必须是「要么完整地切成，要么一个字节
都不写」。下面每条测试都先把出错的场景摆出来，再断言它拒绝且 DB 没动。
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import ai_quality_service, db, export_security, quality_release
from app.models import (
    ConsentType,
    Patient,
    QualityReleaseEpoch,
    QualityReleaseEpochRowSnapshot,
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
TEST_SNAPSHOT_SHA256 = "f" * 64


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


def _snapshot_rows(engine) -> list[QualityReleaseEpochRowSnapshot]:
    with Session(engine) as s:
        return list(s.exec(select(QualityReleaseEpochRowSnapshot)))


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
        lambda _s, _as_of, _config, _builder: ({"rows": [{
            "release": {"cohort_size_band": "30-39",
                        "session_count_band": "240-249"},
            "diagnostics": {"status": "complete"},
            "operational": {}, "research_truth": {},
        }]}, {"S-1": 240}, quality_release.load_thresholds(),
            quality_release.ResearchSnapshot(
                manifest_json="{}", snapshot_sha256=TEST_SNAPSHOT_SHA256,
                rows=()),
            "a" * 64, "b" * 64))
    _seed(wired, status="completed")

    code = cli.main([
        "--approve", *BUILDER, "--approver", "ADMIN-B",
        "--approver-role", "admin", "--expect-sha256", "0" * 64,
        "--idempotency-key", "cut-0001", "--as-of", NOW.isoformat()])

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
            "--idempotency-key", "cut-0001", "--as-of", NOW.isoformat()])

    assert caught.value.code == 2
    assert _epochs(wired) == []


def test_approve_needs_every_one_of_the_five_second_phase_arguments(wired):
    for extra in (
        ["--approver", "ADMIN-B"],
        ["--approver", "ADMIN-B", "--approver-role", "admin"],
        ["--approver", "ADMIN-B", "--approver-role", "admin",
         "--expect-sha256", "0" * 64],
        ["--approver", "ADMIN-B", "--approver-role", "admin",
         "--expect-sha256", "0" * 64, "--idempotency-key", "cut-0001"],
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


def _propose(capsys, *, as_of: datetime = NOW) -> dict:
    assert cli.main([
        "--propose", *BUILDER, "--as-of", as_of.isoformat(),
    ]) == 0
    return json.loads(capsys.readouterr().out)


def _approve_args(
    proposal: dict, *, idempotency_key: str,
    receipt_dir: Path | None = None,
) -> list[str]:
    args = [
        "--approve", *BUILDER,
        "--approver", "ADMIN-B", "--approver-role", "admin",
        "--as-of", proposal["as_of"],
        "--expect-proposal-sha256", proposal["proposal_sha256"],
        "--idempotency-key", idempotency_key,
    ]
    if receipt_dir is not None:
        args.extend(("--receipt-dir", str(receipt_dir)))
    return args


def _recover_args(
    proposal: dict, *, idempotency_key: str, receipt_dir: Path,
) -> list[str]:
    return [
        "--recover-receipt", *BUILDER,
        "--approver", "ADMIN-B", "--approver-role", "admin",
        "--as-of", proposal["as_of"],
        "--expect-proposal-sha256", proposal["proposal_sha256"],
        "--idempotency-key", idempotency_key,
        "--receipt-dir", str(receipt_dir),
    ]


def test_propose_still_writes_nothing_when_the_build_actually_succeeds(
        wired, monkeypatch, capsys):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())

    assert cli.main(["--propose", *BUILDER]) == 0
    output = json.loads(capsys.readouterr().out)
    assert set(output) >= {"payload_sha256", "proposal_sha256", "as_of"}
    assert _epochs(wired) == [], "--propose 只算不写"


def test_real_two_stage_contract_reuses_as_of_and_proposal_digest(
        wired, monkeypatch, capsys):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())

    assert cli.main([
        "--propose", *BUILDER, "--as-of", "2026-08-16T11:00:00+08:00",
    ]) == 0
    proposal = json.loads(capsys.readouterr().out)
    assert proposal["as_of"] == "2026-08-16T03:00:00Z"

    assert cli.main([
        "--approve", *BUILDER,
        "--approver", "ADMIN-B", "--approver-role", "admin",
        "--as-of", proposal["as_of"],
        "--expect-proposal-sha256", proposal["proposal_sha256"],
        "--idempotency-key", "cut-0001",
    ]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["proposal_sha256"] == proposal["proposal_sha256"]
    assert len(_epochs(wired)) == 1


def test_proposal_digest_moves_when_only_an_evidence_watermark_moves(
        wired):
    config = export_security.load_deidentification_config()
    payload = _publishable()

    before = quality_release.proposal_digest(
        payload, {"S-1": 240}, as_of=NOW, config=config,
        thresholds=quality_release.load_thresholds(),
        builder=("STEWARD-A", "data_steward"),
        research_snapshot_sha256=TEST_SNAPSHOT_SHA256)
    after = quality_release.proposal_digest(
        payload, {"S-1": 241}, as_of=NOW, config=config,
        thresholds=quality_release.load_thresholds(),
        builder=("STEWARD-A", "data_steward"),
        research_snapshot_sha256=TEST_SNAPSHOT_SHA256)

    assert before != after


def test_proposal_digest_binds_builder_and_frozen_policy(wired, monkeypatch):
    config = export_security.load_deidentification_config()
    payload = _publishable()
    thresholds = quality_release.load_thresholds()
    original = quality_release.proposal_digest(
        payload, {"S-1": 240}, as_of=NOW, config=config,
        thresholds=thresholds, builder=("STEWARD-A", "data_steward"),
        research_snapshot_sha256=TEST_SNAPSHOT_SHA256)
    different_builder = quality_release.proposal_digest(
        payload, {"S-1": 240}, as_of=NOW, config=config,
        thresholds=thresholds, builder=("STEWARD-X", "data_steward"),
        research_snapshot_sha256=TEST_SNAPSHOT_SHA256)
    monkeypatch.setenv(quality_release.BAND_WIDTH_ENV, "20")
    different_policy = quality_release.proposal_digest(
        payload, {"S-1": 240}, as_of=NOW, config=config,
        thresholds=quality_release.load_thresholds(),
        builder=("STEWARD-A", "data_steward"),
        research_snapshot_sha256=TEST_SNAPSHOT_SHA256)

    assert len({original, different_builder, different_policy}) == 3


def test_future_as_of_is_refused_before_any_release_read(
        wired, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_utc_now_naive", lambda: NOW)

    assert cli.main([
        "--propose", *BUILDER, "--as-of", "2026-08-16T03:00:01Z",
    ]) == 1
    assert "release_as_of_in_future" in capsys.readouterr().err
    assert _epochs(wired) == []


def test_receipt_is_staged_before_commit_and_published_without_overwrite(
        wired, monkeypatch, capsys, tmp_path):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    proposal = _propose(capsys)

    assert cli.main([
        "--approve", *BUILDER,
        "--approver", "ADMIN-B", "--approver-role", "admin",
        "--as-of", NOW.isoformat(),
        "--expect-proposal-sha256", proposal["proposal_sha256"],
        "--idempotency-key", "cut-receipt-ok",
        "--receipt-dir", str(tmp_path),
    ]) == 0

    receipt = tmp_path / "quality-release-001.json"
    assert receipt.is_file()
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt.read_text(encoding="utf-8"))["epoch_seq"] == 1
    assert not list(tmp_path.glob("*.pending"))
    assert "回执已写入" in capsys.readouterr().err


def test_receipt_stage_failure_rolls_back_the_epoch(
        wired, monkeypatch, capsys, tmp_path):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    proposal = _propose(capsys)
    monkeypatch.setattr(
        cli, "_stage_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            quality_release.ReleaseRefused(
                "release_receipt_stage_failed", "回执写入失败")))

    assert cli.main([
        "--approve", *BUILDER,
        "--approver", "ADMIN-B", "--approver-role", "admin",
        "--as-of", NOW.isoformat(),
        "--expect-proposal-sha256", proposal["proposal_sha256"],
        "--idempotency-key", "cut-receipt-fail",
        "--receipt-dir", str(tmp_path),
    ]) == 1

    assert _epochs(wired) == []
    assert "release_receipt_stage_failed" in capsys.readouterr().err


def test_finalize_failure_reports_published_epoch_and_preserves_pending_receipt(
        wired, monkeypatch, capsys, tmp_path):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    proposal = _propose(capsys)
    monkeypatch.setattr(
        cli, "_finalize_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")))

    assert cli.main([
        "--approve", *BUILDER,
        "--approver", "ADMIN-B", "--approver-role", "admin",
        "--as-of", NOW.isoformat(),
        "--expect-proposal-sha256", proposal["proposal_sha256"],
        "--idempotency-key", "cut-receipt-pending",
        "--receipt-dir", str(tmp_path),
    ]) == 1

    assert len(_epochs(wired)) == 1, "程序必须诚实报告 DB 已发布"
    assert len(list(tmp_path.glob(".*.pending"))) == 1
    assert "release_published_receipt_pending" in capsys.readouterr().err


def test_recover_receipt_reconstructs_it_from_the_published_epoch_without_pending(
        wired, monkeypatch, capsys, tmp_path):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    proposal = _propose(capsys)

    assert cli.main(_approve_args(
        proposal, idempotency_key="cut-recover-no-pending")) == 0
    published = json.loads(capsys.readouterr().out)
    monkeypatch.delenv(export_security.DEIDENTIFICATION_KEY_ENV, raising=False)

    assert cli.main(_recover_args(
        proposal, idempotency_key="cut-recover-no-pending",
        receipt_dir=tmp_path)) == 0

    recovered = tmp_path / "quality-release-001.json"
    assert json.loads(recovered.read_text(encoding="utf-8")) == published
    assert recovered.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".*.pending"))
    assert len(_epochs(wired)) == 1, "恢复只能补回执，不能重切纪元"
    assert "回执已恢复" in capsys.readouterr().err


def test_recover_receipt_finalizes_the_exact_precommit_pending_file(
        wired, monkeypatch, capsys, tmp_path):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    proposal = _propose(capsys)
    real_finalize = cli._finalize_receipt
    monkeypatch.setattr(
        cli, "_finalize_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")))

    assert cli.main(_approve_args(
        proposal, idempotency_key="cut-recover-pending",
        receipt_dir=tmp_path)) == 1
    pending = list(tmp_path.glob(".*.pending"))
    assert len(pending) == 1
    expected_bytes = pending[0].read_bytes()
    capsys.readouterr()
    monkeypatch.setattr(cli, "_finalize_receipt", real_finalize)

    assert cli.main(_recover_args(
        proposal, idempotency_key="cut-recover-pending",
        receipt_dir=tmp_path)) == 0

    final = tmp_path / "quality-release-001.json"
    assert final.read_bytes() == expected_bytes
    assert not list(tmp_path.glob(".*.pending"))
    assert len(_epochs(wired)) == 1


def test_recover_receipt_refuses_a_wrong_proposal_without_writing_a_file(
        wired, monkeypatch, capsys, tmp_path):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    proposal = _propose(capsys)
    assert cli.main(_approve_args(
        proposal, idempotency_key="cut-recover-wrong-proposal")) == 0
    capsys.readouterr()
    wrong = dict(proposal, proposal_sha256="0" * 64)

    assert cli.main(_recover_args(
        wrong, idempotency_key="cut-recover-wrong-proposal",
        receipt_dir=tmp_path)) == 1

    assert not list(tmp_path.iterdir())
    assert "release_receipt_proposal_mismatch" in capsys.readouterr().err


def test_recover_receipt_refuses_when_the_idempotency_key_was_not_published(
        wired, capsys, tmp_path):
    proposal = {
        "as_of": NOW.isoformat() + "Z",
        "proposal_sha256": "0" * 64,
    }

    assert cli.main(_recover_args(
        proposal, idempotency_key="cut-never-published",
        receipt_dir=tmp_path)) == 1

    assert not list(tmp_path.iterdir())
    assert "release_receipt_epoch_not_found" in capsys.readouterr().err


def test_recover_receipt_strictly_binds_as_of_and_both_actors(
        wired, monkeypatch, capsys, tmp_path):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    proposal = _propose(capsys)
    assert cli.main(_approve_args(
        proposal, idempotency_key="cut-recover-actors")) == 0
    capsys.readouterr()
    variants = []
    wrong_as_of = _recover_args(
        proposal, idempotency_key="cut-recover-actors",
        receipt_dir=tmp_path)
    wrong_as_of[wrong_as_of.index("--as-of") + 1] = (
        NOW - timedelta(seconds=1)).isoformat()
    variants.append(wrong_as_of)
    wrong_builder = _recover_args(
        proposal, idempotency_key="cut-recover-actors",
        receipt_dir=tmp_path)
    wrong_builder[wrong_builder.index("--builder") + 1] = "STEWARD-X"
    variants.append(wrong_builder)
    wrong_approver = _recover_args(
        proposal, idempotency_key="cut-recover-actors",
        receipt_dir=tmp_path)
    wrong_approver[wrong_approver.index("--approver") + 1] = "ADMIN-X"
    variants.append(wrong_approver)

    for args in variants:
        assert cli.main(args) == 1
        assert "release_receipt_binding_mismatch" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("statement", "parameters", "expected_code"),
    [
        ("UPDATE qualityreleaseepoch SET payload_sha256 = :value",
         {"value": "0" * 64}, "release_receipt_epoch_corrupt"),
        ("UPDATE qualityreleaseepoch "
         "SET entry_quarantine_days_applied = :value",
         {"value": 15}, "release_receipt_proposal_mismatch"),
    ],
    ids=("payload", "policy"),
)
def test_recover_receipt_detects_tampered_frozen_evidence(
        wired, monkeypatch, capsys, tmp_path,
        statement, parameters, expected_code):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    proposal = _propose(capsys)
    assert cli.main(_approve_args(
        proposal, idempotency_key="cut-recover-tamper")) == 0
    capsys.readouterr()
    with wired.begin() as connection:
        connection.execute(text(statement), parameters)

    assert cli.main(_recover_args(
        proposal, idempotency_key="cut-recover-tamper",
        receipt_dir=tmp_path)) == 1

    assert not list(tmp_path.iterdir())
    assert expected_code in capsys.readouterr().err


def test_recover_receipt_refuses_when_the_frozen_rows_are_missing(
        wired, monkeypatch, capsys, tmp_path):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    proposal = _propose(capsys)
    assert cli.main(_approve_args(
        proposal, idempotency_key="cut-recover-missing-snapshot")) == 0
    capsys.readouterr()
    with wired.begin() as connection:
        connection.execute(text(
            "DELETE FROM qualityreleaseepochrowsnapshot"))

    assert cli.main(_recover_args(
        proposal, idempotency_key="cut-recover-missing-snapshot",
        receipt_dir=tmp_path)) == 1

    assert not list(tmp_path.iterdir())
    assert "research_release_snapshot_corrupt" in capsys.readouterr().err


def test_recover_receipt_never_overwrites_a_different_final_file(
        wired, monkeypatch, capsys, tmp_path):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    proposal = _propose(capsys)
    assert cli.main(_approve_args(
        proposal, idempotency_key="cut-recover-conflict")) == 0
    capsys.readouterr()
    conflict = tmp_path / "quality-release-001.json"
    conflict.write_text('{"different":true}\n', encoding="utf-8")
    conflict.chmod(0o600)
    before = conflict.read_bytes()

    assert cli.main(_recover_args(
        proposal, idempotency_key="cut-recover-conflict",
        receipt_dir=tmp_path)) == 1

    assert conflict.read_bytes() == before
    assert "release_receipt_conflict" in capsys.readouterr().err


def test_recover_receipt_refuses_a_symlink_final_target(
        wired, monkeypatch, capsys, tmp_path):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    proposal = _propose(capsys)
    assert cli.main(_approve_args(
        proposal, idempotency_key="cut-recover-symlink")) == 0
    capsys.readouterr()
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text("do not touch\n", encoding="utf-8")
    unrelated.chmod(0o600)
    final = tmp_path / "quality-release-001.json"
    final.symlink_to(unrelated)

    assert cli.main(_recover_args(
        proposal, idempotency_key="cut-recover-symlink",
        receipt_dir=tmp_path)) == 1

    assert final.is_symlink()
    assert unrelated.read_text(encoding="utf-8") == "do not touch\n"
    assert "release_receipt_unsafe" in capsys.readouterr().err


def test_recover_receipt_is_an_idempotent_noop_when_the_same_final_exists(
        wired, monkeypatch, capsys, tmp_path):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    proposal = _propose(capsys)
    assert cli.main(_approve_args(
        proposal, idempotency_key="cut-recover-already",
        receipt_dir=tmp_path)) == 0
    final = tmp_path / "quality-release-001.json"
    before = final.read_bytes()
    capsys.readouterr()

    assert cli.main(_recover_args(
        proposal, idempotency_key="cut-recover-already",
        receipt_dir=tmp_path)) == 0

    assert final.read_bytes() == before
    assert "已存在且核验一致" in capsys.readouterr().err
    assert len(_epochs(wired)) == 1


def test_recover_receipt_cleans_an_identical_stale_pending_and_fsyncs(
        wired, monkeypatch, capsys, tmp_path):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    proposal = _propose(capsys)
    assert cli.main(_approve_args(
        proposal, idempotency_key="cut-recover-stale-pending",
        receipt_dir=tmp_path)) == 0
    final = tmp_path / "quality-release-001.json"
    pending = tmp_path / (
        ".quality-release-001.json.0123456789abcdef01234567.pending")
    pending.write_bytes(final.read_bytes())
    pending.chmod(0o600)
    fsynced: list[Path] = []
    real_fsync = cli._fsync_directory
    monkeypatch.setattr(
        cli, "_fsync_directory",
        lambda path: (fsynced.append(path), real_fsync(path))[1])
    capsys.readouterr()

    assert cli.main(_recover_args(
        proposal, idempotency_key="cut-recover-stale-pending",
        receipt_dir=tmp_path)) == 0

    assert final.is_file()
    assert not pending.exists()
    assert fsynced == [tmp_path]
    assert "已存在且核验一致" in capsys.readouterr().err


def test_as_of_cannot_regress_behind_the_current_epoch(
        wired, monkeypatch, capsys):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    proposal = _propose(capsys)
    assert cli.main(_approve_args(
        proposal, idempotency_key="cut-as-of-first")) == 0
    capsys.readouterr()

    assert cli.main([
        "--propose", *BUILDER,
        "--as-of", (NOW - timedelta(seconds=1)).isoformat(),
    ]) == 1

    rows = _epochs(wired)
    assert len(rows) == 1 and rows[0].status == "published"
    assert "release_as_of_regressed" in capsys.readouterr().err


def test_precommit_snapshot_failure_rolls_back_a_first_epoch(
        wired, monkeypatch, capsys, tmp_path):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    proposal = _propose(capsys)
    real_publish = quality_release.publish_epoch

    def publish_then_fail(*args, **kwargs):
        real_publish(*args, **kwargs)
        raise RuntimeError("snapshot insert failed")

    monkeypatch.setattr(quality_release, "publish_epoch", publish_then_fail)
    assert cli.main(_approve_args(
        proposal, idempotency_key="cut-precommit-first",
        receipt_dir=tmp_path)) == 1

    assert _epochs(wired) == []
    assert _snapshot_rows(wired) == []
    assert not list(tmp_path.iterdir())
    assert "release_publish_failed" in capsys.readouterr().err


def test_precommit_snapshot_failure_does_not_supersede_the_previous_epoch(
        wired, monkeypatch, capsys, tmp_path):
    _seed(wired, status="completed")
    _stub_build(monkeypatch, _publishable())
    first = _propose(capsys)
    assert cli.main(_approve_args(
        first, idempotency_key="cut-precommit-existing")) == 0
    original_snapshot_rows = len(_snapshot_rows(wired))
    capsys.readouterr()
    second = _propose(capsys, as_of=NOW + timedelta(seconds=1))
    real_publish = quality_release.publish_epoch

    def publish_then_fail(*args, **kwargs):
        real_publish(*args, **kwargs)
        raise RuntimeError("snapshot insert failed")

    monkeypatch.setattr(quality_release, "publish_epoch", publish_then_fail)
    assert cli.main(_approve_args(
        second, idempotency_key="cut-precommit-second",
        receipt_dir=tmp_path)) == 1

    rows = _epochs(wired)
    assert len(rows) == 1
    assert rows[0].status == "published"
    assert rows[0].superseded_at is None
    assert len(_snapshot_rows(wired)) == original_snapshot_rows
    assert not list(tmp_path.iterdir())
    assert "release_publish_failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("writable", "isolation_level", "statements"),
    [
        (False, "REPEATABLE READ", ["SET TRANSACTION READ ONLY"]),
        (True, "SERIALIZABLE", []),
    ],
)
def test_postgres_release_snapshot_contract(
        writable, isolation_level, statements):
    connection = SimpleNamespace(
        statements=[],
        exec_driver_sql=lambda statement: connection.statements.append(statement),
    )
    session = SimpleNamespace(
        in_transaction=lambda: False,
        get_bind=lambda: SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql")),
        connection=lambda **kwargs: (
            setattr(session, "execution_options", kwargs["execution_options"])
            or connection),
    )

    quality_release.begin_release_transaction(session, writable=writable)

    assert session.execution_options == {"isolation_level": isolation_level}
    assert connection.statements == statements


def test_release_snapshot_contract_is_backend_specific_and_rejects_dirty_sessions(
        wired):
    with Session(wired) as session:
        quality_release.begin_release_transaction(session, writable=False)
        session.rollback()
        quality_release.begin_release_transaction(session, writable=True)
        session.rollback()
        session.exec(select(QualityReleaseEpoch)).all()
        with pytest.raises(quality_release.ReleaseSnapshotUnavailable):
            quality_release.begin_release_transaction(session, writable=False)


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
