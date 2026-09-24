"""HTTP/2 fetch with encoding detection, size limits, sync + async."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .policies import FetchPolicy, PolicyError, robots_check, validate_mime, validate_url


@dataclass
class FetchedDocument:
    url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    text: str
    encoding: str
    content_type: str | None
    content_hash: str
    content_signature: str
    tls_info: dict[str, Any] | None = None
    ttfb_ms: float | None = None


def _detect_encoding(body: bytes, headers: dict[str, str]) -> str:
    ct = headers.get("content-type", "") or headers.get("Content-Type", "")
    m = re.search(r"charset=([\w-]+)", ct, re.IGNORECASE)
    if m:
        return m.group(1).lower()

    head = body[:8192].decode("ascii", errors="ignore")
    # Both single and double quotes
    m = re.search(r'''<meta[^>]+charset=["']?([\w-]+)''', head, re.IGNORECASE)
    if m:
        return m.group(1).lower()

    m = re.search(r'''<\?xml[^>]+encoding=["']([\w-]+)''', head, re.IGNORECASE)
    if m:
        return m.group(1).lower()

    return "utf-8"


def _decode_body(body: bytes, encoding: str) -> str:
    try:
        return body.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")


def _build_document(
    final_url: str,
    status_code: int,
    headers: dict[str, str],
    body: bytes,
    ttfb_ms: float | None = None,
) -> FetchedDocument:
    encoding = _detect_encoding(body, headers)
    text = _decode_body(body, encoding)
    return FetchedDocument(
        url=final_url,
        status_code=status_code,
        headers=headers,
        body=body,
        text=text,
        encoding=encoding,
        content_type=headers.get("content-type") or headers.get("Content-Type"),
        content_hash="sha256:" + hashlib.sha256(body).hexdigest(),
        # 16 KiB prefix — better SPA discrimination than 4 KiB
        content_signature="sha256:" + hashlib.sha256(body[:16384]).hexdigest(),
        tls_info=None,
        ttfb_ms=ttfb_ms,
    )


def fetch(url: str, policy: FetchPolicy | None = None) -> FetchedDocument:
    """Synchronous HTTP fetch."""
    policy = policy or FetchPolicy()
    validate_url(url, policy)
    if policy.respect_robots:
        rinfo = robots_check(url, policy.user_agent)
        if not rinfo.allowed:
            raise PolicyError(f"robots_disallowed: {url}")

    timeout = httpx.Timeout(policy.timeout_s, connect=10.0)
    with httpx.Client(
        http2=False,  # set True if h2 installed
        follow_redirects=True,
        max_redirects=policy.max_redirects,
        timeout=timeout,
        headers={"user-agent": policy.user_agent},
    ) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            validate_mime(response.headers.get("content-type"), policy)

            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > policy.max_bytes:
                    raise ValueError(f"response exceeds max_bytes={policy.max_bytes}")
                chunks.append(chunk)

            body = b"".join(chunks)
            return _build_document(
                str(response.url),
                response.status_code,
                dict(response.headers),
                body,
            )


async def fetch_async(url: str, policy: FetchPolicy | None = None) -> FetchedDocument:
    """Async HTTP fetch. Stream is consumed exactly once (no double-read)."""
    policy = policy or FetchPolicy()
    validate_url(url, policy)
    if policy.respect_robots:
        rinfo = robots_check(url, policy.user_agent)
        if not rinfo.allowed:
            raise PolicyError(f"robots_disallowed: {url}")

    timeout = httpx.Timeout(policy.timeout_s, connect=10.0)
    async with httpx.AsyncClient(
        http2=False,  # set True if h2 installed
        follow_redirects=True,
        max_redirects=policy.max_redirects,
        timeout=timeout,
        headers={"user-agent": policy.user_agent},
    ) as client:
        from time import perf_counter
        t0 = perf_counter()
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            ttfb_ms = (perf_counter() - t0) * 1000
            validate_mime(response.headers.get("content-type"), policy)

            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > policy.max_bytes:
                    raise ValueError(f"response exceeds max_bytes={policy.max_bytes}")
                chunks.append(chunk)

            body = b"".join(chunks)
            return _build_document(
                str(response.url),
                response.status_code,
                dict(response.headers),
                body,
                ttfb_ms=ttfb_ms,
            )
