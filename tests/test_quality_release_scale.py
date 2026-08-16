"""Permanent acceptance for the isolated quality-release scale harness."""
from __future__ import annotations

import math
import stat

import pytest

from harness import quality_release_scale as scale


def test_full_profile_is_exactly_the_mechanical_240_session_contract():
    profile = scale.FULL_PROFILE

    assert profile.session_count == 240
    assert profile.subject_count == 30
    assert profile.expected_evidence_rows == 158_400
    assert profile.expected_audio_files == 33_600
    assert profile.expected_turn_rows == 16_800
    assert profile.expected_turn_pages == 17
    assert scale.ITEMS_PER_WEEK2_SESSION == 30
    assert scale.TURNS_PER_WEEK2_SESSION == 70
    assert scale.EVIDENCE_ROWS_PER_SESSION == 660


def test_http_429_backoff_retries_the_same_cursor_without_a_server():
    requested: list[str | None] = []
    slept: list[float] = []
    replies = iter((
        scale.HttpClientResponse(200, {}, {
            "rows": [{"row": 1}],
            "has_more": True,
            "next_cursor": "cursor-2",
        }),
        scale.HttpClientResponse(429, {"Retry-After": "1.5"}),
        scale.HttpClientResponse(200, {}, {
            "rows": [{"row": 2}],
            "has_more": False,
            "next_cursor": None,
        }),
    ))

    def request(cursor):
        requested.append(cursor)
        return next(replies)

    result = scale.collect_http_pages_with_backoff(
        request, sleep=slept.append, max_retries=2)

    assert requested == [None, "cursor-2", "cursor-2"]
    assert slept == [1.5]
    assert [row["row"] for row in result.rows] == [1, 2]
    assert result.page_count == 2
    assert result.retry_count == 1


def test_http_429_backoff_fails_closed_on_invalid_retry_after():
    def request(_cursor):
        return scale.HttpClientResponse(429, {"Retry-After": "61"})

    with pytest.raises(
        scale.ScaleAcceptanceError, match="http_429_retry_after_invalid"
    ):
        scale.collect_http_pages_with_backoff(
            request, sleep=lambda _delay: None)


def test_smoke_profile_runs_the_real_frozen_release_chain(tmp_path):
    receipt = scale.run_profile(
        scale.SMOKE_PROFILE, root=tmp_path / "quality-scale")

    assert receipt["status"] == "passed"
    assert receipt["scope"] == {
        "label": "mechanical_capacity_repeated_valid_week2_contracts",
        "week2_contract_repetitions": 4,
        "eight_week_content_complete": False,
        "clinical_study_readiness_claimed": False,
        "database": "temporary_migrated_sqlite",
        "audio_root": "temporary_private_directory",
        "repository_data_touched": False,
        "representative_audio_volume": False,
    }
    assert receipt["schema_version"] == "quality-release-scale-receipt.v2"
    assert receipt["run_at_utc"].endswith("Z")
    assert len(receipt["source_identity"]["git_revision"]) == 40
    assert len(receipt["source_identity"]["selected_source_tree_sha256"]) == 64
    assert set(receipt["source_identity"]["selected_file_sha256"]) == set(
        scale._source_identity_files())
    assert receipt["runtime_fingerprint"]["python"]
    assert receipt["runtime_fingerprint"]["sqlite"]
    assert receipt["cohort"] == {
        "subjects": 2,
        "sessions": 4,
        "week_numbers": [2],
        "items_per_session": 30,
        "turns_per_session": 70,
    }
    assert receipt["evidence"]["rows_per_session"] == 660
    assert receipt["evidence"]["watermark_rows_per_session"] == 660
    assert receipt["evidence"]["total_rows"] == 2_640
    assert receipt["evidence"]["database_counts"] == {
        "items": 120,
        "turns": 280,
        "attempts": 560,
        "audios": 560,
        "receipts": 560,
        "interactions": 280,
        "revisions": 280,
        "pause_receipts": 0,
        "control_events": 0,
    }
    assert receipt["snapshot"]["subjects"] == 2
    assert receipt["snapshot"]["sessions"] == 4
    assert receipt["snapshot"]["turns"] == 280
    assert receipt["snapshot"]["persisted_rows"] == 286
    assert receipt["io"]["audio_directory_scans"] == 1
    assert receipt["io"]["audio_hashed_files"] == 560
    assert receipt["io"]["audio_hashed_bytes"] > 0
    assert receipt["io"]["average_audio_bytes_per_file"] > 0
    assert 0 < receipt["io"]["sql_select_count"] <= 410
    assert receipt["frozen_turn_pagination"] == {
        "page_size": 64,
        "page_count": math.ceil(280 / 64),
        "unique_rows": 280,
        "missing_rows": 0,
        "duplicate_rows": 0,
        "cursor_deterministic": True,
        "body_deterministic": True,
    }
    assert receipt["http_429_client"] == {
        "tested_without_server": True,
        "retry_count": 1,
        "retry_delays_seconds": [0.25],
        "same_cursor_retried_before_advance": True,
    }


