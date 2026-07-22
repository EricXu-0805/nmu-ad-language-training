import pytest

from app import audio_store, auth, cloud_processing, export, export_security, resource_limits


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
    monkeypatch.delenv(cloud_processing.PROVIDER_ID_ENV, raising=False)
    monkeypatch.delenv(cloud_processing.NOTICE_VERSION_ENV, raising=False)


@pytest.fixture(autouse=True)
def _test_deidentification_key(monkeypatch):
    # 导出测试必须显式使用测试专用 HMAC 密钥；不读取开发机/部署环境的真实值。
    monkeypatch.setenv(
        export_security.DEIDENTIFICATION_KEY_ENV,
        "test-only-deidentification-key-32-bytes-minimum",
    )
    monkeypatch.setenv(export_security.DEIDENTIFICATION_KEY_ID_ENV, "test-2026")


@pytest.fixture(autouse=True)
def _reset_auth_state(monkeypatch):
    # 认证靠环境变量 + 进程内粘滞位(限速器/已有账号标记)。逐测试清干净,
    # 避免"上一测试建了账号/触发锁定"渗到下一测试(否则 no-pin-open 会误判为需认证)。
    monkeypatch.delenv("REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("CONSOLE_PIN", raising=False)
    # 当前仓库题库仍是 draft；功能/状态机测试必须走显式模拟数据路径，
    # 真实研究门禁另有专测，不能为了旧测试把 draft 假装成 ready。
    monkeypatch.setenv("ALLOW_SIMULATION_DATA", "1")
    # 大量单元测试需要直接构造历史场次；生产/回环运行绝不设置此测试专用开关，
    # 产品路径只能由 VisitPlan.start 原子创建场次。
    monkeypatch.setenv("NMU_TEST_ALLOW_DIRECT_SESSION_CREATE", "1")
    # 旧自由量表写入口在所有环境永久关闭；历史 legacy 行由测试直接种表。
    monkeypatch.delenv("NMU_ALLOW_LEGACY_UNVERIFIED_SCALE_ENTRY", raising=False)
    monkeypatch.delenv("ALLOW_LEGACY_AI_ENDPOINTS", raising=False)
    auth.reset_for_tests()
    resource_limits.reset_for_tests()
    yield
    auth.reset_for_tests()
    resource_limits.reset_for_tests()
