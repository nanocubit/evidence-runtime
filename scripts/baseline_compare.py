#!/usr/bin/env python3
"""Baseline comparison: evidence-runtime L1 vs a plain trafilatura extractor.

Answers the question the README never did: *does the L1 pipeline beat the
obvious library-only approach on the same URLs, in completeness and latency?*

Both sides see the same dataset and the same required fields; the baseline gets
metadata (title / description / author / date) from trafilatura alone.

    PYTHONPATH=. python scripts/baseline_compare.py --limit 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import trafilatura  # noqa: E402

from evidence_runtime.models import ExtractionRequest  # noqa: E402
from evidence_runtime.service import extract_async  # noqa: E402

UA = "Mozilla/5.0 (compatible; evidence-runtime-baseline/0.1)"


def required_fields(item: dict[str, Any]) -> list[str]:
    schema = item.get("schema") or {}
    fields = schema.get("fields", schema)
    if not isinstance(fields, dict):
        return ["title"]
    return [k for k, v in fields.items() if not (isinstance(v, dict) and v.get("optional"))]


def baseline_extract(url: str, required: list[str]) -> dict[str, Any]:
    """Plain httpx + trafilatura metadata — the 'library only' comparison arm."""
    t0 = time.perf_counter()
    try:
        resp = httpx.get(
            url,
            follow_redirects=True,
            timeout=20.0,
            headers={"User-Agent": UA},
        )
        html = resp.text
        status = resp.status_code
    except Exception as exc:  # noqa: BLE001
        return {"status": f"error:{type(exc).__name__}", "got": [], "completeness": 0.0,
                "latency_ms": (time.perf_counter() - t0) * 1000}

    meta: dict[str, Any] = {}
    raw = trafilatura.extract(html, output_format="json", with_metadata=True, include_comments=False)
    if raw:
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError:
            meta = {}

    title = (meta.get("title") or "").strip()
    values = {
        "title": title,
        "name": title,
        "description": (meta.get("description") or "").strip(),
        "author": (meta.get("author") or "").strip(),
        "publish_date": (meta.get("date") or "").strip(),
        "date": (meta.get("date") or "").strip(),
    }
    got = [f for f in required if str(values.get(f) or "").strip()]
    return {
        "status": "ok" if status < 400 else f"http:{status}",
        "got": sorted(got),
        "completeness": len(got) / len(required) if required else 0.0,
        "latency_ms": (time.perf_counter() - t0) * 1000,
    }


async def run_item(item: dict[str, Any]) -> dict[str, Any]:
    required = required_fields(item)
    schema = item.get("schema") or {"fields": {k: {"type": "string"} for k in required}}

    t0 = time.perf_counter()
    run = await extract_async(
        ExtractionRequest(url=item["url"], schema=schema, mode="http"),
        db_path=":memory:",
        save_snapshot=False,
    )
    l1_latency = (time.perf_counter() - t0) * 1000
    l1_got = sorted(f.field for f in run.facts if f.field in required)
    l1_comp = len(l1_got) / len(required) if required else 0.0

    base = baseline_extract(item["url"], required)

    return {
        "url": item["url"],
        "class": item.get("class"),
        "required": required,
        "l1": {"status": run.status.value, "got": l1_got, "completeness": round(l1_comp, 3),
               "latency_ms": round(l1_latency, 1)},
        "baseline": {**base, "completeness": round(base["completeness"], 3)},
        "delta_completeness": round(l1_comp - base["completeness"], 3),
    }


async def main(dataset: str, limit: int | None, output: str) -> None:
    data = json.loads(Path(dataset).read_text(encoding="utf-8"))
    items = data["items"] if isinstance(data, dict) else data
    if limit:
        items = items[:limit]

    print(f"Comparing L1 vs trafilatura-only on {len(items)} URLs from {dataset}\n")
    results = []
    for i, item in enumerate(items, 1):
        r = await run_item(item)
        results.append(r)
        print(f"[{i}/{len(items)}] {r['class']:10} L1={r['l1']['completeness']:.0%} "
              f"base={r['baseline']['completeness']:.0%} Δ={r['delta_completeness']:+.0%} "
              f"({r['l1']['latency_ms']:.0f}ms vs {r['baseline']['latency_ms']:.0f}ms)")

    l1_comp = sum(r["l1"]["completeness"] for r in results) / len(results) if results else 0.0
    base_comp = sum(r["baseline"]["completeness"] for r in results) / len(results) if results else 0.0
    l1_lat = [r["l1"]["latency_ms"] for r in results]
    base_lat = [r["baseline"]["latency_ms"] for r in results]

    wins = sum(1 for r in results if r["delta_completeness"] > 0)
    losses = sum(1 for r in results if r["delta_completeness"] < 0)
    ties = len(results) - wins - losses

    summary = {
        "n": len(results),
        "l1_mean_completeness": round(l1_comp, 3),
        "baseline_mean_completeness": round(base_comp, 3),
        "delta": round(l1_comp - base_comp, 3),
        "l1_mean_latency_ms": round(sum(l1_lat) / len(l1_lat), 1) if l1_lat else 0.0,
        "baseline_mean_latency_ms": round(sum(base_lat) / len(base_lat), 1) if base_lat else 0.0,
        "wins": wins, "losses": losses, "ties": ties,
    }

    print("\n" + "=" * 58)
    print("BASELINE COMPARISON (required fields only)")
    print("=" * 58)
    print(f"  L1         completeness {summary['l1_mean_completeness']:.1%}  "
          f"latency {summary['l1_mean_latency_ms']:.0f} ms")
    print(f"  trafilatura completeness {summary['baseline_mean_completeness']:.1%}  "
          f"latency {summary['baseline_mean_latency_ms']:.0f} ms")
    print(f"  delta {summary['delta']:+.1%}   wins {wins} / losses {losses} / ties {ties}")
    print("=" * 58)

    Path(output).write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"Saved {output}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="datasets/dataset_v2.json")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--output", default="reports/baseline_vs_trafilatura.json")
    args = ap.parse_args()
    asyncio.run(main(args.dataset, args.limit, args.output))
