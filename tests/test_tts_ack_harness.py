"""Exact demo20 TTS/ACK harness 的隔离契约与生产门禁复用证据。

**本文件模块顶层只允许 stdlib / pytest / harness.tts_ack_harness。**
不得 import fastapi、sqlmodel 或任何 app.*：导入 app.db 会在导入期就绑定引擎，
`TestClient(app)` 还会跑 lifespan——那等于用测试进程去碰默认数据库。凡是需要
content、生产门禁、模型、FastAPI、app.main 的用例，一律丢进隔离子进程，且每个
子进程都在任何 app import 之前先拿到 tmp_path 里 chmod 700 的私有 root 和显式的
root 内 DATABASE_URL / AUDIO_DIR / TTS cache。

这里不模拟浏览器的 playing/ended——真机 ACK 证据由后续 Playwright 走查产生，
本文件只证明 harness 注入的内容确实经过生产 start gate，且存储彻底隔离。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from harness import tts_ack_harness as harness

PIN = "13579024"
PASSWORD = "harness-password-1"
PLACEHOLDER_PROVIDER_KEY = "harness-placeholder-not-a-real-key"
PLACEHOLDER_FINGERPRINT = "harness-placeholder-fingerprint-material"


def _env(root, **overrides) -> dict[str, str]:
    base = {
        harness.ROOT_ENV: str(root),
        "DATABASE_URL": f"sqlite:///{root}/harness.db",
        "AUDIO_DIR": f"{root}/audio",
        harness.TTS_CACHE_ENV: f"{root}/tts-cache",
        "ALLOW_SIMULATION_DATA": "1",
        "ENABLE_AUTOPILOT_P0A_SIMULATION": "1",
        "REQUIRE_AUTH": "1",
        "CONSOLE_PIN": PIN,
        harness.PASSWORD_ENV: PASSWORD,
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def _private_root(tmp_path, name: str):
    # /tmp 在 macOS 是符号链接；harness 用 resolve() 判包含关系，测试也用真实路径。
    root = (tmp_path / name).resolve()
    root.mkdir(mode=0o700)
    return root


def _subprocess_env(root) -> dict[str, str]:
    """最小化的 synthetic-only 子进程环境。

    绝不继承调用者的 os.environ——那会把真实 DASHSCOPE_API_KEY 等 provider 凭据
    带进子进程，synthetic 用例就可能真出网。需要 provider 变量的用例自己往回加。
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "TMPDIR": os.environ.get("TMPDIR", ""),
        "PYTHONPATH": str(harness.PLATFORM_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(_env(root))
    for secret in ("DASHSCOPE_API_KEY", "ASR_ENGINE", "TTS_ENGINE",
                   "TTS_VOICE_PATH", harness.FINGERPRINT_KEY_ENV):
        env.pop(secret, None)
    return {k: v for k, v in env.items() if v}


def _run_script(env, script: str, timeout: int = 900):
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        env=env, capture_output=True, text=True,
        cwd=str(harness.PLATFORM_ROOT), timeout=timeout)


