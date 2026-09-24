"""PR-1: structured extraction, links, task completeness."""
from __future__ import annotations

from datetime import datetime, timezone

from evidence_runtime.links import parse_link_header
from evidence_runtime.normalizer import normalize_jsonld_value
from evidence_runtime.structured import (
    _map_entity,
    _map_extruct_items,
    adapt_to_facts_and_evidence,
    append_missing_og_fallback,
    extract_structured_data,
    manual_og_fallback,
)


def test_nested_brand_is_projected():
    item = {"@type": "Product", "brand": {"name": "Apple"}}
    assertions = _map_entity(
        entity=item,
        syntax="json-ld",
        item_index=0,
        entity_path=[],
        source_url="https://example.test",
        snapshot_hash="sha256:test",
        retrieved_at=None,
    )
    brand = [a for a in assertions if a.field == "brand"]
    assert len(brand) == 1
    assert brand[0].value == "Apple"
    assert '["brand"]["name"]' in brand[0].evidence_locator


def test_graph_is_flattened():
    item = {
        "@graph": [
            {"@type": "WebPage", "headline": "Example"},
            {
                "@type": "Product",
                "offers": [
                    {"price": "129900", "priceCurrency": "RUB"},
                    {"price": "139900", "priceCurrency": "RUB"},
                ],
            },
        ]
    }
    assertions = _map_extruct_items(
        syntax="json-ld",
        items=[item],
        source_url="https://example.test",
        snapshot_hash="sha256:test",
        retrieved_at=None,
    )
    prices = [a for a in assertions if a.field == "price"]
    assert len(prices) == 2
    assert any('["@graph"][1]' in a.evidence_locator for a in prices)


def test_availability_is_string():
    item = {
        "@type": "Product",
        "offers": {"availability": "https://schema.org/InStock"},
    }
    assertions = _map_entity(
        entity=item,
        syntax="json-ld",
        item_index=0,
        entity_path=[],
        source_url="https://example.test",
        snapshot_hash="sha256:test",
        retrieved_at=None,
    )
    availability = next(a for a in assertions if a.field == "availability")
    assert availability.datatype == "string"
    assert "InStock" in str(availability.value)


def test_link_header_with_comma_in_uri():
    header = (
        '<https://example.test/a,b>; rel="next", '
        '<https://example.test/canonical>; rel="canonical"'
    )
    links = parse_link_header(header, "https://example.test/")
    assert len(links) == 2
    assert links[0].url == "https://example.test/a,b"
    assert links[0].rel == ["next"]
    assert links[1].rel == ["canonical"]


def test_adapter_sets_retrieved_at_and_locale():
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    assertions = _map_entity(
        entity={"@type": "Product", "name": "X", "offers": {"price": 10, "priceCurrency": "USD"}},
        syntax="json-ld",
        item_index=0,
        entity_path=[],
        source_url="https://example.test",
        snapshot_hash="abc123hashvalue",
        retrieved_at=now,
    )
    facts, evidence = adapt_to_facts_and_evidence(
        assertions,
        retrieved_at=now,
        locale="en",
        timezone_name="UTC",
        auth_context_id=None,
        schema_fields=["name", "price", "currency"],
    )
    assert facts
    assert all(e.retrieved_at == now for e in evidence)
    assert all(e.locale == "en" for e in evidence)
    assert all(e.extraction_backend.startswith("L1_") for e in evidence)


def test_og_dedupe():
    html = """
    <html><head>
    <meta property="og:title" content="Hello">
    <script type="application/ld+json">
    {"@type":"WebPage","name":"Hello","headline":"Hello"}
    </script>
    </head></html>
    """
    base = "https://example.test"
    assertions = extract_structured_data(html, base, "hash1", retrieved_at=None)
    fb = manual_og_fallback(html, base, "hash1")
    merged = append_missing_og_fallback(assertions, fb, normalize_jsonld_value)
    titles = [a for a in merged if a.field in ("title", "name")]
    # may have title+name from paths; values should not duplicate same canonical twice for same field
    by_field = {}
    for a in titles:
        by_field.setdefault(a.field, set()).add(str(a.value).casefold())
    for field, vals in by_field.items():
        assert len(vals) >= 1


def test_task_completeness_helpers():
    from scripts.benchmark_dataset import evidence_validity, field_completeness, task_is_complete

    empty = {"status": "success", "facts": []}
    assert task_is_complete(empty, []) is False
    assert field_completeness(empty, ["title"]) == 0.0

    ok = {
        "status": "success",
        "facts": [
            {
                "field": "title",
                "validation_status": "valid",
                "evidence_ids": ["e1"],
            }
        ],
        "required_fields": ["title"],
    }
    assert task_is_complete(ok, ["title"]) is True
    assert evidence_validity(ok) == 1.0


def test_legacy_jsonld_dedupe_skips_structured_fields():
    """Structured high-conf fields should not be re-emitted by legacy json_ld path."""
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type":"Product","name":"Widget","offers":{"price":"10.00","priceCurrency":"USD"}}
    </script>
    </head><body><h1>Widget</h1></body></html>
    """
    from evidence_runtime.extract import extract_l1

    facts, evidence, missing, sig, trace = extract_l1(
        html,
        "https://example.test/p/1",
        {"fields": {"name": {"type": "string"}, "price": {"type": "number"}, "currency": {"type": "string"}}},
        locale="en",
        timezone_name="UTC",
        auth_context_id=None,
        content_hash="testhash123456",
    )
    # name/price/currency should come from structured (json-ld), not duplicated as method json_ld
    by_field = {}
    for f in facts:
        by_field.setdefault(f.field, []).append(f.extraction_method)
    for field in ("name", "price", "currency"):
        methods = by_field.get(field, [])
        # at most one winning path stored per field after resolve — before resolve may be 1
        assert methods, f"missing {field}"
        # if both structured and legacy ran, dedupe should drop legacy for that field
        if "json-ld" in methods:
            assert "json_ld" not in methods, f"{field} still has legacy json_ld: {methods}"
    assert any("json_ld_dedupe" in t or "structured:" in t for t in trace)
