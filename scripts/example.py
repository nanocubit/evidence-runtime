#!/usr/bin/env python3
"""
Minimal example: run L1 extraction on a single URL and print results.

Usage:
    python example.py
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path

# Add package to path (adjust if installed via pip)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evidence_runtime.models import ExtractionRequest
from evidence_runtime.service import extract_async


async def main():
    url = "https://example.com"
    fields = ["title", "description"]

    req = ExtractionRequest(
        url=url,
        schema={f: {"type": "string"} for f in fields},
        locale="en-US",
        timezone="UTC",
        mode="http",
    )

    print(f"🔍 Processing: {url}")
    run = await extract_async(req, db_path=":memory:")

    print(f"Status: {run.status.value}")
    print(f"Latency: {run.latency_ms:.2f} ms")
    print(f"Missing: {run.missing_fields}")
    print(f"Warnings: {run.warnings}")
    print(f"Router confidence: {run.backend_prediction_confidence}")

    print("\n📋 Facts:")
    for f in run.facts:
        conflict = ""
        if f.conflict.status != "none":
            conflict = f" [conflict: {f.conflict.resolution_policy}]"
        print(f"  {f.field}: {f.value!r} (method={f.extraction_method}, conf={f.confidence}){conflict}")

    print("\n📋 Evidence:")
    for e in run.evidence:
        print(f"  {e.selector or e.extraction_backend}: {e.evidence_text[:60]!r}...")


if __name__ == "__main__":
    asyncio.run(main())
