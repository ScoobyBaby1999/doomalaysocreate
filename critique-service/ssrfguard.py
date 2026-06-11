from __future__ import annotations
import ipaddress
import os
import socket
from typing import Callable
from urllib.parse import urlsplit

# SSRF guard: block server-side fetches to non-public addresses.
#
# The gateway makes outbound HTTP on behalf of callers in two places: web_fetch (the URL
# comes from a model's web_fetch action during research) and repopack.fetch_repo_files
# (a redirect could leave github). Without a guard, an attacker-influenced URL/redirect
# can hit cloud metadata (169.254.169.254) or internal hosts and return the body to the
# model/user (found by the dogfood repo_audit, 2026-06).
#
# We resolve the target host and reject if ANY resolved address is private / loopback /
# link-local / reserved / multicast / unspecified. Literal-IP URLs are checked directly.
#
# Residual: this does not fully stop DNS rebinding (resolve-public-then-connect-private),
# which would require pinning the resolved IP through the TLS connection. It blocks the
# realistic metadata / internal-host vectors.

#   escape hatch for local dev / sims that legitimately hit localhost.
_ALLOW_PRIVATE = os.environ.get("SSRF_ALLOW_PRIVATE", "0").strip() in ("1", "true", "yes")

#   default resolver: host -> list of IP strings (all A/AAAA records).
def _default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


class BlockedAddress(ValueError):
    """raised when a URL resolves to a non-public (internal/metadata) address."""


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])  # strip any zone id
    except ValueError:
        return False  # unparseable -> treat as unsafe
    #   IPv4-mapped IPv6 (::ffff:127.0.0.1) -> evaluate the embedded v4 address.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def assert_public_url(url: str, *, resolver: Callable[[str], list[str]] | None = None) -> None:
    """Raise BlockedAddress unless `url` is http(s) and its host resolves only to public IPs."""
    if _ALLOW_PRIVATE:
        return
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedAddress(f"unsupported scheme: {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise BlockedAddress("missing host")
    #   literal IP -> check directly (no DNS). hostname -> resolve every record.
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        try:
            ips = (resolver or _default_resolver)(host)
        except OSError as e:
            raise BlockedAddress(f"cannot resolve host {host!r}: {type(e).__name__}") from e
    if not ips:
        raise BlockedAddress(f"host {host!r} resolved to no addresses")
    for ip in ips:
        if not _ip_is_public(ip):
            raise BlockedAddress(f"host {host!r} resolves to non-public address {ip}")
