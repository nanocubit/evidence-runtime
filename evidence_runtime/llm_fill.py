"""L2 — LLM fill for missing schema fields only (optional dependency).

Design:
  - Never invent fields that L1 already filled.
  - Output must attach Evidence with method=llm_fill and lower confidence.
  - Disabled unless LLM_FILL_ENABLED and an API key / client is configured.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from .models import Evidence, Fact

logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE = 0.55
METHOD = "llm_fill"


def llm_fill_available() -> bool:
    if os.environ.get("LLM_FILL_ENABLED", "").lower() not in ("1", "true", "yes"):
        return False
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
        return True
    return False


def _truncate_html(html: str, max_chars: int = 12000) -> str:
    """Keep head + a middle slice; enough for titles/prices, cheap on tokens."""
    if len(html) <= max_chars:
        return html
    head = html[: max_chars // 2]
    tail = html[-max_chars // 2 :]
    return head + "\n<!-- …truncated… -->\n" + tail


def build_fill_prompt(
    *,
    url: str,
    missing_fields: list[str],
    schema_fields: dict[str, Any],
    html_text: str,
    existing: dict[str, Any] | None = None,
) -> str:
    existing = existing or {}
    field_hints = {k: schema_fields.get(k, {}) for k in missing_fields}
    return (
        "Extract ONLY the following missing fields from the HTML snippet.\n"
        "Return a single JSON object mapping field name → scalar value.\n"
        "If a field is not clearly present, omit it (do not guess).\n"
        f"URL: {url}\n"
        f"Missing fields: {json.dumps(missing_fields)}\n"
        f"Field hints: {json.dumps(field_hints, ensure_ascii=False)}\n"
        f"Already extracted (do not repeat): {json.dumps(existing, ensure_ascii=False)}\n"
        "HTML:\n"
        f"{_truncate_html(html_text)}\n"
    )


def _parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    # fenced block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            text = m.group(0)
    data = json.loads(text)
    if not isinstance(data, dict):
        return {}
    return data


async def call_llm(prompt: str) -> str:
    """Provider-agnostic thin client. Prefer OpenAI-compatible, else Anthropic."""
    if os.environ.get("OPENAI_API_KEY"):
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise RuntimeError("openai package required for L2: pip install openai") from e
        client = AsyncOpenAI()
        model = os.environ.get("LLM_FILL_MODEL", "gpt-4o-mini")
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You extract structured fields. Reply with JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or "{}"

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError("anthropic package required for L2") from e
        client = anthropic.AsyncAnthropic()
        model = os.environ.get("LLM_FILL_MODEL", "claude-3-5-haiku-latest")
        msg = await client.messages.create(
            model=model,
            max_tokens=1024,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text if msg.content else "{}"

    raise RuntimeError("No LLM API key configured")


async def fill_missing_fields(
    *,
    url: str,
    html_text: str,
    missing_fields: list[str],
    schema: dict[str, Any],
    existing_facts: list[Fact],
    content_hash: str,
    locale: str = "en-US",
    timezone_name: str = "UTC",
    auth_context_id: str | None = None,
) -> tuple[list[Fact], list[Evidence]]:
    """Return Fact/Evidence only for fields still missing. Empty if disabled or no missings."""
    if not missing_fields or not llm_fill_available():
        return [], []

    fields_spec = schema.get("fields", schema) if isinstance(schema, dict) else {}
    existing = {f.field: f.value for f in existing_facts}
    # only request truly missing
    miss = [m for m in missing_fields if m not in existing]
    if not miss:
        return [], []

    prompt = build_fill_prompt(
        url=url,
        missing_fields=miss,
        schema_fields=fields_spec if isinstance(fields_spec, dict) else {},
        html_text=html_text,
        existing=existing,
    )

    try:
        raw = await call_llm(prompt)
        data = _parse_json_object(raw)
    except Exception as e:
        logger.warning("llm_fill failed: %s", e)
        return [], []

    from datetime import datetime
    from datetime import timezone as tz

    from .extract import _make_evidence, _make_fact

    now = datetime.now(tz.utc)
    facts: list[Fact] = []
    evidence: list[Evidence] = []
    source_id = "src_llm_" + content_hash[:12]

    for field, value in data.items():
        if field not in miss:
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        # normalize trivial types
        if isinstance(value, (dict, list)):
            continue
        ev = _make_evidence(
            source_id=source_id,
            url=url,
            text=str(value)[:200],
            selector="llm_fill",
            content_hash=content_hash,
            locale=locale,
            timezone_name=timezone_name,
            auth_context_id=auth_context_id,
            backend="L2_llm",
            now=now,
        )
        fact = _make_fact(
            field=field,
            value=value,
            datatype="number" if isinstance(value, (int, float)) else "string",
            confidence=DEFAULT_CONFIDENCE,
            source_id=source_id,
            method=METHOD,
            evidence=ev,
        )
        facts.append(fact)
        evidence.append(ev)

    return facts, evidence
