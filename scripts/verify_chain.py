#!/usr/bin/env python3
"""Verify the provenance chain (tamper-evident, fails closed).

    python scripts/verify_chain.py                       # audit/provenance-chain.jsonl
    python scripts/verify_chain.py --path custom.jsonl

Exit code 0 = chain intact, 1 = broken/tampered, 2 = no chain yet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evidence_runtime import chain  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=None, help="chain file (default: ER_CHAIN_PATH or audit/provenance-chain.jsonl)")
    args = ap.parse_args()

    path = chain.chain_path(args.path)
    if not path.exists():
        print(f"no chain at {path}")
        return 2

    ok, count, err = chain.verify(path)
    if ok:
        print(f"OK   {count} entries verified · {path}")
        return 0
    print(f"FAIL {count} entries before failure · {err}")
    print(f"     {path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
