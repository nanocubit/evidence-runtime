"""Headline economics from runtime telemetry (importable, testable).

`scripts/avoidance_report.py` is a thin CLI over :func:`build_report`.

DuckDB allows a single writer, so while the HTTP service is running the file is
locked and a plain read-only connect fails. `build_report` therefore falls back
to reading a snapshot copy — metrics stay readable without stopping the service.
"""

from __future__ import annotations

import shutil
import tempfile
from collections import Counter
from pathlib import Path

import duckdb

BROWSER_LEVELS = {"L3", "L4"}
LLM_MARKERS = ("l2_llm_fill", "l2_failed")

# A live writer surfaces either of these depending on whether it is in-process
# (ConnectionException) or another process (IOException).
_LOCK_ERRORS = tuple(
    err for err in (
        getattr(duckdb, "IOException", None),
        getattr(duckdb, "ConnectionException", None),
    ) if err is not None
)


def _rate(part: int, total: int) -> float:
    return part / total if total else 0.0


def build_report(db_path: str) -> dict:
    """Aggregate browser/LLM avoidance and provenance coverage from telemetry."""
    try:
        return _read(db_path)
    except _LOCK_ERRORS:
        # The service holds the write lock; snapshot the file (and its WAL) and read that.
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "telemetry.duckdb"
            shutil.copy2(db_path, snap)
            wal = Path(str(db_path) + ".wal")
            if wal.exists():
                shutil.copy2(wal, Path(str(snap) + ".wal"))
            return _read(str(snap))


def _read(db_path: str) -> dict:
    con = duckdb.connect(db_path, read_only=True)
    try:
        rows = con.execute(
            "SELECT level, status, latency_ms, CAST(warnings AS VARCHAR) FROM runs"
        ).fetchall()
        total = len(rows)

        facts_total, facts_with_evidence = con.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM facts),
              (SELECT COUNT(*) FROM facts f WHERE EXISTS (
                  SELECT 1 FROM evidence e WHERE e.run_id = f.run_id
              ))
            """
        ).fetchone()
    finally:
        con.close()

    levels = Counter((r[0] or "unknown") for r in rows)
    statuses = Counter((r[1] or "unknown") for r in rows)
    browser_runs = sum(1 for r in rows if (r[0] or "") in BROWSER_LEVELS)
    llm_runs = sum(1 for r in rows if any(m in (r[3] or "") for m in LLM_MARKERS))

    reasons: Counter[str] = Counter()
    for r in rows:
        warnings = (r[3] or "").replace('"', " ").replace("[", " ").replace("]", " ")
        for token in warnings.split(","):
            token = token.strip()
            if token.startswith("route:"):
                reasons[token] += 1

    latencies = sorted(r[2] for r in rows if r[2])
    p50 = latencies[len(latencies) // 2] if latencies else 0.0
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else 0.0

    return {
        "total_runs": total,
        "by_level": dict(levels),
        "by_status": dict(statuses),
        "browser_avoidance_rate": round(_rate(total - browser_runs, total), 3),
        "llm_avoidance_rate": round(_rate(total - llm_runs, total), 3),
        "browser_runs": browser_runs,
        "llm_runs": llm_runs,
        "provenance_coverage": round(_rate(facts_with_evidence, facts_total), 3),
        "facts_total": facts_total,
        "facts_with_evidence": facts_with_evidence,
        "escalation_reasons": dict(reasons),
        "p50_latency_ms": round(p50, 1),
        "p95_latency_ms": round(p95, 1),
    }
