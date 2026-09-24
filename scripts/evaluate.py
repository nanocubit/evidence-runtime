#!/usr/bin/env python3
"""
Evaluate extraction results against ground truth.

Usage:
    python evaluate.py dataset_ground_truth.json --db benchmark.duckdb
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from evidence_runtime.models import ExtractionRequest
from evidence_runtime.service import extract


def fuzzy_match(actual: Any, expected: Any, field: str) -> bool:
    """Fuzzy match for ground truth comparison."""
    if actual is None or expected is None:
        return False
    a = str(actual).lower().strip()
    e = str(expected).lower().strip()
    # Exact or substring
    if a == e or e in a or a in e:
        return True
    # Number tolerance (10%)
    try:
        na, ne = float(a.replace(",", "")), float(e.replace(",", ""))
        if abs(na - ne) / max(abs(ne), 1) < 0.1:
            return True
    except (ValueError, ZeroDivisionError):
        pass
    return False


def evaluate_item(item: dict, run_facts: list) -> dict:
    """Evaluate a single item against ground truth."""
    expected = item.get("expected_values") or {}
    if not expected:
        return {"evaluated": False, "reason": "no_ground_truth"}

    actual = {f.field: f.value for f in run_facts}
    per_field = {}
    correct = 0
    total = 0

    for field, expected_val in expected.items():
        total += 1
        actual_val = actual.get(field)
        matched = fuzzy_match(actual_val, expected_val, field)
        per_field[field] = {
            "expected": expected_val,
            "actual": actual_val,
            "matched": matched,
        }
        if matched:
            correct += 1

    return {
        "evaluated": True,
        "precision": correct / total if total else 0.0,
        "recall": correct / total if total else 0.0,
        "correct_fields": correct,
        "total_fields": total,
        "per_field": per_field,
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate extraction against ground truth")
    parser.add_argument("dataset", help="Path to dataset with expected_values")
    parser.add_argument("--db", default="benchmark.duckdb", help="DuckDB path")
    parser.add_argument("--output", default="evaluation_report.json", help="Output report")
    parser.add_argument("--classes", nargs="+", default=None,
                        help="Filter by classes")
    args = parser.parse_args()

    with open(args.dataset, encoding="utf-8") as f:
        dataset = json.load(f)

    items = dataset.get("items", dataset.get("results", []))
    if args.classes:
        items = [i for i in items if i.get("class") in args.classes]

    reports = []
    class_stats: dict[str, dict] = {}

    for idx, item in enumerate(items, 1):
        url = item["url"]
        cls = item.get("class", "unknown")
        expected = item.get("expected_values") or item.get("candidate_values")

        if not expected:
            print(f"[{idx}] SKIP {url} — no ground truth")
            continue

        print(f"\n[{idx}] {cls}: {url}")

        # Re-run extraction (or use cached)
        req = ExtractionRequest(
            url=url,
            schema=item.get("schema", {}),
            mode="auto",
            locale="en-US",
        )
        try:
            run = extract(req, args.db)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            continue

        eval_result = evaluate_item(item, run.facts)
        report = {
            "url": url,
            "class": cls,
            "status": run.status.value,
            "level": run.level,
            "latency_ms": run.latency_ms,
            "evaluation": eval_result,
        }
        reports.append(report)

        if eval_result["evaluated"]:
            p = eval_result["precision"]
            print(f"  precision={p:.2f} ({eval_result['correct_fields']}/{eval_result['total_fields']})")
            for field, res in eval_result["per_field"].items():
                mark = "✅" if res["matched"] else "❌"
                print(f"    {mark} {field}: expected={res['expected']!r} actual={res['actual']!r}")

            # Class aggregation
            if cls not in class_stats:
                class_stats[cls] = {"correct": 0, "total": 0, "count": 0, "latencies": []}
            class_stats[cls]["correct"] += eval_result["correct_fields"]
            class_stats[cls]["total"] += eval_result["total_fields"]
            class_stats[cls]["count"] += 1
            class_stats[cls]["latencies"].append(run.latency_ms or 0)

    # Aggregate
    summary = {}
    for cls, stats in class_stats.items():
        summary[cls] = {
            "precision": stats["correct"] / stats["total"] if stats["total"] else 0.0,
            "item_count": stats["count"],
            "p50_latency_ms": sorted(stats["latencies"])[len(stats["latencies"]) // 2] if stats["latencies"] else 0,
            "p95_latency_ms": sorted(stats["latencies"])[int(len(stats["latencies"]) * 0.95)] if stats["latencies"] else 0,
        }

    overall_correct = sum(s["correct"] for s in class_stats.values())
    overall_total = sum(s["total"] for s in class_stats.values())

    report = {
        "metadata": {
            "dataset": args.dataset,
            "total_evaluated": len(reports),
            "overall_precision": overall_correct / overall_total if overall_total else 0.0,
        },
        "by_class": summary,
        "per_item": reports,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n{'='*50}")
    print("EVALUATION SUMMARY")
    print(f"{'='*50}")
    print(f"Overall precision: {report['metadata']['overall_precision']:.2%}")
    for cls, s in summary.items():
        print(f"  {cls}: precision={s['precision']:.2%}, items={s['item_count']}, "
              f"p50_latency={s['p50_latency_ms']:.0f}ms")
    print(f"\nReport saved to {args.output}")


if __name__ == "__main__":
    main()
