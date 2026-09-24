#!/usr/bin/env python3
"""
Benchmark runner: extracts stats across multiple URLs.

Usage:
    python benchmark.py
    python benchmark.py --urls https://a.com https://b.com
"""
from __future__ import annotations
import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evidence_runtime.models import ExtractionRequest
from evidence_runtime.service import extract_async


DEFAULT_URLS = [
    "https://example.com",
    # Add 5-10 real URLs for meaningful stats
]

FIELDS = ["title", "description", "name", "price", "currency", "brand", "availability"]


def make_request(url: str) -> ExtractionRequest:
    return ExtractionRequest(
        url=url,
        schema={f: {"type": "string"} for f in FIELDS},
        locale="en-US",
        timezone="UTC",
        mode="http",
    )


async def run_benchmark(urls: list[str]) -> dict:
    stats = {
        "total_urls": len(urls),
        "processed": 0,
        "errors": 0,
        "method_counts": Counter(),
        "method_confidence_sum": Counter(),
        "conflicts_total": 0,
        "conflict_winners": Counter(),
        "missing_fields": Counter(),
        "latencies": [],
        "status_counts": Counter(),
        "signature_flags": Counter(),
    }

    per_url = []

    for idx, url in enumerate(urls, 1):
        print(f"[{idx}/{len(urls)}] {url}")
        try:
            req = make_request(url)
            run = await extract_async(req, db_path=":memory:")

            stats["processed"] += 1
            stats["latencies"].append(run.latency_ms or 0)
            stats["status_counts"][run.status.value] += 1

            url_report = {
                "url": url,
                "status": run.status.value,
                "latency_ms": run.latency_ms,
                "facts": [],
                "conflicts": [],
                "missing": run.missing_fields,
                "warnings": run.warnings,
            }

            for fact in run.facts:
                method = fact.extraction_method
                stats["method_counts"][method] += 1
                stats["method_confidence_sum"][method] += fact.confidence

                url_report["facts"].append({
                    "field": fact.field,
                    "value": str(fact.value)[:100],
                    "method": method,
                    "confidence": fact.confidence,
                })

                if fact.conflict.status != "none":
                    stats["conflicts_total"] += 1
                    winner = fact.conflict.resolution.get("winner_method", "unknown")
                    stats["conflict_winners"][winner] += 1
                    url_report["conflicts"].append({
                        "field": fact.field,
                        "winner": winner,
                        "policy": fact.conflict.resolution_policy,
                        "losers": [v["value"] for v in fact.conflict.values],
                    })

            for field in run.missing_fields:
                stats["missing_fields"][field] += 1

            # Signature flags from warnings
            for w in run.warnings:
                if "likely_spa" in w:
                    stats["signature_flags"]["likely_spa"] += 1
                if "jsonld_detected" in w:
                    stats["signature_flags"]["jsonld_detected"] += 1

            per_url.append(url_report)

        except Exception as exc:
            stats["errors"] += 1
            print(f"  ❌ {exc}")
            per_url.append({"url": url, "error": str(exc)})

    # Compute averages
    avg_latency = sum(stats["latencies"]) / len(stats["latencies"]) if stats["latencies"] else 0
    avg_conf = {
        m: stats["method_confidence_sum"][m] / stats["method_counts"][m]
        for m in stats["method_counts"]
    }

    report = {
        "summary": {
            "total_urls": stats["total_urls"],
            "processed": stats["processed"],
            "errors": stats["errors"],
            "success_rate": stats["status_counts"].get("success", 0) / max(stats["processed"], 1),
            "partial_rate": stats["status_counts"].get("partial", 0) / max(stats["processed"], 1),
            "avg_latency_ms": round(avg_latency, 2),
            "p50_latency_ms": round(sorted(stats["latencies"])[len(stats["latencies"]) // 2], 2) if stats["latencies"] else 0,
            "p95_latency_ms": round(sorted(stats["latencies"])[int(len(stats["latencies"]) * 0.95)], 2) if stats["latencies"] else 0,
        },
        "extraction": {
            "fields_by_method": dict(stats["method_counts"]),
            "avg_confidence_by_method": {k: round(v, 3) for k, v in avg_conf.items()},
            "conflicts": {
                "total": stats["conflicts_total"],
                "winner_by_method": dict(stats["conflict_winners"]),
            },
            "missing_fields": dict(stats["missing_fields"]),
            "signature_flags": dict(stats["signature_flags"]),
        },
        "per_url": per_url,
    }

    return report


def main():
    parser = argparse.ArgumentParser(description="Benchmark L1 extraction")
    parser.add_argument("--urls", nargs="+", default=None, help="URLs to test")
    parser.add_argument("--output", default="benchmark_report.json", help="Output file")
    args = parser.parse_args()

    urls = args.urls or DEFAULT_URLS
    report = asyncio.run(run_benchmark(urls))

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*50}")
    print("BENCHMARK REPORT")
    print(f"{'='*50}")
    print(f"URLs: {report['summary']['processed']}/{report['summary']['total_urls']}")
    print(f"Success rate: {report['summary']['success_rate']:.1%}")
    print(f"Avg latency: {report['summary']['avg_latency_ms']:.1f}ms")
    print(f"Fields by method: {report['extraction']['fields_by_method']}")
    print(f"Conflicts: {report['extraction']['conflicts']['total']}")
    print(f"Conflict winners: {report['extraction']['conflicts']['winner_by_method']}")
    print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
