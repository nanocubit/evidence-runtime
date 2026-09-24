"""Core unit tests for evidence_runtime 0.3+."""
from __future__ import annotations

import pytest

from evidence_runtime.extract import _parse_number, _resolve_conflicts, extract_l1
from evidence_runtime.models import ExtractionRequest, Fact
from evidence_runtime.normalizer import normalize_jsonld_value, normalize_text
from evidence_runtime.policies import FetchPolicy, PolicyError, validate_url
from evidence_runtime.signature import analyze_signature


def test_ssrf_blocks_localhost():
    with pytest.raises(PolicyError):
        validate_url("http://127.0.0.1/", FetchPolicy())
    with pytest.raises(PolicyError):
        validate_url("http://localhost/admin", FetchPolicy())


def test_ssrf_blocks_metadata():
    with pytest.raises(PolicyError):
        validate_url("http://169.254.169.254/latest/meta-data/", FetchPolicy())


def test_parse_number_locales():
    assert _parse_number("1,234.56", "en-US") == 1234.56
    assert _parse_number("1.234,56", "de-DE") == 1234.56
    assert _parse_number("99", "en-US") == 99


def test_normalize_text():
    assert normalize_text('  "Hello   world"  ') == "Hello world"
    assert normalize_text("«Цена»") == "Цена"
    assert normalize_text("  foo\n\tbar  ") == "foo bar"


def test_normalize_price_european():
    assert normalize_jsonld_value("price", "1.234,56") == 1234.56
    assert normalize_jsonld_value("price", 42) == 42.0
    assert normalize_jsonld_value("currency", "eur") == "EUR"
    assert normalize_jsonld_value("currency", "$") == "USD"
    assert normalize_jsonld_value("availability", "https://schema.org/InStock") is True


def test_conflict_method_priority():
    facts = [
        Fact(field="title", value="CSS", datatype="str", confidence=0.99,
             source_id="a", extraction_method="css_selector"),
        Fact(field="title", value="JSONLD", datatype="str", confidence=0.90,
             source_id="b", extraction_method="json_ld"),
        Fact(field="title", value="OG", datatype="str", confidence=0.93,
             source_id="c", extraction_method="meta_og"),
    ]
    resolved = _resolve_conflicts(facts)
    assert len(resolved) == 1
    assert resolved[0].value == "JSONLD"
    assert resolved[0].conflict.resolution["winner_method"] == "json_ld"


def test_signature_page_class_article():
    html = '''<html><head>
    <meta property="article:published_time" content="2023-01-01">
    <script type="application/ld+json">{"@type":"Article"}</script>
    </head><body><article><time datetime="2023-01-01">Jan</time>
    <meta name="author" content="Alice">
    <h1>Title</h1></article></body></html>'''
    sig = analyze_signature(html)
    assert sig["page_class"] == "article"
    assert sig["has_time"] is True


def test_signature_page_class_docs():
    html = "<html><body>" + "".join(f"<h2>S{i}</h2><code>x</code>" for i in range(6)) + "</body></html>"
    sig = analyze_signature(html)
    assert sig["page_class"] == "docs"


def test_signature_page_class_product():
    html = '''<html><head>
    <meta property="product:price:amount" content="99">
    <meta property="og:type" content="product">
    </head><body><span itemprop="price">99</span></body></html>'''
    sig = analyze_signature(html)
    assert sig["page_class"] == "product"


def test_signature_spa():
    spa = '<html><head><script>window.__NEXT_DATA__={}</script></head><body></body></html>'
    assert analyze_signature(spa)["likely_spa"] is True
    assert analyze_signature(spa)["page_class"] == "spa"


def test_meta_og_and_url_derived():
    html = '''<!doctype html><html><head>
    <meta property="og:title" content="OG Title Here">
    <meta property="og:description" content="A description">
    <meta property="og:site_name" content="ExampleSite">
    <title>Fallback Title</title>
    </head><body><h1>H1 Title</h1></body></html>'''
    facts, evidence, missing, sig, trace = extract_l1(
        html,
        "https://docs.example.com/api/v2/intro",
        {"fields": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "source": {"type": "string"},
            "category": {"type": "string"},
        }},
        locale="en-US",
        timezone_name="UTC",
        auth_context_id=None,
        content_hash="sha256:abc",
    )
    by_field = {f.field: f for f in facts}
    assert "title" in by_field
    # OG should win or at least be present
    assert by_field["title"].value in ("OG Title Here", "Fallback Title", "H1 Title")
    assert by_field.get("source") is not None
    assert by_field["source"].value in ("ExampleSite", "docs.example.com")
    assert "category" in by_field  # api from path
    assert any("meta_og" in t for t in trace)


def test_request_schema_alias():
    r = ExtractionRequest(url="https://example.com", schema={"fields": {"title": {"type": "string"}}})
    assert "title" in r.schema["fields"]
    assert "schema" in r.model_dump(by_alias=True)


def test_pick_tactic_url_and_dom():
    from evidence_runtime.tactics import get_profile, pick_tactic, url_tactic_hint

    assert url_tactic_hint("https://docs.python.org/3/library/asyncio.html") == "docs"
    assert url_tactic_hint("https://www.ikea.com/us/en/p/malm") == "product"
    assert url_tactic_hint("https://www.airbnb.com/s/Paris") == "spa"

    # URL wins for product even if DOM generic
    assert pick_tactic("https://www.ozon.ru/product/x", {"page_class": "generic"}) == "product"

    # Agreement
    assert pick_tactic("https://docs.python.org/3/", {"page_class": "docs"}) == "docs"

    p = get_profile("product")
    assert p.browser_hint is True
    assert "price" in p.focus_fields
    assert "json_ld" in p.methods


def test_tactic_configures_pipeline():
    from evidence_runtime.extract import extract_l1

    html = '''<!doctype html><html><head>
    <meta property="og:title" content="API Reference">
    <meta name="description" content="How to use the API">
    </head><body>
    <nav class="breadcrumb"><a href="/">Docs</a><a href="/api">API</a></nav>
    <h1>API Reference</h1>
    <h2>Auth</h2><code>token</code><h2>Endpoints</h2><code>GET /v1</code>
    <h2>Errors</h2><code>404</code><h2>Limits</h2><code>rate</code>
    </body></html>'''
    facts, ev, missing, sig, trace = extract_l1(
        html,
        "https://docs.example.com/api/v1",
        {"fields": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "source": {"type": "string"},
            "category": {"type": "string"},
        }},
        locale="en-US", timezone_name="UTC", auth_context_id=None, content_hash="sha256:x",
    )
    assert sig.get("tactic") in ("docs", "generic")
    assert any("tactic=" in line for line in trace)
    by = {f.field: f for f in facts}
    assert "title" in by


def test_article_url_beats_spa_dom():
    from evidence_runtime.tactics import pick_tactic
    # Modern news sites look like SPAs in DOM but URL says article
    assert pick_tactic("https://www.bbc.com/news/technology", {"page_class": "spa", "likely_spa": True}) == "article"
    assert pick_tactic("https://arstechnica.com/gadgets/", {"page_class": "spa"}) == "article"
    assert pick_tactic("https://www.nike.com/t/air-force-1", {"page_class": "spa"}) == "product"
