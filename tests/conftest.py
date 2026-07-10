import pytest

from app import audio_store


@pytest.fixture(autouse=True)
def _isolate_audio_dir(tmp_path, monkeypatch):
    # 测试字节写临时目录,不污染真实 data/audio/(与真实采集数据物理隔离)。
    monkeypatch.setattr(audio_store, "AUDIO_DIR", tmp_path / "audio")
