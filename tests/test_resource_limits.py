from app import resource_limits


def test_tts_token_bucket_limits_and_refills():
    resource_limits.reset_for_tests()
    for _ in range(12):
        assert resource_limits.consume("POST", "/tts/speak", "device:a", now=10.0) is None
    assert resource_limits.consume("POST", "/tts/speak", "device:a", now=10.0) == 1.0
    assert resource_limits.consume("POST", "/tts/speak", "device:b", now=10.0) is None
    assert resource_limits.consume("POST", "/tts/speak", "device:a", now=11.0) is None
    # Exact command-bound synthesis shares the same per-device TTS budget;
    # changing command keys cannot bypass the expensive-provider limiter.
    assert resource_limits.consume(
        "POST", "/sessions/S/autopilot/commands/C/tts", "device:a", now=11.0
    ) == 1.0


def test_ordinary_routes_are_not_bucketed():
    for _ in range(100):
        assert resource_limits.consume("GET", "/patients", "account:a", now=1.0) is None
