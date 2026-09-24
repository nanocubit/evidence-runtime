#!/usr/bin/env python3
"""
Interactive CLI for quick manual verification of ground truth candidates.

Usage:
    python verify_cli.py dataset_auto_gt.json --output dataset_verified.json

For each item, shows:
- URL and class
- Candidate values from auto_ground_truth
- Original page source hints
- Prompts: [Y] accept / [N] reject / [E] edit / [S] skip / [Q] quit
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))


def color(text: str, code: str) -> str:
    """Simple ANSI colors."""
    codes = {
        "green": "\033[92m",
        "yellow": "\033[93m",
        "red": "\033[91m",
        "blue": "\033[94m",
        "bold": "\033[1m",
        "reset": "\033[0m",
    }
    return f"{codes.get(code, '')}{text}{codes['reset']}"


def show_item(item: dict, idx: int, total: int) -> None:
    """Display item for verification."""
    print(f"\n{color('='*60, 'bold')}")
    print(f"[{idx}/{total}] {color(item['class'].upper(), 'blue')} — {item['url']}")
    print(f"{color('='*60, 'bold')}")

    expected = item.get("expected_values", {})
    validation = item.get("validation", {})
    consensus = item.get("consensus_details", {})

    if not expected:
        print(color("  No candidate values found.", "yellow"))
        return

    print(f"\n  {color('Candidate values:', 'bold')}")
    for field, value in expected.items():
        val = validation.get(field, {})
        valid = val.get("valid", True)
        warnings = val.get("warnings", [])
        conf_data = consensus.get(field, {})
        conf = conf_data.get("confidence", "?")
        sources = conf_data.get("sources", "?")

        status = color("✓", "green") if valid and not warnings else color("⚠", "yellow")
        warn_str = f" [{', '.join(warnings)}]" if warnings else ""
        print(f"    {status} {color(field, 'bold')}: {value!r}")
        print(f"       confidence={conf}, sources={sources}{color(warn_str, 'red')}")

    # Show alternative values from other sources
    print(f"\n  {color('Alternative values (from other sources):', 'bold')}")
    for source_name, source_data in item.get("sources", {}).items():
        if not source_data:
            continue
        print(f"    {color(source_name, 'blue')}:")
        for field, value in source_data.items():
            if field not in expected or expected.get(field) != value:
                print(f"      {field}: {value!r}")


def edit_value(field: str, current: Any) -> Any:
    """Interactive edit of a single value."""
    print(f"\n  Editing {color(field, 'bold')}:")
    print(f"  Current: {current!r}")
    print("  Enter new value (empty = keep current, 'null' = remove):")
    try:
        new = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        return current
    if new == "":
        return current
    if new.lower() == "null":
        return None
    # Try to preserve type
    if isinstance(current, bool):
        return new.lower() in {"true", "1", "yes", "да"}
    if isinstance(current, (int, float)):
        try:
            return float(new) if "." in new else int(new)
        except ValueError:
            pass
    return new


def verify_item(item: dict) -> dict | None:
    """Interactive verification of one item. Returns verified item or None to skip."""
    expected = dict(item.get("expected_values", {}))

    print(f"\n  {color('Actions:', 'bold')} [Y] accept all  [N] reject all  [E] edit  [S] skip  [Q] quit")
    try:
        action = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None

    if action == "q":
        raise SystemExit(0)
    if action == "s":
        return None
    if action == "n":
        item["expected_values"] = {}
        item["verification_status"] = "rejected"
        return item
    if action == "y":
        item["verification_status"] = "accepted"
        return item
    if action == "e":
        # Edit mode
        fields = list(expected.keys())
        print(f"\n  {color('Fields:', 'bold')} {' '.join(f'[{i+1}]{f}' for i, f in enumerate(fields))} [A]ll")
        try:
            choice = input("  Select field to edit > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return item

        if choice == "a":
            for field in fields:
                expected[field] = edit_value(field, expected[field])
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(fields):
                    field = fields[idx]
                    expected[field] = edit_value(field, expected[field])
            except ValueError:
                pass

        item["expected_values"] = expected
        item["verification_status"] = "edited"
        return item

    # Default: accept
    item["verification_status"] = "accepted"
    return item


def main():
    parser = argparse.ArgumentParser(description="Interactive ground truth verification")
    parser.add_argument("input", help="Path to auto_ground_truth output")
    parser.add_argument("--output", default="dataset_verified.json", help="Output path")
    parser.add_argument("--start-from", type=int, default=1, help="Start from item N")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    items = data["items"]
    total = len(items)
    verified = []
    skipped = []

    print(color("\n=== Evidence Runtime Ground Truth Verification ===\n", "bold"))
    print(f"Total items: {total}")
    print("Commands: Y=accept  N=reject  E=edit  S=skip  Q=quit\n")

    for idx, item in enumerate(items, 1):
        if idx < args.start_from:
            continue

        show_item(item, idx, total)
        result = verify_item(item)

        if result:
            verified.append(result)
        else:
            skipped.append(item)

    # Merge: verified + skipped (marked as unverified)
    for item in skipped:
        item["verification_status"] = "skipped"

    output = {
        "metadata": {
            "source": args.input,
            "total": total,
            "verified": len(verified),
            "skipped": len(skipped),
            "method": "human_in_the_loop",
        },
        "items": verified + skipped,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n{color('='*50, 'bold')}")
    print(f"Verified: {len(verified)}")
    print(f"Skipped:  {len(skipped)}")
    print(f"Saved to: {args.output}")


if __name__ == "__main__":
    main()
