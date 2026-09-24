"""L0 extraction cache keyed by (normalized_url, schema_hash, content_signature)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

from .models import ExtractionRun


def _schema_hash(schema: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _cache_key(url: str, schema: dict[str, Any], content_signature: str) -> str:
    return f"{url}|{_schema_hash(schema)}|{content_signature}"


class ExtractionCache:
    """In-process LRU-ish cache. Thread-safe."""

    def __init__(self, max_entries: int = 256, ttl_s: float = 3600.0):
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(
        self,
        url: str,
        schema: dict[str, Any],
        content_signature: str,
    ) -> dict[str, Any] | None:
        key = _cache_key(url, schema, content_signature)
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            ts, payload = item
            if time.time() - ts > self.ttl_s:
                del self._data[key]
                return None
            return payload

    def put(
        self,
        url: str,
        schema: dict[str, Any],
        content_signature: str,
        run: ExtractionRun,
    ) -> None:
        key = _cache_key(url, schema, content_signature)
        # Store minimal replay payload
        payload = {
            "status": run.status.value,
            "facts": [f.model_dump(mode="json") for f in run.facts],
            "evidence": [e.model_dump(mode="json") for e in run.evidence],
            "missing_fields": list(run.missing_fields),
            "warnings": list(run.warnings),
            "content_hash": run.content_hash,
            "content_signature": run.content_signature,
            "failure_reason": run.failure_reason,
            "cache_hit": True,
        }
        with self._lock:
            if len(self._data) >= self.max_entries:
                # drop oldest
                oldest = min(self._data.items(), key=lambda x: x[1][0])
                del self._data[oldest[0]]
            self._data[key] = (time.time(), payload)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# process-wide singleton
_CACHE = ExtractionCache()


def get_cache() -> ExtractionCache:
    return _CACHE


def cache_key_with_etag(url: str, schema: dict, content_signature: str, etag: str | None) -> str:
    base = _cache_key(url, schema, content_signature)
    if etag:
        return base + "|" + etag.strip('"')
    return base
