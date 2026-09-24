"""Path-aware structured data extraction (JSON-LD, microdata, OG, RDFa).

Intermediate StructuredAssertion layer → adapt to Fact/Evidence.
"""

from __future__ import annotations

from collections.abc import Generator, Iterable
from datetime import datetime
from typing import Any

from selectolax.parser import HTMLParser

from .models import Conflict, Evidence, Fact, StructuredAssertion
from .normalizer import normalize_jsonld_value

PRIMARY_SYNTAXES = ["json-ld", "microdata", "opengraph"]
SECONDARY_SYNTAXES = ["microformat", "rdfa"]

FIELD_DATATYPES: dict[str, str] = {
    "price": "number",
    "currency": "string",
    "availability": "string",
    "publish_date": "date",
    "date": "date",
}

FIELD_PATHS: dict[str, list[list[str]]] = {
    "title": [["headline"], ["name"]],
    "name": [["name"]],
    "description": [["description"]],
    "price": [["offers", "price"], ["price"]],
    "currency": [["offers", "priceCurrency"], ["priceCurrency"]],
    "availability": [["offers", "availability"], ["availability"]],
    "brand": [["brand", "name"], ["brand"]],
    "author": [["author", "name"], ["author"]],
    "publish_date": [["datePublished"]],
    "date": [["datePublished"]],
    "sku": [["sku"]],
}

MANUAL_OG_MAP = {
    "og:title": ("title", "string"),
    "og:description": ("description", "string"),
    "product:price:amount": ("price", "number"),
    "product:price:currency": ("currency", "string"),
    "product:brand": ("brand", "string"),
    "product:availability": ("availability", "string"),
    "article:author": ("author", "string"),
    "article:published_time": ("publish_date", "date"),
}

_CONFIDENCE = {
    "json-ld": 0.96,
    "microdata": 0.91,
    "opengraph": 0.89,
    "og_manual": 0.88,
    "microformat": 0.86,
    "rdfa": 0.86,
}


def is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def should_run_secondary(html_text: str, missing_fields: Iterable[str]) -> bool:
    if list(missing_fields):
        return True
    lower_html = html_text.lower()
    markers = (
        "h-card",
        "h-entry",
        "h-product",
        "h-recipe",
        "typeof=",
        "property=",
        "vocab=",
    )
    return any(marker in lower_html for marker in markers)


def _iter_entities(
    item: Any,
    prefix: list[Any] | None = None,
) -> Generator[tuple[dict[str, Any], list[Any]], None, None]:
    prefix = prefix or []
    if isinstance(item, list):
        for index, value in enumerate(item):
            yield from _iter_entities(value, prefix + [index])
        return
    if not isinstance(item, dict):
        return
    graph = item.get("@graph")
    if isinstance(graph, list):
        for index, entity in enumerate(graph):
            yield from _iter_entities(entity, prefix + ["@graph", index])
        return
    # Schema.org nesting: WebPage.mainEntity → Article
    main = item.get("mainEntity")
    if isinstance(main, dict):
        yield from _iter_entities(main, prefix + ["mainEntity"])
    elif isinstance(main, list):
        for index, entity in enumerate(main):
            yield from _iter_entities(entity, prefix + ["mainEntity", index])
    yield item, prefix


def _walk_path(
    value: Any,
    path: list[str],
    current_path: list[Any] | None = None,
) -> Generator[tuple[Any, list[Any]], None, None]:
    current_path = current_path or []
    if not path:
        yield value, current_path
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_path(item, path, current_path + [index])
        return
    if not isinstance(value, dict):
        return
    key = path[0]
    if key not in value:
        return
    yield from _walk_path(value[key], path[1:], current_path + [key])


def _project_scalar(field: str, value: Any) -> tuple[Any, list[str]]:
    if is_scalar(value):
        return value, []
    if isinstance(value, dict):
        for key in ("name", "value", "@id"):
            candidate = value.get(key)
            if is_scalar(candidate) and candidate not in (None, ""):
                return candidate, [key]
    return value, []


def _path_to_locator(
    syntax: str,
    item_index: int,
    entity_path: list[Any],
    value_path: list[Any],
) -> str:
    full_path = entity_path + value_path
    path_text = "".join(
        f"[{part}]" if isinstance(part, int) else f'["{part}"]' for part in full_path
    )
    return f"{syntax}:$[{item_index}]{path_text}"


