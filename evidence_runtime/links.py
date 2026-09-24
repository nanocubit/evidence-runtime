"""RFC-8288 Link header + HTML <link rel> extraction."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from .models import LinkRelation

_PARAM_NAME_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_TOKEN_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


def _split_link_values(header: str) -> list[str]:
    """Split Link header on commas not inside <URI> or quotes."""
    parts: list[str] = []
    start = 0
    angle_depth = 0
    quoted = False
    escaped = False

    for index, char in enumerate(header):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue

        if char == '"':
            quoted = True
        elif char == "<":
            angle_depth += 1
        elif char == ">":
            angle_depth = max(0, angle_depth - 1)
        elif char == "," and angle_depth == 0:
            value = header[start:index].strip()
            if value:
                parts.append(value)
            start = index + 1

    tail = header[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _read_quoted(value: str, index: int) -> tuple[str, int]:
    assert value[index] == '"'
    index += 1
    chars: list[str] = []
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            chars.append(value[index + 1])
            index += 2
            continue
        if char == '"':
            return "".join(chars), index + 1
        chars.append(char)
        index += 1
    return "".join(chars), index


def _parse_parameters(parameters: str) -> dict[str, str]:
    result: dict[str, str] = {}
    index = 0
    while index < len(parameters):
        while index < len(parameters) and parameters[index].isspace():
            index += 1
        if index >= len(parameters):
            break
        if parameters[index] != ";":
            index += 1
            continue
        index += 1
        while index < len(parameters) and parameters[index].isspace():
            index += 1
        name_match = _PARAM_NAME_RE.match(parameters, index)
        if not name_match:
            break
        name = name_match.group(0).lower()
        index = name_match.end()
        while index < len(parameters) and parameters[index].isspace():
            index += 1
        if index >= len(parameters) or parameters[index] != "=":
            result[name] = ""
            continue
        index += 1
        while index < len(parameters) and parameters[index].isspace():
            index += 1
        if index < len(parameters) and parameters[index] == '"':
            value, index = _read_quoted(parameters, index)
        else:
            start = index
            while index < len(parameters) and parameters[index] not in ";,":
                index += 1
            value = parameters[start:index].strip()
        result[name] = value
    return result


def parse_link_header(header_value: str, base_url: str) -> list[LinkRelation]:
    relations: list[LinkRelation] = []
    for raw_value in _split_link_values(header_value):
        if not raw_value.startswith("<"):
            continue
        closing = raw_value.find(">")
        if closing < 0:
            continue
        target = raw_value[1:closing].strip()
        if not target:
            continue
        params = _parse_parameters(raw_value[closing + 1 :])
        rel = [item.lower() for item in _TOKEN_RE.findall(params.get("rel", ""))]
        if not rel:
            continue
        relations.append(
            LinkRelation(
                url=urljoin(base_url, target),
                rel=rel,
                type=params.get("type") or None,
                hreflang=params.get("hreflang") or None,
                media=params.get("media") or None,
                source="http_header",
                raw_value=raw_value,
            )
        )
    return relations


def extract_html_links(raw_tree, base_url: str) -> list[LinkRelation]:
    relations: list[LinkRelation] = []
    for node in raw_tree.css("link[rel]"):
        href = node.attributes.get("href")
        rel_value = node.attributes.get("rel", "")
        if not href or not rel_value:
            continue
        rel = [item.lower() for item in _TOKEN_RE.findall(rel_value)]
        if not rel:
            continue
        relations.append(
            LinkRelation(
                url=urljoin(base_url, href.strip()),
                rel=rel,
                type=node.attributes.get("type"),
                hreflang=node.attributes.get("hreflang"),
                media=node.attributes.get("media"),
                source="html",
                raw_value=node.html or "",
            )
        )
    return relations


def extract_all_links(
    raw_tree,
    response_headers: dict[str, str] | None,
    base_url: str,
) -> list[LinkRelation]:
    result = extract_html_links(raw_tree, base_url)
    headers = response_headers or {}
    link_header = next(
        (value for key, value in headers.items() if key.lower() == "link"),
        "",
    )
    if link_header:
        result.extend(parse_link_header(link_header, base_url))
    return result


def primary_canonical(links: list[LinkRelation]) -> str | None:
    for link in links:
        if "canonical" in link.rel:
            return link.url
    return None
