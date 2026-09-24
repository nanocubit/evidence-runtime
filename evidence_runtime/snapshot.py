"""Store normalized HTML snapshots for audit and evidence alignment."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class SnapshotStore:
    """Store normalized HTML snapshots for audit and evidence alignment."""

    def __init__(self, base_dir: str = "snapshots"):
        self.base = Path(base_dir)
        self.enabled = True

    def _path(self, content_hash: str) -> Path:
        shard = content_hash.replace("sha256:", "")[:4]
        return self.base / shard / f"{content_hash.replace('sha256:', '')}.json"

    def save(self, content_hash: str, snapshot: dict[str, Any], raw_html: str) -> Path | None:
        if not self.enabled:
            return None
        path = self._path(content_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "snapshot": snapshot,
            "raw_html_length": len(raw_html),
            "raw_html_hash": "sha256:" + hashlib.sha256(raw_html.encode("utf-8")).hexdigest(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        return path

    def load(self, content_hash: str) -> dict[str, Any] | None:
        path = self._path(content_hash)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None
