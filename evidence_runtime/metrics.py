from __future__ import annotations

from typing import Any

from .models import ExtractionRun, RunMetrics


def compute_run_metrics(run: ExtractionRun, schema_fields: list[str]) -> RunMetrics:
    """Compute metrics for a single extraction run."""
    total_fields = len(schema_fields)
    extracted_fields = len(run.facts)
    m = RunMetrics()
    m.bytes_downloaded = run.bytes_downloaded
    m.schema_completeness = extracted_fields / total_fields if total_fields else 0.0
    m.fact_recall = extracted_fields / total_fields if total_fields else 0.0
    m.deterministic_success_rate = 1.0 if run.level == "L1" and run.status.value in ("success", "partial") else 0.0
    m.browser_avoidance_rate = 1.0 if run.level in ("L0", "L1", "L2") else 0.0
    m.llm_avoidance_rate = 1.0 if run.level in ("L0", "L1") else 0.0
    m.cost_per_fact = 0.0  # TODO: track token/API costs
    return m


def aggregate_metrics(runs: list[ExtractionRun]) -> dict[str, Any]:
    """Aggregate metrics across multiple runs."""
    if not runs:
        return {}
    total = len(runs)
    success = sum(1 for r in runs if r.status.value == "success")
    partial = sum(1 for r in runs if r.status.value == "partial")
    failed = sum(1 for r in runs if r.status.value == "failed")
    latencies = [r.latency_ms for r in runs if r.latency_ms is not None]
    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0
    return {
        "total_runs": total,
        "success_rate": success / total,
        "partial_rate": partial / total,
        "failure_rate": failed / total,
        "p50_latency_ms": p50,
        "p95_latency_ms": p95,
        "avg_bytes_downloaded": sum(r.bytes_downloaded for r in runs) / total,
        "browser_fallback_rate": sum(1 for r in runs if r.level in ("L3", "L4")) / total,
    }
