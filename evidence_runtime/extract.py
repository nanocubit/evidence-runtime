"""Level-1 deterministic extraction pipeline.

Order (by METHOD_PRIORITY):
  1. JSON-LD          (priority 3)
  2. Meta / OpenGraph (priority 2)   ← early structured meta
  3. Trafilatura      (priority 2)
  4. CSS selectors    (priority 1)
  5. URL-derived      (priority 0)   ← source/category from path
  6. Price regex      (priority 0)   ← product pages only, last resort
  7. Conflict resolution
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

# Thread-local-ish clock for this module (set by extract_l1)
_ACTIVE_RETRIEVED_AT: datetime | None = None


def _clock() -> datetime:
    return _ACTIVE_RETRIEVED_AT or datetime.now(timezone.utc)


from w3lib.html import get_base_url

from .embedded import extract_embedded_state
from .links import extract_all_links, primary_canonical
from .models import Conflict, Evidence, Fact
from .normalizer import normalize_jsonld_value, normalize_text
from .signature import analyze_signature
from .structured import (
    adapt_to_facts_and_evidence,
    append_missing_og_fallback,
    extract_structured_data,
    manual_og_fallback,
    publisher_author_fallback,
)
from .tactics import get_profile, pick_tactic, should_hint_browser

STRUCTURED_METHODS = frozenset({
    "json-ld", "microdata", "opengraph", "og_manual", "microformat", "rdfa",
})
STRUCTURED_CONF_SKIP = 0.88  # legacy json_ld skips field if structured ≥ this

FIELD_DEFAULTS: dict[str, list[str]] = {
    "name": [
        'meta[property="og:title"]',
        "h1",
        '[itemprop="name"]',
    ],
    "title": [
        'meta[property="og:title"]',
        "title",
        "h1",
    ],
    "price": [
        'meta[property="product:price:amount"]',
        "[data-price]",
        '[itemprop="price"]',
        ".price",
        ".product-price",
        ".cost",
        '[class*="price"]',
    ],
    "currency": [
        'meta[property="product:price:currency"]',
        '[itemprop="priceCurrency"]',
    ],
    "description": [
        'meta[property="og:description"]',
        'meta[name="description"]',
        '[itemprop="description"]',
    ],
    "brand": [
        'meta[property="product:brand"]',
        '[itemprop="brand"]',
        ".brand",
    ],
    "availability": [
        'meta[property="product:availability"]',
        '[itemprop="availability"]',
    ],
    "author": [
        'meta[property="article:author"]',
        'meta[name="author"]',
        'meta[name="dc.creator"]',
        'meta[name="citation_author"]',
        'meta[name="parsely-author"]',
        'meta[property="og:article:author"]',
        '[itemprop="author"]',
        '[itemprop="author"] [itemprop="name"]',
        'a[rel="author"]',
        '[rel="author"]',
        ".author",
        ".byline",
        ".author-name",
        "[data-author]",
    ],
    "date": [
        'meta[property="article:published_time"]',
        '[itemprop="datePublished"]',
        "time[datetime]",
    ],
    "publish_date": [
        'meta[property="article:published_time"]',
        '[itemprop="datePublished"]',
        "time[datetime]",
    ],
    "image": [
        'meta[property="og:image"]',
        '[itemprop="image"]',
        ".product-image img",
    ],
    "category": [
        'meta[property="article:section"]',
        '[itemprop="articleSection"]',
        'meta[name="category"]',
    ],
    "source": [
        'meta[property="og:site_name"]',
        'meta[name="application-name"]',
    ],
}

# page_class → extra / preferred CSS selectors
_CLASS_SELECTOR_BOOST: dict[str, dict[str, list[str]]] = {
    "product": {
        "name": ['[data-testid*="product"]', ".product-title", ".product-name"],
        "price": ['[data-testid*="price"]', ".product-price", ".a-price .a-offscreen"],
    },
    "article": {
        "title": ["article h1", ".article-title", ".post-title", "h1.entry-title"],
        "author": [".byline", ".author-name", "[data-author]"],
        "date": [".published", ".post-date", "time.entry-date"],
    },
    "docs": {
        "title": ["article h1", ".document-title", "h1"],
        "description": [".document-description", "article p"],
    },
}

METHOD_PRIORITY = {
    "json_ld": 3,
    "meta_og": 2,
    "trafilatura": 2,
    "css_selector": 1,
    "url_derived": 0,
    "price_regex": 0,
}


_BOT_WALL_MARKERS = (
    "robot or human",
    "are you a robot",
    "verify you are human",
    "attention required",
    "access denied",
    "captcha",
    "cf-browser-verification",
    "challenge-platform",
    "enable javascript and cookies",
)

# Scripts/styles are excluded before bot-wall matching (see _looks_like_bot_wall).
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)

_BOT_WALL_TITLES = (
    "robot or human",
    "access denied",
    "attention required",
    "just a moment",
)


def _looks_like_bot_wall(html_text: str, title_guess: str | None = None) -> bool:
    # Match markers against *visible* text only: challenge copy is rendered, whereas
    # strings like MediaWiki's `wgConfirmEditCaptchaNeededForGenericEdit` live in a
    # <script> config and used to make every Wikipedia page look bot-walled.
    visible = _SCRIPT_STYLE_RE.sub(" ", (html_text or "")[:20000]).lower()
    if any(m in visible for m in _BOT_WALL_MARKERS):
        return True
    title_low = (title_guess or "").strip().lower()
    # Real challenge titles carry decoration ("Attention Required! | Cloudflare"),
    # so match on substring rather than equality.
    if title_low and any(phrase in title_low for phrase in _BOT_WALL_TITLES):
        return True
    return False


def _is_low_quality_title(value: Any) -> bool:
    """Reject description-like, site-brand, or challenge titles that should not win conflicts."""
    if value is None:
        return True
    s = str(value).strip()
    if not s or len(s) < 2:
        return True
    low = s.lower()
    if low in {"robot or human?", "access denied", "home", "untitled"}:
        return True
    # Publisher / site brand used as page title (Nature WebPage.name, etc.)
    _SITE_BRANDS = {
        "nature", "science", "bbc", "cnn", "reuters", "wikipedia", "google",
        "github", "microsoft", "apple", "amazon", "facebook", "twitter", "x",
        "youtube", "medium", "substack", "wordpress", "blogger", "home",
        "docs", "documentation", "api", "reference", "index",
    }
    if low in _SITE_BRANDS:
        return True
    # Single-token brand-like (no spaces, short) — weak as article title
    if " " not in s and len(s) <= 12 and s.isalpha():
        return True
    # Abstract-style: starts with adjective phrase, no capital product name pattern
    if len(s) > 80 and s[0].islower():
        return True
    # Wikipedia JSON-LD sometimes puts definition without entity name
    if low.startswith("general-purpose") or low.startswith("a type of"):
        return True
    return False



_JSONLD_TYPES = {
    "Product", "Offer", "AggregateOffer",
    "Article", "NewsArticle", "BlogPosting",
    "Event", "WebPage", "AboutPage",
}

# Price pattern: optional currency symbol + number with optional decimals
_PRICE_RE = re.compile(
    r"(?:(?P<sym>[$€£¥₽])\s*)?(?P<num>\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d+[.,]\d{1,2})\s*(?P<code>USD|EUR|GBP|RUB|JPY)?",
    re.IGNORECASE,
)
_PRICE_CONTEXT_RE = re.compile(
    r"(?:price|cost|sale|buy|from|only|за|цена|стоимость)",
    re.IGNORECASE,
)


def _value(node) -> str:
    for attr in ("content", "value", "data-price", "aria-label", "src", "href", "datetime"):
        if node.attributes.get(attr):
            return node.attributes[attr].strip()
    return node.text(separator=" ", strip=True)


def _coerce(value: str, typ: str, locale: str = "en-US") -> Any:
    if typ == "number":
        return _parse_number(value, locale)
    if typ == "integer":
        return int(re.sub(r"\D", "", value) or "0")
    if typ == "boolean":
        return value.lower() in {
            "1", "true", "yes", "да", "in stock", "available", "instock",
            "https://schema.org/instock", "http://schema.org/instock",
        }
    if typ == "url":
        return value if value.startswith("http") else None
    return normalize_text(value)


def _parse_number(value: str, locale: str) -> float | int:
    cleaned = re.sub(r"[^0-9,.\s-]", "", value).strip()
    if not cleaned or cleaned in {"-", ".", ","}:
        raise ValueError(f"not a number: {value!r}")
    comma_count = cleaned.count(",")
    dot_count = cleaned.count(".")
    last_comma = cleaned.rfind(",")
    last_dot = cleaned.rfind(".")
    if comma_count == 0 and dot_count == 0:
        return int(cleaned)
    if last_comma > last_dot and comma_count == 1:
        return float(cleaned.replace(".", "").replace(",", "."))
    if last_dot > last_comma and dot_count == 1:
        return float(cleaned.replace(",", ""))
    try:
        return float(cleaned.replace(",", ""))
    except ValueError:
        return float(cleaned.replace(".", "").replace(",", "."))


def _build_evidence_text(value: Any, max_len: int = 200) -> str:
    s = str(value)[:max_len]
    return s if s else "[empty]"


def _schema_fields(schema: dict[str, Any]) -> list[str]:
    if isinstance(schema, dict) and "fields" in schema:
        return list(schema["fields"].keys())
    if isinstance(schema, dict):
        return list(schema.keys())
    return []


def _field_type(schema: dict[str, Any], field: str) -> str:
    fields = schema.get("fields", schema) if isinstance(schema, dict) else {}
    spec = fields.get(field, {}) if isinstance(fields, dict) else {}
    if isinstance(spec, str):
        return "string"
    return spec.get("type", "string") if isinstance(spec, dict) else "string"


def _field_selectors(schema: dict[str, Any], field: str, page_class: str) -> list[str]:
    fields = schema.get("fields", schema) if isinstance(schema, dict) else {}
    spec = fields.get(field, {}) if isinstance(fields, dict) else {}
    custom = spec.get("selectors") if isinstance(spec, dict) else None
    base = list(custom or FIELD_DEFAULTS.get(field, [f'[itemprop="{field}"]', f"[data-{field}]"]))
    boost = _CLASS_SELECTOR_BOOST.get(page_class, {}).get(field, [])
    # boost first, then defaults, unique
    seen: set[str] = set()
    out: list[str] = []
    for s in boost + base:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _make_evidence(
    *,
    source_id: str,
    url: str,
    text: str,
    selector: str,
    content_hash: str,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
    backend: str,
    now: datetime,
) -> Evidence:
    return Evidence(
        source_id=source_id,
        source_url=url,
        evidence_text=text,
        selector=selector,
        content_hash=content_hash,
        content_signature=content_hash[:16],
        locale=locale,
        timezone=timezone_name,
        auth_context_id=auth_context_id,
        retrieved_at=now,
        extraction_backend=backend,
    )


def _make_fact(
    *,
    field: str,
    value: Any,
    datatype: str,
    confidence: float,
    source_id: str,
    method: str,
    evidence: Evidence,
) -> Fact:
    return Fact(
        field=field,
        value=value,
        datatype=datatype,
        confidence=confidence,
        source_id=source_id,
        extraction_method=method,
        validation_status="valid",
        evidence_ids=[evidence.evidence_id],
    )


# ---------------------------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------------------------
def _flatten_jsonld_items(data: dict) -> list[dict]:
    items: list[dict] = []
    for item in data.get("json-ld", []) or []:
        if not isinstance(item, dict):
            continue
        if "@graph" in item and isinstance(item["@graph"], list):
            for g in item["@graph"]:
                if isinstance(g, dict):
                    items.append(g)
        else:
            items.append(item)
    return items


def _type_name(t: Any) -> str:
    if isinstance(t, list):
        for x in t:
            if isinstance(x, str) and x in _JSONLD_TYPES:
                return x
        return str(t[0]) if t else ""
    return str(t) if t else ""


def _dig(obj: Any, *keys: str) -> Any:
    cur = obj
    for key in keys:
        if isinstance(cur, list) and cur:
            cur = cur[0]
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def _extract_jsonld(
    html_text: str,
    url: str,
    content_hash: str,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
) -> tuple[list[Fact], list[Evidence]]:
    facts: list[Fact] = []
    evidence: list[Evidence] = []
    source_id = "src_jsonld_" + hashlib.sha256(url.encode()).hexdigest()[:12]
    now = _clock()

    try:
        import extruct
        from w3lib.html import get_base_url

        base_url = get_base_url(html_text, url)
        data = extruct.extract(html_text, base_url=base_url, syntaxes=["json-ld"])
    except Exception:
        return facts, evidence

    for item in _flatten_jsonld_items(data):
        t = _type_name(item.get("@type"))
        if t not in _JSONLD_TYPES:
            continue

        offer: dict = {}
        if t == "Product":
            raw_offer = item.get("offers") or item.get("offer")
            if isinstance(raw_offer, list) and raw_offer:
                raw_offer = raw_offer[0]
            if isinstance(raw_offer, dict):
                offer = raw_offer

        if t in ("Product", "Offer", "AggregateOffer"):
            pairs = [
                (item.get("name"), "name"),
                (item.get("headline"), "title"),
                (item.get("description"), "description"),
                (item.get("sku"), "sku"),
                (_dig(item, "brand", "name") or item.get("brand"), "brand"),
                (offer.get("price") or item.get("price") or _dig(item, "offers", "price"), "price"),
                (
                    offer.get("priceCurrency")
                    or item.get("priceCurrency")
                    or _dig(item, "offers", "priceCurrency"),
                    "currency",
                ),
                (
                    offer.get("availability")
                    or item.get("availability")
                    or _dig(item, "offers", "availability"),
                    "availability",
                ),
            ]
        elif t in ("Article", "NewsArticle", "BlogPosting"):
            pairs = [
                (item.get("headline") or item.get("name"), "title"),
                (
                    _dig(item, "author", "name")
                    or (item.get("author") if isinstance(item.get("author"), str) else None),
                    "author",
                ),
                (item.get("datePublished"), "date"),
                (item.get("datePublished"), "publish_date"),
                (item.get("description"), "description"),
            ]
        else:
            pairs = [
                (item.get("name") or item.get("headline"), "title"),
                (item.get("description"), "description"),
            ]

        for raw, field in pairs:
            if raw is None or raw == "":
                continue
            if field == "author" and isinstance(raw, dict):
                raw = raw.get("name") or raw.get("@id", "")
            value = normalize_jsonld_value(field, raw)
            if value is None or value == "":
                continue
            if field in ("title", "name") and _is_low_quality_title(value):
                continue
            ev = _make_evidence(
                source_id=source_id, url=url, text=_build_evidence_text(raw),
                selector=f"jsonld:@type={t}:{field}", content_hash=content_hash,
                locale=locale, timezone_name=timezone_name, auth_context_id=auth_context_id,
                backend="L1_jsonld", now=now,
            )
            facts.append(_make_fact(
                field=field, value=value, datatype=type(value).__name__,
                confidence=0.96, source_id=source_id, method="json_ld", evidence=ev,
            ))
            evidence.append(ev)

    return facts, evidence


# ---------------------------------------------------------------------------
# Meta / OpenGraph pass (priority between JSON-LD and Trafilatura)
# ---------------------------------------------------------------------------
_META_OG_MAP = [
    # (attr_name, attr_value, target_field)
    ("property", "og:title", "title"),
    ("property", "og:title", "name"),
    ("property", "og:description", "description"),
    ("property", "og:site_name", "source"),
    ("property", "og:image", "image"),
    ("property", "product:price:amount", "price"),
    ("property", "product:price:currency", "currency"),
    ("property", "product:brand", "brand"),
    ("property", "product:availability", "availability"),
    ("property", "article:published_time", "date"),
    ("property", "article:published_time", "publish_date"),
    ("property", "article:author", "author"),
    ("property", "article:section", "category"),
    ("name", "description", "description"),
    ("name", "author", "author"),
    ("name", "application-name", "source"),
]


def _extract_meta_og(
    html_text: str,
    url: str,
    content_hash: str,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
    existing_fields: set[str],
    schema_fields: list[str],
) -> tuple[list[Fact], list[Evidence]]:
    facts: list[Fact] = []
    evidence: list[Evidence] = []
    source_id = "src_meta_" + hashlib.sha256(url.encode()).hexdigest()[:12]
    now = _clock()
    tree = HTMLParser(html_text)

    wanted = set(schema_fields) | {"title", "name", "description", "source"}  # always try core
    seen_fields: set[str] = set()

    for attr, attr_val, field in _META_OG_MAP:
        if field in existing_fields or field in seen_fields:
            continue
        if field not in wanted and field not in schema_fields:
            continue
        try:
            node = tree.css_first(f'meta[{attr}="{attr_val}"]')
        except Exception:
            node = None
        if node is None:
            continue
        raw = node.attributes.get("content", "")
        if not raw or not raw.strip():
            continue

        typ = "number" if field == "price" else "boolean" if field == "availability" else "string"
        try:
            value = _coerce(raw, typ, locale) if field in ("price", "availability") else normalize_jsonld_value(field, raw)
        except (ValueError, TypeError):
            value = normalize_text(raw)
        if value is None or value == "":
            continue

        ev = _make_evidence(
            source_id=source_id, url=url, text=raw[:200],
            selector=f'meta[{attr}="{attr_val}"]', content_hash=content_hash,
            locale=locale, timezone_name=timezone_name, auth_context_id=auth_context_id,
            backend="L1_meta_og", now=now,
        )
        conf = 0.93 if attr_val.startswith("og:") or attr_val.startswith("product:") else 0.88
        facts.append(_make_fact(
            field=field, value=value, datatype=type(value).__name__,
            confidence=conf, source_id=source_id, method="meta_og", evidence=ev,
        ))
        evidence.append(ev)
        seen_fields.add(field)

    return facts, evidence


# ---------------------------------------------------------------------------
# Trafilatura
# ---------------------------------------------------------------------------
def _extract_trafilatura(
    html_text: str,
    url: str,
    content_hash: str,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
    existing_fields: set[str],
) -> tuple[list[Fact], list[Evidence]]:
    facts: list[Fact] = []
    evidence: list[Evidence] = []
    source_id = "src_traf_" + hashlib.sha256(url.encode()).hexdigest()[:12]
    now = _clock()

    try:
        import trafilatura

        raw = trafilatura.extract(
            html_text,
            include_tables=False,
            include_comments=False,
            output_format="json",
            with_metadata=True,
        )
    except Exception:
        return facts, evidence

    if not raw:
        return facts, evidence
    if isinstance(raw, str):
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            return facts, evidence
    elif isinstance(raw, dict):
        doc = raw
    else:
        return facts, evidence

    mapping = {
        "title": "title",
        "author": "author",
        "date": "date",
        "description": "description",
        "text": "main_text",
    }
    for traf_key, field in mapping.items():
        if field in existing_fields:
            continue
        val = doc.get(traf_key)
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        val = normalize_text(val) if field != "main_text" else val
        ev_text = str(val)[:300] if field == "main_text" else str(val)[:200]
        ev = _make_evidence(
            source_id=source_id, url=url, text=ev_text,
            selector=f"trafilatura::{traf_key}", content_hash=content_hash,
            locale=locale, timezone_name=timezone_name, auth_context_id=auth_context_id,
            backend="L1_trafilatura", now=now,
        )
        facts.append(_make_fact(
            field=field, value=val, datatype=type(val).__name__,
            confidence=0.90, source_id=source_id, method="trafilatura", evidence=ev,
        ))
        evidence.append(ev)

    return facts, evidence


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
def _extract_css(
    html_text: str,
    url: str,
    content_hash: str,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
    missing_fields: list[str],
    schema: dict[str, Any],
    page_class: str,
) -> tuple[list[Fact], list[Evidence], list[str]]:
    facts: list[Fact] = []
    evidence: list[Evidence] = []
    still_missing: list[str] = []
    source_id = "src_css_" + hashlib.sha256(url.encode()).hexdigest()[:12]
    now = _clock()
    tree = HTMLParser(html_text)

    for field in missing_fields:
        typ = _field_type(schema, field)
        selectors = _field_selectors(schema, field, page_class)
        found = False
        for selector in selectors:
            try:
                node = tree.css_first(selector)
            except Exception:
                node = None
            if node is None:
                continue
            raw = _value(node)
            if not raw:
                continue
            try:
                value = _coerce(raw, typ, locale)
            except (ValueError, TypeError):
                continue
            if value is None or value == "":
                continue
            ev = _make_evidence(
                source_id=source_id, url=url, text=raw[:200],
                selector=selector, content_hash=content_hash,
                locale=locale, timezone_name=timezone_name, auth_context_id=auth_context_id,
                backend="L1_css", now=now,
            )
            facts.append(_make_fact(
                field=field, value=value, datatype=typ,
                confidence=0.85, source_id=source_id, method="css_selector", evidence=ev,
            ))
            evidence.append(ev)
            found = True
            break
        if not found:
            still_missing.append(field)

    return facts, evidence, still_missing


# ---------------------------------------------------------------------------
# URL-derived facts (source, category)
# ---------------------------------------------------------------------------
def _extract_url_derived(
    url: str,
    content_hash: str,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
    existing_fields: set[str],
    schema_fields: list[str],
) -> tuple[list[Fact], list[Evidence]]:
    facts: list[Fact] = []
    evidence: list[Evidence] = []
    source_id = "src_url_" + hashlib.sha256(url.encode()).hexdigest()[:12]
    now = _clock()
    parsed = urlparse(url)
    host = (parsed.hostname or "").removeprefix("www.")
    segments = [s for s in parsed.path.split("/") if s]

    candidates: dict[str, str] = {}
    if "source" in schema_fields and "source" not in existing_fields and host:
        candidates["source"] = host
    if "category" in schema_fields and "category" not in existing_fields and segments:
        # first meaningful path segment
        skip = {"en", "en-us", "ru", "de", "fr", "www", "p", "product", "products", "dp", "wiki"}
        for seg in segments:
            if seg.lower() not in skip and not seg.isdigit() and len(seg) > 1:
                candidates["category"] = normalize_text(seg.replace("-", " ").replace("_", " "))
                break
    if "version" in schema_fields and "version" not in existing_fields:
        for seg in segments:
            if re.match(r"^v?\d+(\.\d+)*(\.x)?$", seg, re.I):
                candidates["version"] = seg
                break

    for field, value in candidates.items():
        ev = _make_evidence(
            source_id=source_id, url=url, text=value,
            selector=f"url_derived::{field}", content_hash=content_hash,
            locale=locale, timezone_name=timezone_name, auth_context_id=auth_context_id,
            backend="L1_url", now=now,
        )
        facts.append(_make_fact(
            field=field, value=value, datatype="string",
            confidence=0.70, source_id=source_id, method="url_derived", evidence=ev,
        ))
        evidence.append(ev)

    return facts, evidence


# ---------------------------------------------------------------------------
# Price regex (product pages only, last resort)
# ---------------------------------------------------------------------------
def _extract_price_regex(
    html_text: str,
    url: str,
    content_hash: str,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
    existing_fields: set[str],
    schema_fields: list[str],
    page_class: str,
) -> tuple[list[Fact], list[Evidence]]:
    if page_class != "product":
        return [], []
    if "price" not in schema_fields or "price" in existing_fields:
        return [], []

    facts: list[Fact] = []
    evidence: list[Evidence] = []
    source_id = "src_preg_" + hashlib.sha256(url.encode()).hexdigest()[:12]
    now = _clock()

    # Prefer text near price-related keywords
    tree = HTMLParser(html_text)
    candidates: list[tuple[float, str, str]] = []  # score, raw, selector

    for node in tree.css("[class*='price'], [data-price], .cost, .amount, [itemprop='price']"):
        raw = _value(node)
        if not raw:
            continue
        m = _PRICE_RE.search(raw)
        if not m:
            continue
        try:
            val = _parse_number(m.group("num"), locale)
            fv = float(val)
            if fv <= 0 or fv > 10_000_000:
                continue
            if 1900 <= fv <= 2100 and "." not in m.group("num"):
                continue
            if fv < 15 and not m.group("sym") and not m.group("code") and "." not in m.group("num") and "," not in m.group("num"):
                continue
        except (ValueError, TypeError):
            continue
        score = 0.6
        if _PRICE_CONTEXT_RE.search(raw) or _PRICE_CONTEXT_RE.search(node.attributes.get("class", "") or ""):
            score = 0.75
        candidates.append((score, m.group(0), "price_regex:near_price_class"))

    # Fallback: scan body text (first 50k)
    if not candidates:
        body = html_text[:50000]
        for m in _PRICE_RE.finditer(body):
            start = max(0, m.start() - 40)
            ctx = body[start:m.end() + 20]
            if not _PRICE_CONTEXT_RE.search(ctx):
                continue
            # skip years / versions
            num = m.group("num")
            try:
                val = _parse_number(num, locale)
                fv = float(val)
                # years, versions, pure small ints without currency
                if 1900 <= fv <= 2100 and "." not in num and "," not in num:
                    continue
                if fv <= 0 or fv > 10_000_000:
                    continue
                # bare small integers without $€ symbol are usually not prices
                if fv < 50 and not m.group("sym") and not m.group("code") and "." not in num and "," not in num:
                    continue
            except (ValueError, TypeError):
                continue
            candidates.append((0.50, m.group(0), "price_regex:body_context"))
            break

    if not candidates:
        return [], []

    candidates.sort(key=lambda x: x[0], reverse=True)
    score, raw, selector = candidates[0]
    try:
        # extract numeric part
        m = _PRICE_RE.search(raw)
        num_s = m.group("num") if m else raw
        value = _parse_number(num_s, locale)
    except (ValueError, TypeError):
        return [], []

    ev = _make_evidence(
        source_id=source_id, url=url, text=raw[:200],
        selector=selector, content_hash=content_hash,
        locale=locale, timezone_name=timezone_name, auth_context_id=auth_context_id,
        backend="L1_price_regex", now=now,
    )
    facts.append(_make_fact(
        field="price", value=value, datatype="number",
        confidence=round(score, 2), source_id=source_id, method="price_regex", evidence=ev,
    ))
    evidence.append(ev)

    # currency from same match if present
    if m and "currency" in schema_fields and "currency" not in existing_fields:
        sym = m.group("sym") or m.group("code")
        if sym:
            cur = normalize_jsonld_value("currency", sym)
            if cur:
                ev2 = _make_evidence(
                    source_id=source_id, url=url, text=str(sym),
                    selector=selector + ":currency", content_hash=content_hash,
                    locale=locale, timezone_name=timezone_name, auth_context_id=auth_context_id,
                    backend="L1_price_regex", now=now,
                )
                facts.append(_make_fact(
                    field="currency", value=cur, datatype="string",
                    confidence=round(score * 0.9, 2), source_id=source_id,
                    method="price_regex", evidence=ev2,
                ))
                evidence.append(ev2)

    return facts, evidence


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------
def _title_score(fact: Fact) -> float:
    """Higher is better for title/name selection."""
    base = METHOD_PRIORITY.get(fact.extraction_method, 0) + fact.confidence
    val = fact.value
    if _is_low_quality_title(val):
        base -= 5.0
    s = str(val or "")
    # Prefer page-specific headlines over site brand
    if 20 <= len(s) <= 160:
        base += 0.8
    elif 3 <= len(s) <= 80:
        base += 0.3
    if s[:1].isupper():
        base += 0.2
    # meta_og / html title / og_manual usually more page-specific than WebPage.name
    if fact.extraction_method in ("meta_og", "og_manual", "css_selector", "css"):
        base += 0.5
    if fact.extraction_method in ("json-ld", "microdata") and len(s) < 20:
        base -= 0.4
    return base


def _resolve_conflicts(facts: list[Fact]) -> list[Fact]:
    by_field: dict[str, list[Fact]] = {}
    for f in facts:
        by_field.setdefault(f.field, []).append(f)

    result: list[Fact] = []
    for field, group in by_field.items():
        # drop obvious garbage for title/name
        if field in ("title", "name"):
            cleaned = [f for f in group if not _is_low_quality_title(f.value)]
            if cleaned:
                group = cleaned
        if len(group) == 1:
            result.append(group[0])
            continue
        if field in ("title", "name"):
            group.sort(key=_title_score, reverse=True)
            policy = "title_quality_then_method"
        else:
            group.sort(
                key=lambda f: (METHOD_PRIORITY.get(f.extraction_method, 0), f.confidence),
                reverse=True,
            )
            policy = "method_priority_then_confidence"
        winner = group[0]
        losers = group[1:]
        if losers:
            winner.conflict = Conflict(
                status="resolved",
                fields=[field],
                values=[
                    {"value": str(l.value), "method": l.extraction_method, "confidence": l.confidence}
                    for l in losers
                ],
                resolution_policy=policy,
                resolution={"winner_method": winner.extraction_method},
            )
            winner.validation_status = "valid"
        result.append(winner)
    return result



# ---------------------------------------------------------------------------
# Breadcrumbs → source / category
# ---------------------------------------------------------------------------
_BREADCRUMB_SELECTORS = (
    "nav[aria-label='breadcrumb'] a",
    "nav.breadcrumb a",
    ".breadcrumb a",
    "[itemprop='breadcrumb'] a",
    "ol.breadcrumb a",
    ".breadcrumbs a",
)


def _extract_breadcrumbs(
    html_text: str,
    url: str,
    content_hash: str,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
    existing_fields: set[str],
    schema_fields: list[str],
) -> tuple[list[Fact], list[Evidence]]:
    facts: list[Fact] = []
    evidence: list[Evidence] = []
    if "source" not in schema_fields and "category" not in schema_fields:
        return facts, evidence

    tree = HTMLParser(html_text)
    crumbs: list[str] = []
    for sel in _BREADCRUMB_SELECTORS:
        try:
            nodes = tree.css(sel)
        except Exception:
            nodes = []
        for n in nodes:
            txt = n.text(strip=True)
            if txt and txt.lower() not in {"home", "главная", "/"}:
                crumbs.append(txt)
        if len(crumbs) >= 2:
            break

    if len(crumbs) < 2:
        return facts, evidence

    source_id = "src_crumb_" + hashlib.sha256(url.encode()).hexdigest()[:12]
    now = _clock()
    candidates = {}
    if "source" in schema_fields and "source" not in existing_fields:
        candidates["source"] = normalize_text(crumbs[0])
    if "category" in schema_fields and "category" not in existing_fields:
        candidates["category"] = normalize_text(crumbs[-1])

    for field, value in candidates.items():
        ev = _make_evidence(
            source_id=source_id, url=url, text=value,
            selector=f"breadcrumb::{field}", content_hash=content_hash,
            locale=locale, timezone_name=timezone_name, auth_context_id=auth_context_id,
            backend="L1_breadcrumb", now=now,
        )
        facts.append(_make_fact(
            field=field, value=value, datatype="string",
            confidence=0.75, source_id=source_id, method="css_selector", evidence=ev,
        ))
        evidence.append(ev)
    return facts, evidence


# ---------------------------------------------------------------------------
# Unified L1 entry
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Domain / field heuristics (fill gaps cheaply)
# ---------------------------------------------------------------------------
_DOMAIN_BRAND = {
    "apple.com": "Apple",
    "samsung.com": "Samsung",
    "shop.samsung.com": "Samsung",
    "dell.com": "Dell",
    "ikea.com": "IKEA",
    "amazon.com": "Amazon",
    "nike.com": "Nike",
    "walmart.com": "Walmart",
    "store.google.com": "Google",
    "google.com": "Google",
    "sony.com": "Sony",
    "hp.com": "HP",
    "lenovo.com": "Lenovo",
    "steampowered.com": "Steam",
    "store.steampowered.com": "Steam",
    "ebay.com": "eBay",
    "bestbuy.com": "Best Buy",
    "target.com": "Target",
    "etsy.com": "Etsy",
    "adidas.com": "Adidas",
}

_TLD_CURRENCY = {
    ".ru": "RUB", ".uk": "GBP", ".de": "EUR", ".fr": "EUR", ".eu": "EUR",
    ".jp": "JPY", ".cn": "CNY", ".in": "INR", ".br": "BRL", ".au": "AUD",
    ".ca": "CAD", ".us": "USD", ".com": "USD",
}


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.").lower()
    except Exception:
        return ""


def _extract_domain_heuristics(
    url: str,
    content_hash: str,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
    existing_fields: set[str],
    schema_fields: list[str],
    tactic: str,
) -> tuple[list[Fact], list[Evidence]]:
    """Cheap defaults: brand from domain, currency from TLD, version from path."""
    facts: list[Fact] = []
    evidence: list[Evidence] = []
    source_id = "src_domh_" + hashlib.sha256(url.encode()).hexdigest()[:12]
    now = _clock()
    host = _host(url)
    parsed = urlparse(url)

    candidates: dict[str, tuple[str, float]] = {}

    # brand from domain (product only)
    if tactic == "product" and "brand" in schema_fields and "brand" not in existing_fields:
        for domain, brand in _DOMAIN_BRAND.items():
            if host == domain or host.endswith("." + domain):
                candidates["brand"] = (brand, 0.72)
                break

    # currency from TLD / domain (product)
    if tactic == "product" and "currency" in schema_fields and "currency" not in existing_fields:
        # prefer longer suffix match
        for tld, cur in sorted(_TLD_CURRENCY.items(), key=lambda x: -len(x[0])):
            if host.endswith(tld.lstrip(".")) or host.endswith(tld):
                candidates["currency"] = (cur, 0.65)
                break
        if "currency" not in candidates and host.endswith(".com"):
            candidates["currency"] = ("USD", 0.60)

    # version from path (docs): /3/, /en/5.0/, /v2/, /en/20/, /latest/
    if "version" in schema_fields and "version" not in existing_fields:
        for seg in parsed.path.split("/"):
            if not seg:
                continue
            if re.match(r"^v?\d+(\.\d+)*(\.x)?$", seg, re.I):
                candidates["version"] = (seg.lstrip("v"), 0.82)
                break
            if seg.isdigit() and len(seg) <= 3:
                candidates["version"] = (seg, 0.78)
                break
            if seg.lower() in ("latest", "stable", "current"):
                candidates["version"] = (seg.lower(), 0.70)
                break
        # host hints: docs.python.org → often /3/
        if "version" not in candidates and "docs.python.org" in host:
            candidates["version"] = ("3", 0.60)

    for field, (value, conf) in candidates.items():
        ev = _make_evidence(
            source_id=source_id, url=url, text=value,
            selector=f"domain_heuristic::{field}", content_hash=content_hash,
            locale=locale, timezone_name=timezone_name, auth_context_id=auth_context_id,
            backend="L1_domain", now=now,
        )
        facts.append(_make_fact(
            field=field, value=value, datatype="string",
            confidence=conf, source_id=source_id, method="url_derived", evidence=ev,
        ))
        evidence.append(ev)
    return facts, evidence


def _alias_fields(facts: list[Fact], schema_fields: list[str], existing: set[str]) -> list[Fact]:
    """Copy date→publish_date, title↔name when schema asks and value missing."""
    by = {f.field: f for f in facts}
    extra: list[Fact] = []

    pairs = [
        ("date", "publish_date"),
        ("publish_date", "date"),
        ("title", "name"),
        ("name", "title"),
        ("main_text", "description"),  # docs without meta description
    ]
    for src, dst in pairs:
        if dst in schema_fields and dst not in existing and dst not in by and src in by:
            src_f = by[src]
            # shallow clone via model_copy if available
            try:
                nf = src_f.model_copy(deep=True)
            except Exception:
                continue
            nf.field = dst
            nf.confidence = round(src_f.confidence * 0.95, 3)
            if src == "main_text" and dst == "description":
                text = str(src_f.value or "").strip()
                # first paragraph-ish chunk, keep readable card length
                chunk = text.split("\n\n")[0].strip() if text else ""
                if len(chunk) < 40:
                    chunk = text[:400]
                else:
                    chunk = chunk[:500]
                nf.value = chunk
                nf.datatype = "string"
                nf.confidence = round(min(src_f.confidence, 0.72) * 0.95, 3)
                nf.extraction_method = "main_text_alias"
            extra.append(nf)
            by[dst] = nf
    return extra




# ---------------------------------------------------------------------------
# Microdata itemprop pass
# ---------------------------------------------------------------------------
_MICRODATA_FIELDS = (
    "name", "title", "price", "brand", "availability", "description",
    "author", "date", "datePublished", "sku", "priceCurrency",
)

def _extract_microdata(
    html_text: str,
    url: str,
    content_hash: str,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
    existing_fields: set[str],
    schema_fields: list[str],
) -> tuple[list[Fact], list[Evidence]]:
    facts: list[Fact] = []
    evidence: list[Evidence] = []
    source_id = "src_md_" + hashlib.sha256(url.encode()).hexdigest()[:12]
    now = _clock()
    tree = HTMLParser(html_text)
    wanted = set(schema_fields) | {"title", "name", "description", "author", "price"}

    # itemprop map: collect first value per target field
    found: dict[str, str] = {}
    for prop, field in [
        ("name", "name"),
        ("headline", "title"),
        ("price", "price"),
        ("priceCurrency", "currency"),
        ("brand", "brand"),
        ("availability", "availability"),
        ("description", "description"),
        ("author", "author"),
        ("datePublished", "date"),
        ("datePublished", "publish_date"),
    ]:
        if field in existing_fields or field in found:
            continue
        if field not in wanted and field not in schema_fields:
            continue
        try:
            node = tree.css_first(f'[itemprop="{prop}"]')
        except Exception:
            node = None
        if node is None:
            continue
        # nested name inside brand/author
        if prop in ("brand", "author"):
            nested = node.css_first('[itemprop="name"]')
            raw = _value(nested) if nested is not None else _value(node)
        else:
            raw = _value(node)
        if not raw:
            continue
        found[field] = raw

    for field, raw in found.items():
        try:
            if field in ("price", "availability", "currency"):
                value = normalize_jsonld_value(field, raw) if field != "price" else _coerce(raw, "number", locale)
            else:
                value = normalize_text(raw)
        except (ValueError, TypeError):
            continue
        if value is None or value == "":
            continue
        ev = _make_evidence(
            source_id=source_id, url=url, text=str(raw)[:200],
            selector=f'[itemprop="{field}"]', content_hash=content_hash,
            locale=locale, timezone_name=timezone_name, auth_context_id=auth_context_id,
            backend="L1_microdata", now=now,
        )
        facts.append(_make_fact(
            field=field, value=value, datatype=type(value).__name__,
            confidence=0.87, source_id=source_id, method="css_selector", evidence=ev,
        ))
        evidence.append(ev)
    return facts, evidence


def _availability_default(
    facts: list[Fact],
    schema_fields: list[str],
    existing: set[str],
    url: str,
    content_hash: str,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
) -> tuple[list[Fact], list[Evidence]]:
    """If product has price but no availability → assume InStock at low confidence."""
    if "availability" not in schema_fields or "availability" in existing:
        return [], []
    has_price = any(f.field == "price" for f in facts)
    if not has_price:
        return [], []
    source_id = "src_avdef_" + hashlib.sha256(url.encode()).hexdigest()[:12]
    now = _clock()
    ev = _make_evidence(
        source_id=source_id, url=url, text="InStock (inferred from price)",
        selector="heuristic::availability_from_price", content_hash=content_hash,
        locale=locale, timezone_name=timezone_name, auth_context_id=auth_context_id,
        backend="L1_heuristic", now=now,
    )
    fact = _make_fact(
        field="availability", value=True, datatype="boolean",
        confidence=0.55, source_id=source_id, method="url_derived", evidence=ev,
    )
    return [fact], [ev]




def _extract_wikipedia_lead(
    tree: Any,
    url: str,
    content_hash: str,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
    existing_fields: set[str],
    schema_fields: list[str],
) -> tuple[list[Fact], list[Evidence]]:
    """First non-empty paragraph under #mw-content-text as description."""
    url_s = str(url or "")
    if "wikipedia.org" not in url_s.lower():
        return [], []
    if "description" not in schema_fields or "description" in existing_fields:
        return [], []
    facts: list[Fact] = []
    evidence: list[Evidence] = []
    now = _clock()
    source_id = "src_wiki_" + hashlib.sha256(url_s.encode()).hexdigest()[:12]
    try:
        nodes = tree.css("#mw-content-text p")
        text = None
        for n in nodes[:12]:
            raw = (n.text(strip=True) if hasattr(n, "text") else "") or ""
            raw = re.sub(r"\s+", " ", raw).strip()
            if len(raw) >= 60 and not raw.startswith("Coordinates"):
                text = raw[:500]
                break
        if not text:
            return [], []
        ev = _make_evidence(
            source_id=source_id, url=url_s, text=text[:200],
            selector="#mw-content-text p", content_hash=content_hash,
            locale=locale, timezone_name=timezone_name, auth_context_id=auth_context_id,
            backend="L1_wiki", now=now,
        )
        facts.append(_make_fact(
            field="description", value=text, datatype="string", confidence=0.78,
            source_id=source_id, method="css_selector", evidence=ev,
        ))
        evidence.append(ev)
    except Exception:
        return [], []
    return facts, evidence




