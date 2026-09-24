#!/usr/bin/env python3
"""
Semi-automatic ground truth collection via cross-source consensus.

Uses multiple extraction strategies and votes for the most likely value:
- JSON-LD (structured data)
- OpenGraph meta tags
- Standard meta tags
- Visible text (h1, title, etc.)

Usage:
    python auto_ground_truth.py dataset.json --output dataset_auto_gt.json
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent))

from evidence_runtime.fetcher import fetch
from evidence_runtime.policies import FetchPolicy


def extract_jsonld(html_text: str) -> dict[str, Any]:
    """Extract JSON-LD structured data."""
    results = {}
    # Find all JSON-LD scripts - use two patterns to avoid quote issues
    patterns = [
        '<script[^>]*type=\\s*["\']application/ld\\+json["\'][^>]*>(.*?)</script>',
        "<script[^>]*type=\\s*['\"]application/ld\\+json['\"][^>]*>(.*?)</script>",
    ]
    for pat in patterns:
        for match in re.finditer(pat, html_text, re.DOTALL | re.IGNORECASE):
            try:
                data = json.loads(match.group(1).strip())
                if isinstance(data, list):
                    for item in data:
                        _extract_jsonld_item(item, results)
                else:
                    _extract_jsonld_item(data, results)
            except (json.JSONDecodeError, ValueError):
                continue
    return results


def _extract_jsonld_item(item: dict, results: dict) -> None:
    if not isinstance(item, dict):
        return
    if item.get("@type") in ("Product", "Offer", "AggregateOffer"):
        results["name"] = item.get("name", results.get("name"))
        results["brand"] = _get_nested(item, "brand", "name")
        results["price"] = _get_nested(item, "offers", "price") or item.get("price")
        results["currency"] = _get_nested(item, "offers", "priceCurrency") or item.get("priceCurrency")
        results["availability"] = _get_nested(item, "offers", "availability") or item.get("availability")
        results["description"] = item.get("description", results.get("description"))
    elif item.get("@type") in ("Article", "NewsArticle", "BlogPosting"):
        results["title"] = item.get("headline", results.get("title"))
        results["author"] = _get_nested(item, "author", "name")
        results["publish_date"] = item.get("datePublished", results.get("publish_date"))
        results["description"] = item.get("description", results.get("description"))
    elif item.get("@type") in ("WebPage", "AboutPage"):
        results["title"] = item.get("name", results.get("title")) or item.get("headline", results.get("title"))
        results["description"] = item.get("description", results.get("description"))


def _get_nested(d: dict, *keys: str) -> Any:
    for key in keys:
        if isinstance(d, dict) and key in d:
            d = d[key]
        else:
            return None
    return d


def _extract_meta_pattern(html_text: str, attr_name: str, attr_value: str, value_attr: str = "content") -> str | None:
    """Extract meta tag value by attribute match."""
    # Try double quotes first
    pat1 = f'<meta[^>]+{attr_name}="{re.escape(attr_value)}"[^>]+{value_attr}="([^"]*)"'
    m = re.search(pat1, html_text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Try single quotes
    pat2 = f"<meta[^>]+{attr_name}='{re.escape(attr_value)}'[^>]+{value_attr}='([^']*)'"
    m = re.search(pat2, html_text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def extract_opengraph(html_text: str) -> dict[str, Any]:
    """Extract OpenGraph meta tags."""
    results = {}
    og_map = {
        "og:title": "title",
        "og:description": "description",
        "og:site_name": "source",
        "product:price:amount": "price",
        "product:price:currency": "currency",
        "product:brand": "brand",
        "product:availability": "availability",
    }
    for og_key, field in og_map.items():
        val = _extract_meta_pattern(html_text, "property", og_key)
        if val:
            results[field] = val
    return results


def extract_standard_meta(html_text: str) -> dict[str, Any]:
    """Extract standard meta tags."""
    results = {}
    meta_map = {
        "description": "description",
        "author": "author",
        "keywords": "keywords",
    }
    for name, field in meta_map.items():
        val = _extract_meta_pattern(html_text, "name", name)
        if val:
            results[field] = val
    return results


def extract_visible(html_text: str) -> dict[str, Any]:
    """Extract from visible HTML elements."""
    from selectolax.parser import HTMLParser
    tree = HTMLParser(html_text)
    results = {}

    title = tree.css_first("title")
    if title:
        results["title"] = title.text(strip=True)

    h1 = tree.css_first("h1")
    if h1:
        results["name"] = h1.text(strip=True)
        results["title"] = results.get("title") or h1.text(strip=True)

    for node in tree.css('[data-price], .price, .cost, [itemprop="price"]'):
        text = node.text(strip=True) or node.attributes.get("content", "")
        if text:
            if re.search(r"[$\u20ac\u00a3\u00a5\u20bd]", text) or re.search(r"\d+[.,]?\d*", text):
                results["price_raw"] = text
                break

    return results


def consensus_vote(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Vote across sources. Pick most common non-null value."""
    all_fields = set()
    for src in sources:
        all_fields.update(src.keys())

    consensus = {}
    for field in all_fields:
        values = [str(src[field]).strip() for src in sources if field in src and src[field] is not None]
        if not values:
            continue
        normalized = [v.lower() for v in values]
        counts = Counter(normalized)
        most_common, count = counts.most_common(1)[0]
        original = next(v for v in values if v.lower() == most_common)
        agreement = count / len(values)
        consensus[field] = {
            "value": original,
            "confidence": round(agreement, 2),
            "sources": len(values),
            "agreeing": count,
            "all_values": list(dict.fromkeys(values)),
        }
    return consensus


