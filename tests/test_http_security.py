from app import http_security


def test_host_allowlist_is_exact_and_opt_in(monkeypatch):
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    assert http_security.host_allowed("anything.invalid:8000")
    monkeypatch.setenv("TRUSTED_HOSTS", "training.example.com, 127.0.0.1")
    assert http_security.host_allowed("training.example.com:443")
    assert http_security.host_allowed("127.0.0.1:8000")
    assert not http_security.host_allowed("evil.training.example.com")
    assert not http_security.host_allowed("training.example.com.evil.invalid")
    assert not http_security.host_allowed("")


def test_security_header_set_matches_direct_browser_boundary():
    headers = http_security.SECURITY_HEADERS
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Permissions-Policy"].endswith("microphone=(self)")
