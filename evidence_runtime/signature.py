"""Page signature: DOM + HTTP headers + scripts + head signals + SPA score."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

_SPA_GENERATORS = ("next.js", "nuxt.js", "astro", "sveltekit", "gatsby", "remix")
_SPA_MARKERS = (
    "window.__INITIAL_STATE__",
    "window.__DATA__",
    "__NEXT_DATA__",
    "window.__NUXT__",
    "window.__APOLLO_STATE__",
    "window.__PRELOADED_STATE__",
    "window.Shopify",
    "data-reactroot",
    "ng-version",
)

_SCRIPT_FINGERPRINTS: list[tuple[str, str]] = [
    ("_next/static", "next.js"),
    ("next/dist", "next.js"),
    ("_nuxt/", "nuxt.js"),
    ("nuxt.", "nuxt.js"),
    ("cdn.shopify.com", "shopify"),
    ("shopify", "shopify"),
    ("wp-content", "wordpress"),
    ("wp-includes", "wordpress"),
    ("woocommerce", "woocommerce"),
    ("react", "react"),
    ("vue.", "vue"),
    ("angular", "angular"),
    ("cdn.jsdelivr.net/npm/vue", "vue"),
    ("gatsby", "gatsby"),
    ("svelte", "svelte"),
    ("webflow", "webflow"),
    ("squarespace", "squarespace"),
    ("wix.com", "wix"),
    ("magento", "magento"),
    ("requirejs", "requirejs"),
]

_TRACKER_NEEDLES = (
    "google-analytics", "googletagmanager", "gtag/js", "facebook.net",
    "connect.facebook", "hotjar", "segment.com", "mixpanel", "amplitude",
    "newrelic", "sentry.io", "clarity.ms", "doubleclick", "adservice",
    "optimizely", "fullstory", "heap-api", "intercom", "cdn.cookielaw",
)

_CMS_COMMENT_RE = re.compile(
    r"<!--\s*(WordPress|Drupal|Joomla|Shopify|Wix|Squarespace|Ghost|Hugo|Jekyll|TYPO3)[^>]*>",
    re.IGNORECASE,
)


def _text_ratio(html_head: str) -> float:
    text_len = len("".join(c for c in html_head if not c.isspace()))
    tag_count = html_head.count("<")
    if tag_count == 0:
        return 1.0
    return text_len / tag_count


def _extract_generator(html_head: str) -> str | None:
    m = re.search(
        r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)["\']',
        html_head, flags=re.IGNORECASE,
    )
    if m:
        return m.group(1)
    m = re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']generator["\']',
        html_head, flags=re.IGNORECASE,
    )
    return m.group(1) if m else None


def _count_tag(html: str, tag: str) -> int:
    return len(re.findall(rf"<{tag}[\s>]", html, flags=re.IGNORECASE))


def _script_frameworks(html_head: str) -> list[str]:
    found: list[str] = []
    lower = html_head.lower()
    for needle, name in _SCRIPT_FINGERPRINTS:
        if needle in lower and name not in found:
            found.append(name)
    return found


def _tracker_count(html_head: str) -> tuple[int, list[str]]:
    lower = html_head.lower()
    hits = [n for n in _TRACKER_NEEDLES if n in lower]
    return len(hits), hits[:8]


def _script_stats(html: str) -> dict[str, Any]:
    """Count scripts, external vs inline, chunk-like paths."""
    head = html[:65536]
    # simpler counts
    n_script = len(re.findall(r"<script[\s>]", head, re.I))
    srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', head, re.I)
    external = len(srcs)
    inline = max(0, n_script - external)
    chunks = sum(1 for s in srcs if any(x in s for x in ("/chunk", "/_next/", ".chunk.", "/static/js/")))
    return {
        "script_count": n_script,
        "script_external": external,
        "script_inline": inline,
        "chunk_like": chunks,
    }


def _embedded_blobs(html: str) -> list[str]:
    blobs = []
    for name, marker in (
        ("next_data", "__NEXT_DATA__"),
        ("nuxt", "__NUXT__"),
        ("initial_state", "__INITIAL_STATE__"),
        ("apollo", "__APOLLO_STATE__"),
        ("preloaded", "__PRELOADED_STATE__"),
        ("data", "window.__DATA__"),
        ("shopify", "window.Shopify"),
        ("shopify_analytics", "ShopifyAnalytics"),
    ):
        if marker in html:
            blobs.append(name)
    return blobs


def _head_links(html_head: str, base_url: str = "") -> dict[str, Any]:
    """canonical, hreflang, rss alternate, preload, next/prev."""
    canonical = None
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', html_head, re.I)
    if not m:
        m = re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']', html_head, re.I)
    if m:
        canonical = m.group(1).strip()
        if base_url:
            canonical = urljoin(base_url, canonical)

    hreflangs = re.findall(
        r'<link[^>]+rel=["\']alternate["\'][^>]+hreflang=["\']([^"\']+)["\'][^>]+href=["\']([^"\']+)["\']',
        html_head, re.I,
    )
    if not hreflangs:
        hreflangs = re.findall(
            r'<link[^>]+hreflang=["\']([^"\']+)["\'][^>]+rel=["\']alternate["\'][^>]+href=["\']([^"\']+)["\']',
            html_head, re.I,
        )

    rss = []
    for m in re.finditer(
        r'<link[^>]+rel=["\']alternate["\'][^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]+href=["\']([^"\']+)["\']',
        html_head, re.I,
    ):
        rss.append(m.group(1))

    preloads = re.findall(r'<link[^>]+rel=["\']preload["\'][^>]+href=["\']([^"\']+)["\']', html_head, re.I)
    rel_next = None
    m = re.search(r'<link[^>]+rel=["\']next["\'][^>]+href=["\']([^"\']+)["\']', html_head, re.I)
    if m:
        rel_next = urljoin(base_url, m.group(1)) if base_url else m.group(1)
    rel_prev = None
    m = re.search(r'<link[^>]+rel=["\']prev["\'][^>]+href=["\']([^"\']+)["\']', html_head, re.I)
    if m:
        rel_prev = urljoin(base_url, m.group(1)) if base_url else m.group(1)

    theme = None
    m = re.search(r'<meta[^>]+name=["\']theme-color["\'][^>]+content=["\']([^"\']+)["\']', html_head, re.I)
    if m:
        theme = m.group(1)

    return {
        "canonical": canonical,
        "hreflang_count": len(hreflangs),
        "hreflangs": [{"lang": a, "href": b} for a, b in hreflangs[:5]],
        "rss": rss[:3],
        "preload_count": len(preloads),
        "rel_next": rel_next,
        "rel_prev": rel_prev,
        "theme_color": theme,
    }


def _cms_from_comments(html_head: str) -> str | None:
    m = _CMS_COMMENT_RE.search(html_head)
    return m.group(1) if m else None


def _product_sku_candidates(html: str, base_url: str) -> list[str]:
    """Find likely product detail links for one-hop follow (marketing → SKU)."""
    head = html[:120_000]
    candidates: list[str] = []
    # common product path patterns
    for href in re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', head, re.I):
        low = href.lower()
        if any(p in low for p in ("/dp/", "/product/", "/p/", "/ip/", "/shop/product", "/buy/", "/sku/")):
            full = urljoin(base_url, href)
            if full.startswith("http") and full not in candidates:
                candidates.append(full)
        if len(candidates) >= 5:
            break
    return candidates


def analyze_headers(headers: dict[str, str] | None) -> dict[str, Any]:
    if not headers:
        return {}
    h = {str(k).lower(): str(v) for k, v in headers.items()}

    server = h.get("server")
    powered = h.get("x-powered-by")
    generator_hdr = h.get("x-generator")
    cache_control = h.get("cache-control")
    cf_status = h.get("cf-cache-status")
    via = h.get("via")
    x_cache = h.get("x-cache")
    content_encoding = h.get("content-encoding")
    hsts = h.get("strict-transport-security")
    etag = h.get("etag")
    age = h.get("age")
    set_cookie = h.get("set-cookie") or ""

    cdn = None
    server_l = (server or "").lower()
    via_l = (via or "").lower()
    if "cloudflare" in server_l or cf_status:
        cdn = "cloudflare"
    elif "amazon" in server_l or "cloudfront" in via_l or "cloudfront" in server_l:
        cdn = "cloudfront"
    elif "fastly" in server_l or "fastly" in via_l:
        cdn = "fastly"
    elif "akamai" in server_l or "akamai" in via_l:
        cdn = "akamai"
    elif "vercel" in server_l or h.get("x-vercel-id"):
        cdn = "vercel"
    elif "netlify" in server_l or h.get("x-nf-request-id"):
        cdn = "netlify"

    static_hint = False
    if cf_status and cf_status.upper() == "HIT":
        static_hint = True
    if x_cache and "hit" in x_cache.lower():
        static_hint = True
    if cache_control and "max-age=" in cache_control and "no-cache" not in (cache_control or ""):
        m = re.search(r"max-age=(\d+)", cache_control)
        if m and int(m.group(1)) >= 300:
            static_hint = True
    age_s = None
    if age and age.isdigit():
        age_s = int(age)
        if age_s >= 60:
            static_hint = True

    framework_hint = None
    blob = f"{powered or ''} {server or ''} {generator_hdr or ''}".lower()
    for token, name in (("next.js", "next.js"), ("express", "express"), ("php", "php"),
                        ("asp.net", "asp.net"), ("django", "django")):
        if token in blob:
            framework_hint = name
            break

    # A/B / experiment cookies (names only, not values)
    ab_cookies = []
    for part in set_cookie.split(","):
        name = part.split("=", 1)[0].strip().lower()
        if any(x in name for x in ("ab_", "experiment", "variant", "optimize", "split", "flag")):
            ab_cookies.append(name)

    return {
        "server": server,
        "x_powered_by": powered,
        "cdn": cdn,
        "cache_control": cache_control,
        "cf_cache_status": cf_status,
        "content_encoding": content_encoding,
        "hsts": bool(hsts),
        "static_hint": static_hint,
        "framework_hint": framework_hint,
        "set_cookie": bool(set_cookie),
        "etag": etag,
        "age_s": age_s,
        "ab_cookies": ab_cookies[:5],
    }


def _infer_page_class(html: str, has_jsonld: bool, likely_spa: bool) -> str:
    head = html[:65536]
    lower = head.lower()

    product_hits = 0
    if "product:price" in lower or 'og:type" content="product' in lower or "og:type' content='product" in lower:
        product_hits += 2
    if 'itemprop="price"' in lower or "itemprop='price'" in lower:
        product_hits += 2
    if "application/ld+json" in lower and ("product" in lower or "aggregateoffer" in lower or '"offer"' in lower):
        product_hits += 1
    if re.search(r'class=["\'][^"\']*price[^"\']*["\']', lower):
        product_hits += 1
    if product_hits >= 2:
        return "product"

    h2 = _count_tag(head, "h2")
    code = _count_tag(head, "code") + _count_tag(head, "pre")
    if h2 >= 4 and code >= 2:
        return "docs"
    if "docs." in lower or "/docs/" in lower or "documentation" in lower:
        if h2 >= 2 or code >= 1:
            return "docs"

    has_time = bool(re.search(r"<time[\s>]", head, re.IGNORECASE)) or "article:published_time" in lower
    has_article_tag = "<article" in lower
    has_author = 'itemprop="author"' in lower or 'name="author"' in lower or "article:author" in lower
    if has_article_tag and (has_time or has_author):
        return "article"
    if "og:type" in lower and "article" in lower:
        return "article"

    if "wikipedia.org" in lower or "infobox" in lower:
        if _count_tag(head, "table") >= 2 or "infobox" in lower:
            return "reference"

    if likely_spa:
        return "spa"
    return "generic"


def analyze_signature(
    html: str,
    headers: dict[str, str] | None = None,
    *,
    url: str = "",
    ttfb_ms: float | None = None,
    body_bytes: int | None = None,
) -> dict[str, Any]:
    html_text = html or ""
    head = html_text[:32768]

    generator = _extract_generator(head)
    text_ratio = _text_ratio(head)
    has_jsonld = "application/ld+json" in head.lower() or "ld+json" in head.lower()
    spa_marker = any(m in html_text for m in _SPA_MARKERS)
    frameworks = _script_frameworks(head)
    blobs = _embedded_blobs(html_text[:500_000])
    stats = _script_stats(html_text)
    tracker_n, trackers = _tracker_count(head)
    links = _head_links(head, base_url=url)
    cms_comment = _cms_from_comments(head)

    gen_l = (generator or "").lower()
    spa_from_gen = any(g in gen_l for g in _SPA_GENERATORS)
    spa_from_fw = any(f in ("next.js", "nuxt.js", "gatsby", "svelte", "react", "shopify") for f in frameworks)
    likely_spa = spa_marker or spa_from_gen or (spa_from_fw and text_ratio < 8.0) or (
        text_ratio < 4.0 and stats["script_count"] >= 5
    )

    page_class = _infer_page_class(html_text, has_jsonld, likely_spa)
    hdr = analyze_headers(headers)

    if hdr.get("framework_hint") == "next.js" or hdr.get("cdn") == "vercel":
        if "next.js" not in frameworks:
            frameworks.append("next.js")
        likely_spa = True

    # SPA weight score 0..1 from signals (for router confidence)
    spa_score = 0.0
    if spa_marker:
        spa_score += 0.35
    if spa_from_fw:
        spa_score += 0.25
    if stats["chunk_like"] >= 2:
        spa_score += 0.15
    if text_ratio < 5:
        spa_score += 0.15
    if ttfb_ms is not None and body_bytes is not None and body_bytes < 8000 and ttfb_ms < 400:
        # tiny body + fast TTFB → empty shell SPA
        spa_score += 0.2
    spa_score = min(1.0, spa_score)

    sku_links = []
    if page_class in ("product", "spa", "generic") and url:
        sku_links = _product_sku_candidates(html_text, url)

    has_time = bool(re.search(r"<time[\s>]", head, re.I)) or "article:published_time" in head.lower()

    return {
        "text_ratio": round(text_ratio, 2),
        "generator": generator or cms_comment,
        "likely_spa": bool(likely_spa),
        "has_time": has_time,
        "spa_score": round(spa_score, 2),
        "has_jsonld": has_jsonld,
        "page_class": page_class,
        "frameworks": frameworks,
        "embedded_blobs": blobs,
        "headers": hdr,
        "cdn": hdr.get("cdn"),
        "static_hint": hdr.get("static_hint", False),
        "etag": hdr.get("etag"),
        "age_s": hdr.get("age_s"),
        "ab_cookies": hdr.get("ab_cookies") or [],
        "scripts": stats,
        "trackers": trackers,
        "tracker_count": tracker_n,
        "canonical": links.get("canonical"),
        "hreflang_count": links.get("hreflang_count", 0),
        "rss": links.get("rss") or [],
        "preload_count": links.get("preload_count", 0),
        "rel_next": links.get("rel_next"),
        "rel_prev": links.get("rel_prev"),
        "theme_color": links.get("theme_color"),
        "sku_candidates": sku_links,
        "ttfb_ms": ttfb_ms,
        "body_bytes": body_bytes,
    }
