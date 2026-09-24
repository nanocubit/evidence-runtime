"""Adaptive backend router — Phase 0: static rules; Phase 1: telemetry-driven ML.

The router decides which backend a request should start on and whether L1 must
escalate to L3. Every escalation is recorded via :meth:`AdaptiveRouter.record_fallback`
so the same telemetry can later train a learned policy.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .models import ExtractionRequest
from .tactics import get_profile, pick_tactic

Backend = Literal["L0_cache", "L1_http", "L2_http_deep", "L3_browser", "L4_full_browser"]


@dataclass(frozen=True)
class RoutingDecision:
    """Outcome of a routing decision, with the rule that produced it."""

    backend: Backend
    confidence: float
    reason: str


class AdaptiveRouter:
    """
    Phase 0: static rules from mode + URL tactic (+ optional per-domain overrides).
    Phase 1+: swap :meth:`decide` for a model trained on recorded fallbacks.

    Telemetry: pass ``telemetry_path`` (or set ``EVIDENCE_ROUTER_TELEMETRY``) to
    append every fallback as one JSON line — that file is the training set.
    """

    def __init__(self, telemetry_path: str | None = None) -> None:
        self._rules: dict[str, Backend] = {}
        self._fallbacks: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        path = telemetry_path if telemetry_path is not None else os.environ.get("EVIDENCE_ROUTER_TELEMETRY")
        self.telemetry_path: str | None = path or None

    # -- domain rules -------------------------------------------------------

    def register_rule(self, domain: str, backend: Backend) -> None:
        self._rules[domain.replace("www.", "")] = backend

    # -- decisions ----------------------------------------------------------

    @staticmethod
    def _domain(req: ExtractionRequest) -> str:
        raw = str(req.url)
        return raw.split("/")[2].replace("www.", "") if "://" in raw else ""

    def predict(
        self,
        req: ExtractionRequest,
        signature: dict[str, Any] | None = None,
    ) -> tuple[Backend, float]:
        """Start-backend prediction (kept for compatibility)."""
        decision = self.decide(req, signature=signature, missing=(), failure_reason=None)
        return decision.backend, decision.confidence

    def decide(
        self,
        req: ExtractionRequest,
        *,
        signature: dict[str, Any] | None = None,
        missing: list[str] | tuple[str, ...] = (),
        failure_reason: str | None = None,
        status: Any | None = None,
    ) -> RoutingDecision:
        """Return the backend to use next, with the rule that fired."""
        del status  # reserved: status-aware rules land with the ML policy
        sig = signature or {}
        missing = list(missing or [])

        if req.mode == "http":
            return RoutingDecision("L1_http", 1.0, "mode=http")

        domain = self._domain(req)
        if domain and domain in self._rules:
            backend = self._rules[domain]
            if backend == "L3_browser":
                return RoutingDecision("L3_browser", 0.9, f"domain_rule:{domain}")
            return RoutingDecision(backend, 0.7, f"domain_rule:{domain}")

        if req.mode == "browser":
            return RoutingDecision("L3_browser", 1.0, "mode=browser")

        if failure_reason == "bot_wall":
            return RoutingDecision("L3_browser", 0.95, "bot_wall")
        if failure_reason and failure_reason.startswith("needs_browser"):
            return RoutingDecision("L3_browser", 0.9, f"failure:{failure_reason}")

        if missing and sig.get("browser_hint"):
            return RoutingDecision("L3_browser", 0.85, "browser_hint+missing")

        if "price" in missing and float(sig.get("spa_score") or 0) >= 0.5:
            return RoutingDecision("L3_browser", 0.8, "price_missing+spa_score")

        if missing and sig.get("likely_spa") and req.mode == "auto":
            return RoutingDecision("L3_browser", 0.75, "likely_spa+missing")

        tactic = pick_tactic(str(req.url), sig)
        profile = get_profile(tactic)
        if profile.browser_hint:
            # Product/SPA often need a browser eventually — but L1 first.
            return RoutingDecision("L1_http", 0.55, f"tactic:{tactic}:browser_hint")
        if tactic == "docs":
            return RoutingDecision("L1_http", 0.9, "tactic:docs")
        return RoutingDecision("L1_http", 0.7, f"tactic:{tactic}")

    # -- telemetry ----------------------------------------------------------

    def record_fallback(
        self,
        url: str,
        attempted: Backend,
        actual: Backend,
        reason: str,
    ) -> dict[str, Any]:
        """Record an escalation event (in-memory + optional JSONL append)."""
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "host": self._host(url),
            "attempted": attempted,
            "actual": actual,
            "reason": reason,
        }
        with self._lock:
            self._fallbacks.append(event)
        if self.telemetry_path:
            try:
                path = Path(self.telemetry_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            except OSError:
                pass  # telemetry must never break extraction
        return event

    def fallbacks(self) -> list[dict[str, Any]]:
        """In-memory view of recorded fallbacks (tests, reports)."""
        with self._lock:
            return list(self._fallbacks)

    @staticmethod
    def _host(url: str) -> str:
        try:
            return url.split("/")[2].lower()
        except Exception:
            return url


def decision_to_dict(decision: RoutingDecision) -> dict[str, Any]:
    return asdict(decision)
