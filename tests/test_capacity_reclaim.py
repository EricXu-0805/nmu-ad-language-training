from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "capacity_reclaim",
    Path(__file__).resolve().parents[1] / "scripts" / "capacity_reclaim.py")
assert _SPEC is not None and _SPEC.loader is not None
reclaim = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reclaim)


def _write(path: Path, payload: bytes = b"x" * 5000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _tree(tmp_path: Path) -> Path:
    """仿 VPS /opt/nmu 布局的小树：P0、Tier B、必须保留的 live 与 daily 各放几样。"""
    root = tmp_path / "vps"
    nmu = root / "opt" / "nmu"
    _write(nmu / "backups" / "app-2026-09-26.db")
    _write(nmu / "backups" / "audio-2026-09-26.tgz")
    _write(nmu / "backups" / "backup.log", b"[2026-10-01 19:34:19] ok snapshot=20261001-193419\n")
    _write(nmu / "backups" / "daily" / "20261001-193419" / "app.db")
    _write(nmu / "app-before-deploy-20260806-021813.tar.gz")
    _write(nmu / "app-before-deploy-20260820-170248-语法错疑含data勿用.tar.gz")
    _write(nmu / "dist-stale-20260913-093018" / "assets" / "index.js")
    _write(nmu / "app-candidate-20260924-e510a09" / "app" / "main.py")
    _write(nmu / "space-maintenance-20260924" / "sing-box-access-pre-20260924.log.gz")
    _write(nmu / "space-maintenance-20260924" / "cold-archive" / "20260907-5684a3a.tar.gz")
    _write(nmu / "space-maintenance-20260924" / "caches.json", b"{}")
    _write(nmu / "releases-private" / "20260924-1ab262a" / "rotation-held"
           / "20260918-193753" / "app.db")
    _write(nmu / "maintenance-20260924-kernel" / "rotation-held" / "20260917-193000" / "app.db")
    _write(nmu / "maintenance-20260924-kernel" / "backup.done.json", b"{}")
    _write(nmu / "python3.14" / "bin" / "python3.14")
    _write(nmu / "venv" / "pyvenv.cfg", b"home = /opt/nmu/python3.14/bin\n")
    live_tts = _write(nmu / "app" / "data" / "tts-cache" / "prompt.wav", b"t" * 40_000)
    old_tree = nmu / "app-before-20260924-ce314df"
    _write(old_tree / "data" / "audio" / "aud-1.webm", b"a" * 9000)
    (old_tree / "data" / "tts-cache").mkdir(parents=True)
    os.link(live_tts, old_tree / "data" / "tts-cache" / "prompt.wav")
    _write(root / "var" / "log" / "btmp.1")
    _write(root / "var" / "log" / "btmp")
    _write(root / "var" / "cache" / "apt" / "archives" / "linux-image.deb", b"d" * 7000)
    return root


def _rows(root: Path) -> dict[str, dict[str, object]]:
    return {Path(row["path"]).relative_to(root).as_posix(): row
            for row in reclaim.candidates(root)}


def test_dry_run_lists_p0_and_tier_b_with_category_bytes_and_participant_flag(tmp_path):
    root = _tree(tmp_path)

    rows = _rows(root)

    assert {name: (row["tier"], row["participant_data"]) for name, row in rows.items()} == {
        "opt/nmu/backups/app-2026-09-26.db": ("P0", True),
        "opt/nmu/backups/audio-2026-09-26.tgz": ("P0", True),
        "opt/nmu/app-before-deploy-20260806-021813.tar.gz": ("P0", False),
        "opt/nmu/dist-stale-20260913-093018": ("P0", False),
        "opt/nmu/app-candidate-20260924-e510a09": ("P0", False),
        "opt/nmu/space-maintenance-20260924/sing-box-access-pre-20260924.log.gz": ("P0", False),
        "var/log/btmp.1": ("P0", False),
        "opt/nmu/app-before-deploy-20260820-170248-语法错疑含data勿用.tar.gz": ("B", True),
        "opt/nmu/space-maintenance-20260924/cold-archive": ("B", True),
        "opt/nmu/releases-private/20260924-1ab262a": ("B", True),
        "opt/nmu/maintenance-20260924-kernel/rotation-held": ("B", True),
        "opt/nmu/app-before-20260924-ce314df": ("B", True),
    }
    assert all(row["category"] and row["allocated_bytes"] > 0 for row in rows.values())
    # live 的语音缓存仍链着这棵旧树：删它只释放旧树自己的那份录音和目录。
    old_tree = rows["opt/nmu/app-before-20260924-ce314df"]
    shared = root / "opt/nmu/app/data/tts-cache/prompt.wav"
    assert old_tree["allocated_bytes"] - old_tree["reclaimable_bytes"] == (
        shared.stat().st_blocks * 512)


def test_cli_report_is_read_only_and_names_the_apt_cache_as_advice(tmp_path, capsys):
    root = _tree(tmp_path)
    before = sorted(path.relative_to(root) for path in root.rglob("*"))

    assert reclaim.main(["--root", str(root)]) == 0
    report = capsys.readouterr().out

    assert sorted(path.relative_to(root) for path in root.rglob("*")) == before
    assert "apt-get clean" in report
    assert str(root / "opt/nmu/backups/app-2026-09-26.db") in report
    assert not (root / "opt/nmu/backups/capacity-reclaim.log").exists()
    assert reclaim.main(["--root", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {row["tier"] for row in payload["candidates"]} == {"P0", "B"}
    assert payload["apt_cache_bytes"] > 0


def test_delete_removes_only_the_named_candidates_and_logs_each(tmp_path, capsys):
    root = _tree(tmp_path)
    targets = [root / "opt/nmu/dist-stale-20260913-093018", root / "var/log/btmp.1"]

    code = reclaim.main(["--root", str(root),
                         *(argument for target in targets
                           for argument in ("--delete", str(target)))])

    assert code == 0, capsys.readouterr()
    assert not any(target.exists() for target in targets)
    assert (root / "opt/nmu/app-before-deploy-20260806-021813.tar.gz").exists()
    assert (root / "var/log/btmp").exists()
    log = root / "opt/nmu/backups/capacity-reclaim.log"
    assert log.stat().st_mode & 0o777 == 0o600
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all(" removed tier=P0 " in line for line in lines)
    assert any(line.endswith(f"path={targets[1]}") for line in lines)


def test_participant_data_needs_explicit_confirmation(tmp_path):
    root = _tree(tmp_path)
    target = root / "opt/nmu/backups/app-2026-09-26.db"

    assert reclaim.main(["--root", str(root), "--delete", str(target)]) == 1
    assert target.exists()

    assert reclaim.main(["--root", str(root), "--delete", str(target),
                         "--confirm-participant-data"]) == 0
    assert not target.exists()
    log = (root / "opt/nmu/backups/capacity-reclaim.log").read_text(encoding="utf-8")
    assert "refused reason=participant_data_unconfirmed" in log
    assert " removed tier=P0 category=old_cron_db_copy participant=yes " in log


@pytest.mark.parametrize("relative", [
    "opt/nmu/app",
    "opt/nmu/app/data/tts-cache/prompt.wav",
    "opt/nmu/backups/daily",
    "opt/nmu/backups/daily/20261001-193419",
    "opt/nmu/backups",
    "opt/nmu/python3.14",
    "opt/nmu/venv",
    "opt/nmu/space-maintenance-20260924/caches.json",
    "opt/nmu/maintenance-20260924-kernel",
    "var/log/btmp",
    "var/cache/apt/archives",
    "var/cache/apt/archives/linux-image.deb",
    "opt/nmu/no-such-candidate.tar.gz",
])
def test_anything_that_is_not_a_listed_candidate_is_refused_and_nothing_is_deleted(
        relative, tmp_path):
    root = _tree(tmp_path)
    allowed = root / "var/log/btmp.1"
    target = root / relative
    existed = target.exists()

    code = reclaim.main(["--root", str(root), "--confirm-participant-data",
                         "--delete", str(allowed), "--delete", str(target)])

    assert code == 1
    assert allowed.exists()  # 任何一个点名被拒，整批都不删
    assert target.exists() == existed
    log = (root / "opt/nmu/backups/capacity-reclaim.log").read_text(encoding="utf-8")
    assert "refused reason=" in log and " removed " not in log


def test_a_symlink_or_a_candidate_reached_through_a_symlinked_parent_is_refused(tmp_path):
    root = _tree(tmp_path)
    live = root / "opt/nmu/app/data"
    (root / "opt/nmu/dist-stale-20260101-000000").symlink_to(live, target_is_directory=True)
    alias = tmp_path / "alias"
    alias.symlink_to(root / "opt/nmu", target_is_directory=True)

    for target in (root / "opt/nmu/dist-stale-20260101-000000",
                   alias / "dist-stale-20260913-093018"):
        assert reclaim.main(["--root", str(root), "--delete", str(target)]) == 1

    assert live.is_dir()
    assert (root / "opt/nmu/dist-stale-20260913-093018").is_dir()


def test_the_guard_rejects_live_and_daily_even_if_a_candidate_pattern_ever_matched(
        tmp_path, monkeypatch):
    root = _tree(tmp_path)
    monkeypatch.setattr(reclaim, "CANDIDATES", (
        *reclaim.CANDIDATES,
        reclaim.Candidate("P0", "bad_rule", "opt/nmu", r"^app$", False, ""),
        reclaim.Candidate("P0", "bad_rule", "opt/nmu/backups", r"^daily$", False, ""),
    ))

    for relative in ("opt/nmu/app", "opt/nmu/backups/daily"):
        assert reclaim.main(["--root", str(root), "--delete", str(root / relative)]) == 1
        assert (root / relative).is_dir()
    log = (root / "opt/nmu/backups/capacity-reclaim.log").read_text(encoding="utf-8")
    assert log.count("refused reason=protected_path") == 2
