#!/usr/bin/env python3
"""Bounded, offline audio-volume exercise; never opens application data or SSH.

Produces decodable synthetic (not human/patient) audio, physically writes and
hash-verifies 14 retained copies plus a publish-before-rotation copy, then models
an explicitly assumed eight-week workload. This complements, not replaces,
quality_release_scale's database/row test and capacity_check's observed trend.

ffmpeg/ffprobe are optional development-machine tools, not app dependencies.
No codec/bitrate below is asserted to be the browser's actual chosen bitrate.
The recorder currently requests audio/webm without specifying a bitrate.

Example (the free-space input must come from a fresh, separately recorded read):
    python scripts/audio_volume_capacity.py --receipt /private/audit/audio.json \
      --available-bytes 6500000000 --available-observed-at 2026-09-25T01:00:00Z

The full exercise returns 0 only if all stated scenarios fit, 2 if any is
insufficient, or 3 if no live free-space input was supplied. A completed receipt
keeps physical-copy success separate from capacity. --copy-only returns 0 for
its narrower physical exercise. Neither mode is research/clinical acceptance.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import shutil
import stat
import struct
import subprocess
import tempfile
import time
import wave


MIB = 1024 * 1024
GIB = 1024 * MIB
MAX_WRITTEN_BYTES = 1024 * MIB
KEEP = 14
SOURCE_ROOT = Path(__file__).resolve().parents[1]


class CapacityExerciseError(RuntimeError):
    """A bounded exercise could not substantiate its receipt."""


@dataclass(frozen=True)
class Workload:
    # Engineering sizing assumptions, not a claim that these are approved
    # contents/counts for all eight weeks. 78 and 2 match the mechanical harness.
    subjects: int = 30
    weeks: int = 8
    sessions_per_subject_week: int = 1
    turns_per_session: int = 78
    attempts_per_turn: int = 2
    seconds_per_audio: int = 15
    backup_keep: int = KEEP
    # A retained release tree and an independent restore/candidate copy.
    additional_full_copies: int = 2
    # Explicit unmeasured allowance for DB, exports, logs, and derived files.
    non_audio_growth_per_week: int = 16 * MIB
    # Fresh source measurement for future backups/copies of pre-existing data.
    # Existing retained snapshots are already charged to today's available bytes.
    next_snapshot_baseline_bytes: int = 0
    reserve_bytes: int = 2 * GIB
    filesystem_block_bytes: int = 4096

    def validate(self) -> None:
        positive = (
            self.subjects, self.weeks, self.sessions_per_subject_week,
            self.turns_per_session, self.attempts_per_turn,
            self.seconds_per_audio, self.backup_keep, self.filesystem_block_bytes,
        )
        if any(type(value) is not int or value < 1 for value in positive):
            raise ValueError("workload_counts_must_be_positive_integers")
        if self.weeks > 52 or self.backup_keep > 365 or self.seconds_per_audio > 300:
            raise ValueError("workload_exceeds_bounded_model")
        if any(type(value) is not int or value < 0 for value in (
            self.additional_full_copies, self.non_audio_growth_per_week,
            self.reserve_bytes, self.next_snapshot_baseline_bytes,
        )):
            raise ValueError("workload_allowances_must_be_nonnegative_integers")

    @property
    def files_per_week(self) -> int:
        return (self.subjects * self.sessions_per_subject_week
                * self.turns_per_session * self.attempts_per_turn)


def rounded_bytes(value: int, block: int) -> int:
    return ((value + block - 1) // block) * block


def project_storage(*, sample_bytes: int, workload: Workload,
                    available_bytes: int | None = None) -> dict[str, object]:
    """Model incremental bytes against today's free space, without double count.

    Existing live/backups already consume today's measured disk. We grant no
    speculative credit for rotating them away. Future snapshots contain the
    measured old source baseline and *all new audio accumulated so far*.
    All subjects arrive on the first day
    of each week (conservative early burst). At each day, create before rotating.
    Two extra full copies are budgeted at each potential release/restore point.
    After eight weeks, run 14 quiet days so the backup window fills with the
    final corpus: stopping at day 56 would understate ongoing retention cost.

    Per-file allocation rounds to the supplied server block size. Non-audio
    growth is an explicit allowance, not a measurement or universal upper bound.
    """
    workload.validate()
    if type(sample_bytes) is not int or sample_bytes <= 0:
        raise ValueError("sample_bytes_must_be_positive")
    if available_bytes is not None and (
            type(available_bytes) is not int or available_bytes < 0):
        raise ValueError("available_bytes_must_be_nonnegative")
    per_file = rounded_bytes(sample_bytes, workload.filesystem_block_bytes)
    weekly_audio = per_file * workload.files_per_week
    weekly_growth = weekly_audio + workload.non_audio_growth_per_week
    live = 0
    queue: list[int] = []
    peak = 0
    first_reserve_shortfall_day: int | None = None
    first_exhaustion_day: int | None = None
    timeline: list[dict[str, int]] = []
    training_days = workload.weeks * 7
    for day in range(1, training_days + workload.backup_keep + 1):
        if day <= training_days and (day - 1) % 7 == 0:
            live += weekly_growth
        next_copy = live + workload.next_snapshot_baseline_bytes
        held = next_copy * workload.additional_full_copies
        copy_peak = live + sum(queue) + next_copy + held
        peak = max(peak, copy_peak)
        if available_bytes is not None:
            if first_exhaustion_day is None and copy_peak >= available_bytes:
                first_exhaustion_day = day
            if (first_reserve_shortfall_day is None
                    and copy_peak + workload.reserve_bytes > available_bytes):
                first_reserve_shortfall_day = day
        queue.append(next_copy)
        while len(queue) > workload.backup_keep:
            queue.pop(0)
        timeline.append({
            "day": day, "live_incremental_bytes": live,
            "retained_backup_incremental_bytes": sum(queue),
            "new_snapshot_bytes_including_existing_baseline": next_copy,
            "additional_copy_incremental_bytes": held,
            "pre_rotation_peak_incremental_bytes": copy_peak,
            "post_rotation_incremental_bytes": live + sum(queue) + held,
        })
    required = peak + workload.reserve_bytes
    return {
        "assumptions": asdict(workload),
        "sample_logical_bytes": sample_bytes,
        "projected_allocated_bytes_per_audio": per_file,
        "total_sessions": (workload.subjects * workload.weeks
                           * workload.sessions_per_subject_week),
        "total_audio_files": workload.files_per_week * workload.weeks,
        "total_audio_seconds": (workload.files_per_week * workload.weeks
                                * workload.seconds_per_audio),
        "audio_corpus_bytes": weekly_audio * workload.weeks,
        "non_audio_growth_allowance_bytes": (
            workload.non_audio_growth_per_week * workload.weeks),
        "peak_incremental_bytes": peak,
        "free_bytes_required_including_reserve": required,
        "available_bytes_input": available_bytes,
        "additional_free_bytes_needed": (max(0, required - available_bytes)
                                         if available_bytes is not None else None),
        "capacity_status": ("not_compared_no_live_free_space_input"
                            if available_bytes is None else
                            "insufficient_under_stated_assumptions"
                            if required > available_bytes else
                            "fits_under_stated_assumptions"),
        "first_reserve_shortfall_day": first_reserve_shortfall_day,
        "first_exhaustion_day": first_exhaustion_day,
        "timeline": timeline,
    }


def file_facts(path: Path, *, max_bytes: int = 32 * MIB) -> dict[str, object]:
    meta = path.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
        raise CapacityExerciseError("sample_not_independent_regular_file")
    if meta.st_size > max_bytes:
        raise CapacityExerciseError("sample_exceeds_bounded_fixture_size")
    allocated = getattr(meta, "st_blocks", 0) * 512
    if meta.st_size <= 0 or allocated < meta.st_size:
        raise CapacityExerciseError("sample_sparse_or_allocation_unverified")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(MIB), b""):
            digest.update(chunk)
    return {"bytes": meta.st_size, "allocated_bytes": allocated,
            "sha256": digest.hexdigest()}


def make_pcm_sample(path: Path, seconds: int) -> None:
    """Seeded speech-band noise plus tones, always labelled synthetic, 48k mono."""
    if type(seconds) is not int or not 1 <= seconds <= 300:
        raise ValueError("sample_duration_out_of_bounds")
    rng = random.Random(20260925)
    rate = 48000
    # Exclusive creation prevents overwriting even inside a provided temp root.
    with path.open("xb") as stream:
        with wave.open(stream, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            filtered = 0.0
            for second in range(seconds):
                samples = []
                for position in range(rate):
                    at = second + position / rate
                    filtered = 0.75 * filtered + 0.25 * rng.uniform(-1, 1)
                    envelope = 0.35 + 0.65 * math.sin(2 * math.pi * 2.1 * at) ** 2
                    value = envelope * (
                        0.18 * math.sin(2 * math.pi * 211 * at)
                        + 0.14 * math.sin(2 * math.pi * 607 * at)
                        + 0.40 * filtered)
                    samples.append(int(value * 32767))
                output.writeframesraw(struct.pack(f"<{len(samples)}h", *samples))
        stream.flush()
        os.fsync(stream.fileno())


def make_samples(root: Path, seconds: int) -> list[dict[str, object]]:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise CapacityExerciseError("ffmpeg_and_ffprobe_required_for_offline_exercise")
    source = root / "synthetic-pcm48k-mono.wav"
    make_pcm_sample(source, seconds)
    paths = [(source, "pcm_s16le", "uncompressed_reference")]
    for bitrate in (128000, 256000):
        target = root / f"synthetic-opus-{bitrate}.webm"
        subprocess.run([
            ffmpeg, "-nostdin", "-v", "error", "-i", str(source),
            "-c:a", "libopus", "-b:a", str(bitrate), "-vbr", "off",
            "-fs", str(16 * MIB), "-f", "webm", "-n", str(target),
        ], check=True, capture_output=True, timeout=60)
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        paths.append((target, "opus", f"assumed_cbr_{bitrate}_bps"))
    results = []
    for path, expected_codec, profile in paths:
        probe = subprocess.run([
            ffprobe, "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(path),
        ], check=True, capture_output=True, timeout=30)
        facts = json.loads(probe.stdout)
        streams = facts.get("streams", [])
        duration = float(facts["format"]["duration"])
        if (len(streams) != 1 or streams[0]["codec_name"] != expected_codec
                or streams[0]["codec_type"] != "audio"
                or abs(duration - seconds) > 0.1):
            raise CapacityExerciseError("generated_audio_shape_invalid")
        subprocess.run([
            ffmpeg, "-nostdin", "-v", "error", "-xerror", "-i", str(path),
            "-f", "null", "-",
        ], check=True, capture_output=True, timeout=30)
        results.append({
            "name": path.name, "profile": profile, "codec": expected_codec,
            "duration_seconds": duration,
            "sample_rate": int(streams[0]["sample_rate"]),
            "channels": streams[0]["channels"], "decode_passed": True,
            **file_facts(path),
        })
    return results


def copy_and_verify(source: Path, target: Path, expected_hash: str) -> int:
    """Normal read/write (no clone, sparse seek, hardlink or copyfile fast path)."""
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        for chunk in iter(lambda: incoming.read(MIB), b""):
            outgoing.write(chunk)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    copied = file_facts(target)
    if copied["sha256"] != expected_hash:
        raise CapacityExerciseError("physical_copy_hash_mismatch")
    if (source.stat().st_dev, source.stat().st_ino) == (
            target.stat().st_dev, target.stat().st_ino):
        raise CapacityExerciseError("physical_copy_not_independent")
    return int(copied["allocated_bytes"])


def export_verified_samples(source: Path, destination: Path,
                            samples: list[dict[str, object]]) -> dict[str, object]:
    """Publish an independent codec artifact; clean an interrupted own export."""
    destination.mkdir(mode=0o700)  # Never removes/replaces pre-existing contents.
    try:
        for item in samples:
            copy_and_verify(source / str(item["name"]),
                            destination / str(item["name"]), str(item["sha256"]))
        exported = {
            "schema": "nmu-synthetic-audio-fixtures-v1", "synthetic_audio_only": True,
            "directory": str(destination),
            "generator_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "samples": samples,
            "scope": "codec_generation_only_physical_copy_receipt_is_separate",
        }
        publish_receipt(destination / "synthetic-samples.json", exported)
        return exported
    except BaseException:
        shutil.rmtree(destination)
        raise


def exercise_copies(root: Path, sample: Path, *, target_dataset_bytes: int,
                    materialized_limit_bytes: int) -> dict[str, object]:
    """Own all payloads, retain 14, publish #15/#16 before rotation; held copy too."""
    sample_facts = file_facts(sample)
    count = max(1, math.ceil(target_dataset_bytes / int(sample_facts["bytes"])))
    # live + held + 14 old backups + incoming = 17, with one-off generators
    # included separately by the caller. Metadata allowance is deliberately high.
    predicted = count * int(sample_facts["allocated_bytes"]) * (KEEP + 3)
    predicted += (count * (KEEP + 3) + KEEP + 10) * 4096
    predicted_written = count * int(sample_facts["bytes"]) * (KEEP + 4)
    if max(predicted, predicted_written) > materialized_limit_bytes:
        raise CapacityExerciseError("materialized_budget_would_be_exceeded")
    local_free = shutil.disk_usage(root).free
    if local_free < predicted + 2 * GIB:
        raise CapacityExerciseError("local_free_space_below_budget_and_reserve")
    live = root / "live-synthetic"
    live.mkdir(mode=0o700)
    started = time.monotonic()
    logical_written = 0
    allocated = 0
    peak_allocated = 0
    hashes_verified = 0
    names = [f"synthetic-{index:05d}.webm" for index in range(count)]
    digest = str(sample_facts["sha256"])
    for name in names:
        allocated += copy_and_verify(sample, live / name, digest)
        logical_written += int(sample_facts["bytes"])
        hashes_verified += 1
    held = root / "held-independent-copy"
    held.mkdir(mode=0o700)
    for name in names:
        allocated += copy_and_verify(live / name, held / name, digest)
        logical_written += int(sample_facts["bytes"])
        hashes_verified += 1
    queue: list[tuple[Path, int]] = []
    peaks = []
    for ordinal in range(1, KEEP + 3):
        backup = root / f"backup-{ordinal:02d}"
        backup.mkdir(mode=0o700)
        copy_allocated = 0
        for name in names:
            copy_allocated += copy_and_verify(live / name, backup / name, digest)
            logical_written += int(sample_facts["bytes"])
            hashes_verified += 1
        allocated += copy_allocated
        peak_allocated = max(peak_allocated, allocated)
        if allocated > materialized_limit_bytes:
            raise CapacityExerciseError("materialized_budget_exceeded")
        queue.append((backup, copy_allocated))
        peaks.append({"ordinal": ordinal, "copies_before_rotation": len(queue),
                      "payload_allocated_bytes_before_rotation": allocated})
        # Only generated directories from this invocation; source/receipt untouched.
        while len(queue) > KEEP:
            retired, retired_bytes = queue.pop(0)
            shutil.rmtree(retired)
            allocated -= retired_bytes
    return {
        "status": "passed", "dataset_files": count,
        "dataset_logical_bytes": count * int(sample_facts["bytes"]),
        "dataset_allocated_bytes": count * int(sample_facts["allocated_bytes"]),
        "retained_snapshots": len(queue), "independent_held_copies": 1,
        "incoming_snapshots_written": KEEP + 2,
        "all_copy_hash_checks": hashes_verified,
        "total_logical_bytes_written": logical_written,
        "peak_payload_allocated_bytes": peak_allocated,
        "conservative_preflight_materialized_budget_bytes": predicted,
        "preflight_total_logical_write_budget_bytes": predicted_written,
        "configured_materialized_limit_bytes": materialized_limit_bytes,
        "preflight_local_free_bytes": local_free,
        "wall_seconds": round(time.monotonic() - started, 4),
        "copy_method": "userspace_read_write_fsync_sha256_no_clone_or_hardlink",
        "peaks": peaks,
        "scope": "local_filesystem_only_not_vps_performance_or_restore_acceptance",
    }


