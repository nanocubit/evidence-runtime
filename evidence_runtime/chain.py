"""Tamper-evident provenance chain over extraction runs.

Every run appends one entry that links to the previous entry's hash:

    {ts, run_id, url, level, status, evidence_digest, context, prev_hash, hash}

`evidence_digest` is a digest of the run's facts *and* their evidence rows, so a
tampered fact or a swapped selector changes the chain. `verify()` walks the whole
file and fails closed on the first broken link, bad hash or malformed line —
same posture as the audit chain in Pearl Necklace, applied to our provenance.

Enabled by default for real runs; tests (in-memory DB) skip it.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def chain_path(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(path or os.environ.get("ER_CHAIN_PATH", "audit/provenance-chain.jsonl"))


def _canonical(entry: dict[str, Any]) -> bytes:
    core = {k: v for k, v in entry.items() if k not in ("hash", "signature")}
    return json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def entry_hash(entry: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(entry)).hexdigest()


def head(path: str | os.PathLike[str] | None = None) -> str:
    """Hash of the last entry, or GENESIS when the chain is empty."""
    p = chain_path(path)
    if not p.exists():
        return GENESIS
    last: str | None = None
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = line
    if last is None:
        return GENESIS
    try:
        return str(json.loads(last)["hash"])
    except (json.JSONDecodeError, KeyError):
        return GENESIS


def evidence_digest(run: Any) -> str:
    """Digest of facts + their evidence (selector, backend, content hash)."""
    rows: list[tuple[str, str, str, str, str]] = []
    evidence_by_id = {str(getattr(e, "evidence_id", "")): e for e in getattr(run, "evidence", []) or []}
    for fact in getattr(run, "facts", []) or []:
        ev_ids = sorted(str(x) for x in (getattr(fact, "evidence_ids", None) or []))
        first = evidence_by_id.get(ev_ids[0]) if ev_ids else None
        rows.append((
            str(getattr(fact, "field", "")),
            str(getattr(fact, "value", "")),
            str(getattr(first, "extraction_backend", "") if first else ""),
            str(getattr(first, "selector", "") if first else ""),
            str(getattr(first, "content_hash", "") if first else ""),
        ))
    rows.sort()
    blob = json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def append(
    run: Any,
    *,
    context: dict[str, Any] | None = None,
    path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Append one run to the chain (durable write) and return the entry."""
    p = chain_path(path)
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "run_id": str(getattr(run, "run_id", "")),
        "url": str(getattr(run, "normalized_url", "")),
        "level": str(getattr(run, "level", "")),
        "status": str(getattr(getattr(run, "status", None), "value", getattr(run, "status", ""))),
        "evidence_digest": evidence_digest(run),
        "context": context or {},
        "prev_hash": head(p),
    }
    entry["hash"] = entry_hash(entry)

    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return entry


def verify(path: str | os.PathLike[str] | None = None) -> tuple[bool, int, str | None]:
    """Walk the chain. Returns (ok, entries, error). Fails closed."""
    p = chain_path(path)
    if not p.exists():
        return True, 0, None

    prev = GENESIS
    count = 0
    with p.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                return False, count, f"line {line_number}: malformed JSON ({exc})"
            if not isinstance(entry, dict):
                return False, count, f"line {line_number}: entry is not an object"
            if entry.get("prev_hash") != prev:
                return False, count, f"line {line_number}: broken link (prev_hash mismatch)"
            if entry.get("hash") != entry_hash(entry):
                return False, count, f"line {line_number}: entry hash mismatch"
            prev = str(entry.get("hash"))
            count += 1
    return True, count, None
