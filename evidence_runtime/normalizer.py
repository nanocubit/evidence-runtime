"""Normalize raw JSON-LD / meta / CSS values into typed Python objects."""

from __future__ import annotations

import re
from typing import Any

AVAILABILITY_MAP = {
    "https://schema.org/InStock": True,
    "http://schema.org/InStock": True,
    "InStock": True,
    "in_stock": True,
    "instock": True,
    "https://schema.org/OutOfStock": False,
    "http://schema.org/OutOfStock": False,
    "OutOfStock": False,
    "out_of_stock": False,
    "outofstock": False,
    "https://schema.org/PreOrder": "preorder",
    "http://schema.org/PreOrder": "preorder",
    "PreOrder": "preorder",
}


def normalize_text(text: str) -> str:
    """Collapse whitespace, strip, remove wrapping quotes."""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", str(text)).strip()
    if len(text) >= 2:
        if (text[0] == text[-1]) and text[0] in {'"', "'", "«", "»", "“", "”"}:
            text = text[1:-1].strip()
        elif text.startswith("«") and text.endswith("»"):
            text = text[1:-1].strip()
    return text


def normalize_jsonld_value(field: str, raw_value: Any) -> Any:
    if raw_value is None:
        return None

    if field == "availability":
        if isinstance(raw_value, str):
            key = raw_value.strip()
            return AVAILABILITY_MAP.get(key, AVAILABILITY_MAP.get(key.split("/")[-1], raw_value))
        if isinstance(raw_value, dict):
            if "@id" in raw_value:
                rid = raw_value["@id"]
                return AVAILABILITY_MAP.get(rid, AVAILABILITY_MAP.get(str(rid).split("/")[-1], rid))
            return AVAILABILITY_MAP.get(str(raw_value.get("name", "")), raw_value)
        return raw_value

    if field in ("price", "price_usd"):
        if isinstance(raw_value, (int, float)):
            return float(raw_value)
        if isinstance(raw_value, str):
            clean = "".join(ch for ch in raw_value if ch.isdigit() or ch in ".,-")
            if not clean or clean in {".", ",", "-", "-."}:
                return None
            if clean.count(",") == 1 and clean.rfind(",") > clean.rfind("."):
                clean = clean.replace(".", "").replace(",", ".")
            else:
                clean = clean.replace(",", "")
            try:
                return float(clean)
            except ValueError:
                return None
        return None

    if field == "currency":
        if isinstance(raw_value, str):
            s = raw_value.strip()
            # Symbol → ISO
            symbols = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₽": "RUB", "руб": "RUB", "руб.": "RUB"}
            if s in symbols:
                return symbols[s]
            return s[:3].upper() if len(s) >= 3 else s.upper()
        return None

    if field in ("name", "title", "brand", "description", "author", "category", "source"):
        return normalize_text(str(raw_value))

    if isinstance(raw_value, str):
        return normalize_text(raw_value)

    return raw_value