def _map_entity(
    entity: dict[str, Any],
    syntax: str,
    item_index: int,
    entity_path: list[Any],
    source_url: str,
    snapshot_hash: str,
    retrieved_at: datetime | None,
) -> list[StructuredAssertion]:
    assertions: list[StructuredAssertion] = []
    entity_type = entity.get("@type", entity.get("type", ""))
    context = {"type": entity_type, "entity_path": entity_path}
    type_names = (
        {entity_type.lower()}
        if isinstance(entity_type, str)
        else {str(x).lower() for x in entity_type}
        if isinstance(entity_type, list)
        else set()
    )
    # Organization / Brand: do not use as page title
    _skip_title_types = {"organization", "brand", "corporation", "newsmediaorganization", "periodical", "publicationvolume", "publicationissue"}
    # WebSite alone is weak for article title
    _skip_title_types_weak = {"website"}

    for field, candidate_paths in FIELD_PATHS.items():
        if field in ("title", "name") and type_names & _skip_title_types:
            continue
        if field in ("title", "name") and type_names & _skip_title_types_weak:
            # only skip if value is short (site brand)
            pass
        field_emitted = False
        for candidate_path in candidate_paths:
            matches = list(_walk_path(entity, candidate_path))
            for raw_value, value_path in matches:
                if raw_value in (None, ""):
                    continue
                projected, projection_path = _project_scalar(field, raw_value)
                if not is_scalar(projected):
                    continue
                if field in ("title", "name") and type_names & _skip_title_types_weak:
                    if len(str(projected).strip()) < 20:
                        continue
                locator = _path_to_locator(
                    syntax=syntax,
                    item_index=item_index,
                    entity_path=entity_path,
                    value_path=value_path + projection_path,
                )
                assertions.append(
                    StructuredAssertion(
                        field=field,
                        value=projected,
                        raw_value=str(projected),
                        datatype=FIELD_DATATYPES.get(field, "string"),
                        evidence_locator=locator,
                        structured_syntax=syntax,  # type: ignore[arg-type]
                        source_url=source_url,
                        snapshot_hash=snapshot_hash,
                        retrieved_at=retrieved_at,
                        context=context,
                    )
                )
                field_emitted = True
            if field_emitted:
                break
    return assertions


def _map_extruct_items(
    syntax: str,
    items: Any,
    source_url: str,
    snapshot_hash: str,
    retrieved_at: datetime | None,
) -> list[StructuredAssertion]:
    assertions: list[StructuredAssertion] = []
    if not isinstance(items, list):
        items = [items]
    for item_index, item in enumerate(items):
        for entity, entity_path in _iter_entities(item):
            assertions.extend(
                _map_entity(
                    entity=entity,
                    syntax=syntax,
                    item_index=item_index,
                    entity_path=entity_path,
                    source_url=source_url,
                    snapshot_hash=snapshot_hash,
                    retrieved_at=retrieved_at,
                )
            )
    return assertions


def extract_structured_data(
    html_text: str,
    base_url: str,
    content_hash: str,
    missing_fields: Iterable[str] = (),
    retrieved_at: datetime | None = None,
) -> list[StructuredAssertion]:
    assertions: list[StructuredAssertion] = []
    try:
        import extruct
    except ImportError:
        return assertions

    try:
        primary = extruct.extract(
            html_text,
            base_url=base_url,
            syntaxes=PRIMARY_SYNTAXES,
            uniform=True,
        )
    except Exception:
        primary = {}

    for syntax, items in (primary or {}).items():
        assertions.extend(
            _map_extruct_items(
                syntax=syntax,
                items=items,
                source_url=base_url,
                snapshot_hash=content_hash,
                retrieved_at=retrieved_at,
            )
        )

    if should_run_secondary(html_text, missing_fields):
        try:
            secondary = extruct.extract(
                html_text,
                base_url=base_url,
                syntaxes=SECONDARY_SYNTAXES,
                uniform=True,
            )
        except Exception:
            secondary = {}
        for syntax, items in (secondary or {}).items():
            assertions.extend(
                _map_extruct_items(
                    syntax=syntax,
                    items=items,
                    source_url=base_url,
                    snapshot_hash=content_hash,
                    retrieved_at=retrieved_at,
                )
            )
    return assertions


def manual_og_fallback(
    html_text: str,
    source_url: str,
    snapshot_hash: str,
    retrieved_at: datetime | None = None,
) -> list[StructuredAssertion]:
    tree = HTMLParser(html_text)
    assertions: list[StructuredAssertion] = []
    for property_name, (field, datatype) in MANUAL_OG_MAP.items():
        selector = f'meta[property="{property_name}"]'
        node = tree.css_first(selector)
        if node is None:
            continue
        raw_value = (node.attributes.get("content") or "").strip()
        if not raw_value:
            continue
        assertions.append(
            StructuredAssertion(
                field=field,
                value=raw_value,
                raw_value=raw_value,
                datatype=datatype,
                evidence_locator=f"og_manual:{selector}",
                structured_syntax="og_manual",
                source_url=source_url,
                snapshot_hash=snapshot_hash,
                retrieved_at=retrieved_at,
                context={"property": property_name},
            )
        )
    return assertions


def _canonical_value(field: str, value: Any) -> str:
    text = str(value).strip().casefold()
    if field == "price":
        text = text.replace("\u00a0", "").replace(" ", "").replace(",", "")
    if field == "availability":
        text = text.removeprefix("https://schema.org/").removeprefix("http://schema.org/")
    return text


