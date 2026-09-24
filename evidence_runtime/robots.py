"""robots.txt fetch, parse, allow-check, crawl-delay, sitemap discovery.

Voluntary standard — we respect it when policy.respect_robots=True.
Cached per-host with TTL to avoid extra RTT on every request.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

# host -> (RobotFileParser | None, crawl_delay, sitemaps, fetched_at)
_CACHE: dict[str, tuple[RobotFileParser | None, float | None, list[str], float]] = {}
_CACHE_TTL_S = 3600.0


@dataclass
class RobotsInfo:
    allowed: bool
    crawl_delay: float | None = None
    sitemaps: list[str] = field(default_factory=list)
    robots_url: str | None = None
    fetched: bool = False
    error: str | None = None


def _robots_url(base: str) -> str:
    p = urlparse(base)
    return f"{p.scheme}://{p.netloc}/robots.txt"


def _parse_crawl_delay(text: str, user_agent: str) -> float | None:
    """Extract Crawl-delay for matching UA (or *). urllib may not expose it on all Pythons."""
    ua_token = (user_agent or "*").split("/")[0].strip().lower()
    current_ua = "*"
    delay_for: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            current_ua = val.lower()
        elif key == "crawl-delay":
            try:
                delay_for[current_ua] = float(val)
            except ValueError:
                pass
    if ua_token in delay_for:
        return delay_for[ua_token]
    # prefix match e.g. user-agent: evidence-runtime
    for k, v in delay_for.items():
        if k != "*" and (ua_token.startswith(k) or k in ua_token):
            return v
    return delay_for.get("*")


def _parse_sitemaps(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("sitemap:"):
            url = line.split(":", 1)[1].strip()
            if url:
                out.append(url)
    return out


def fetch_robots(host_url: str, user_agent: str, timeout: float = 5.0) -> RobotsInfo:
    """Fetch and cache robots.txt for the host of host_url."""
    p = urlparse(host_url)
    host_key = f"{p.scheme}://{p.netloc}".lower()
    now = time.time()

    cached = _CACHE.get(host_key)
    if cached and (now - cached[3]) < _CACHE_TTL_S:
        rp, delay, sitemaps, _ = cached
        # allowed checked per-URL later
        return RobotsInfo(
            allowed=True,  # placeholder; use is_allowed
            crawl_delay=delay,
            sitemaps=sitemaps,
            robots_url=_robots_url(host_key),
            fetched=True,
        )

    robots_url = _robots_url(host_key)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers={"user-agent": user_agent}) as client:
            resp = client.get(robots_url)
            if resp.status_code == 404:
                # No robots.txt → allow all
                _CACHE[host_key] = (None, None, [], now)
                return RobotsInfo(allowed=True, robots_url=robots_url, fetched=True)
            if resp.status_code >= 400:
                _CACHE[host_key] = (None, None, [], now)
                return RobotsInfo(
                    allowed=True,
                    robots_url=robots_url,
                    fetched=True,
                    error=f"HTTP {resp.status_code}",
                )
            text = resp.text
    except Exception as exc:
        # Fail open on network errors (don't block extraction if robots unreachable)
        _CACHE[host_key] = (None, None, [], now)
        return RobotsInfo(allowed=True, robots_url=robots_url, fetched=False, error=str(exc))

    rp = RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.parse(text.splitlines())
    except Exception:
        rp = None

    delay = _parse_crawl_delay(text, user_agent)
    sitemaps = _parse_sitemaps(text)
    _CACHE[host_key] = (rp, delay, sitemaps, now)

    return RobotsInfo(
        allowed=True,
        crawl_delay=delay,
        sitemaps=sitemaps,
        robots_url=robots_url,
        fetched=True,
    )


def is_allowed(url: str, user_agent: str, timeout: float = 5.0) -> RobotsInfo:
    """Return whether url is allowed for user_agent, plus crawl-delay and sitemaps."""
    info = fetch_robots(url, user_agent, timeout=timeout)
    p = urlparse(url)
    host_key = f"{p.scheme}://{p.netloc}".lower()
    cached = _CACHE.get(host_key)
    if not cached or cached[0] is None:
        # no robots or unparsable → allow
        return RobotsInfo(
            allowed=True,
            crawl_delay=info.crawl_delay,
            sitemaps=info.sitemaps,
            robots_url=info.robots_url,
            fetched=info.fetched,
            error=info.error,
        )
    rp = cached[0]
    try:
        allowed = rp.can_fetch(user_agent, url)
    except Exception:
        allowed = True
    return RobotsInfo(
        allowed=allowed,
        crawl_delay=cached[1],
        sitemaps=cached[2],
        robots_url=info.robots_url,
        fetched=True,
    )


def clear_robots_cache() -> None:
    _CACHE.clear()
