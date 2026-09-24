from __future__ import annotations

import hashlib
from typing import Any

from selectolax.parser import HTMLParser


def normalize_html(html_text: str, url: str) -> dict[str, Any]:
    """Produce a normalized snapshot with semantic sections and metadata."""
    tree = HTMLParser(html_text)
    snapshot = {
        "url": url,
        "title": _extract_title(tree),
        "language": _extract_lang(tree),
        "canonical": _extract_canonical(tree),
        "semantic_sections": _extract_sections(tree),
        "links": _extract_links(tree, url),
        "meta_tags": _extract_meta(tree),
        "structured_data_candidates": [],  # populated by extruct later
    }
    snapshot["snapshot_hash"] = "sha256:" + hashlib.sha256(
        html_text.encode("utf-8")
    ).hexdigest()
    return snapshot


def _extract_title(tree: HTMLParser) -> str | None:
    node = tree.css_first("title")
    return node.text(strip=True) if node else None


def _extract_lang(tree: HTMLParser) -> str | None:
    html = tree.css_first("html")
    return html.attributes.get("lang") if html else None


def _extract_canonical(tree: HTMLParser) -> str | None:
    node = tree.css_first('link[rel="canonical"]')
    return node.attributes.get("href") if node else None


def _extract_sections(tree: HTMLParser) -> list[dict[str, Any]]:
    sections = []
    for tag in ("header", "main", "article", "section", "aside", "footer"):
        for node in tree.css(tag):
            text = node.text(separator=" ", strip=True)
            if text:
                sections.append({
                    "tag": tag,
                    "text_preview": text[:500],
                    "id": node.attributes.get("id"),
                    "class": node.attributes.get("class"),
                })
    return sections


def _extract_links(tree: HTMLParser, base_url: str) -> list[dict[str, str]]:
    links = []
    for node in tree.css("a[href]"):
        href = node.attributes.get("href", "").strip()
        if href.startswith("http"):
            links.append({"href": href, "text": node.text(strip=True) or ""})
    return links[:200]  # cap to avoid huge snapshots


def _extract_meta(tree: HTMLParser) -> dict[str, str]:
    meta = {}
    for node in tree.css("meta"):
        name = node.attributes.get("name") or node.attributes.get("property")
        content = node.attributes.get("content")
        if name and content:
            meta[name] = content
    return meta