def _payload(result):
    assert result.returncode == 0, result.stderr[-6000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


# ==========================================================================
# A. 主进程：纯 stdlib / harness 的 fail-closed 校验，不碰 app.*
# ==========================================================================
@pytest.mark.parametrize("overrides, fragment", [
    ({harness.ROOT_ENV: None}, harness.ROOT_ENV),
    ({harness.ROOT_ENV: "relative/root"}, "绝对路径"),
    ({"DATABASE_URL": None}, "DATABASE_URL"),
    ({"DATABASE_URL": "postgresql://u@localhost/nmu"}, "SQLite"),
    ({"DATABASE_URL": "sqlite:///relative.db"}, "绝对路径"),
    ({"DATABASE_URL": "sqlite:////etc/nmu.db"}, harness.ROOT_ENV),
    ({"AUDIO_DIR": "/var/tmp/elsewhere"}, harness.ROOT_ENV),
    ({harness.TTS_CACHE_ENV: "/var/tmp/elsewhere"}, harness.ROOT_ENV),
    ({"ALLOW_SIMULATION_DATA": "0"}, "ALLOW_SIMULATION_DATA"),
    ({"ENABLE_AUTOPILOT_P0A_SIMULATION": None}, "ENABLE_AUTOPILOT_P0A_SIMULATION"),
    ({"REQUIRE_AUTH": None}, "REQUIRE_AUTH"),
    ({"CONSOLE_PIN": "  "}, "CONSOLE_PIN"),
    ({"CONSOLE_PIN": "12345"}, "CONSOLE_PIN"),
    ({"CONSOLE_PIN": "12345a"}, "CONSOLE_PIN"),
    ({"CONSOLE_PIN": "1" * 33}, "CONSOLE_PIN"),
    ({harness.PASSWORD_ENV: "short"}, harness.PASSWORD_ENV),
    ({harness.TTS_MODE_ENV: "loopback"}, harness.TTS_MODE_ENV),
    ({harness.TTS_MODE_ENV: "production"}, "TTS_ENGINE"),
])
def test_resolve_config_fails_closed(tmp_path, overrides, fragment):
    root = _private_root(tmp_path, "root")
    with pytest.raises(harness.HarnessConfigError) as excinfo:
        harness.resolve_config(_env(root, **overrides))
    assert fragment in str(excinfo.value)


def test_default_database_url_is_rejected(tmp_path):
    root = _private_root(tmp_path, "root")
    default_url = f"sqlite:///{harness.PRODUCTION_DATA_DIR / 'app.db'}"
    with pytest.raises(harness.HarnessConfigError):
        harness.resolve_config(_env(root, DATABASE_URL=default_url))


def test_outside_paths_are_rejected_before_any_filesystem_probe(
        tmp_path, monkeypatch):
    """词法判定必须先于文件系统探测：默认库连一次 lstat/resolve 都不该挨上。

    做法是给 Path 的探测方法装一道拦网——只要目标是默认库就直接炸。若实现里
    还残留"先 is_symlink/resolve 再判是否越界"，这个用例会以 AssertionError 失败
    而不是 HarnessConfigError。这里不做任何真实 stat 前后对比。
    """
    root = _private_root(tmp_path, "root")
    forbidden = str(harness.PRODUCTION_DATA_DIR / "app.db")

    def _tripwire(name, original):
        def _wrapped(self, *args, **kwargs):
            if str(self) == forbidden:
                raise AssertionError(f"默认库被 Path.{name} 探测了")
            return original(self, *args, **kwargs)
        return _wrapped

    for name in ("is_symlink", "resolve", "stat", "lstat", "exists", "is_dir",
                 "is_file"):
        monkeypatch.setattr(Path, name, _tripwire(name, getattr(Path, name)))

    with pytest.raises(harness.HarnessConfigError):
        harness.resolve_config(_env(root, DATABASE_URL=f"sqlite:///{forbidden}"))


def test_repo_root_is_rejected_as_harness_root():
    with pytest.raises(harness.HarnessConfigError) as excinfo:
        harness.resolve_config(_env(harness.PLATFORM_ROOT / "data" / "t5"))
    assert "仓库目录" in str(excinfo.value)


def test_root_must_exist_and_be_private(tmp_path):
    missing = (tmp_path / "never-created").resolve()
    with pytest.raises(harness.HarnessConfigError) as absent:
        harness.resolve_config(_env(missing))
    assert "真实目录" in str(absent.value)

    shared = (tmp_path / "shared-root").resolve()
    shared.mkdir()
    shared.chmod(0o755)
    with pytest.raises(harness.HarnessConfigError) as loose:
        harness.resolve_config(_env(shared))
    assert "chmod 700" in str(loose.value)


def test_root_symlink_is_rejected(tmp_path):
    real = _private_root(tmp_path, "real-root")
    link = tmp_path / "link-root"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(harness.HarnessConfigError) as excinfo:
        harness.resolve_config(_env(link))
    assert "符号链接" in str(excinfo.value)


def test_directory_symlinks_inside_root_are_rejected(tmp_path):
    root = _private_root(tmp_path, "root")
    outside = _private_root(tmp_path, "outside")
    for name in ("asr-scratch", "exports", "controlled-audio-exports", "audio"):
        link = root / name
        link.symlink_to(outside, target_is_directory=True)
        with pytest.raises(harness.HarnessConfigError) as excinfo:
            harness.resolve_config(_env(root))
        assert "符号链接" in str(excinfo.value)
        link.unlink()


def test_production_mode_pins_cloud_engine_and_disables_piper(tmp_path):
    root = _private_root(tmp_path, "root")
    production = {
        harness.TTS_MODE_ENV: "production",
        "TTS_ENGINE": "qwen-tts",
        "DASHSCOPE_API_KEY": PLACEHOLDER_PROVIDER_KEY,
        harness.FINGERPRINT_KEY_ENV: PLACEHOLDER_FINGERPRINT,
    }
    with pytest.raises(harness.HarnessConfigError) as no_key:
        harness.resolve_config(_env(
            root, **{**production, "DASHSCOPE_API_KEY": None}))
    assert "DASHSCOPE_API_KEY" in str(no_key.value)

    with pytest.raises(harness.HarnessConfigError) as no_fingerprint:
        harness.resolve_config(_env(
            root, **{**production, harness.FINGERPRINT_KEY_ENV: None}))
    assert harness.FINGERPRINT_KEY_ENV in str(no_fingerprint.value)

    # piper 声音文件必须落在 root 内且不存在，云失败才不会静默读仓库 onnx。
    config = harness.resolve_config(_env(root, **production))
    assert config.piper_voice_path.is_relative_to(root)
    assert not config.piper_voice_path.exists()

    existing = root / "voice.onnx"
    existing.write_bytes(b"onnx")
    with pytest.raises(harness.HarnessConfigError) as present:
        harness.resolve_config(_env(
            root, **production, TTS_VOICE_PATH=str(existing)))
    assert "piper" in str(present.value)

    repo_voice = str(harness.PRODUCTION_DATA_DIR / "tts" / "zh_CN-huayan-medium.onnx")
    with pytest.raises(harness.HarnessConfigError) as outside:
        harness.resolve_config(_env(root, **production, TTS_VOICE_PATH=repo_voice))
    assert harness.ROOT_ENV in str(outside.value)


def test_resolved_config_never_exposes_secrets(tmp_path):
    root = _private_root(tmp_path, "root")
    config = harness.resolve_config(_env(root))
    printed = json.dumps(config.scrubbed(), ensure_ascii=False)
    assert PASSWORD not in printed
    assert PIN not in printed


def test_generic_loader_wrapper_remains_path_scoped_for_research_rehearsal(
        tmp_path):
    """The shared rehearsal helper stays scoped; demo20 install never calls it."""
    seen: list[object] = []
    loader = harness.canonical_bank_loader(
        lambda path: seen.append(path) or "ORIGINAL", "SYNTHETIC")

    assert loader(harness.CANONICAL_BANK_PATH) == "SYNTHETIC"
    assert loader(str(harness.CANONICAL_BANK_PATH)) == "SYNTHETIC"
    assert seen == []

    other = tmp_path / "item_bank_errata.json"
    assert loader(other) == "ORIGINAL"
    assert seen == [other]


def test_cli_help_names_exact_demo20_and_canonical_boundary():
    result = subprocess.run(
        [sys.executable, "-m", "harness.tts_ack_harness", "--help"],
        capture_output=True,
        text=True,
        cwd=str(harness.PLATFORM_ROOT),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "exact 20 题本机模拟" in result.stdout
    assert harness.DEMO_PROFILE_VERSION in result.stdout
    assert "canonical 题库不会被替换" in result.stdout
    assert "单位置合成验收" not in result.stdout


def test_deterministic_wav_is_stable_and_non_empty():
    first = harness.deterministic_wav("小语来了")
    assert first == harness.deterministic_wav("小语来了")
    assert first != harness.deterministic_wav("请看这张图")
    assert first.startswith(b"RIFF") and first[8:12] == b"WAVE"
    assert len(first) > 44


def test_browser_duration_profile_is_closed_and_changes_cache_identity(monkeypatch):
    monkeypatch.setenv(harness.TTS_DURATION_PROFILE_ENV, "browser-start-pause")
    browser_engine = harness.DeterministicHarnessTtsEngine()
    assert browser_engine.duration_seconds == 5.0
    assert browser_engine.version == "harness-synthetic-browser/1"
    assert browser_engine.cache_params == "harness-synthetic-v1"
    assert len(browser_engine.synthesize("小语来了")) > len(
        harness.deterministic_wav("小语来了"))

    monkeypatch.setenv(harness.TTS_DURATION_PROFILE_ENV, "invented")
    with pytest.raises(harness.HarnessConfigError):
        harness.DeterministicHarnessTtsEngine()


# ==========================================================================
# B. 隔离子进程：校验失败必须早于任何 app import
# ==========================================================================
_PRE_IMPORT_SCRIPT = """
    import json, os, sys
    from harness import tts_ack_harness as h
    try:
        h.resolve_config(os.environ)
    except h.HarnessConfigError as exc:
        print(json.dumps({
            "error": str(exc),
            "app_modules": sorted(
                m for m in sys.modules if m.split(".")[0] == "app"),
        }))
        sys.exit(3)
    sys.exit(0)
"""


def test_validation_happens_before_any_app_import(tmp_path):
    root = _private_root(tmp_path, "root")
    env = _subprocess_env(root)
    env.pop("REQUIRE_AUTH")
    result = _run_script(env, _PRE_IMPORT_SCRIPT)
    assert result.returncode == 3, result.stderr[-4000:]
    payload = json.loads(result.stdout)
    assert payload["app_modules"] == []
    assert "REQUIRE_AUTH" in payload["error"]


_FINGERPRINT_PREFLIGHT_SCRIPT = """
    import json, os, sys
    from harness import tts_ack_harness as h
    h.resolve_config(os.environ)
    try:
        h.assert_production_fingerprint_contract()
    except h.HarnessConfigError as exc:
        print(json.dumps({
            "error": str(exc),
            "app_db_imported": "app.db" in sys.modules,
            "app_main_imported": "app.main" in sys.modules,
        }))
        sys.exit(3)
    sys.exit(0)
"""


@pytest.mark.parametrize("fingerprint, expected_code", [
    (PLACEHOLDER_PROVIDER_KEY, "provider_credential_fingerprint_key_reused"),
    ("aaaa", "provider_credential_fingerprint_key_invalid"),
    ("has whitespace in the fingerprint material",
     "provider_credential_fingerprint_key_invalid"),
])
def test_fingerprint_preflight_fails_before_app_db_import(
        tmp_path, fingerprint, expected_code):
    """完整合同用生产判据判死，且判死时 app.db 还没有被导入。"""
    root = _private_root(tmp_path, "root")
    env = _subprocess_env(root)
    env.update({
        harness.TTS_MODE_ENV: "production",
        "TTS_ENGINE": "qwen-tts",
        "DASHSCOPE_API_KEY": PLACEHOLDER_PROVIDER_KEY,
        harness.FINGERPRINT_KEY_ENV: fingerprint,
        "TTS_VOICE_PATH": str(root / "no-piper-voice.onnx"),
    })
    result = _run_script(env, _FINGERPRINT_PREFLIGHT_SCRIPT)
    assert result.returncode == 3, result.stderr[-4000:]
    payload = json.loads(result.stdout)
    assert expected_code in payload["error"]
    assert payload["app_db_imported"] is False
    assert payload["app_main_imported"] is False


_DB_BINDING_SCRIPT = """
    import json, os, sys
    from pathlib import Path
    from harness import tts_ack_harness as h

    # 进程环境指向 root-A，但配置是从 root-B 的 mapping 解析出来的。
    # 这正是"用 mapping 校验通过、却忘了写回 os.environ"的那条缝。
    root_b = Path(os.environ["NMU_HARNESS_ALT_ROOT"])
    mapping = dict(os.environ)
    mapping.update({
        h.ROOT_ENV: str(root_b),
        "DATABASE_URL": "sqlite:///" + str(root_b / "harness.db"),
        "AUDIO_DIR": str(root_b / "audio"),
        h.TTS_CACHE_ENV: str(root_b / "tts-cache"),
    })
    config = h.resolve_config(mapping)
    assert str(config.root) == str(root_b)

    results = {}
    for name, call in (("migrate", lambda: h.migrate(config)),
                       ("install", lambda: h.install(config))):
        try:
            call()
            results[name] = None
        except h.HarnessConfigError as exc:
            results[name] = str(exc)
        results[name + "_app_db_imported"] = "app.db" in sys.modules
        results[name + "_app_main_imported"] = "app.main" in sys.modules
    print(json.dumps(results))
"""


def test_migrate_and_install_reject_mismatched_process_binding(tmp_path):
    """mapping 校验通过但 os.environ 没同步时，migrate/install 必须在导入前失败。

    两个 root 都在 tmp_path 下；全程不接触也不检查默认库。
    """
    root_a = _private_root(tmp_path, "process-root-a")
    root_b = _private_root(tmp_path, "config-root-b")
    env = _subprocess_env(root_a)
    env["NMU_HARNESS_ALT_ROOT"] = str(root_b)

    payload = _payload(_run_script(env, _DB_BINDING_SCRIPT))
    for name in ("migrate", "install"):
        assert payload[name] is not None, f"{name} 没有拒绝错配的进程绑定"
        assert "DATABASE_URL" in payload[name]
        assert payload[f"{name}_app_db_imported"] is False
        assert payload[f"{name}_app_main_imported"] is False


_ZERO_CLOUD_SCRIPT = """
    import json, os, socket, sys

    # 故意伪造"父 shell 里已经 export 过云凭据"的处境。
    os.environ["DASHSCOPE_API_KEY"] = "fake-parent-shell-key-not-a-real-credential"
    os.environ["PROVIDER_READINESS_FINGERPRINT_KEY"] = "fake-parent-shell-fingerprint"
    os.environ["LLM_JUDGE"] = "qwen"
    os.environ["ASR_ENGINE"] = "dashscope"
    os.environ["TTS_ENGINE"] = "qwen-tts"

    # 出网探针：任何 socket 外连都记账并炸掉，比替换某个具体 provider 类更严。
    outbound = []

    def _tripwire_connect(self, address, *args, **kwargs):
        outbound.append(str(address))
        raise AssertionError("harness synthetic 进程试图出网")

    def _tripwire_create(address, *args, **kwargs):
        outbound.append(str(address))
        raise AssertionError("harness synthetic 进程试图出网")

    socket.socket.connect = _tripwire_connect
    socket.create_connection = _tripwire_create

    from harness import tts_ack_harness as h

    config = h.resolve_config(os.environ)
    h.install(config)

    from app import provider_readiness

    configuration = provider_readiness.capture_configuration()
    probe = provider_readiness.run_synthetic_probe()
    print(json.dumps({
        "key_visible": "DASHSCOPE_API_KEY" in os.environ,
        "fingerprint_visible": h.FINGERPRINT_KEY_ENV in os.environ,
        "tts_engine_env": os.environ.get("TTS_ENGINE"),
        "asr_engine_env": os.environ.get("ASR_ENGINE"),
        "llm_judge_env": os.environ.get("LLM_JUDGE"),
        "llm_engine_version": configuration.llm_engine_version,
        "llm_configured": configuration.llm_configured,
        "tts_ready": probe.tts.success,
        "tts_failure": probe.tts.failure_code,
        "asr_ready": probe.asr.success,
        "asr_failure": probe.asr.failure_code,
        "required_ready": probe.required_capabilities_ready,
        "all_configured_ready": probe.all_configured_capabilities_ready,
        "outbound": outbound,
    }))
"""


def test_synthetic_mode_is_zero_cloud_even_with_parent_shell_key(tmp_path):
    """父 shell 有 Key 也必须零云：凭据摘掉、引擎钉死、一次外连都不发生。"""
    root = _private_root(tmp_path, "zero-cloud-root")
    payload = _payload(_run_script(_subprocess_env(root), _ZERO_CLOUD_SCRIPT))

    assert payload["key_visible"] is False
    assert payload["fingerprint_visible"] is False
    assert payload["tts_engine_env"] == "null"
    assert payload["asr_engine_env"] == "null"
    assert payload["llm_judge_env"] == "off"

    # llm 不再被 auto 选成 qwen——否则 readiness 会真的外呼。
    assert payload["llm_engine_version"] == "off"
    assert payload["llm_configured"] is False

    # 必需能力仍然由确定性替身真正跑通。
    assert payload["tts_ready"] is True, payload["tts_failure"]
    assert payload["asr_ready"] is True, payload["asr_failure"]
    assert payload["required_ready"] is True
    assert payload["all_configured_ready"] is True

    assert payload["outbound"] == []


_PRODUCTION_MIGRATE_SCRIPT = """
    import json, os, sys
    from harness import tts_ack_harness as h

    config = h.resolve_config(os.environ)
    try:
        h.migrate(config)
    except h.HarnessConfigError as exc:
        print(json.dumps({
            "error": str(exc),
            "app_db_imported": "app.db" in sys.modules,
            "alembic_imported": "alembic" in sys.modules,
        }))
        sys.exit(3)
    sys.exit(0)
"""


def test_direct_production_migrate_rejects_invalid_fingerprint(tmp_path):
    """production 下 migrate 也必须先过指纹预检，且失败时 app.db 未被导入。"""
    root = _private_root(tmp_path, "prod-migrate-root")
    env = _subprocess_env(root)
    env.update({
        harness.TTS_MODE_ENV: "production",
        "TTS_ENGINE": "qwen-tts",
        "DASHSCOPE_API_KEY": PLACEHOLDER_PROVIDER_KEY,
        # 与 provider key 完全相同 → 生产判据判 reused。
        harness.FINGERPRINT_KEY_ENV: PLACEHOLDER_PROVIDER_KEY,
        "TTS_VOICE_PATH": str(root / "no-piper-voice.onnx"),
    })
    result = _run_script(env, _PRODUCTION_MIGRATE_SCRIPT)
    assert result.returncode == 3, result.stderr[-4000:]
    payload = json.loads(result.stdout)
    assert "provider_credential_fingerprint_key_reused" in payload["error"]
    assert payload["app_db_imported"] is False
    assert payload["alembic_imported"] is False


_CREDENTIAL_ROTATION_SCRIPT = """
    import json, os, sys
    from harness import tts_ack_harness as h

    config = h.resolve_config(os.environ)
    h.assert_production_fingerprint_contract()      # 首次通过，记下凭据摘要
    h.migrate(config)                               # 这一步把 app.db 带进来

    results = {"app_db_after_migrate": "app.db" in sys.modules}

    # 同一份凭据再调用应当幂等跳过。
    h.assert_production_fingerprint_contract()
    results["idempotent_ok"] = True

    os.environ["CONSOLE_PIN"] = "99988824"
    try:
        h.assert_production_fingerprint_contract()
        results["pin_rotation"] = None
    except h.HarnessConfigError as exc:
        results["pin_rotation"] = str(exc)

    os.environ["CONSOLE_PIN"] = os.environ["NMU_HARNESS_ORIGINAL_PIN"]
    os.environ[h.FINGERPRINT_KEY_ENV] = "another-Fingerprint-Material-7k2Wq9Zx"
    try:
        h.assert_production_fingerprint_contract()
        results["fingerprint_rotation"] = None
    except h.HarnessConfigError as exc:
        results["fingerprint_rotation"] = str(exc)

    print(json.dumps(results))
"""


def test_verified_credentials_cannot_be_rotated_after_app_db_import(tmp_path):
    """预检通过之后换掉 PIN 或指纹密钥，旧验证一律作废。"""
    root = _private_root(tmp_path, "rotation-root")
    env = _subprocess_env(root)
    env.update({
        harness.TTS_MODE_ENV: "production",
        "TTS_ENGINE": "qwen-tts",
        "DASHSCOPE_API_KEY": PLACEHOLDER_PROVIDER_KEY,
        harness.FINGERPRINT_KEY_ENV: "h4rn3ss-Fingerprint-Material-9x7Qz2Lm",
        "TTS_VOICE_PATH": str(root / "no-piper-voice.onnx"),
        "NMU_HARNESS_ORIGINAL_PIN": PIN,
    })
    payload = _payload(_run_script(env, _CREDENTIAL_ROTATION_SCRIPT))

    assert payload["app_db_after_migrate"] is True
    assert payload["idempotent_ok"] is True
    for key in ("pin_rotation", "fingerprint_rotation"):
        assert payload[key] is not None, f"{key} 没有被拒绝"
        assert "旧验证不能沿用" in payload[key]


_STALE_ENGINE_SCRIPT = """
    import json, os, sqlite3, sys
    from pathlib import Path
    from harness import tts_ack_harness as h

    # root-A：正常建库、导入并绑定引擎。
    config_a = h.resolve_config(os.environ)
    h.migrate(config_a)
    h.install(config_a)

    from app import db
    engine_after_a = str(db.engine.url)

    # root-B：把进程环境整体切到另一个私有 root，配置本身完全合法。
    root_b = Path(os.environ["NMU_HARNESS_ALT_ROOT"])
    os.environ[h.ROOT_ENV] = str(root_b)
    os.environ["DATABASE_URL"] = "sqlite:///" + str(root_b / "harness.db")
    os.environ["AUDIO_DIR"] = str(root_b / "audio")
    os.environ[h.TTS_CACHE_ENV] = str(root_b / "tts-cache")
    config_b = h.resolve_config(os.environ)

    # 给 root-A 的库路径架探针：这条检查必须纯词法，旧引擎的目标路径连一次
    # lstat/resolve 都不该挨上。只对 root-A 的库路径生效，别的路径照常放行。
    stale_targets = {
        str(config_a.database_file),
        str(h._sqlite_file(config_a.database_url)),
    }
    probed = []
    originals = {}

    def _arm(name, original):
        def _wrapped(self, *args, **kwargs):
            if str(self) in stale_targets:
                probed.append(name)
                raise AssertionError("旧引擎路径被 Path." + name + " 探测了")
            return original(self, *args, **kwargs)
        return _wrapped

    for name in ("resolve", "stat", "lstat", "exists", "is_file", "is_dir",
                 "is_symlink"):
        originals[name] = getattr(Path, name)
        setattr(Path, name, _arm(name, originals[name]))

    # 环境已经对得上 config_b，只看 os.environ 的前置闸不会报警；
    # 但引擎还绑在 root-A 上，seed 必须在读写任何一行之前失败。
    try:
        seed_error = None
        try:
            h.seed(config_b)
        except h.HarnessConfigError as exc:
            seed_error = str(exc)

        install_error = None
        try:
            h.install(config_b)
        except h.HarnessConfigError as exc:
            install_error = str(exc)
    finally:
        for name, original in originals.items():
            setattr(Path, name, original)

    def _counts(path):
        if not path.exists():
            return None
        connection = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
        try:
            names = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
            out = {}
            for table in ("researchuser", "patient", "session", "visitplan"):
                out[table] = (
                    connection.execute(
                        "SELECT COUNT(*) FROM " + table).fetchone()[0]
                    if table in names else None)
            return out
        finally:
            connection.close()

    print(json.dumps({
        "engine_after_a": engine_after_a,
        "engine_now": str(db.engine.url),
        "seed_error": seed_error,
        "install_error": install_error,
        "probed": probed,
        "root_a_counts": _counts(config_a.database_file),
        "root_b_db_exists": config_b.database_file.exists(),
        "root_b_entries": sorted(p.name for p in root_b.iterdir()),
    }))
"""


def test_seed_refuses_a_stale_engine_bound_to_another_database(tmp_path):
    """引擎已绑 root-A 时，拿一份合法的 root-B 配置直接 seed 必须零读写失败。"""
    root_a = _private_root(tmp_path, "stale-root-a")
    root_b = _private_root(tmp_path, "stale-root-b")
    env = _subprocess_env(root_a)
    env["NMU_HARNESS_ALT_ROOT"] = str(root_b)

    payload = _payload(_run_script(env, _STALE_ENGINE_SCRIPT))

    # seed 与 install 都拒绝，理由都指向"已导入的引擎没绑对库"。
    for key in ("seed_error", "install_error"):
        assert payload[key] is not None, f"{key} 没有被拒绝"
        assert "已导入的生产引擎未绑定 harness 库" in payload[key]

    # 判定纯词法：旧引擎绑的那条路径一次都没被文件系统探过。
    assert payload["probed"] == []

    # 引擎自始至终还是 root-A 那一个，没有被悄悄改绑到 root-B。
    assert payload["engine_now"] == payload["engine_after_a"]
    assert payload["engine_now"] == env["DATABASE_URL"]

    # root-A 一行都没被 seed 写进去（表在，计数全 0）。
    counts = payload["root_a_counts"]
    assert counts is not None
    for table in ("researchuser", "patient", "session", "visitplan"):
        assert counts[table] == 0, f"root-A 的 {table} 被写入了"

    # root-B 完全没被碰：既没有库文件，目录里也没有 harness 建出来的任何东西。
    assert payload["root_b_db_exists"] is False
    assert payload["root_b_entries"] == []


# ==========================================================================
# C. 真实 CLI smoke：python -m harness.tts_ack_harness --migrate-and-seed
#    再由独立 inspector 子进程用同一临时 env 读回证据
# ==========================================================================
_INSPECTOR_SCRIPT = """
    import json, os
    from sqlmodel import Session, select
    from harness import tts_ack_harness as h

    config = h.resolve_config(os.environ)
    h.install(config)

    from app import (audio_store, autopilot_plan_profiles, content, db,
                     provider_readiness, tts)
    from app.models import Patient, VisitPlan
    from app.models import Session as TrainSession
    import sqlalchemy

    probe = provider_readiness.run_synthetic_probe()
    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")
    with Session(db.engine) as session:
        patients = [p.patient_id for p in session.exec(select(Patient))]
        plans = [
            (p.status, p.patient_id, p.autopilot_profile_version_id)
            for p in session.exec(select(VisitPlan))
        ]
        sessions = [
            (s.session_id, s.patient_id, s.week_no, s.visit_plan_id,
             s.autopilot_profile_version_id)
            for s in session.exec(select(TrainSession))
        ]
        train = session.exec(select(TrainSession)).one()
        protocol = content.load_autopilot_protocol(
            content.CONTENT_DIR / "autopilot_protocol_v1.json")
        resolved = autopilot_plan_profiles.resolve_for_session(
            train, bank=bank, protocol=protocol)
    print(json.dumps({
        "engine_url": str(db.engine.url),
        "tables": sorted(sqlalchemy.inspect(db.engine).get_table_names()),
        "patients": patients,
        "plans": plans,
        "sessions": sessions,
        "positions": len(bank.single_element),
        "source_count": bank.meta["source_protocol_position_count"],
        "unstructured": bank.meta["source_unstructured_positions"],
        "loader_module": content.load_item_bank.__module__,
        "resolved_profile": resolved.profile_version_id,
        "resolved_positions": resolved.resolved_position_count,
        "unsupported_positions": resolved.unsupported_position_count,
        "completion_scope": resolved.completion_scope,
        "tts_ready": probe.tts.success,
        "tts_failure": probe.tts.failure_code,
        "asr_ready": probe.asr.success,
        "asr_failure": probe.asr.failure_code,
        "required_ready": probe.required_capabilities_ready,
        "audio_dir": str(audio_store.AUDIO_DIR),
        "tts_cache_dir": str(tts.CACHE_DIR),
    }))
"""


def test_real_cli_migrates_and_seeds_then_inspector_reads_temp_db(tmp_path):
    """真正跑 CLI，再用独立进程读回临时库；隔离证据取自引擎绑的是哪个 URL。

    刻意不去 stat/读默认库：边界是默认数据库零触达，不是零写入。校验失败早于
    app import 那一条由 test_validation_happens_before_any_app_import 单独证明。
    """
    root = _private_root(tmp_path, "smoke-root")
    env = _subprocess_env(root)

    cli = subprocess.run(
        [sys.executable, "-m", "harness.tts_ack_harness", "--migrate-and-seed"],
        env=env, capture_output=True, text=True,
        cwd=str(harness.PLATFORM_ROOT), timeout=900)
    assert cli.returncode == 0, cli.stderr[-6000:]
    assert PASSWORD not in cli.stderr and PIN not in cli.stderr

    payload = _payload(_run_script(env, _INSPECTOR_SCRIPT))

    # 引擎精确绑在临时 URL 上——默认库不在这条链路里的任何位置。
    assert payload["engine_url"] == env["DATABASE_URL"]
    assert payload["audio_dir"].startswith(str(root))
    assert payload["tts_cache_dir"].startswith(str(root))
    # 临时库确实经过 alembic 迁移，不是被 create_all 兜出来的。
    assert "alembic_version" in payload["tables"]
    # 合成受试者 + 已 started 的 VisitPlan + 原子建出的场次。
    assert payload["patients"] == [harness.HARNESS_PATIENT_ID]
    # JSON 往返之后元组变成列表，断言按列表写。
    assert payload["plans"] == [[
        "started", harness.HARNESS_PATIENT_ID, harness.DEMO_PROFILE_VERSION]]
    assert len(payload["sessions"]) == 1
    session_id, patient_id, week_no, plan_id, profile_version = (
        payload["sessions"][0])
    assert patient_id == harness.HARNESS_PATIENT_ID
    assert week_no == 2 and plan_id is not None and session_id
    assert profile_version == harness.DEMO_PROFILE_VERSION
    # install 不再换 canonical loader：题库仍保留完整源清单和 58 结构化缺口。
    assert payload["loader_module"] == "app.content"
    assert payload["positions"] == 20
    assert payload["source_count"] == 78
    assert len(payload["unstructured"]) == 0
    # 可执行范围来自不可变 profile，而不是裁题库。
    assert payload["resolved_profile"] == harness.DEMO_PROFILE_VERSION
    assert payload["resolved_positions"] == 20
    assert payload["unsupported_positions"] == 0
    assert payload["completion_scope"] == "demo_plan_only"
    # readiness 合成探针真的跑通了 TTS→ASR，而不是被绕过或塞了一行 ready。
    assert payload["tts_ready"] is True, payload["tts_failure"]
    assert payload["asr_ready"] is True, payload["asr_failure"]
    assert payload["required_ready"] is True


# ==========================================================================
# D. 隔离子进程：canonical 58 缺口仍拒绝 + exact demo20 解析为 20
# ==========================================================================
_CONTENT_CONTRACT_SCRIPT = """
    import json, os
    from harness import tts_ack_harness as h

    config = h.resolve_config(os.environ)

    from app import (autopilot_plan_profiles, autopilot_service, content,
                     repeat_intent)
    from app.models import Session as TrainSession

    production = content.load_item_bank(
        content.CONTENT_DIR / "item_bank_v1.json")
    protocol = content.load_autopilot_protocol(
        content.CONTENT_DIR / "autopilot_protocol_v1.json")
    repeat_protocol = repeat_intent.active_protocol()
    def _session(*, demo20):
        return TrainSession(
            session_id="S-HARNESS-CONTRACT",
            patient_id=h.HARNESS_PATIENT_ID,
            is_simulation=True,
            week_no=2,
            phase_type="正式训练",
            event_line="正式训练",
            item_bank_version_id=production.version_id,
            item_bank_definition_digest=(
                content.item_bank_definition_digest(production)),
            autopilot_protocol_version_id=protocol["protocol_version_id"],
            autopilot_protocol_definition_digest=(
                content.autopilot_protocol_definition_digest(protocol)),
            repeat_protocol_version_id=repeat_protocol.version_id,
            repeat_protocol_definition_digest=repeat_protocol.definition_digest,
            autopilot_profile_version_id=(
                h.DEMO_PROFILE_VERSION if demo20 else None),
            autopilot_profile_definition_digest=(
                autopilot_plan_profiles.WEEK2_SINGLE20_DEMO_DIGEST
                if demo20 else None),
        )

    canonical_session = _session(demo20=False)
    demo_session = _session(demo20=True)
    canonical_resolution = autopilot_plan_profiles.resolve_for_session(
        canonical_session, bank=production, protocol=protocol)
    demo_resolution = autopilot_plan_profiles.resolve_for_session(
        demo_session, bank=production, protocol=protocol)

    refusal = {}
    try:
        autopilot_service._require_entire_plan_supported(
            canonical_session, production, protocol)
    except autopilot_service.AutopilotServiceError as exc:
        refusal = {"code": exc.code, "context": exc.context}

    demo_gate_error = None
    try:
        autopilot_service._require_entire_plan_supported(
            demo_session, production, protocol)
    except autopilot_service.AutopilotServiceError as exc:
        demo_gate_error = exc.code

    digest_once = content.item_bank_definition_digest(production)
    digest_twice = content.item_bank_definition_digest(
        content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json"))
    canonical_ids = {row["item_id"] for row in production.single_element}

    print(json.dumps({
        "production_refusal": refusal,
        "production_positions": len(production.single_element),
        "production_source_count": production.meta[
            "source_protocol_position_count"],
        "canonical_resolved_positions": (
            canonical_resolution.resolved_position_count),
        "canonical_unsupported_positions": (
            canonical_resolution.unsupported_position_count),
        "canonical_completion_scope": canonical_resolution.completion_scope,
        "demo_gate_error": demo_gate_error,
        "demo_profile": demo_resolution.profile_version_id,
        "demo_positions": demo_resolution.resolved_position_count,
        "demo_unsupported": demo_resolution.unsupported_position_count,
        "demo_completion_scope": demo_resolution.completion_scope,
        "demo_items_are_canonical": all(
            item.item_id in canonical_ids
            for item in demo_resolution.session_plan.items),
        "digest_stable": digest_once == digest_twice,
        "validation_errors": content.validate_item_bank(production)["errors"],
    }))
"""


def test_content_contract_and_production_gate(tmp_path):
    root = _private_root(tmp_path, "content-root")
    payload = _payload(_run_script(_subprocess_env(root), _CONTENT_CONTRACT_SCRIPT))

    # 2026-08-21 交互数据包闭合全部 58 个协议缺口后,canonical 整计划通过生产门。
    # 门函数本身未松动:拆掉数据包时 _require_entire_plan_supported 仍拒
    # (tests/test_autopilot_interaction.py 与 autopilot service 套件双向钉住)。
    assert payload["production_refusal"] == {}
    assert payload["production_positions"] == 20
    assert payload["production_source_count"] == 78
    assert payload["canonical_resolved_positions"] == 78
    assert payload["canonical_unsupported_positions"] == 0
    assert payload["canonical_completion_scope"] == "canonical_full_source"

    # 同一份 canonical 题库只经 exact profile 选位，20 位置完整通过。
    assert payload["demo_gate_error"] is None
    assert payload["demo_profile"] == harness.DEMO_PROFILE_VERSION
    assert payload["demo_positions"] == 20
    assert payload["demo_unsupported"] == 0
    assert payload["demo_completion_scope"] == "demo_plan_only"
    assert payload["demo_items_are_canonical"] is True

    assert payload["digest_stable"] is True
    assert payload["validation_errors"] == []


# ==========================================================================
# E. 隔离子进程：真实 VisitPlan demo20 账本经生产 start gate
#    只签发 profile 第一个位置的一条 TTS。
# ==========================================================================
_START_GATE_SCRIPT = """
    import json, os
    # datetime 只用于 LiveState.updated_at 这一个运行态字段（真实流程由床旁写入）；
    # readiness 一律走生产 run_synthetic_probe + persist_probe，不手造 ready 行。
    from datetime import datetime
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select
    from harness import tts_ack_harness as h

    config = h.resolve_config(os.environ)
    h.migrate(config)

    app = h.install(config)
    seeded = h.seed(config)
    session_id = seeded["session_id"]

    from app import (auth, autopilot_orchestration, autopilot_plan_profiles,
                     content, db, provider_readiness)
    from app.models import LiveState, PatientDeviceCapability, RuntimeCommand
    from app.models import Session as TrainSession

    autopilot_orchestration.submit = lambda _session_id, _worker: True

    bank = content.load_item_bank(content.CONTENT_DIR / "item_bank_v1.json")

    # readiness 走生产路径：合成引擎真的跑一遍 TTS→ASR，再由生产 persist_probe
    # 落库。这里不构造 ProviderReadinessProbe，探针失败就该让 start 也失败。
    probe_result = provider_readiness.run_synthetic_probe()

    with Session(db.engine) as session:
        train = session.get(TrainSession, session_id)
        resolved = autopilot_plan_profiles.resolve_for_session(
            train, bank=bank, protocol=content.load_autopilot_protocol(
                content.CONTENT_DIR / "autopilot_protocol_v1.json"))
        expected_first = resolved.positions[0]
        session.add(LiveState(
            id=1,
            seq=1,
            session_json=json.dumps({
                "sessionId": session_id,
                "weekNo": train.week_no,
                "eventLine": "正式训练",
                "mode": "task",
                "itemBankVersionId": train.item_bank_version_id,
            }, ensure_ascii=False),
            updated_at=datetime.now(),
        ))
        provider_readiness.persist_probe(
            session, result=probe_result, actor_display_id=train.trainer_id)
        session.commit()

    # 两个客户端都用 with 管理：进出时真的跑一遍生产 lifespan，并保证正常关闭。
    with TestClient(app) as account, TestClient(app) as device:
        login = account.post("/auth/login", json={
            "username": config.actor_username, "password": config.actor_password})
        assert login.status_code == 200, login.text
        account.headers["X-CSRF-Token"] = account.cookies.get(auth.CSRF_COOKIE_NAME)
        marker_header = account.get("/healthz").headers.get(h.MARKER_HEADER)

        paired = device.post(
            "/device/pair",
            headers={"X-Console-Pin": os.environ["CONSOLE_PIN"]},
            json={"deviceId": "harness-device-000001"})
        assert paired.status_code == 200, paired.text
        with Session(db.engine) as session:
            capability = session.exec(select(PatientDeviceCapability)).one()
            capability.session_id = session_id
            capability.active_session_key = session_id
            session.add(capability)
            session.commit()

        started = account.post(
            f"/sessions/{session_id}/autopilot/start",
            json={"idempotency_key": "harness-start-0001", "expected_revision": 0})
        with Session(db.engine) as session:
            after_start = [
                (c.kind, c.session_id, c.item_id, c.turn_seq)
                for c in session.exec(select(RuntimeCommand))]

    print(json.dumps({
        "session_id": session_id,
        "probe_tts": probe_result.tts.success,
        "probe_tts_failure": probe_result.tts.failure_code,
        "probe_asr": probe_result.asr.success,
        "probe_asr_failure": probe_result.asr.failure_code,
        "probe_required_ready": probe_result.required_capabilities_ready,
        "start_status": started.status_code,
        "start_body": started.json() if started.status_code != 200 else None,
        "commands_after_start": after_start,
        "profile_version": resolved.profile_version_id,
        "resolved_positions": resolved.resolved_position_count,
        "completion_scope": resolved.completion_scope,
        "expected_first": [expected_first.item_id, expected_first.turn_seq],
        "marker_header": marker_header,
        "bank_positions": len(bank.single_element),
        "bank_source_count": bank.meta["source_protocol_position_count"],
    }))
"""


def test_start_gate_issues_exactly_one_first_tts_command(tmp_path):
    root = _private_root(tmp_path, "start-root")
    payload = _payload(_run_script(_subprocess_env(root), _START_GATE_SCRIPT))

    # readiness 是真跑出来再由生产 persist_probe 落库的，不是手写的 ready 行。
    assert payload["probe_tts"] is True, payload["probe_tts_failure"]
    assert payload["probe_asr"] is True, payload["probe_asr_failure"]
    assert payload["probe_required_ready"] is True

    # 走的是生产 start gate，不是直接造命令。
    assert payload["start_status"] == 200, payload["start_body"]
    expected_item, expected_turn = payload["expected_first"]
    assert payload["commands_after_start"] == [[
        "tts", payload["session_id"], expected_item, expected_turn]]
    assert payload["profile_version"] == harness.DEMO_PROFILE_VERSION
    assert payload["resolved_positions"] == 20
    assert payload["completion_scope"] == "demo_plan_only"

    # harness-only 标记头在生产路由上生效。
    assert payload["marker_header"] == harness.MARKER_VALUE
    assert payload["bank_positions"] == 20
    assert payload["bank_source_count"] == 78


# ==========================================================================
# F. 隔离子进程：引擎替身、缓存重定向、marker 中间件边界
# ==========================================================================
_ENGINE_AND_MARKER_SCRIPT = """
    import json, os
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    from fastapi.testclient import TestClient
    from harness import tts_ack_harness as h

    config = h.resolve_config(os.environ)
    h.install(config)

    from app import tts

    data, version, cached_first = tts.speak("小语来了")
    _, _, cached_second = tts.speak("小语来了")
    # The exact autopilot route resolves its engine through a separate,
    # Qwen-only entry point.  Distinct text keeps its cache entry separate, so
    # a missing harness injection shows up as a degradation here rather than
    # being masked by the generic call's cached bytes.
    exact_data, exact_version, exact_first = tts.speak_autopilot("请说出这个词")
    _, _, exact_second = tts.speak_autopilot("请说出这个词")
    written = sorted(p.name for p in config.tts_cache_dir.glob("*.wav"))

    engine = h.DeterministicHarnessAsrEngine()
    transcript = engine.transcribe(b"RIFF-placeholder", ["苹果"])

    probe = FastAPI()

    @probe.get("/cookies")
    def _cookies():
        response = PlainTextResponse("ok")
        response.set_cookie("session", "a")
        response.set_cookie("csrf", "b")
        return response

    probe.add_middleware(h.harness_marker_middleware_class())
    with TestClient(probe) as client:
        marked = client.get("/cookies")

    print(json.dumps({
        "wav_ok": bool(data) and data.startswith(b"RIFF"),
        "version": version,
        "cached_first": cached_first,
        "cached_second": cached_second,
        "exact_wav_ok": bool(exact_data) and exact_data.startswith(b"RIFF"),
        "exact_version": exact_version,
        "exact_cached_first": exact_first,
        "exact_cached_second": exact_second,
        "cache_files": len(written),
        "cache_dir": str(tts.CACHE_DIR),
        "asr_boundary": engine.data_boundary,
        "asr_provider_id": engine.provider_id,
        "asr_text": transcript.asr_text,
        "asr_version": transcript.engine_version,
        "marker": marked.headers.get(h.MARKER_HEADER),
        "body": marked.text,
        "set_cookie_count": len(marked.headers.get_list("set-cookie")),
    }))
"""

_UNINSTALLED_APP_SCRIPT = """
    import json, os
    from harness import tts_ack_harness as h

    h.resolve_config(os.environ)          # 只校验环境，故意不调用 install()
    from app.main import app

    print(json.dumps({
        "harness_middleware": [
            str(m.cls) for m in app.user_middleware if "harness" in str(m.cls)],
    }))
"""


def test_production_app_carries_no_harness_middleware(tmp_path):
    """不调用 install() 时，生产 app 上不存在任何 harness 中间件。"""
    root = _private_root(tmp_path, "bare-root")
    payload = _payload(_run_script(_subprocess_env(root), _UNINSTALLED_APP_SCRIPT))
    assert payload["harness_middleware"] == []


def test_engine_substitutes_and_marker_boundaries(tmp_path):
    root = _private_root(tmp_path, "engine-root")
    payload = _payload(_run_script(
        _subprocess_env(root), _ENGINE_AND_MARKER_SCRIPT))

    # 合成引擎产出可用 WAV，且缓存落在临时 root（第二次才命中缓存）。
    assert payload["wav_ok"] is True
    assert payload["version"] == "harness-synthetic/1"
    assert payload["cached_first"] is False and payload["cached_second"] is True
    # The exact autopilot entry point must be injected too, or synthetic
    # acceptance would correctly degrade to 204 and play nothing.
    assert payload["exact_wav_ok"] is True
    assert payload["exact_version"] == "harness-synthetic/1"
    assert payload["exact_cached_first"] is False
    assert payload["exact_cached_second"] is True
    # One cache entry per distinct text, both inside the temporary root.
    assert payload["cache_files"] == 2
    assert payload["cache_dir"].startswith(str(root))
    assert not payload["cache_dir"].startswith(str(harness.PRODUCTION_DATA_DIR))

    # ASR 替身是本地边界，探针才可能真的跑通。
    assert payload["asr_boundary"] == "local"
    assert payload["asr_provider_id"] is None
    assert payload["asr_text"] == "苹果"
    assert payload["asr_version"] == "harness-synthetic-asr/1"

    # marker 中间件原样透传：正文不变、两条 Set-Cookie 都在。
    assert payload["marker"] == harness.MARKER_VALUE
    assert payload["body"] == "ok"
    assert payload["set_cookie_count"] == 2
