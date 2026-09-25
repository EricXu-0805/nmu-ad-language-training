"""Capacity regressions: independent small oracle, real bytes, no write on refusal."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import shutil
import struct
import subprocess
import sys
from types import SimpleNamespace
import wave

import pytest

from scripts import audio_volume_capacity as capacity


def small_workload(**changes):
    return replace(capacity.Workload(
        subjects=1, weeks=2, sessions_per_subject_week=1, turns_per_session=1,
        attempts_per_turn=1, seconds_per_audio=1, backup_keep=2,
        additional_full_copies=0, non_audio_growth_per_week=0,
        reserve_bytes=10, filesystem_block_bytes=1,
    ), **changes)


def test_weekly_arrivals_keep_old_backups_until_new_copy_is_complete():
    result = capacity.project_storage(sample_bytes=100, workload=small_workload(),
                                      available_bytes=705)
    days = result["timeline"]
    # One 100-byte file in week 1; second arrives day 8. Existing snapshots
    # are still 100 each. At day 10 two 200-byte snapshots + live + new = 800.
    assert [days[index]["pre_rotation_peak_incremental_bytes"]
            for index in (0, 1, 2, 7, 8, 9)] == [200, 300, 400, 600, 700, 800]
    assert days[7]["post_rotation_incremental_bytes"] == 500
    assert result["peak_incremental_bytes"] == 800
    assert result["free_bytes_required_including_reserve"] == 810
    assert result["first_reserve_shortfall_day"] == 9
    assert result["first_exhaustion_day"] == 10
    assert result["additional_free_bytes_needed"] == 105


def test_backup_window_continues_after_training_and_budgets_release_copies():
    result = capacity.project_storage(sample_bytes=100, workload=small_workload(
        weeks=1, backup_keep=14, additional_full_copies=2))
    assert len(result["timeline"]) == 21
    assert result["timeline"][6]["pre_rotation_peak_incremental_bytes"] == 1000
    # Steady state has live + 14 retained + 1 incoming + 2 independent copies.
    assert result["peak_incremental_bytes"] == 1800
    assert result["timeline"][-1]["retained_backup_incremental_bytes"] == 1400


def test_future_backups_and_extra_copies_include_already_existing_source():
    result = capacity.project_storage(sample_bytes=1, workload=small_workload(
        weeks=1, backup_keep=14, additional_full_copies=2,
        next_snapshot_baseline_bytes=capacity.GIB, reserve_bytes=0),
        available_bytes=2 * capacity.GIB)
    assert result["capacity_status"] == "insufficient_under_stated_assumptions"
    assert result["first_exhaustion_day"] == 1
    # Existing 1 GiB live file is already charged to free space. The first new
    # backup and two independent copies require 3 GiB, plus four 1-byte increments.
    assert result["timeline"][0]["pre_rotation_peak_incremental_bytes"] == (
        3 * capacity.GIB + 4)
    # No old-retirement credit, so all 15 new backups are charged at peak.
    assert result["peak_incremental_bytes"] == 17 * capacity.GIB + 18


def test_known_free_space_is_not_charged_for_already_existing_backups():
    result = capacity.project_storage(sample_bytes=1, workload=small_workload(
        weeks=1, backup_keep=1, reserve_bytes=0), available_bytes=3)
    assert result["free_bytes_required_including_reserve"] == 3
    assert result["additional_free_bytes_needed"] == 0
    assert result["capacity_status"] == "fits_under_stated_assumptions"
    # Exactly full without reserve is distinguished from a failed reserve check.
    assert result["first_exhaustion_day"] == 2


def test_block_rounding_and_non_audio_allowance_are_both_in_every_copy():
    result = capacity.project_storage(sample_bytes=4097, workload=small_workload(
        weeks=1, backup_keep=1, reserve_bytes=0,
        filesystem_block_bytes=4096, non_audio_growth_per_week=1024))
    assert result["audio_corpus_bytes"] == 8192
    assert result["non_audio_growth_allowance_bytes"] == 1024
    assert result["peak_incremental_bytes"] == 27648  # 3 * (8192 + 1024)
    assert result["capacity_status"] == "not_compared_no_live_free_space_input"


def test_eight_week_count_is_explicit_assumption_not_mechanical_placeholder_size():
    result = capacity.project_storage(sample_bytes=240000, workload=capacity.Workload())
    assert result["total_sessions"] == 240
    assert result["total_audio_files"] == 37440
    assert result["total_audio_seconds"] == 561600
    assert result["audio_corpus_bytes"] > 8 * capacity.GIB
    assert result["peak_incremental_bytes"] > 140 * capacity.GIB


@pytest.mark.parametrize("changes", [
    {"subjects": 0}, {"weeks": 53}, {"subjects": True},
    {"additional_full_copies": -1}, {"reserve_bytes": -1},
    {"filesystem_block_bytes": 0}, {"non_audio_growth_per_week": -1},
])
def test_invalid_workload_never_silently_turns_into_zero_capacity(changes):
    with pytest.raises(ValueError):
        capacity.project_storage(sample_bytes=100, workload=small_workload(**changes))


def test_pcm_is_decodable_non_silent_fully_written_audio(tmp_path):
    target = tmp_path / "synthetic.wav"
    capacity.make_pcm_sample(target, 1)
    with wave.open(str(target), "rb") as stream:
        assert (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(),
                stream.getnframes()) == (1, 2, 48000, 48000)
        values = struct.unpack("<48000h", stream.readframes(48000))
    assert max(values) > 3000 and min(values) < -3000
    assert len(set(values)) > 1000
    facts = capacity.file_facts(target)
    assert facts["bytes"] == 96044
    assert facts["allocated_bytes"] >= 96044
    with pytest.raises(FileExistsError):
        capacity.make_pcm_sample(target, 1)


def test_physical_copies_retain_fourteen_and_hash_before_retiring(tmp_path):
    source = tmp_path / "synthetic.webm"
    source.write_bytes(bytes(range(256)) * 32)
    facts = capacity.exercise_copies(tmp_path, source, target_dataset_bytes=16000,
                                    materialized_limit_bytes=capacity.MIB)
    backups = sorted(tmp_path.glob("backup-*"))
    assert [path.name for path in backups] == [f"backup-{i:02d}" for i in range(3, 17)]
    assert facts["retained_snapshots"] == 14
    assert facts["all_copy_hash_checks"] == 36  # two files * (live + held + 16)
    assert facts["total_logical_bytes_written"] == 36 * 8192
    assert facts["peaks"][-1]["copies_before_rotation"] == 15
    all_files = [source, *[path for path in tmp_path.rglob("*") if path.is_file()
                          and path != source]]
    assert len({path.stat().st_ino for path in all_files}) == len(all_files)
    assert all(path.stat().st_blocks * 512 >= path.stat().st_size for path in all_files)
    assert all(hashlib.sha256(path.read_bytes()).digest()
               == hashlib.sha256(source.read_bytes()).digest() for path in all_files)


def test_tiny_budget_refuses_before_any_copy(tmp_path):
    source = tmp_path / "synthetic.webm"
    source.write_bytes(b"nonsparse" * 2000)
    with pytest.raises(capacity.CapacityExerciseError, match="budget_would"):
        capacity.exercise_copies(tmp_path, source, target_dataset_bytes=capacity.MIB,
                                 materialized_limit_bytes=capacity.MIB)
    assert list(tmp_path.iterdir()) == [source]


def test_local_disk_reserve_refuses_before_any_copy(tmp_path, monkeypatch):
    source = tmp_path / "synthetic.webm"
    source.write_bytes(b"nonsparse" * 2000)
    monkeypatch.setattr(capacity.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    with pytest.raises(capacity.CapacityExerciseError, match="local_free_space"):
        capacity.exercise_copies(tmp_path, source, target_dataset_bytes=16000,
                                 materialized_limit_bytes=capacity.MIB)
    assert list(tmp_path.iterdir()) == [source]


def test_symlink_and_hardlink_cannot_pass_physical_measurement(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"audio" * 1000)
    symlink = tmp_path / "symlink"
    symlink.symlink_to(source)
    with pytest.raises(capacity.CapacityExerciseError, match="independent_regular"):
        capacity.file_facts(symlink)
    hardlink = tmp_path / "hardlink"
    hardlink.hardlink_to(source)
    with pytest.raises(capacity.CapacityExerciseError, match="independent_regular"):
        capacity.file_facts(hardlink)


def test_corrupt_copy_cannot_be_accepted(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"actual-audio" * 1000)
    with pytest.raises(capacity.CapacityExerciseError, match="hash_mismatch"):
        capacity.copy_and_verify(source, tmp_path / "copy", "0" * 64)


def test_receipt_refuses_overwrite_or_missing_live_timestamp(tmp_path):
    receipt = tmp_path / "receipt.json"
    receipt.write_text("existing evidence")
    with pytest.raises(capacity.CapacityExerciseError, match="refusing_overwrite"):
        capacity.run_exercise(receipt)
    assert receipt.read_text() == "existing evidence"
    with pytest.raises(ValueError, match="requires_observation_timestamp"):
        capacity.run_exercise(tmp_path / "new.json", available_bytes=10)
    assert not (tmp_path / "new.json").exists()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="offline audio codec tools are not production dependencies")
def test_codec_samples_decode_and_receipt_cleans_owned_temporary_files(tmp_path):
    receipt = tmp_path / "receipt.json"
    facts = capacity.run_exercise(
        receipt, workload=small_workload(), dataset_bytes=40000,
        materialized_limit_bytes=16 * capacity.MIB)
    assert facts["synthetic_audio_only"] is True
    assert facts["physical_device_validated"] is False
    assert facts["temporary_payload_removed"] is True
    assert {row["codec"] for row in facts["measured_samples"]} == {"opus", "pcm_s16le"}
    assert all(row["decode_passed"] for row in facts["measured_samples"])
    assert list(tmp_path.iterdir()) == [receipt]
    assert receipt.stat().st_mode & 0o777 == 0o600


def test_receipt_cannot_be_written_inside_application_repository():
    with pytest.raises(capacity.CapacityExerciseError, match="outside_application"):
        capacity.run_exercise(capacity.SOURCE_ROOT / "data" / "capacity-proof.json")


@pytest.mark.parametrize("runtime_subprocess", [False, True], ids=["host-runtime", "lazy-runtime"])
def test_copy_only_runs_without_codec_tools_and_verifies_transferred_sample(
        tmp_path, monkeypatch, runtime_subprocess):
    tmp_path.chmod(0o700)
    sample = tmp_path / "uploaded-synthetic.webm"
    sample.write_bytes(bytes(range(256)) * 100)
    digest = hashlib.sha256(sample.read_bytes()).hexdigest()
    manifest = tmp_path / "synthetic-samples.json"
    manifest.write_text(json.dumps({
        "schema": "nmu-synthetic-audio-fixtures-v1", "synthetic_audio_only": True,
        "samples": [{"name": sample.name, "sha256": digest,
                     "bytes": sample.stat().st_size, "decode_passed": True, "codec": "opus"}],
    }))
    manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    args = {"audit_root": tmp_path, "manifest": manifest,
            "expected_manifest_sha256": manifest_digest}
    if runtime_subprocess:
        # On Linux platform.platform() may lazily call subprocess.check_output
        # for uname. Exercise that dependency even on hosts with warm caches.
        def runtime_description():
            return subprocess.check_output(
                [sys.executable, "-c", "print('runtime-probe-via-stdlib')"],
                text=True,
            ).strip()
        monkeypatch.setattr(capacity, "platform", SimpleNamespace(
            platform=runtime_description, python_version=capacity.platform.python_version,
        ))
    # This mode must not invoke a codec process or discover application data.
    def no_process(*args, **kwargs):
        raise AssertionError("copy-only unexpectedly launched external tool")
    # Replace only the tool's binding, not the shared stdlib module that the
    # platform runtime probe also uses. Direct codec invocations still fail.
    monkeypatch.setattr(capacity, "subprocess", SimpleNamespace(run=no_process))
    receipt = tmp_path / "copy-result.json"
    facts = capacity.run_copy_only(receipt, sample, digest, dataset_bytes=40000,
                                   materialized_limit_bytes=2 * capacity.MIB, **args)
    assert facts["codec_decoding_performed_on_this_host"] is False
    if runtime_subprocess:
        assert facts["local_runtime"]["system"] == "runtime-probe-via-stdlib"
    assert facts["temporary_payload_removed"] is True
    assert facts["physical_copy_exercise"]["total_logical_bytes_written"] < 2 * capacity.MIB
    assert set(tmp_path.iterdir()) == {receipt, sample, manifest}
    with pytest.raises(capacity.CapacityExerciseError, match="uploaded_sample_hash"):
        capacity.run_copy_only(tmp_path / "wrong.json", sample, "0" * 64, **args)
    assert not (tmp_path / "wrong.json").exists()
    with pytest.raises(capacity.CapacityExerciseError, match="stay_in_audit_root"):
        capacity.run_copy_only(tmp_path.parent / "escape.json", sample, digest, **args)
    alias = tmp_path / "alias"
    alias.symlink_to(sample)
    with pytest.raises(capacity.CapacityExerciseError, match="symlink"):
        capacity.run_copy_only(tmp_path / "link.json", alias, digest, **args)


def test_interrupted_fixture_export_cleans_only_new_export(tmp_path, monkeypatch):
    source = tmp_path / "generated"
    source.mkdir()
    samples = []
    for name in ("first", "second"):
        path = source / name
        path.write_bytes(name.encode() * 1000)
        samples.append({"name": name, **capacity.file_facts(path)})
    original = capacity.copy_and_verify
    def fail_second(source, target, digest):
        if target.name == "second":
            raise OSError("injected disk error")
        return original(source, target, digest)
    monkeypatch.setattr(capacity, "copy_and_verify", fail_second)
    destination = tmp_path / "new-export"
    with pytest.raises(OSError, match="injected"):
        capacity.export_verified_samples(source, destination, samples)
    assert not destination.exists()
    assert len(list(source.iterdir())) == 2
    with pytest.raises(FileExistsError):
        capacity.export_verified_samples(source, source, samples)
    assert len(list(source.iterdir())) == 2


def test_oversized_fixture_refuses_before_hashing(tmp_path, monkeypatch):
    sample = tmp_path / "too-big"
    sample.write_bytes(b"nonsparse" * 100)
    def no_read(*args, **kwargs):
        raise AssertionError("refused fixture was read")
    monkeypatch.setattr(type(sample), "open", no_read)
    with pytest.raises(capacity.CapacityExerciseError, match="exceeds_bounded"):
        capacity.file_facts(sample, max_bytes=100)


@pytest.mark.parametrize(("status", "expected_exit"), [
    ("fits_under_stated_assumptions", 0),
    ("insufficient_under_stated_assumptions", 2),
    ("not_compared_no_live_free_space_input", 3),
])
def test_cli_never_reports_green_gate_for_insufficient_or_uncompared_capacity(
        monkeypatch, tmp_path, status, expected_exit):
    monkeypatch.setattr(capacity, "run_exercise", lambda *args, **kwargs: {
        "exercise_status": "passed", "capacity_status": status,
        "model_scenarios": [{"capacity_status": status}], "temporary_payload_removed": True,
    })
    monkeypatch.setattr("sys.argv", ["audio-volume", "--receipt", str(tmp_path / "result")])
    assert capacity.main() == expected_exit
