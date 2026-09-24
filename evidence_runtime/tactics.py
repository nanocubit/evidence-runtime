"""Tactic profiles: configure L1 pipeline by page type (hybrid router).

Design:
  - pick_tactic(url, signature) fuses URL hints + DOM page_class
  - TacticProfile controls: method order, extra selectors, early needs_browser,
    preferred fields, breadcrumb extraction
  - Does NOT replace multi-method extraction — it configures it
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class TacticProfile:
    name: str
    # Methods to run (subset of full pipeline). Empty = all.
    methods: tuple[str, ...] = (
        "json_ld",
        "meta_og",
        "trafilatura",
        "css",
        "url_derived",
        "price_regex",
    )
    # Prefer these schema fields; boost completeness scoring
    focus_fields: tuple[str, ...] = ()
    # Extra CSS selectors merged ahead of defaults
    selector_boost: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # If True and yield is low → failure_reason needs_browser
    browser_hint: bool = False
    # Extract breadcrumbs into source/category
    use_breadcrumbs: bool = False
    # Confidence penalty applied to all facts from this tactic (0 = none)
    confidence_scale: float = 1.0


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

PROFILE_DOCS = TacticProfile(
    name="docs",
    methods=("meta_og", "json_ld", "css", "trafilatura", "url_derived"),
    focus_fields=("title", "description", "source", "category", "version"),
    selector_boost={
        "title": ("article h1", ".document-title", "h1", ".wy-nav-content h1"),
        "description": (".document-description", "article > p", ".section > p"),
        "version": ('meta[name="docs-version"]', ".version", "[data-version]"),
    },
    use_breadcrumbs=True,
)

PROFILE_ARTICLE = TacticProfile(
    name="article",
    methods=("json_ld", "meta_og", "trafilatura", "css"),
    focus_fields=("title", "author", "date", "publish_date", "description"),
    selector_boost={
        "title": ("article h1", ".article-title", ".post-title", "h1.entry-title"),
        "author": (".byline", ".author-name", "[rel='author']", "[data-author]"),
        "date": (".published", ".post-date", "time.entry-date", "time[datetime]"),
        "description": ('meta[property="og:description"]', "article > p"),
    },
)

PROFILE_PRODUCT = TacticProfile(
    name="product",
    methods=("json_ld", "meta_og", "css", "price_regex", "trafilatura"),
    focus_fields=("name", "title", "price", "currency", "brand", "availability"),
    selector_boost={
        "name": ('[data-testid*="product"]', ".product-title", ".product-name", "h1"),
        "price": (
            '[data-testid*="price"]',
            ".product-price",
            ".a-price .a-offscreen",
            "[itemprop='price']",
            "[data-price]",
        ),
        "brand": ("[itemprop='brand']", ".brand", ".product-brand"),
    },
    browser_hint=True,  # many product pages need JS for live price
    confidence_scale=0.95,
)

PROFILE_SPA = TacticProfile(
    name="spa",
    methods=("json_ld", "meta_og", "css", "url_derived"),  # skip heavy trafilatura often empty
    focus_fields=("title", "description", "source"),
    selector_boost={
        "title": ('meta[property="og:title"]', "title", "h1"),
        "description": ('meta[property="og:description"]',),
    },
    browser_hint=True,
    confidence_scale=0.90,
)

PROFILE_REFERENCE = TacticProfile(
    name="reference",
    methods=("json_ld", "meta_og", "trafilatura", "css", "url_derived"),
    focus_fields=("title", "description", "category", "source"),
    selector_boost={
        "title": ("h1", "#firstHeading", ".mw-page-title-main"),
        "description": ('meta[name="description"]', ".lead", "p"),
    },
    use_breadcrumbs=True,
)

PROFILE_GENERIC = TacticProfile(
    name="generic",
    methods=("json_ld", "meta_og", "trafilatura", "css", "url_derived", "price_regex"),
    focus_fields=("title", "description"),
    use_breadcrumbs=True,
)

PROFILES: dict[str, TacticProfile] = {
    "docs": PROFILE_DOCS,
    "article": PROFILE_ARTICLE,
    "product": PROFILE_PRODUCT,
    "spa": PROFILE_SPA,
    "reference": PROFILE_REFERENCE,
    "generic": PROFILE_GENERIC,
    "fallback": PROFILE_GENERIC,
}


# ---------------------------------------------------------------------------
# URL-based hints (from the sketch) — fused with DOM signature
# ---------------------------------------------------------------------------

_URL_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("/docs/", "/api/", "readthedocs", "docs.python", "docs.djangoproject",
      "fastapi.tiangolo", "developer.mozilla", "kubernetes.io/docs", "docs.docker",
      "docs.rust-lang", "docs.pydantic", "docs.sqlalchemy", "react.dev"),
     "docs"),
    (("habr.", "medium.com", "arstechnica", "vc.ru", "theverge.com", "techcrunch",
      "wired.com", "bbc.com/news", "theguardian.com", "reuters.com", "nature.com",
      "scientificamerican", "techcrunch.com", "space.com", "smashingmagazine",
      "css-tricks", "dev.to", "news.ycombinator"),
     "article"),
    (("notion.", "airbnb.", "booking.com", "spotify.", "figma.com", "yelp.com",
      "tripadvisor", "reddit.com", "google.com/maps"),
     "spa"),
    (("/product", "/p/", "/item", "/dp/", "/shop/", "/t/",
      "ikea.", "ozon.", "newegg.", "mvideo.", "wildberries.", "citilink.",
      "bestbuy.", "apple.com", "samsung.com", "dell.com", "hp.com",
      "nike.com", "walmart.com", "amazon.com", "store.google.com", "shop.samsung"),
     "product"),
    (("wikipedia.org", "britannica.com", "worldometers", "cia.gov", "worldbank",
      "ourworldindata", "numbeo.", "imdb.com", "nationalgeographic"),
     "reference"),
]


def url_tactic_hint(url: str) -> str | None:
    u = url.lower()
    for keys, tactic in _URL_HINTS:
        if any(k in u for k in keys):
            return tactic
    # path heuristics
    path = urlparse(url).path.lower()
    if any(p in path for p in ("/docs/", "/doc/", "/api/", "/reference/")):
        return "docs"
    if any(p in path for p in ("/product", "/p/", "/item/", "/dp/")):
        return "product"
    return None


def pick_tactic(url: str, signature: dict[str, Any] | None = None) -> str:
    """
    Fuse URL hints + DOM page_class.
    Priority: strong URL hint > DOM page_class > weak URL > generic.
    """
    url_hint = url_tactic_hint(url)
    dom_class = (signature or {}).get("page_class") or "generic"

    # Strong agreement
    if url_hint and url_hint == dom_class:
        return url_hint

    # URL hints for known site types beat SPA-looking DOM (news sites are often SPA shells)
    if url_hint in ("product", "spa", "docs", "article", "reference"):
        return url_hint

    # DOM signals when URL is unknown
    if dom_class in ("product", "article", "docs", "spa", "reference"):
        return dom_class

    if url_hint:
        return url_hint
    if dom_class and dom_class != "generic":
        return dom_class
    return "generic"


def get_profile(tactic: str) -> TacticProfile:
    return PROFILES.get(tactic, PROFILE_GENERIC)


def should_hint_browser(profile: TacticProfile, facts_count: int, missing_count: int) -> bool:
    """Whether to surface needs_browser after L1 attempt."""
    if not profile.browser_hint:
        return False
    if facts_count == 0:
        return True
    # product/spa with many gaps
    if missing_count >= 2 and facts_count < 3:
        return True
    return False
