"""Browser pool unit tests (Playwright optional)."""
from __future__ import annotations

import asyncio

import pytest

from evidence_runtime.browser import (
    BrowserPool,
    fetch_browser,
    get_browser_pool,
    playwright_available,
    shutdown_browser_pool,
)
from evidence_runtime.policies import BrowserPolicy


@pytest.fixture(autouse=True)
def _reset_pool():
    yield
    if playwright_available():
        try:
            asyncio.run(shutdown_browser_pool())
        except Exception:
            pass


def test_pool_stats_initial():
    pool = BrowserPool(max_concurrency=2)
    assert pool.stats["launches"] == 0
    assert pool.stats["fetches"] == 0
    assert pool.stats["max_concurrency"] == 2


@pytest.mark.skipif(not playwright_available(), reason="playwright not installed")
def test_pool_reuses_browser_across_fetches():
    async def _run():
        await shutdown_browser_pool()
        pool = get_browser_pool(max_concurrency=2)
        policy = BrowserPolicy(timeout_s=20, use_pool=True)
        d1 = await pool.fetch("https://example.com", policy)
        d2 = await pool.fetch("https://example.com", policy)
        stats = pool.stats
        await pool.stop()
        return d1, d2, stats

    d1, d2, stats = asyncio.run(_run())
    assert d1.from_pool and d2.from_pool
    assert "Example" in (d1.text or "")
    assert stats["launches"] == 1  # single Chromium launch
    assert stats["fetches"] == 2


@pytest.mark.skipif(not playwright_available(), reason="playwright not installed")
def test_oneshot_not_from_pool():
    async def _run():
        await shutdown_browser_pool()
        doc = await fetch_browser(
            "https://example.com",
            BrowserPolicy(timeout_s=20, use_pool=False),
            use_pool=False,
        )
        return doc

    doc = asyncio.run(_run())
    assert doc.from_pool is False
    assert "Example" in (doc.text or "")