def test_full_240_session_profile_is_a_permanent_release_regression():
    receipt = scale.run_profile(scale.FULL_PROFILE)

    assert receipt["status"] == "passed"
    assert receipt["scope"]["eight_week_content_complete"] is False
    assert receipt["cohort"]["subjects"] == 30
    assert receipt["cohort"]["sessions"] == 240
    assert receipt["evidence"]["total_rows"] == 158_400
    assert receipt["snapshot"]["turns"] == 16_800
    assert receipt["io"]["audio_directory_scans"] == 1
    assert receipt["frozen_turn_pagination"]["page_count"] == 17
    assert receipt["frozen_turn_pagination"]["missing_rows"] == 0
    assert receipt["frozen_turn_pagination"]["duplicate_rows"] == 0
    assert receipt["frozen_turn_pagination"]["body_deterministic"] is True


def test_receipt_writer_requires_a_private_directory_and_never_overwrites(
        tmp_path):
    private = tmp_path / "receipts"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    target = private / "scale.json"
    payload = {"schema_version": "quality-release-scale-receipt.v2",
               "status": "passed"}

    assert scale._write_receipt(target, payload) == target
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text(encoding="utf-8").endswith("\n")
    with pytest.raises(scale.ScaleAcceptanceError, match="receipt_write_failed"):
        scale._write_receipt(target, payload)
    assert target.read_text(encoding="utf-8").endswith("\n")


def test_source_identity_closes_over_runtime_content_and_migrations():
    selected = set(scale._source_identity_files())

    assert {
        "app/ai_quality_service.py",
        "app/audio_store.py",
        "app/content.py",
        "app/research_dataset.py",
        "app/quality_release.py",
        "harness/__init__.py",
        "content/item_bank_index.json",
        "content/item_bank_v1.json",
        "content/autopilot_protocol_v1.json",
        "alembic/env.py",
        "alembic/versions/6f2a9c4d8e17_frozen_research_row_snapshot.py",
    } <= selected
    assert all(path != "data" and not path.startswith("data/") for path in selected)


def test_source_identity_marks_a_pre_run_tracked_deletion_dirty(monkeypatch):
    selected = scale._source_identity_files()
    assert "app/content.py" in selected
    monkeypatch.setattr(
        scale,
        "_source_identity_files",
        lambda: tuple(path for path in selected if path != "app/content.py"),
    )

    identity = scale._source_identity()

    assert identity["selected_files_dirty"] is True
    assert "app/content.py" not in identity["selected_file_sha256"]


def test_receipt_writer_never_publishes_a_partial_target(tmp_path, monkeypatch):
    private = tmp_path / "receipts"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    target = private / "scale.json"
    original_write = scale.os.write
    calls = 0

    def interrupted_write(fd, value):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(fd, value[:5])
        raise OSError("simulated interrupted receipt write")

    monkeypatch.setattr(scale.os, "write", interrupted_write)

    with pytest.raises(scale.ScaleAcceptanceError, match="receipt_write_failed"):
        scale._write_receipt(target, {"status": "passed"})

    assert not target.exists()
    assert list(private.iterdir()) == []


def test_full_cli_refuses_to_run_without_a_durable_receipt(capsys):
    with pytest.raises(SystemExit) as caught:
        scale.main(["--profile", "full"])

    assert caught.value.code == 2
    assert "--receipt" in capsys.readouterr().err


def test_full_cli_refuses_a_dirty_source_closure(
        tmp_path, monkeypatch, capsys):
    private = tmp_path / "receipts"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    target = private / "scale.json"
    monkeypatch.setattr(scale, "run_profile", lambda _profile: {
        "source_identity": {"selected_files_dirty": True},
    })

    assert scale.main([
        "--profile", "full", "--receipt", str(target),
    ]) == 1

    output = capsys.readouterr().out
    assert '"code": "source_identity_dirty"' in output
    assert not target.exists()
