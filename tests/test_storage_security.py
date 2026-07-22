import stat

import pytest

from app import audio_store
from app.storage_security import ensure_private_directory, ensure_private_file


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_existing_storage_modes_are_hardened(tmp_path):
    directory = tmp_path / "research-data"
    directory.mkdir(mode=0o755)
    file = directory / "voice.webm"
    file.write_bytes(b"voice")
    file.chmod(0o644)

    ensure_private_directory(directory)
    ensure_private_file(file)

    assert _mode(directory) == 0o700
    assert _mode(file) == 0o600


def test_storage_helpers_reject_symlink_targets(tmp_path):
    target_dir = tmp_path / "outside"
    target_dir.mkdir()
    target_file = target_dir / "secret"
    target_file.write_text("secret", encoding="utf-8")
    target_dir.chmod(0o755)
    target_file.chmod(0o644)
    dir_link = tmp_path / "dir-link"
    file_link = tmp_path / "file-link"
    try:
        dir_link.symlink_to(target_dir, target_is_directory=True)
        file_link.symlink_to(target_file)
    except OSError:
        pytest.skip("当前文件系统不支持符号链接")

    with pytest.raises(RuntimeError):
        ensure_private_directory(dir_link)
    with pytest.raises(RuntimeError):
        ensure_private_file(file_link)
    assert _mode(target_dir) == 0o755
    assert _mode(target_file) == 0o644


def test_audio_store_rejects_symlink_root_and_target(tmp_path, monkeypatch):
    outside = tmp_path / "outside-audio"
    outside.mkdir()
    linked_root = tmp_path / "audio-link"
    try:
        linked_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前文件系统不支持符号链接")
    monkeypatch.setattr(audio_store, "AUDIO_DIR", linked_root)
    with pytest.raises(audio_store.AudioStoreIntegrityError, match="根目录"):
        audio_store.save_blob_atomic("safe-id", b"voice", "audio/webm")
    assert list(outside.iterdir()) == []

    real_root = tmp_path / "real-audio"
    real_root.mkdir()
    outside_file = tmp_path / "outside.webm"
    outside_file.write_bytes(b"outside")
    (real_root / "safe-id.webm").symlink_to(outside_file)
    monkeypatch.setattr(audio_store, "AUDIO_DIR", real_root)
    with pytest.raises(audio_store.AudioStoreIntegrityError, match="软链接"):
        audio_store.save_blob_atomic("safe-id", b"new", "audio/webm")
    assert outside_file.read_bytes() == b"outside"
