#!/usr/bin/env python3
"""Benchmark against dataset.json using per-item class schemas.

Reports completeness = extracted_schema_fields / requested_schema_fields
(not polluted by always asking for product fields on articles).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evidence_runtime.models import ExtractionRequest
from evidence_runtime.service import extract_async



def task_is_complete(result: dict, required_fields: list[str]) -> bool:
    if not required_fields:
        return False
    if result.get("status") == "error":
        return False
    valid_fields = {
        fact["field"]
        for fact in result.get("facts", [])
        if fact.get("validation_status") == "valid"
        and fact.get("evidence_ids")
        and fact["field"] in required_fields
    }
    return all(field in valid_fields for field in required_fields)


def field_completeness(result: dict, required_fields: list[str]) -> float:
    if not required_fields:
        return 0.0
    valid_fields = {
        fact["field"]
        for fact in result.get("facts", [])
        if fact.get("validation_status") == "valid"
        and fact.get("evidence_ids")
        and fact["field"] in required_fields
    }
    return len(valid_fields) / len(required_fields)


def evidence_validity(result: dict) -> float:
    facts = result.get("facts", [])
    if not facts:
        return 0.0
    valid = [
        fact
        for fact in facts
        if fact.get("validation_status") == "valid" and fact.get("evidence_ids")
    ]
    return len(valid) / len(facts)

async def run_one(item: dict) -> dict:
    url = item["url"]
    cls = item["class"]
    schema = item.get("schema") or {"fields": {"title": {"type": "string"}}}
    all_fields = schema.get("fields", schema)
    # Required fields drive completeness; optional=* is known L1 ceiling / not scored
    if isinstance(all_fields, dict):
        schema_fields = [
            k for k, v in all_fields.items()
            if not (isinstance(v, dict) and v.get("optional"))
        ]
        # still request optional fields for extraction, but don't score them
        request_fields = list(all_fields.keys())
    else:
        schema_fields = list(all_fields.keys()) if hasattr(all_fields, "keys") else ["title"]
        request_fields = schema_fields

    try:
        req = ExtractionRequest(
            url=url,
            schema={"fields": {k: all_fields.get(k, {"type": "string"}) if isinstance(all_fields, dict) else {"type": "string"} for k in request_fields}},
            locale="en-US",
            timezone="UTC",
            mode="http",
        )
        run = await extract_async(req, db_path=":memory:", save_snapshot=False, debug=True)

        extracted = {f.field for f in run.facts if f.field in schema_fields}
        completeness = len(extracted) / len(schema_fields) if schema_fields else 0.0

        tactic = next((w.split(":", 1)[1] for w in run.warnings if w.startswith("tactic:")), None)
        page_class = next((w.split(":", 1)[1] for w in run.warnings if w.startswith("page_class:")), None)
        needs_browser = any("needs_browser" in w for w in run.warnings)

        return {
            "url": url,
            "class": cls,
            "status": run.status.value,
            "latency_ms": run.latency_ms,
            "schema_fields": schema_fields,
            "extracted_fields": sorted(extracted),
            "missing_fields": [f for f in schema_fields if f not in extracted],
            "completeness": round(completeness, 3),
            "required_fields": schema_fields,
            "task_complete": task_is_complete(
                {"status": run.status.value,
                 "facts": [
                    {"field": f.field, "validation_status": f.validation_status,
                     "evidence_ids": [str(x) for x in f.evidence_ids]}
                    for f in run.facts
                 ]},
                schema_fields,
            ),
            "n_facts": len(run.facts),
            "methods": dict(Counter(f.extraction_method for f in run.facts)),
            "tactic": tactic,
            "page_class": page_class,
            "needs_browser": needs_browser,
            "failure_reason": run.failure_reason,
            "warnings": run.warnings,
            "facts": [
                {
                    "field": f.field,
                    "value": str(f.value)[:120],
                    "method": f.extraction_method,
                    "confidence": f.confidence,
                }
                for f in run.facts
                if f.field in schema_fields
            ],
            "debug_trace": run.debug_trace,
        }
    except Exception as exc:
        return {
            "url": url,
            "class": cls,
            "status": "error",
            "error": str(exc),
            "completeness": 0.0,
            "schema_fields": schema_fields,
            "extracted_fields": [],
            "missing_fields": schema_fields,
            "n_facts": 0,
            "methods": {},
            "latency_ms": None,
        }


async def main(dataset_path: str, output: str, limit: int | None, classes: list[str] | None):
    data = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    items = data["items"]
    if classes:
        items = [i for i in items if i["class"] in classes]
    if limit:
        items = items[:limit]

    print(f"Running {len(items)} items from {dataset_path}")
    results = []
    for idx, item in enumerate(items, 1):
        print(f"[{idx}/{len(items)}] {item['class']}: {item['url'][:70]}", flush=True)
        r = await run_one(item)
        results.append(r)
        print(
            f"  → {r.get('status')} completeness={r.get('completeness', 0):.0%} "
            f"fields={r.get('extracted_fields')} tactic={r.get('tactic')} "
            f"lat={r.get('latency_ms') and round(r['latency_ms'])}ms",
            flush=True,
        )

    # Aggregate
    by_class: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "completeness_sum": 0.0, "success": 0, "partial": 0,
        "failed": 0, "fetch_fail": 0, "needs_browser": 0, "latencies": [],
        "field_hits": Counter(), "field_misses": Counter(), "methods": Counter(),
        "tactics": Counter(),
    })

    for r in results:
        cls = r["class"]
        s = by_class[cls]
        s["n"] += 1
        s["completeness_sum"] += r.get("completeness") or 0
        st = r.get("status", "error")
        if st == "success":
            s["success"] += 1
        elif st == "partial":
            s["partial"] += 1
        else:
            s["failed"] += 1
            # distinguish network/404 from true extraction failure
            fr = (r.get("failure_reason") or r.get("error") or "")
            if any(x in fr for x in ("404", "401", "403", "timeout", "Timeout", "Connect", "HTTPStatus", "no_html")):
                s["fetch_fail"] += 1
        if r.get("needs_browser"):
            s["needs_browser"] += 1
        if r.get("latency_ms"):
            s["latencies"].append(r["latency_ms"])
        for f in r.get("extracted_fields") or []:
            s["field_hits"][f] += 1
        for f in r.get("missing_fields") or []:
            s["field_misses"][f] += 1
        for m, c in (r.get("methods") or {}).items():
            s["methods"][m] += c
        if r.get("tactic"):
            s["tactics"][r["tactic"]] += 1

    # Exclude pure fetch failures from completeness mean (optional fair metric)
    reachable = [r for r in results if r.get("status") in ("success", "partial", "unresolved")]
    overall_comp = (
        sum(r["completeness"] for r in results) / len(results) if results else 0
    )
    reachable_comp = (
        sum(r["completeness"] for r in reachable) / len(reachable) if reachable else 0
    )

    summary = {
        "total": len(results),
        "reachable": len(reachable),
        "overall_completeness": round(overall_comp, 3),
        "reachable_completeness": round(reachable_comp, 3),
        "status_counts": dict(Counter(r.get("status") for r in results)),
        "by_class": {},
        "methods_total": dict(sum((Counter(r.get("methods") or {}) for r in results), Counter())),
    }

    for cls, s in sorted(by_class.items()):
        lats = sorted(s["latencies"]) or [0]
        summary["by_class"][cls] = {
            "n": s["n"],
            "avg_completeness": round(s["completeness_sum"] / s["n"], 3) if s["n"] else 0,
            "success": s["success"],
            "partial": s["partial"],
            "failed": s["failed"],
            "fetch_fail": s["fetch_fail"],
            "needs_browser": s["needs_browser"],
            "p50_latency_ms": round(lats[len(lats) // 2], 1),
            "field_hits": dict(s["field_hits"]),
            "field_misses": dict(s["field_misses"]),
            "methods": dict(s["methods"]),
            "tactics": dict(s["tactics"]),
        }

    # --- honesty block: completeness alone can hide unusable runs ------------
    usable = [r for r in results if r.get("status") in ("success", "partial")]
    unusable = [r for r in results if r.get("status") not in ("success", "partial")]

    def _mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    completeness_by_status = {
        st: round(_mean([r.get("completeness") or 0 for r in results if r.get("status") == st]), 3)
        for st in ("success", "partial", "failed", "unresolved", "error")
    }
    failed_but_complete = [
        r["url"] for r in unusable if (r.get("completeness") or 0) >= 1.0
    ]
    summary.update({
        "usable": len(usable),
        "usable_rate": round(len(usable) / len(results), 3) if results else 0.0,
        "completeness_by_status": completeness_by_status,
        "failed_but_complete": failed_but_complete,
    })

    report = {"summary": summary, "per_url": results}
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # Print report
    print("\n" + "=" * 60)
    print("DATASET BENCHMARK (class-specific schemas)")
    print("=" * 60)
    print(f"Total: {summary['total']}  reachable: {summary['reachable']}")
    print(f"Overall completeness:   {summary['overall_completeness']:.1%}")
    print(f"Reachable completeness: {summary['reachable_completeness']:.1%}")
    print(f"Status: {summary['status_counts']}")
    print(f"Usable (success+partial): {summary['usable']}/{summary['total']} = {summary['usable_rate']:.1%}")
    print(f"Completeness by status: {summary['completeness_by_status']}")
    if summary["failed_but_complete"]:
        print(f"⚠ failed yet 100% complete: {len(summary['failed_but_complete'])} url(s) "
              f"→ {summary['failed_but_complete']}")
    print(f"Methods: {summary['methods_total']}")
    print(f"\n{'class':12} {'n':>3} {'comp':>6} {'ok':>3} {'part':>4} {'fail':>4} {'fetch':>5} {'brwsr':>5} {'p50ms':>6}")
    for cls, c in summary["by_class"].items():
        print(
            f"{cls:12} {c['n']:3} {c['avg_completeness']:6.1%} "
            f"{c['success']:3} {c['partial']:4} {c['failed']:4} {c['fetch_fail']:5} "
            f"{c['needs_browser']:5} {c['p50_latency_ms']:6.0f}"
        )
        hits = c["field_hits"]
        misses = c["field_misses"]
        fields = sorted(set(hits) | set(misses))
        for f in fields:
            h, m = hits.get(f, 0), misses.get(f, 0)
            rate = h / (h + m) if (h + m) else 0
            print(f"    {f:16} hit={h}/{h+m} ({rate:.0%})")
    print(f"\nSaved to {output}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="datasets/dataset.json")
    ap.add_argument("--output", default="benchmark_dataset_report.json")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--classes", nargs="+", default=None)
    args = ap.parse_args()
    asyncio.run(main(args.dataset, args.output, args.limit, args.classes))
