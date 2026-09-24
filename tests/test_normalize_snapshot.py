"""Regression: snapshotting must survive valueless attributes.

selectolax parses `<a href>` as {'href': None}; `dict.get("href", "")` still
returns None for a present-but-valueless key, so any `.strip()` on it crashed
`normalize_html` — and because snapshotting runs by default in the service,
every such page was reported as `failed`.
"""

from __future__ import annotations

from evidence_runtime.normalize import normalize_html

HTML = """
<html><head><title>T</title><link rel="canonical" href="https://example.com/x"></head>
<body>
  <a href>valueless anchor</a>
  <a href="https://example.com/a">real</a>
  <a>no href at all</a>
  <meta name="description" content="d">
</body></html>
"""


def test_normalize_html_survives_valueless_href():
    snap = normalize_html(HTML, "https://example.com/x")
    assert snap["title"] == "T"
    hrefs = [link["href"] for link in snap["links"]]
    assert hrefs == ["https://example.com/a"]  # valueless + href-less dropped, no crash


def test_normalize_html_handles_empty_document():
    snap = normalize_html("<html></html>", "https://example.com/")
    assert snap["links"] == []
    assert snap["snapshot_hash"].startswith("sha256:")