def _docs_lead_description(
    html_text: str,
    url: str,
    content_hash: str,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
    existing: set[str],
    schema_fields: list[str],
) -> tuple[list[Fact], list[Evidence]]:
    """Sphinx/docs: first substantial paragraph in body as description."""
    facts: list[Fact] = []
    evidence: list[Evidence] = []
    if "description" not in schema_fields or "description" in existing:
        return facts, evidence
    host = urlparse(url).netloc.lower()
    if not any(x in host for x in ("readthedocs", "docs.", "readthedocs.io")) and "/docs" not in url and "sqlalchemy" not in host:
        # still allow generic docs tactic via caller
        pass
    tree = HTMLParser(html_text)
    candidates = []
    for sel in (
        "div.body div.section > p",
        "div.document div.section > p",
        "div.body p",
        "main p",
        "article p",
        "div[role='main'] p",
    ):
        for node in tree.css(sel)[:8]:
            text = " ".join((node.text() or "").split())
            if len(text) >= 60 and not text.lower().startswith("home"):
                candidates.append(text)
        if candidates:
            break
    if not candidates:
        return facts, evidence
    text = candidates[0][:500]
    now = _clock()
    source_id = "src_docs_lead_" + hashlib.sha256(url.encode()).hexdigest()[:12]
    ev = _make_evidence(
        source_id=source_id, url=url, text=text[:200],
        selector="docs_lead:first_body_p", content_hash=content_hash,
        locale=locale, timezone_name=timezone_name, auth_context_id=auth_context_id,
        backend="L1_docs_lead", now=now,
    )
    facts.append(_make_fact(
        field="description", value=text, datatype="string",
        confidence=0.68, source_id=source_id, method="docs_lead", evidence=ev,
    ))
    evidence.append(ev)
    return facts, evidence

