"""Extract structured facts from embedded SPA state blobs in HTML.

Still pure L1 (no browser): parse JSON already present in the first response.
Supports: __NEXT_DATA__, __NUXT__, window.__INITIAL_STATE__, window.__DATA__.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from .models import Conflict, Evidence, Fact
from .normalizer import normalize_text

# Patterns that capture a JSON object assigned to known globals
_BLOB_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("next_data", re.compile(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>\s*(\{.*?\})\s*</script>',
        re.DOTALL | re.IGNORECASE,
    )),
    ("nuxt", re.compile(
        r'window\.__NUXT__\s*=\s*(\{.*?\});\s*</script>',
        re.DOTALL | re.IGNORECASE,
    )),
    ("nuxt_script", re.compile(
        r'<script[^>]*>\s*window\.__NUXT__\s*=\s*(\{.*?\});\s*</script>',
        re.DOTALL | re.IGNORECASE,
    )),
    ("initial_state", re.compile(
        r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*(?:</script>|window\.)',
        re.DOTALL | re.IGNORECASE,
    )),
    ("data", re.compile(
        r'window\.__DATA__\s*=\s*(\{.*?\});\s*</script>',
        re.DOTALL | re.IGNORECASE,
    )),
    ("apollo", re.compile(
        r'window\.__APOLLO_STATE__\s*=\s*(\{.*?\});\s*</script>',
        re.DOTALL | re.IGNORECASE,
    )),
    ("redux", re.compile(
        r'window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});\s*</script>',
        re.DOTALL | re.IGNORECASE,
    )),
    ("shopify_meta", re.compile(
        r'var\s+meta\s*=\s*(\{.*?\});\s*(?:var\s+|</script>)',
        re.DOTALL | re.IGNORECASE,
    )),
    ("shopify_analytics", re.compile(
        r'ShopifyAnalytics\.lib\.track\([^)]*product[^)]*,\s*(\{.*?\})\s*\)',
        re.DOTALL | re.IGNORECASE,
    )),
    ("json_script", re.compile(
        r"<script[^>]+type=['\"]application/json['\"][^>]*>\s*(\{.*?\}|\[.*?\])\s*</script>",
        re.DOTALL | re.IGNORECASE,
    )),
]


# Keys we map from nested JSON → schema fields (order = preference within blob)
_KEY_TO_FIELD: list[tuple[str, str]] = [
    ("productName", "name"),
    ("productTitle", "name"),
    ("product_title", "name"),
    ("product_name", "name"),
    ("fullTitle", "title"),
    ("pageTitle", "title"),
    ("headline", "title"),
    ("title", "title"),
    ("name", "name"),
    ("brandName", "brand"),
    ("brand", "brand"),
    ("priceCurrency", "currency"),
    ("currency", "currency"),
    ("currencyCode", "currency"),
    ("currentPrice", "price"),
    ("salePrice", "price"),
    ("listPrice", "price"),
    ("unitPrice", "price"),
    ("price", "price"),
    ("amount", "price"),
    ("availability", "availability"),
    ("inStock", "availability"),
    ("isAvailable", "availability"),
    ("description", "description"),
    ("metaDescription", "description"),
    ("summary", "description"),
    ("authorName", "author"),
    ("author", "author"),
    ("byline", "author"),
    ("datePublished", "publish_date"),
    ("publishedAt", "publish_date"),
    ("publishDate", "publish_date"),
    ("published_time", "publish_date"),
    ("date", "date"),
]


def _walk(obj: Any, max_nodes: int = 800) -> list[tuple[str, Any]]:
    """Flatten nested dict/list into (key, value) pairs, depth-first."""
    out: list[tuple[str, Any]] = []
    stack: list[Any] = [obj]
    seen = 0
    while stack and seen < max_nodes:
        cur = stack.pop()
        seen += 1
        if isinstance(cur, dict):
            for k, v in cur.items():
                if isinstance(v, (dict, list)):
                    stack.append(v)
                elif v is not None and v != "":
                    out.append((str(k), v))
        elif isinstance(cur, list):
            for v in cur[:50]:
                if isinstance(v, (dict, list)):
                    stack.append(v)
                # skip bare scalars in lists
    return out


def _coerce_field(field: str, value: Any, locale: str) -> Any | None:
    if value is None:
        return None
    if field in ("price",):
        if isinstance(value, (int, float)):
            return float(value) if float(value) > 0 else None
        s = str(value).strip().replace(" ", "").replace(",", "")
        s = s.replace("$", "").replace("€", "").replace("£", "")
        try:
            v = float(s)
            return v if v > 0 else None
        except ValueError:
            return None
    if field == "availability":
        if isinstance(value, bool):
            return value
        s = str(value).lower()
        if any(x in s for x in ("instock", "in_stock", "available", "true", "yes")):
            return True
        if any(x in s for x in ("outofstock", "out_of_stock", "unavailable", "false", "no")):
            return False
        return None
    if field in ("title", "name", "description", "author", "brand", "currency", "date", "publish_date"):
        if isinstance(value, (dict, list)):
            if isinstance(value, dict) and "name" in value:
                value = value["name"]
            else:
                return None
        if value is None:
            return None
        s = normalize_text(str(value))
        if not s:
            return None
        bad = {
            "compact", "default", "true", "false", "null", "undefined", "none",
            "yes", "no", "on", "off", "unknown", "n/a", "test", "primary",
            "secondary", "small", "large", "medium",
        }
        if s.lower() in bad:
            return None
        if field in ("title", "name", "description") and len(s) < 4:
            return None
        if field == "currency" and len(s) not in (3, 4):
            return None
        return s
    return normalize_text(str(value)) if value else None


def _make_evidence(
    source_id: str, url: str, text: str, selector: str, content_hash: str,
    locale: str, timezone_name: str, auth_context_id: str | None, backend: str, now: datetime,
) -> Evidence:
    return Evidence(
        source_id=source_id,
        source_url=url,
        evidence_text=text[:500],
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
    field: str, value: Any, datatype: str, confidence: float,
    source_id: str, method: str, evidence: Evidence,
) -> Fact:
    return Fact(
        field=field,
        value=value,
        datatype=datatype,
        confidence=confidence,
        source_id=source_id,
        evidence_ids=[evidence.evidence_id],
        extraction_method=method,
        conflict=Conflict(),
    )


def extract_embedded_state(
    html_text: str,
    url: str,
    content_hash: str,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
    existing_fields: set[str],
    schema_fields: list[str],
) -> tuple[list[Fact], list[Evidence], list[str]]:
    """
    Returns (facts, evidence, blob_names_found).
    Only fills fields present in schema and not already extracted.
    """
    facts: list[Fact] = []
    evidence: list[Evidence] = []
    found_blobs: list[str] = []
    now = datetime.now(timezone.utc)
    wanted = set(schema_fields) | {"title", "name", "description", "price", "brand", "author"}

    for blob_name, pattern in _BLOB_PATTERNS:
        m = pattern.search(html_text)
        if not m:
            continue
        raw = m.group(1)
        # Guard: huge blobs — take first 2MB of match
        if len(raw) > 2_000_000:
            raw = raw[:2_000_000]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # try to fix trailing junk
            try:
                data = json.loads(raw.rsplit("}", 1)[0] + "}")
            except Exception:
                continue
        found_blobs.append(blob_name)

        pairs = _walk(data)
        # Prefer first match per field
        picked: dict[str, tuple[Any, str]] = {}
        for key, val in pairs:
            for src_key, field in _KEY_TO_FIELD:
                if key != src_key and key.lower() != src_key.lower():
                    continue
                if field in existing_fields or field in picked:
                    continue
                if field not in wanted and field not in schema_fields:
                    continue
                coerced = _coerce_field(field, val, locale)
                if coerced is None or coerced == "":
                    continue
                picked[field] = (coerced, key)
                break

        source_id = "src_emb_" + hashlib.sha256(f"{url}:{blob_name}".encode()).hexdigest()[:12]
        for field, (value, src_key) in picked.items():
            conf = 0.88 if blob_name == "next_data" else 0.84
            ev = _make_evidence(
                source_id=source_id, url=url, text=str(value)[:200],
                selector=f"{blob_name}.{src_key}", content_hash=content_hash,
                locale=locale, timezone_name=timezone_name, auth_context_id=auth_context_id,
                backend=f"L1_{blob_name}", now=now,
            )
            dtype = type(value).__name__
            facts.append(_make_fact(
                field=field, value=value, datatype=dtype, confidence=conf,
                source_id=source_id, method="json_ld", evidence=ev,  # reuse priority tier near json_ld
            ))
            evidence.append(ev)
            existing_fields.add(field)

        # One successful rich blob is usually enough
        if len(picked) >= 2:
            break

    return facts, evidence, found_blobs
