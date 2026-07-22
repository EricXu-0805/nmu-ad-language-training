"""直连 Uvicorn 与反代路径共用的最小 HTTP 浏览器安全边界。"""
from __future__ import annotations

import os
from urllib.parse import urlsplit


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=(self)",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self'; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'"
    ),
    "Cross-Origin-Resource-Policy": "same-origin",
}


def configured_hosts() -> tuple[str, ...]:
    return tuple(
        value.strip().rstrip(".").lower()
        for value in os.environ.get("TRUSTED_HOSTS", "").split(",")
        if value.strip()
    )


def host_allowed(host_header: str | None) -> bool:
    """未配置时保留本地开发兼容；显式配置后只接受精确 host，不接受通配符。"""
    allowed = configured_hosts()
    if not allowed:
        return True
    try:
        hostname = (urlsplit("//" + (host_header or "")).hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    return bool(hostname and hostname in allowed)


def apply_security_headers(response):  # noqa: ANN001
    for name, value in SECURITY_HEADERS.items():
        if name not in response.headers:
            response.headers[name] = value
    return response
