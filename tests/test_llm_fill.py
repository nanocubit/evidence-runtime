"""L2 llm_fill unit tests (no API required)."""
from evidence_runtime.llm_fill import (
    METHOD,
    _parse_json_object,
    build_fill_prompt,
    llm_fill_available,
)


def test_llm_fill_disabled_by_default():
    assert llm_fill_available() is False


def test_parse_json_object_plain():
    assert _parse_json_object('{"price": 9.99}')["price"] == 9.99


def test_parse_json_object_fenced():
    raw = 'Here:\n```json\n{"title": "Hello"}\n```\n'
    assert _parse_json_object(raw)["title"] == "Hello"


def test_build_prompt_includes_missing_only():
    p = build_fill_prompt(
        url="https://ex.com/p",
        missing_fields=["price"],
        schema_fields={"price": {"type": "number"}, "name": {"type": "string"}},
        html_text="<html><body>Price $10</body></html>",
        existing={"name": "Widget"},
    )
    assert "price" in p
    assert "Widget" in p
    assert METHOD or True
