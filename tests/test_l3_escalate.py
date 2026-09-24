"""L3 escalate unit tests (Playwright optional)."""
from __future__ import annotations

import pytest

from evidence_runtime.browser import playwright_available
from evidence_runtime.models import ExtractionRequest
from evidence_runtime.service import should_escalate


def test_should_escalate_http_never():
    req = ExtractionRequest(url="https://example.com", schema={"fields": {"title": {}}}, mode="http")
    assert should_escalate(req, missing=["title"], sig={"browser_hint": True}, failure_reason=None) is False


def test_should_escalate_browser_mode_always():
    req = ExtractionRequest(url="https://example.com", schema={"fields": {"title": {}}}, mode="browser")
    assert should_escalate(req, missing=[], sig={}, failure_reason=None) is True


def test_should_escalate_bot_wall_auto():
    req = ExtractionRequest(url="https://example.com", schema={"fields": {"title": {}}}, mode="auto")
    assert should_escalate(req, missing=[], sig={}, failure_reason="bot_wall") is True


def test_should_escalate_price_spa():
    req = ExtractionRequest(url="https://example.com", schema={"fields": {"price": {}}}, mode="auto")
    assert should_escalate(
        req, missing=["price"], sig={"spa_score": 0.7}, failure_reason=None
    ) is True


def test_should_not_escalate_complete_auto():
    req = ExtractionRequest(url="https://example.com", schema={"fields": {"title": {}}}, mode="auto")
    assert should_escalate(req, missing=[], sig={"browser_hint": True}, failure_reason=None) is False


@pytest.mark.skipif(not playwright_available(), reason="playwright not installed")
def test_l3_smoke_example_com():
    import asyncio

    from evidence_runtime.service import extract_async

    async def _run():
        req = ExtractionRequest(
            url="https://example.com",
            schema={"fields": {"title": {"type": "string"}}},
            mode="browser",
            locale="en-US",
            timezone="UTC",
        )
        return await extract_async(req, db_path=":memory:", save_snapshot=False, debug=True)

    run = asyncio.run(_run())
    assert run.attempted_level in ("L1", "L3")
    assert any("escalated:L3" in w for w in run.warnings)
