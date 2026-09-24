#!/usr/bin/env python3
"""Evaluate extraction precision against ground-truth matchers.

Honesty upgrade (v0.4.1): the summary now reports
  * strict vs soft matcher split (soft = `contains` / `min_len`),
  * fields the extractor returned that ground truth did not ask for,
  * an `effective_precision` that counts unexpected extra fields as potential
    false positives — the number a reviewer would expect from the word "precision".

Matcher + aggregation live in `evidence_runtime.evalgt` (covered by tests).
The legacy `precision` key is kept unchanged so older reports stay comparable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evidence_runtime.evalgt import match_kind, rule_matches, summarize
from evidence_runtime.models import ExtractionRequest
from evidence_runtime.service import extract_async


async def run_item(item: dict[str, Any]) -> dict[str, Any]:
    schema = item.get("schema") or {"fields": {k: {"type": "string"} for k in item.get("expected_values", {})}}
    req = ExtractionRequest(url=item["url"], schema=schema, mode="http")
    run = await extract_async(req, db_path=":memory:", save_snapshot=False, debug=False)
    extracted = {f.field: f.value for f in run.facts}
    expected = item.get("expected_values") or {}

    field_results = {}
    tp = fp = fn = 0
    by_kind: dict[str, list[int]] = {"strict": [0, 0, 0], "soft": [0, 0, 0], "unknown": [0, 0, 0]}

    for field, rule in expected.items():
        kind = match_kind(rule)
        val = extracted.get(field)
        if val is None:
            fn += 1
            by_kind[kind][2] += 1
            field_results[field] = {"status": "missing", "kind": kind, "expected": rule, "got": None}
        elif rule_matches(rule, val):
            tp += 1
            by_kind[kind][0] += 1
            field_results[field] = {"status": "correct", "kind": kind, "got": val}
        else:
            fp += 1
            by_kind[kind][1] += 1
            field_results[field] = {"status": "wrong", "kind": kind, "expected": rule, "got": val}

    expected_fields = set(expected)
    extra_fields = sorted(f for f in extracted if f not in expected_fields)

    return {
        "id": item.get("id"),
        "url": item["url"],
        "class": item.get("class"),
        "status": run.status.value,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": tp / (tp + fp) if (tp + fp) else 0.0,
        "recall": tp / (tp + fn) if (tp + fn) else 0.0,
        "by_kind": by_kind,
        "extra_fields": extra_fields,
        "fields": field_results,
        "extracted": {k: extracted.get(k) for k in expected},
    }


async def main_async(path: Path, out: Path | None, limit: int | None = None) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data["items"] if isinstance(data, dict) else data
    if limit:
        items = items[:limit]
    results = []
    for i, item in enumerate(items, 1):
        print(f"[{i}/{len(items)}] {item.get('class')} {item['url'][:60]}")
        r = await run_item(item)
        results.append(r)
        print(f"  → P={r['precision']:.0%} R={r['recall']:.0%} tp={r['tp']} fp={r['fp']} fn={r['fn']}")
        for f, fr in r["fields"].items():
            if fr["status"] != "correct":
                print(f"     {f}: {fr['status']} got={fr.get('got')!r}")

    summary = summarize(results)

    print("\n" + "=" * 58)
    print(f"GT PRECISION  {summary['precision']:.1%}   RECALL  {summary['recall']:.1%}   "
          f"F1  {summary['f1']:.1%}")
    print(f"tp={summary['tp']} fp={summary['fp']} fn={summary['fn']} over {summary['n']} URLs")
    print("-" * 58)
    print("HONESTY BLOCK")
    for kind in ("strict", "soft", "unknown"):
        k = summary["by_kind"][kind]
        if k["tp"] or k["fp"] or k["fn"]:
            print(f"  {kind:7} P={k['precision']:.0%} R={k['recall']:.0%} "
                  f"tp={k['tp']} fp={k['fp']} fn={k['fn']}")
    print(f"  soft share of matches: {summary['soft_share_of_matches']:.0%}")
    print(f"  extra fields returned:  {summary['extra_fields_returned']} "
          f"across {summary['items_with_extra_fields']}/{summary['n']} items")
    print(f"  effective precision (extras as FP): {summary['effective_precision']:.1%}")
    print("=" * 58)

    payload = {"summary": summary, "results": results}
    if out:
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"Saved {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="datasets/dataset_gt.json")
    ap.add_argument("--output", default="benchmark_gt.json")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    asyncio.run(
        main_async(
            Path(args.dataset),
            Path(args.output) if args.output else None,
            args.limit,
        )
    )


if __name__ == "__main__":
    main()
