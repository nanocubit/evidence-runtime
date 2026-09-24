"""AdaptiveRouter: decision rules, telemetry, and service wiring."""

from __future__ import annotations

import json

from evidence_runtime import service
from evidence_runtime.models import ExtractionRequest
from evidence_runtime.router import AdaptiveRouter


def _req(url: str = "https://example.com/item/1", mode: str = "auto") -> ExtractionRequest:
    return ExtractionRequest(
        url=url,
        schema={"fields": {"title": {"type": "string"}}},
        mode=mode,  # type: ignore[arg-type]
    )


def test_http_mode_never_escalates():
    d = AdaptiveRouter().decide(_req(mode="http"), missing=["title"], signature={"browser_hint": True})
    assert d.backend == "L1_http"
    assert d.reason == "mode=http"


def test_browser_mode_always_escalates():
    d = AdaptiveRouter().decide(_req(mode="browser"), missing=[], signature={})
    assert d.backend == "L3_browser"
    assert d.reason == "mode=browser"


def test_bot_wall_escalates():
    d = AdaptiveRouter().decide(_req(), missing=[], signature={}, failure_reason="bot_wall")
    assert d.backend == "L3_browser"
    assert d.reason == "bot_wall"


def test_price_missing_with_spa_signal_escalates():
    d = AdaptiveRouter().decide(
        _req(),
        missing=["price"],
        signature={"spa_score": 0.7},
    )
    assert d.backend == "L3_browser"


def test_likely_spa_with_missing_escalates_in_auto_only():
    router = AdaptiveRouter()
    assert router.decide(_req(mode="auto"), missing=["title"], signature={"likely_spa": True}).backend == "L3_browser"
    assert router.decide(_req(mode="http"), missing=["title"], signature={"likely_spa": True}).backend == "L1_http"


def test_domain_rule_wins():
    router = AdaptiveRouter()
    router.register_rule("spa-shop.example", "L3_browser")
    d = router.decide(_req("https://spa-shop.example/p/7"), missing=[], signature={})
    assert d.backend == "L3_browser"
    assert d.reason.startswith("domain_rule")


def test_default_is_l1():
    d = AdaptiveRouter().decide(_req("https://example.com/blog/post"), missing=[], signature={})
    assert d.backend == "L1_http"


def test_record_fallback_writes_telemetry(tmp_path):
    path = tmp_path / "fallbacks.jsonl"
    router = AdaptiveRouter(telemetry_path=str(path))
    event = router.record_fallback("https://example.com/x", "L1_http", "L3_browser", "bot_wall")
    assert event["host"] == "example.com"
    assert router.fallbacks() and router.fallbacks()[0]["reason"] == "bot_wall"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["actual"] == "L3_browser"


def test_service_should_escalate_delegates_to_router():
    assert service.should_escalate(_req(mode="http"), missing=["t"], sig={"browser_hint": True}, failure_reason=None) is False
    assert service.should_escalate(_req(mode="browser"), missing=[], sig={}, failure_reason=None) is True
    assert service.should_escalate(_req(), missing=[], sig={}, failure_reason="bot_wall") is True


def test_service_uses_injected_router():
    router = AdaptiveRouter()
    router.register_rule("pinned.example", "L3_browser")
    previous = service.get_router()
    try:
        service.set_router(router)
        assert service.should_escalate(
            _req("https://pinned.example/p"), missing=[], sig={}, failure_reason=None
        ) is True
    finally:
        service.set_router(previous)