def extract_l1(
    html_text: str,
    url: str,
    schema: dict[str, Any],
    *,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
    content_hash: str,
    headers: dict[str, str] | None = None,
    ttfb_ms: float | None = None,
    body_bytes: int | None = None,
    retrieved_at: datetime | None = None,
) -> tuple[list[Fact], list[Evidence], list[str], dict[str, Any], list[str]]:
    """
    Returns: (facts, evidence, missing_fields, signature, debug_trace)

    Hybrid: pick_tactic(url, DOM) → TacticProfile configures which methods run,
    selector boosts, breadcrumbs, and browser hints. Multi-method evidence chain preserved.
    """
    trace: list[str] = []
    now = retrieved_at or datetime.now(timezone.utc)
    global _ACTIVE_RETRIEVED_AT
    _ACTIVE_RETRIEVED_AT = now
    sig = analyze_signature(html_text, headers=headers, url=url, ttfb_ms=ttfb_ms, body_bytes=body_bytes)
    # Bot / challenge detection (must live on sig for service escalate)
    _title_guess = None
    try:
        import re as _re
        _m = _re.search(r"<title[^>]*>([^<]+)", html_text or "", _re.I)
        _title_guess = _m.group(1).strip() if _m else None
    except Exception:
        pass
    if _looks_like_bot_wall(html_text, _title_guess):
        sig["bot_wall"] = True
        sig["bot_wall_title"] = _title_guess
    tactic = pick_tactic(url, sig)
    profile = get_profile(tactic)
    page_class = sig.get("page_class", "generic")
    # Prefer tactic name for selector boosts (more specific than raw DOM class)
    boost_class = tactic if tactic in _CLASS_SELECTOR_BOOST or tactic != "generic" else page_class

    trace.append(
        f"tactic={tactic} (url+dom) page_class={page_class} spa={sig.get('likely_spa')} "
        f"spa_score={sig.get('spa_score')} jsonld={sig.get('has_jsonld')} "
        f"fw={sig.get('frameworks')} blobs={sig.get('embedded_blobs')} "
        f"methods={list(profile.methods)}"
    )
    if sig.get("canonical"):
        trace.append(f"canonical={sig['canonical']}")
    if sig.get("sku_candidates"):
        trace.append(f"sku_candidates={sig['sku_candidates'][:3]}")

    schema_fields = _schema_fields(schema)
    existing: set[str] = set()
    all_facts: list[Fact] = []
    all_evidence: list[Evidence] = []
    methods = set(profile.methods)

    # Merge profile selector boosts into class boost for this run
    if profile.selector_boost:
        # temporarily extend _CLASS_SELECTOR_BOOST for boost_class
        merged = dict(_CLASS_SELECTOR_BOOST.get(boost_class, {}))
        for f, sels in profile.selector_boost.items():
            prev = list(merged.get(f, []))
            merged[f] = list(sels) + prev
        _CLASS_SELECTOR_BOOST[boost_class] = merged  # type: ignore[index]

    # 1. JSON-LD

    # --- PR-1 structured layer (path-aware JSON-LD / microdata / OG) ---
    try:
        base_url = get_base_url(html_text, url)
    except Exception:
        base_url = url
    try:
        tree_for_links = HTMLParser(html_text)
        links = extract_all_links(tree_for_links, headers, base_url)
        canon = primary_canonical(links)
        if canon:
            sig["canonical_link"] = canon
            if not sig.get("canonical"):
                sig["canonical"] = canon
        sig["link_relations"] = len(links)
    except Exception as link_exc:
        trace.append(f"links_failed: {type(link_exc).__name__}")
        links = []

    structured_assertions = extract_structured_data(
        html_text=html_text,
        base_url=base_url,
        content_hash=content_hash,
        missing_fields=schema_fields,
        retrieved_at=now,
    )
    og_fb = manual_og_fallback(
        html_text, url, content_hash, retrieved_at=now,
    )
    structured_assertions = append_missing_og_fallback(
        structured_assertions, og_fb, normalize_jsonld_value,
    )
    sfacts, sevidence = adapt_to_facts_and_evidence(
        structured_assertions,
        retrieved_at=now,
        locale=locale,
        timezone_name=timezone_name,
        auth_context_id=auth_context_id,
        schema_fields=schema_fields,
    )
    all_facts.extend(sfacts)
    all_evidence.extend(sevidence)
    existing.update(f.field for f in sfacts)
    # title/name alias
    if "title" in existing and "name" not in existing and "name" in schema_fields:
        for f in list(sfacts):
            if f.field == "title":
                all_facts.append(f.model_copy(update={"field": "name"}))
                existing.add("name")
                break
    elif "name" in existing and "title" not in existing and "title" in schema_fields:
        for f in list(sfacts):
            if f.field == "name":
                all_facts.append(f.model_copy(update={"field": "title"}))
                existing.add("title")
                break
    # Publisher as institutional author when NewsArticle.author == []
    if "author" in schema_fields and "author" not in existing:
        pub_as = publisher_author_fallback(html_text, url, content_hash, retrieved_at=now)
        if pub_as:
            pf, pe = adapt_to_facts_and_evidence(
                pub_as,
                retrieved_at=now,
                locale=locale,
                timezone_name=timezone_name,
                auth_context_id=auth_context_id,
                schema_fields=schema_fields,
            )
            for f in pf:
                f.confidence = min(f.confidence, 0.58)
                f.extraction_method = "publisher_fallback"
            all_facts.extend(pf)
            all_evidence.extend(pe)
            existing.update(f.field for f in pf)
            if pf:
                trace.append(f"publisher_author: +{len(pf)}")

    trace.append(
        f"structured: +{len(sfacts)} fields={sorted({f.field for f in sfacts})} "
        f"links={sig.get('link_relations', 0)}"
    )

    if "json_ld" in methods:
        jf, je = _extract_jsonld(html_text, url, content_hash, locale, timezone_name, auth_context_id)
        # Dedupe: skip fields already claimed by structured layer at high confidence
        structured_claimed = {
            f.field
            for f in all_facts
            if f.extraction_method in STRUCTURED_METHODS
            and f.confidence >= STRUCTURED_CONF_SKIP
            and f.validation_status == "valid"
        }
        if structured_claimed:
            ev_by_id = {e.evidence_id: e for e in je}
            jf_f, je_f = [], []
            kept_ev = set()
            for f in jf:
                if f.field in structured_claimed:
                    continue
                jf_f.append(f)
                for eid in f.evidence_ids:
                    if eid in ev_by_id and eid not in kept_ev:
                        je_f.append(ev_by_id[eid])
                        kept_ev.add(eid)
            skipped = sorted(structured_claimed & {f.field for f in jf})
            jf, je = jf_f, je_f
            if skipped:
                trace.append(f"json_ld_dedupe: skip={skipped}")
        all_facts.extend(jf)
        all_evidence.extend(je)
        existing.update(f.field for f in jf)
        trace.append(f"json_ld: +{len(jf)} fields={sorted({f.field for f in jf})}")

    # 1b. Embedded SPA state (__NEXT_DATA__, __NUXT__, ...)
    ef, ee, blobs = extract_embedded_state(
        html_text, url, content_hash, locale, timezone_name, auth_context_id,
        existing, schema_fields,
    )
    all_facts.extend(ef)
    all_evidence.extend(ee)
    existing.update(f.field for f in ef)
    if blobs:
        trace.append(f"embedded({','.join(blobs)}): +{len(ef)} fields={sorted({f.field for f in ef})}")
        sig["embedded_blobs"] = blobs

    # 2. Meta / OG
    if "meta_og" in methods:
        mf, me = _extract_meta_og(
            html_text, url, content_hash, locale, timezone_name, auth_context_id,
            existing, schema_fields,
        )
        all_facts.extend(mf)
        all_evidence.extend(me)
        existing.update(f.field for f in mf)
        trace.append(f"meta_og: +{len(mf)} fields={sorted({f.field for f in mf})}")

    # 3. Trafilatura
    if "trafilatura" in methods:
        tf, te = _extract_trafilatura(
            html_text, url, content_hash, locale, timezone_name, auth_context_id, existing,
        )
        all_facts.extend(tf)
        all_evidence.extend(te)
        existing.update(f.field for f in tf)
        trace.append(f"trafilatura: +{len(tf)} fields={sorted({f.field for f in tf})}")

    # Docs lead paragraph → description if still missing
    if "description" in schema_fields and "description" not in existing:
        df, de = _docs_lead_description(
            html_text, url, content_hash, locale, timezone_name, auth_context_id,
            existing, schema_fields,
        )
        all_facts.extend(df)
        all_evidence.extend(de)
        existing.update(f.field for f in df)
        if df:
            trace.append(f"docs_lead: +{len(df)}")

    # 4. CSS
    if "css" in methods:
        missing = [f for f in schema_fields if f not in existing]
        cf, ce, still_missing = _extract_css(
            html_text, url, content_hash, locale, timezone_name, auth_context_id,
            missing, schema, boost_class,
        )
        all_facts.extend(cf)
        all_evidence.extend(ce)
        existing.update(f.field for f in cf)
        trace.append(f"css: +{len(cf)} still_missing={still_missing}")
    else:
        still_missing = [f for f in schema_fields if f not in existing]

    # 4b. Microdata itemprop
    mdf, mde = _extract_microdata(
        html_text, url, content_hash, locale, timezone_name, auth_context_id,
        existing, schema_fields,
    )
    all_facts.extend(mdf)
    all_evidence.extend(mde)
    existing.update(f.field for f in mdf)
    if mdf:
        trace.append(f"microdata: +{len(mdf)} fields={sorted({f.field for f in mdf})}")

    # 5. Breadcrumbs (docs/reference/generic)
    if profile.use_breadcrumbs:
        bf, be = _extract_breadcrumbs(
            html_text, url, content_hash, locale, timezone_name, auth_context_id,
            existing, schema_fields,
        )
        all_facts.extend(bf)
        all_evidence.extend(be)
        existing.update(f.field for f in bf)
        if bf:
            trace.append(f"breadcrumbs: +{len(bf)} fields={sorted({f.field for f in bf})}")

    # 6. URL-derived
    if "url_derived" in methods:
        uf, ue = _extract_url_derived(
            url, content_hash, locale, timezone_name, auth_context_id, existing, schema_fields,
        )
        all_facts.extend(uf)
        all_evidence.extend(ue)
        existing.update(f.field for f in uf)
        if uf:
            trace.append(f"url_derived: +{len(uf)} fields={sorted({f.field for f in uf})}")

    # 7. Price regex
    if "price_regex" in methods:
        pf, pe = _extract_price_regex(
            html_text, url, content_hash, locale, timezone_name, auth_context_id,
            existing, schema_fields, "product" if tactic == "product" else page_class,
        )
        all_facts.extend(pf)
        all_evidence.extend(pe)
        existing.update(f.field for f in pf)
        if pf:
            trace.append(f"price_regex: +{len(pf)}")

    # 7c. Wikipedia lead paragraph → description
    try:
        tree_w = HTMLParser(html_text)
        wf, we = _extract_wikipedia_lead(
            tree_w, url, content_hash, locale, timezone_name, auth_context_id,
            existing, schema_fields,
        )
        all_facts.extend(wf)
        all_evidence.extend(we)
        existing.update(f.field for f in wf)
        if wf:
            trace.append(f"wikipedia_lead: +{len(wf)}")
    except Exception as e:
        trace.append(f"wikipedia_lead_error: {type(e).__name__}")

    # 8. Domain heuristics (brand/currency/version)
    dhf, dhe = _extract_domain_heuristics(
        url, content_hash, locale, timezone_name, auth_context_id,
        existing, schema_fields, tactic,
    )
    all_facts.extend(dhf)
    all_evidence.extend(dhe)
    existing.update(f.field for f in dhf)
    if dhf:
        trace.append(f"domain_heuristics: +{len(dhf)} fields={sorted({f.field for f in dhf})}")

    # 8b. Availability default when price present
    if tactic == "product":
        avf, ave = _availability_default(
            all_facts, schema_fields, existing, url, content_hash,
            locale, timezone_name, auth_context_id,
        )
        all_facts.extend(avf)
        all_evidence.extend(ave)
        existing.update(f.field for f in avf)
        if avf:
            trace.append("availability_default: +1")

    # 9. Field aliases (date↔publish_date, title↔name)
    aliased = _alias_fields(all_facts, schema_fields, existing)
    all_facts.extend(aliased)
    existing.update(f.field for f in aliased)
    if aliased:
        trace.append(f"aliases: +{len(aliased)} fields={sorted({f.field for f in aliased})}")

    # Scale confidence if profile asks
    if profile.confidence_scale != 1.0:
        for f in all_facts:
            f.confidence = round(min(1.0, f.confidence * profile.confidence_scale), 3)

    # 8. Conflicts
    final = _resolve_conflicts(all_facts)
    conflicts = sum(1 for f in final if f.conflict.status != "none")
    trace.append(f"resolve: {len(all_facts)} → {len(final)} facts, conflicts={conflicts}")

    still = [f for f in schema_fields if f not in {x.field for x in final}]

    # Browser hint annotation in signature (service reads it)
    sig = dict(sig)
    sig["tactic"] = tactic
    sig["browser_hint"] = should_hint_browser(profile, len(final), len(still))
    sig["focus_fields"] = list(profile.focus_fields)

    return final, all_evidence, still, sig, trace
