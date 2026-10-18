"""URL normalization and SSRF destination checks."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

ALLOWED_PORTS = {80, 443}
_TRACKING_PARAMS = {"gclid", "fbclid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src", "yclid", "msclkid"}
# Login-walled or terms-restricted sites: never fetched (search results pointing there are ignored).
BLOCKED_DOMAINS = frozenset({"linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com",
                             "threads.net", "glassdoor.com", "zoominfo.com", "rocketreach.co", "apollo.io"})


class BlockedURL(ValueError):
    """The URL is not an allowed fetch destination. Message is safe to log."""


def normalize_url(url: str) -> str:
    """Canonical form for caching/dedup: lowercase scheme+host, no default port, fragment, or tracking params."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port
    netloc = host if port in (None, 80 if scheme == "http" else 443) else f"{host}:{port}"
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS])
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, query, ""))


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def same_site(host: str, domain: str) -> bool:
    host, domain = host.lower().removeprefix("www."), domain.lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


def is_blocked_domain(host: str) -> bool:
    return any(same_site(host, d) for d in BLOCKED_DOMAINS)


def check_url_shape(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL(f"scheme {parts.scheme!r} not allowed")
    if parts.username or parts.password:
        raise BlockedURL("credentials in URL not allowed")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise BlockedURL("missing host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise BlockedURL(f"port {port} not allowed")
    if is_blocked_domain(host):
        raise BlockedURL(f"{host} is on the do-not-fetch list")
    return host


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not ip.is_multicast and not ip.is_reserved


async def resolve_public(host: str, port: int) -> list[str]:
    """Resolve and require EVERY address to be public (an attacker controls which one a client would pick)."""
    try:
        ipaddress.ip_address(host)
        literal = [host]
    except ValueError:
        literal = []
    if literal:
        addrs = literal
    else:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise BlockedURL(f"cannot resolve {host}") from exc
        addrs = sorted({str(info[4][0]) for info in infos})
    if not addrs or not all(is_public_ip(a) for a in addrs):
        raise BlockedURL(f"{host} resolves to a non-public address")
    return addrs


def absolute(base: str, href: str) -> str | None:
    href = href.strip()
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
        return None
    joined = urljoin(base, href)
    return joined if urlsplit(joined).scheme in ("http", "https") else None