def append_missing_og_fallback(
    assertions: list[StructuredAssertion],
    fallback: list[StructuredAssertion],
    normalizer,
) -> list[StructuredAssertion]:
    existing: set[tuple[str, str]] = set()
    for assertion in assertions:
        normalized = normalizer(assertion.field, assertion.value)
        if normalized in (None, ""):
            normalized = assertion.value
        existing.add((assertion.field, _canonical_value(assertion.field, normalized)))

    for assertion in fallback:
        normalized = normalizer(assertion.field, assertion.value)
        if normalized in (None, ""):
            normalized = assertion.value
        key = (assertion.field, _canonical_value(assertion.field, normalized))
        if key not in existing:
            assertions.append(assertion)
            existing.add(key)
    return assertions


def validate_datatype(value: Any, datatype: str) -> bool:
    if datatype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if datatype == "boolean":
        return isinstance(value, bool)
    if datatype in {"string", "date"}:
        return isinstance(value, str) and bool(value.strip())
    return True


def adapt_to_facts_and_evidence(
    assertions: list[StructuredAssertion],
    *,
    retrieved_at: datetime,
    locale: str,
    timezone_name: str,
    auth_context_id: str | None,
    schema_fields: list[str] | None = None,
) -> tuple[list[Fact], list[Evidence]]:
    """Convert StructuredAssertion → Fact/Evidence matching project models."""
    facts: list[Fact] = []
    evidence: list[Evidence] = []
    allowed = set(schema_fields) if schema_fields else None

    for assertion in assertions:
        if allowed is not None and assertion.field not in allowed:
            # still allow title/name alias pair
            if not (
                assertion.field in ("title", "name")
                and allowed.intersection({"title", "name"})
            ):
                continue

        value = normalize_jsonld_value(assertion.field, assertion.value)
        if value in (None, ""):
            continue

        # Soft datatype: accept string prices that normalizer kept as str if number expected
        dt = assertion.datatype
        if dt == "number" and isinstance(value, str):
            try:
                value = float(value) if "." in value else int(value)
            except ValueError:
                pass
        if not validate_datatype(value, dt):
            # keep string fields even if datatype hint was wrong
            if isinstance(value, str) and value.strip():
                dt = "string"
            else:
                continue

        source_id = (
            f"src_struct_{assertion.structured_syntax}_{assertion.snapshot_hash[:12]}"
        )
        conf = _CONFIDENCE.get(assertion.structured_syntax, 0.86)

        ev = Evidence(
            source_id=source_id,
            source_url=assertion.source_url,
            evidence_text=assertion.raw_value[:200],
            selector=assertion.evidence_locator,
            content_hash=assertion.snapshot_hash,
            content_signature=assertion.snapshot_hash[:16],
            locale=locale,
            timezone=timezone_name,
            auth_context_id=auth_context_id,
            retrieved_at=retrieved_at,
            extraction_backend=f"L1_{assertion.structured_syntax}",
        )
        facts.append(
            Fact(
                field=assertion.field,
                value=value,
                datatype=dt,
                confidence=conf,
                source_id=source_id,
                extraction_method=assertion.structured_syntax,
                validation_status="valid",
                evidence_ids=[ev.evidence_id],
                conflict=Conflict(),
            )
        )
        evidence.append(ev)

    return facts, evidence


def publisher_author_fallback(
    html_text: str,
    source_url: str,
    snapshot_hash: str,
    retrieved_at: datetime | None = None,
) -> list[StructuredAssertion]:
    """When NewsArticle has empty author[], use publisher.name (e.g. Nature editorials)."""
    assertions: list[StructuredAssertion] = []
    try:
        import extruct
        from w3lib.html import get_base_url
        base = get_base_url(html_text, source_url)
        data = extruct.extract(html_text, base_url=base, syntaxes=["json-ld"], uniform=True)
    except Exception:
        return assertions

    items = data.get("json-ld") or []
    if not isinstance(items, list):
        items = [items]

    for item_index, item in enumerate(items):
        for entity, entity_path in _iter_entities(item):
            t = entity.get("@type") or entity.get("type") or ""
            type_names = (
                {t.lower()} if isinstance(t, str)
                else {str(x).lower() for x in t} if isinstance(t, list)
                else set()
            )
            if not any("article" in x for x in type_names):
                continue
            author = entity.get("author")
            # empty list or missing → try publisher
            if author not in (None, [], "", {}):
                if isinstance(author, list) and len(author) == 0:
                    pass
                else:
                    continue
            pub = entity.get("publisher")
            pub_name = None
            if isinstance(pub, dict):
                pub_name = pub.get("name")
            elif isinstance(pub, str):
                pub_name = pub
            if not pub_name or not str(pub_name).strip():
                continue
            # path for evidence
            locator = _path_to_locator(
                "json-ld", item_index, entity_path, ["publisher", "name"],
            )
            assertions.append(
                StructuredAssertion(
                    field="author",
                    value=str(pub_name).strip(),
                    raw_value=str(pub_name).strip(),
                    datatype="string",
                    evidence_locator=locator + "#publisher_fallback",
                    structured_syntax="json-ld",
                    source_url=source_url,
                    snapshot_hash=snapshot_hash,
                    retrieved_at=retrieved_at,
                    context={"type": t, "fallback": "publisher"},
                )
            )
    return assertions
