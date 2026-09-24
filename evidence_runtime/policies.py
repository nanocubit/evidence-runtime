"""Fetch policies: URL validation, MIME allowlist, SSRF protection, robots.txt."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

from .robots import RobotsInfo, is_allowed


@dataclass(frozen=True)
class FetchPolicy:
    timeout_s: float = 12.0
    max_bytes: int = 10_000_000
    max_redirects: int = 5
    user_agent: str = "evidence-runtime/0.3 (+research; https://github.com/evidence-runtime)"
    allowed_schemes: tuple[str, ...] = ("http", "https")
    respect_robots: bool = True  # real implementation via robots.py
    allowed_mime_prefixes: tuple[str, ...] = (
        "text/html",
        "application/xhtml+xml",
        "application/json",
        "application/ld+json",
    )
    blocked_hosts: tuple[str, ...] = (
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "metadata.google.internal",
        "169.254.169.254",
    )
    block_private_ips: bool = True
    min_tls_version: str = "1.2"
    # Honour Crawl-delay from robots.txt (seconds). None = use robots value or 0.
    min_crawl_delay_s: float = 0.0


class PolicyError(ValueError):
    """Raised when a URL or response violates fetch policy."""


_PRIVATE_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        return False


def validate_url(url: str, policy: FetchPolicy) -> None:
    p = urlparse(url)
    if p.scheme not in policy.allowed_schemes or not p.netloc:
        raise PolicyError(f"unsupported URL: {url}")

    hostname = (p.hostname or "").lower()
    if not hostname:
        raise PolicyError(f"missing hostname: {url}")

    if hostname in policy.blocked_hosts or hostname.endswith(".local"):
        raise PolicyError(f"blocked host: {hostname}")

    if policy.block_private_ips:
        if _is_private_ip(hostname):
            raise PolicyError(f"private IP blocked: {hostname}")
        try:
            infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
            for info in infos:
                ip = info[4][0]
                if _is_private_ip(ip):
                    raise PolicyError(f"resolves to private IP {ip}: {hostname}")
        except socket.gaierror:
            pass


def validate_mime(content_type: str | None, policy: FetchPolicy) -> None:
    if content_type is None:
        return
    ct = content_type.split(";")[0].strip().lower()
    if not any(ct.startswith(prefix) for prefix in policy.allowed_mime_prefixes):
        raise PolicyError(f"disallowed MIME type: {ct}")


def robots_check(url: str, user_agent: str) -> RobotsInfo:
    """Full robots.txt check. Returns RobotsInfo with allowed/crawl_delay/sitemaps."""
    return is_allowed(url, user_agent)


@dataclass
class BrowserPolicy:
    """L3 Playwright settings — acquisition only, extract stays L1."""

    enabled: bool = True
    timeout_s: float = 25.0
    wait_until: str = "domcontentloaded"  # load | domcontentloaded | networkidle | commit
    wait_selector: str | None = None
    wait_selector_timeout_ms: int = 8000
    headless: bool = True
    user_agent: str | None = None  # default: FetchPolicy.user_agent
    block_resources: tuple[str, ...] = ("image", "font", "media")
    viewport_width: int = 1280
    viewport_height: int = 720
    use_pool: bool = True
    pool_concurrency: int = 3  # max concurrent pages in shared Chromium
