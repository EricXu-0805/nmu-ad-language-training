import pytest

from app import audio_store, export


@pytest.fixture(autouse=True)
def _isolate_audio_dir(tmp_path, monkeypatch):
    # 测试字节写临时目录,不污染真实 data/audio/(与真实采集数据物理隔离)。
    monkeypatch.setattr(audio_store, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(export, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(export, "CONTROLLED_AUDIO_DIR", tmp_path / "controlled-audio-exports")


@pytest.fixture(autouse=True)
def _no_cloud_key(monkeypatch):
    # auto 引擎(TTS/ASR/判分)见 DASHSCOPE_API_KEY 会真出网;测试一律摘 Key,
    # 云端行为全部用替身注入,保证套件在配了 Key 的机器上照样零网络。
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
