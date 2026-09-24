#!/usr/bin/env python3
"""
Example with full JSON output. Runs extraction on multiple URLs and saves results.

Usage:
    python example_with_json.py
"""
from __future__ import annotations
import json
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evidence_runtime.models import ExtractionRequest
from evidence_runtime.service import extract_async


URLS = [
    "https://example.com",
    # Add real URLs here for testing
]

FIELDS = ["title", "description", "name", "price", "currency", "brand"]


async def main():
    results = []

    for url in URLS:
        print(f"\n🔍 {url}")
        try:
            req = ExtractionRequest(
                url=url,
                schema={f: {"type": "string"} for f in FIELDS},
                locale="en-US",
                timezone="UTC",
                mode="http",
            )
            run = await extract_async(req, db_path=":memory:")

            print(f"  status={run.status.value} latency={run.latency_ms:.1f}ms facts={len(run.facts)}")
            for f in run.facts:
                if f.conflict.status != "none":
                    print(f"  ⚠️  {f.field}: conflict resolved by {f.conflict.resolution.get('winner_method')}")

            results.append(run.model_dump(mode="json"))

        except Exception as exc:
            print(f"  ❌ Error: {exc}")
            results.append({"url": url, "error": str(exc)})

    output_path = Path("example_output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
