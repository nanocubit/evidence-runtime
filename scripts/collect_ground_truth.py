#!/usr/bin/env python3
"""
Collect ground truth candidates from dataset extraction.
Run this to get candidate values for manual verification.

Usage:
    python collect_ground_truth.py dataset.json --db benchmark.duckdb --output dataset_candidates.json
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

# Add package to path if running from repo root
sys.path.insert(0, str(Path(__file__).parent))

from evidence_runtime.models import ExtractionRequest
from evidence_runtime.service import extract


def main():
    parser = argparse.ArgumentParser(description="Collect ground truth candidates")
    parser.add_argument("dataset", help="Path to dataset.json")
    parser.add_argument("--db", default="benchmark.duckdb", help="DuckDB path")
    parser.add_argument("--output", default="dataset_candidates.json", help="Output path")
    parser.add_argument("--limit", type=int, default=None, help="Limit items to process")
    parser.add_argument("--classes", nargs="+", default=None,
                        help="Filter by classes (e.g. article product)")
    args = parser.parse_args()

    with open(args.dataset, encoding="utf-8") as f:
        dataset = json.load(f)

    items = dataset["items"]
    if args.classes:
        items = [i for i in items if i["class"] in args.classes]
    if args.limit:
        items = items[:args.limit]

    results = []
    for idx, item in enumerate(items, 1):
        url = item["url"]
        cls = item["class"]
        print(f"\n[{idx}/{len(items)}] {cls}: {url}")

        req = ExtractionRequest(
            url=url,
            schema=item["schema"],
            mode="auto",
            locale="en-US",
        )

        try:
            run = extract(req, args.db)
            candidate = {
                "url": url,
                "class": cls,
                "schema": item["schema"],
                "status": run.status.value,
                "level": run.level,
                "latency_ms": run.latency_ms,
                "bytes_downloaded": run.bytes_downloaded,
                "missing_fields": run.missing_fields,
                "warnings": run.warnings,
                "candidate_values": {f.field: f.value for f in run.facts},
                "evidence_count": len(run.evidence),
                "notes": item.get("notes", ""),
            }
            results.append(candidate)

            print(f"  status={run.status.value} level={run.level} "
                  f"latency={run.latency_ms:.1f}ms facts={len(run.facts)} "
                  f"missing={run.missing_fields}")
            for f in run.facts:
                print(f"    {f.field}: {f.value!r} (confidence={f.confidence})")

        except Exception as exc:
            print(f"  ERROR: {exc}")
            results.append({
                "url": url,
                "class": cls,
                "status": "error",
                "error": str(exc),
                "notes": item.get("notes", ""),
            })

    # Save candidates
    output = {
        "metadata": {
            "source_dataset": args.dataset,
            "total_processed": len(results),
            "db": args.db,
            "ground_truth_status": "candidates_collected",
            "next_step": "Manually verify candidate_values, fill expected_values, save as dataset_ground_truth.json",
        },
        "items": results,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n✅ Candidates saved to {args.output}")
    print("   Next: manually verify values, then run evaluate.py")


if __name__ == "__main__":
    main()