def validate_value(field: str, value: Any, item_class: str) -> dict[str, Any]:
    """Apply domain-specific validation rules."""
    checks = {"valid": True, "warnings": []}

    if field == "price":
        try:
            v = float(str(value).replace(",", "").replace(" ", ""))
            if v <= 0:
                checks["valid"] = False
                checks["warnings"].append("price <= 0")
            if v > 10_000_000:
                checks["valid"] = False
                checks["warnings"].append("price suspiciously high")
        except ValueError:
            checks["valid"] = False
            checks["warnings"].append("price not a number")

    elif field == "currency":
        valid = {"USD", "EUR", "GBP", "RUB", "JPY", "CNY", "\u20bd", "$", "\u20ac", "\u00a3"}
        if str(value).upper() not in valid and str(value) not in valid:
            checks["warnings"].append(f"unusual currency: {value}")

    elif field == "availability":
        val = str(value).lower()
        if val not in {"true", "false", "1", "0", "in stock", "out of stock",
                       "available", "unavailable", "instock", "outofstock"}:
            checks["warnings"].append(f"unusual availability value: {value}")

    elif field in ("title", "name"):
        if len(str(value)) < 3:
            checks["valid"] = False
            checks["warnings"].append("title/name too short")
        if len(str(value)) > 500:
            checks["warnings"].append("title/name suspiciously long")

    return checks


def process_item(item: dict) -> dict:
    """Process a single dataset item and return auto-ground-truth."""
    url = item["url"]
    cls = item["class"]

    try:
        doc = fetch(url, FetchPolicy())
    except Exception as exc:
        return {"url": url, "class": cls, "status": "fetch_error", "error": str(exc)}

    jsonld = extract_jsonld(doc.text)
    og = extract_opengraph(doc.text)
    meta = extract_standard_meta(doc.text)
    visible = extract_visible(doc.text)

    sources = [jsonld, og, meta, visible]
    consensus = consensus_vote(sources)

    expected = {}
    validation = {}
    for field, data in consensus.items():
        if data["confidence"] >= 0.5:
            expected[field] = data["value"]
            validation[field] = validate_value(field, data["value"], cls)

    return {
        "url": url,
        "class": cls,
        "schema": item["schema"],
        "status": "auto",
        "expected_values": expected,
        "consensus_details": consensus,
        "validation": validation,
        "sources": {"jsonld": jsonld, "opengraph": og, "meta": meta, "visible": visible},
        "notes": item.get("notes", ""),
    }


def main():
    parser = argparse.ArgumentParser(description="Auto ground truth via consensus")
    parser.add_argument("dataset", help="Path to dataset.json")
    parser.add_argument("--output", default="dataset_auto_gt.json", help="Output path")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--classes", nargs="+", default=None)
    parser.add_argument("--min-confidence", type=float, default=0.5)
    args = parser.parse_args()

    with open(args.dataset, encoding="utf-8") as f:
        dataset = json.load(f)

    items = dataset["items"]
    if args.classes:
        items = [i for i in items if i["class"] in args.classes]
    if args.limit:
        items = items[:args.limit]

    results = []
    auto_accepted = 0
    needs_review = 0

    for idx, item in enumerate(items, 1):
        print(f"\n[{idx}/{len(items)}] {item['class']}: {item['url']}")
        result = process_item(item)
        results.append(result)

        if result["status"] == "auto":
            accepted = sum(1 for v in result["validation"].values() if v["valid"])
            total = len(result["validation"])
            print(f"  Auto: {accepted}/{total} fields valid")
            for field, data in result["consensus_details"].items():
                status = "\u2705" if result["validation"].get(field, {}).get("valid") else "\u26a0\ufe0f"
                print(f"    {status} {field}: {data['value']!r} (confidence={data['confidence']})")
            if accepted == total and total > 0:
                auto_accepted += 1
            else:
                needs_review += 1
        else:
            print(f"  ERROR: {result.get('error')}")
            needs_review += 1

    output = {
        "metadata": {
            "source": args.dataset,
            "method": "cross_source_consensus",
            "min_confidence": args.min_confidence,
            "total": len(results),
            "auto_accepted": auto_accepted,
            "needs_review": needs_review,
            "next_step": "Run verify_cli.py to review flagged items",
        },
        "items": results,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n{'='*50}")
    print(f"Auto-accepted: {auto_accepted}/{len(results)}")
    print(f"Needs review:  {needs_review}/{len(results)}")
    print(f"Saved to: {args.output}")


if __name__ == "__main__":
    main()
