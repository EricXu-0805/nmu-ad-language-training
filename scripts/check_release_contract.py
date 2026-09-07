#!/usr/bin/env python3
"""Reject mutable production image references without echoing the reference."""
from __future__ import annotations

import os
from ipaddress import ip_address, ip_network
import re
import sys


_IMMUTABLE_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_REJECTED_EDGE_DIGESTS = frozenset({
    # Caddy 2.11.4 upstream image built with vulnerable Go. Renaming the
    # registry/tag cannot make the same immutable content safe.
    "5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648",
})
_PRIVATE_PROXY_NETWORKS = (
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("fc00::/7"),
)


class ReleaseContractError(RuntimeError):
    """A stable, non-secret release-contract rejection."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def validate_image_reference(value: object) -> str:
    """Return a digest-pinned OCI reference or fail closed.

    A tag may remain before the digest for readability, but the final selector
    must be exactly one lowercase sha256 digest.  Diagnostics deliberately never
    echo the supplied value because registry references can contain private
    infrastructure names.
    """

    if not isinstance(value, str) or len(value) > 512:
        raise ReleaseContractError("release_image_not_immutable")
    if not _IMMUTABLE_IMAGE.fullmatch(value):
        raise ReleaseContractError("release_image_not_immutable")
    return value


def validate_forwarded_allow_ips(value: object) -> str:
    """Return one exact private proxy address or fail closed.

    Uvicorn accepts ``*``, CIDRs and comma-separated proxy lists in
    ``FORWARDED_ALLOW_IPS``.  Those forms are intentionally forbidden here:
    this deployment has exactly one measured Caddy bridge peer, and widening
    trust would let a client-supplied X-Forwarded-For bypass source-IP audit
    and authentication throttles.  Diagnostics never echo the supplied value.
    """

    if not isinstance(value, str) or not value or len(value) > 64:
        raise ReleaseContractError("forwarded_allow_ips_not_exact_private_ip")
    if value != value.strip():
        raise ReleaseContractError("forwarded_allow_ips_not_exact_private_ip")
    try:
        address = ip_address(value)
    except ValueError as exc:
        raise ReleaseContractError(
            "forwarded_allow_ips_not_exact_private_ip") from exc
    if (
        address.is_loopback
        or address.is_unspecified
        or address.is_multicast
        or address.is_link_local
        or not any(address in network for network in _PRIVATE_PROXY_NETWORKS)
    ):
        raise ReleaseContractError("forwarded_allow_ips_not_exact_private_ip")
    return value


def validate_edge_contract(mode: object, image: object) -> None:
    """Require an explicit edge topology and immutable embedded image.

    Host binary provenance and future OCI publication/scan evidence are separate
    release gates. This check rejects missing/mutable/known-bad pointers; an
    arbitrary digest is not an approval or an attestation.
    """
    if mode == "host":
        if image not in (None, ""):
            raise ReleaseContractError("host_edge_image_must_be_unset")
        return
    if mode != "embedded":
        raise ReleaseContractError("edge_mode_not_explicit")
    try:
        reference = validate_image_reference(image)
    except ReleaseContractError as exc:
        raise ReleaseContractError("edge_image_not_immutable") from exc
    if reference.rsplit("@sha256:", 1)[1] in _REJECTED_EDGE_DIGESTS:
        raise ReleaseContractError("edge_image_known_vulnerable")


def main() -> int:
    try:
        validate_image_reference(os.environ.get("NMU_RELEASE_IMAGE"))
        validate_forwarded_allow_ips(os.environ.get("FORWARDED_ALLOW_IPS"))
        validate_edge_contract(os.environ.get("NMU_EDGE_MODE"), os.environ.get("NMU_EDGE_IMAGE"))
    except ReleaseContractError as exc:
        print(f"REJECTED code={exc.code}", file=sys.stderr, flush=True)
        return 78
    print("OK release_image_immutable", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