def run_exercise(receipt: Path, *, available_bytes: int | None = None,
                 available_observed_at: str | None = None,
                 workload: Workload = Workload(), dataset_bytes: int = 16 * MIB,
                 materialized_limit_bytes: int = 512 * MIB,
                 export_samples: Path | None = None) -> dict[str, object]:
    workload.validate()
    if available_bytes is not None and (
            type(available_bytes) is not int or available_bytes < 0):
        raise ValueError("available_bytes_must_be_nonnegative")
    if (type(dataset_bytes) is not int or dataset_bytes < 1
            or type(materialized_limit_bytes) is not int
            or not 1 <= materialized_limit_bytes <= MAX_WRITTEN_BYTES):
        raise ValueError("exercise_disk_budget_invalid")
    if available_bytes is not None:
        if not available_observed_at:
            raise ValueError("live_free_space_requires_observation_timestamp")
        observation = datetime.fromisoformat(available_observed_at.replace("Z", "+00:00"))
        if observation.utcoffset() is None:
            raise ValueError("observation_timestamp_requires_timezone")
    elif available_observed_at is not None:
        raise ValueError("observation_timestamp_requires_live_free_space")
    receipt = receipt.absolute()
    if receipt.resolve().is_relative_to(SOURCE_ROOT.resolve()):
        raise CapacityExerciseError("receipt_must_be_outside_application_repository")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    if receipt.exists() or receipt.is_symlink():
        raise CapacityExerciseError("receipt_already_exists_refusing_overwrite")
    # Check before generating any audio and reserve the worst PCM + encoded sizes.
    sample_budget = workload.seconds_per_audio * (48000 * 2 + 2 * 32000) + MIB
    if sample_budget * (2 if export_samples else 1) >= materialized_limit_bytes:
        raise CapacityExerciseError("sample_budget_would_exceed_limit")
    if shutil.disk_usage(receipt.parent).free < materialized_limit_bytes + 2 * GIB:
        raise CapacityExerciseError("local_disk_reserve_insufficient")
    with tempfile.TemporaryDirectory(prefix="nmu-audio-volume-", dir=receipt.parent) as raw:
        root = Path(raw)
        samples = make_samples(root, workload.seconds_per_audio)
        exported = None
        if export_samples is not None:
            export_samples = export_samples.absolute()
            if export_samples.resolve().is_relative_to(SOURCE_ROOT.resolve()):
                raise CapacityExerciseError("sample_export_must_be_outside_application_repository")
            exported = export_verified_samples(root, export_samples, samples)
        # The larger compressed profile is used for physical copies. PCM remains
        # a separate fallback comparison, not a claim that browsers produce WAV.
        sample = root / str(samples[-1]["name"])
        copies = exercise_copies(
            root, sample, target_dataset_bytes=dataset_bytes,
            materialized_limit_bytes=(materialized_limit_bytes
                                      - sample_budget * (2 if export_samples else 1)))
        source_paths = (
            "scripts/audio_volume_capacity.py", "scripts/capacity_check.py",
            "harness/quality_release_scale.py", "scripts/vps-backup-daily.sh",
            "web/src/audio/recorder.ts", "web/src/patient/autopilotCaptureWindow.ts",
        )
        source_hashes = {name: hashlib.sha256((SOURCE_ROOT / name).read_bytes()).hexdigest()
                         for name in source_paths}
        facts: dict[str, object] = {
            "schema": "nmu-synthetic-audio-volume-v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "exercise_status": "passed", "source_sha256": source_hashes,
            "local_runtime": {"system": platform.platform(),
                              "python": platform.python_version(),
                              "ffmpeg_version": subprocess.run(
                                  [str(shutil.which("ffmpeg")), "-version"],
                                  check=True, capture_output=True, text=True,
                                  timeout=10).stdout.splitlines()[0]},
            "synthetic_audio_only": True, "patient_audio_read": False,
            "production_written": False, "physical_device_validated": False,
            "available_observed_at": available_observed_at,
            "exported_synthetic_samples": exported,
            "measured_samples": samples, "physical_copy_exercise": copies,
            "model_scenarios": [
                {"profile": item["profile"], **project_storage(
                    sample_bytes=int(item["bytes"]), workload=workload,
                    available_bytes=available_bytes)} for item in samples
            ],
            "limitations": [
                "Synthetic noise/tones; not elderly speech or browser microphone capture.",
                "Opus bitrates are sizing assumptions; browser bitrate is not configured.",
                "Thirty subjects, 78 turns and two attempts each are sizing assumptions.",
                "All eight weeks use the same count, not validated eight-week content.",
                "Non-audio growth and extra copies are explicit allowances, not measurements.",
                "Two extra copies are a conservative release/restore scenario, not routine policy.",
                "No credit for future deletion of pre-existing backups or patient audio.",
                "Local copy timing is not VPS throughput, ASR, concurrency or restore testing.",
                "No retention policy, production configuration or research approval changed.",
            ],
        }
    facts["temporary_payload_removed"] = not root.exists()
    statuses = {item["capacity_status"] for item in facts["model_scenarios"]}
    facts["capacity_status"] = (
        "insufficient_under_stated_assumptions"
        if "insufficient_under_stated_assumptions" in statuses
        else "not_compared_no_live_free_space_input" if available_bytes is None
        else "fits_under_stated_assumptions")
    publish_receipt(receipt, facts)
    return facts


