"""Headline economics: how often did we avoid the browser and the LLM?

Reads the runtime telemetry (DuckDB written by every extraction run) and reports
the two numbers the pitch rests on:

    browser_avoidance_rate  — runs resolved without L3/L4 (no headless browser)
    llm_avoidance_rate      — runs resolved without the L2 LLM gap-fill

Plus provenance coverage (share of facts that carry an evidence row) and the
recorded escalation reasons, so a claim like "X% resolved without a browser"
can be audited instead of asserted.

    python scripts/avoidance_report.py                 # runtime.duckdb
    python scripts/avoidance_report.py --db path.duckdb --output reports/avoidance.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evidence_runtime.economy import build_report  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="runtime.duckdb")
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    if not Path(args.db).exists():
        raise SystemExit(f"no telemetry DB at {args.db} — run some extractions first")

    report = build_report(args.db)

    print("=" * 58)
    print("HEADLINE ECONOMICS (evidence-runtime telemetry)")
    print("=" * 58)
    print(f"  runs                     {report['total_runs']}")
    print(f"  browser_avoidance_rate   {report['browser_avoidance_rate']:.1%}"
          f"   (browser used in {report['browser_runs']} run(s))")
    print(f"  llm_avoidance_rate       {report['llm_avoidance_rate']:.1%}"
          f"   (LLM used in {report['llm_runs']} run(s))")
    print(f"  provenance_coverage      {report['provenance_coverage']:.1%}"
          f"   ({report['facts_with_evidence']}/{report['facts_total']} facts backed by evidence)")
    print(f"  latency p50/p95          {report['p50_latency_ms']:.0f} / {report['p95_latency_ms']:.0f} ms")
    print(f"  levels                   {report['by_level']}")
    print(f"  statuses                 {report['by_status']}")
    if report["escalation_reasons"]:
        print(f"  escalation reasons       {report['escalation_reasons']}")
    print("=" * 58)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
