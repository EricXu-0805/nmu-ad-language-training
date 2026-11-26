"""scripts/smoke_caddy_release.py 里不依赖真网关的判定(真网关部分只在 CI 的 image 门跑)。"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import ssl

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("smoke_caddy_release", ROOT / "scripts/smoke_caddy_release.py")
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


def test_oversized_post_refused_with_413():
    assert smoke.oversized_post_outcome(lambda: (413, {}, b"")) == "status_413"


@pytest.mark.parametrize("error", [
    ConnectionResetError(104, "Connection reset by peer"),
    BrokenPipeError(32, "Broken pipe"),
    ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
])
def test_oversized_post_refused_by_connection_reset(error):
    # Go 的服务端拒收超限请求体后只等约 500 ms 就关连接;runner 慢时客户端还在发,收到的是重置
    # 而不是 413(2026-11-26 CI 连续三次)。上游没收到这一条另由调用方核。
    def send():
        raise error
    assert smoke.oversized_post_outcome(send) == "connection_reset"


@pytest.mark.parametrize("status", [200, 400, 502])
def test_oversized_post_any_other_answer_fails(status):
    with pytest.raises(smoke.SmokeError, match="ordinary_large_post_not_413"):
        smoke.oversized_post_outcome(lambda: (status, {}, b""))


def test_oversized_post_other_transport_errors_are_not_treated_as_refusal():
    def send():
        raise TimeoutError("timed out")
    with pytest.raises(TimeoutError):
        smoke.oversized_post_outcome(send)


def _document(limits):
    return {"apps": {"http": {"servers": {"srv0": {"routes": [{"handle": [{"handler": "subroute", "routes": [
        {"handle": [{"handler": "request_body", "max_size": size}]} for size in limits]}]}]}}}}}


def test_request_body_limits_are_read_from_the_adapted_config():
    # 与生产 Caddyfile(ffc22bbc)经 caddy adapt 的结果一致:录音上传 64MB,其余 256KB。
    assert smoke.request_body_limits(_document([64000000, 256000])) == [256000, 64000000]


@pytest.mark.parametrize("limits", [[64000000], [64000000, 512000], [64000000, 256000, 256000], []])
def test_request_body_limits_must_be_exactly_audio_and_ordinary(limits):
    with pytest.raises(smoke.SmokeError, match="fixture_request_body_limits_changed"):
        smoke.require_request_body_limits(_document(limits))