def publish_receipt(receipt: Path, facts: dict[str, object]) -> None:
    """No overwrite and private permissions; caller finishes payload cleanup first."""
    descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(facts, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def run_copy_only(receipt: Path, sample: Path, expected_sha256: str, *,
                  audit_root: Path, manifest: Path, expected_manifest_sha256: str,
                  dataset_bytes: int = 8 * MIB,
                  materialized_limit_bytes: int = 256 * MIB) -> dict[str, object]:
    """Standard-library VPS path: verify uploaded codec fixture, own temp writes.

    The supplied file must previously have been codec-verified on the development
    machine. This run verifies the explicitly supplied hash, not a decoder claim.
    No production data/config is discovered. All generated payloads live below
    the operator's private audit directory and are removed before receipt publish.
    """
    audit_root = audit_root.absolute()
    meta = audit_root.lstat()
    if not stat.S_ISDIR(meta.st_mode) or meta.st_mode & 0o077:
        raise CapacityExerciseError("copy_only_audit_root_must_be_private_directory")
    protected = [Path("/opt/nmu/app")]
    if (SOURCE_ROOT / "pyproject.toml").is_file():
        protected.append(SOURCE_ROOT)
    if any(audit_root.resolve().is_relative_to(path.resolve()) for path in protected):
        raise CapacityExerciseError("copy_only_audit_root_is_application_repository")
    for ancestor in (audit_root.resolve(), *audit_root.resolve().parents):
        if ((ancestor / "pyproject.toml").is_file() and (ancestor / "app").is_dir()
                and (ancestor / "data").is_dir()):
            raise CapacityExerciseError("copy_only_audit_root_is_application_repository")
    for path in (receipt, sample, manifest):
        absolute = path.absolute()
        try:
            relative = absolute.relative_to(audit_root)
        except ValueError as exc:
            raise CapacityExerciseError("copy_only_paths_must_stay_in_audit_root") from exc
        current = audit_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink() or part in (".", ".."):
                raise CapacityExerciseError("copy_only_symlink_or_parent_traversal")
        if not absolute.resolve().is_relative_to(audit_root.resolve()):
            raise CapacityExerciseError("copy_only_paths_must_stay_in_audit_root")
    receipt = receipt.absolute()
    parent = receipt.parent
    if receipt.exists() or receipt.is_symlink():
        raise CapacityExerciseError("receipt_already_exists_refusing_overwrite")
    if (type(dataset_bytes) is not int or dataset_bytes < 1
            or type(materialized_limit_bytes) is not int
            or not 1 <= materialized_limit_bytes <= MAX_WRITTEN_BYTES):
        raise ValueError("exercise_disk_budget_invalid")
    manifest_facts = file_facts(manifest, max_bytes=128 * 1024)
    if manifest_facts["sha256"] != expected_manifest_sha256:
        raise CapacityExerciseError("uploaded_manifest_hash_mismatch")
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    if (manifest_data.get("schema") != "nmu-synthetic-audio-fixtures-v1"
            or manifest_data.get("synthetic_audio_only") is not True):
        raise CapacityExerciseError("uploaded_manifest_not_synthetic_codec_fixture")
    sample_facts = file_facts(sample, max_bytes=16 * MIB)
    if sample_facts["sha256"] != expected_sha256:
        raise CapacityExerciseError("uploaded_sample_hash_mismatch")
    matching = [item for item in manifest_data.get("samples", [])
                if item.get("name") == sample.name
                and item.get("sha256") == expected_sha256
                and item.get("bytes") == sample_facts["bytes"]
                and item.get("decode_passed") is True and item.get("codec") == "opus"]
    if len(matching) != 1:
        raise CapacityExerciseError("uploaded_sample_not_bound_to_codec_manifest")
    before = shutil.disk_usage(parent)
    with tempfile.TemporaryDirectory(prefix="nmu-synthetic-copy-", dir=parent) as raw:
        root = Path(raw)
        copies = exercise_copies(root, sample, target_dataset_bytes=dataset_bytes,
                                 materialized_limit_bytes=materialized_limit_bytes)
    facts = {
        "schema": "nmu-synthetic-audio-copy-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "exercise_status": "passed", "synthetic_audio_only": True,
        "production_data_read_or_written": False,
        "supplied_sample": {"name": sample.name, **sample_facts},
        "externally_verified_codec_fixture_manifest_sha256": expected_manifest_sha256,
        "fixture_generator_script_sha256": manifest_data.get("generator_script_sha256"),
        "codec_decoding_performed_on_this_host": False,
        "physical_copy_exercise": copies,
        "temporary_payload_removed": not root.exists(),
        "local_runtime": {"system": platform.platform(), "python": platform.python_version()},
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "free_bytes_before": before.free,
        "free_bytes_after": shutil.disk_usage(parent).free,
        "scope": "bounded_synthetic_bytes_only_not_application_load_or_backup_restore",
    }
    publish_receipt(receipt, facts)
    return facts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--available-bytes", type=int)
    parser.add_argument("--available-observed-at")
    parser.add_argument("--subjects", type=int, default=30)
    parser.add_argument("--turns-per-session", type=int, default=78)
    parser.add_argument("--attempts-per-turn", type=int, default=2)
    parser.add_argument("--dataset-mib", type=int, default=16)
    parser.add_argument("--materialized-limit-mib", type=int, default=512)
    parser.add_argument("--filesystem-block-bytes", type=int, default=4096)
    parser.add_argument("--next-snapshot-baseline-bytes", type=int, default=0)
    parser.add_argument("--export-samples", type=Path)
    parser.add_argument("--copy-only", action="store_true")
    parser.add_argument("--sample", type=Path)
    parser.add_argument("--sample-sha256")
    parser.add_argument("--audit-root", type=Path)
    parser.add_argument("--sample-manifest", type=Path)
    parser.add_argument("--sample-manifest-sha256")
    args = parser.parse_args()
    if args.copy_only:
        if any(value is None for value in (
            args.sample, args.sample_sha256, args.audit_root,
            args.sample_manifest, args.sample_manifest_sha256,
        )):
            parser.error("--copy-only requires sample, audit-root and manifest paths/hashes")
        facts = run_copy_only(args.receipt, args.sample, args.sample_sha256,
                              audit_root=args.audit_root, manifest=args.sample_manifest,
                              expected_manifest_sha256=args.sample_manifest_sha256,
                              dataset_bytes=args.dataset_mib * MIB,
                              materialized_limit_bytes=args.materialized_limit_mib * MIB)
        print(json.dumps({"receipt": str(args.receipt.absolute()),
                          "exercise_status": facts["exercise_status"],
                          "temporary_payload_removed": facts["temporary_payload_removed"]}))
        return 0
    if any(value is not None for value in (
        args.sample, args.sample_sha256, args.audit_root,
        args.sample_manifest, args.sample_manifest_sha256,
    )):
        parser.error("sample/manifest/audit-root arguments require --copy-only")
    facts = run_exercise(
        args.receipt, available_bytes=args.available_bytes,
        available_observed_at=args.available_observed_at,
        workload=Workload(subjects=args.subjects, turns_per_session=args.turns_per_session,
                          attempts_per_turn=args.attempts_per_turn,
                          filesystem_block_bytes=args.filesystem_block_bytes,
                          next_snapshot_baseline_bytes=args.next_snapshot_baseline_bytes),
        dataset_bytes=args.dataset_mib * MIB,
        materialized_limit_bytes=args.materialized_limit_mib * MIB,
        export_samples=args.export_samples)
    print(json.dumps({
        "receipt": str(args.receipt.absolute()), "exercise_status": facts["exercise_status"],
        "capacity_status": facts["capacity_status"],
        "capacity_statuses": [item["capacity_status"] for item in facts["model_scenarios"]],
        "temporary_payload_removed": facts["temporary_payload_removed"],
    }, ensure_ascii=False))
    return {"fits_under_stated_assumptions": 0,
            "insufficient_under_stated_assumptions": 2,
            "not_compared_no_live_free_space_input": 3}[str(facts["capacity_status"])]


if __name__ == "__main__":
    raise SystemExit(main())
