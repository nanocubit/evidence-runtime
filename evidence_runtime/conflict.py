"""Conflict detection — thin re-export of the canonical resolver in extract.py.

Kept for backward compatibility with external imports.
Prefer extract._resolve_conflicts / extract_l1 for new code.
"""

from __future__ import annotations

from .extract import _resolve_conflicts as detect_conflicts  # noqa: F401
from .models import Conflict, Fact  # noqa: F401

CONFLICT_POLICIES = {
    "method_priority_then_confidence": "JSON-LD > Trafilatura > CSS, then confidence",
    "highest_confidence": "max confidence wins (legacy)",
}

__all__ = ["detect_conflicts", "Conflict", "Fact", "CONFLICT_POLICIES"]
