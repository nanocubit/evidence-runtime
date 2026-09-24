#!/usr/bin/env python3
"""Evidence Runtime MCP server (stdio, newline-delimited JSON-RPC 2.0).

Exposes the extraction runtime as MCP tools so agents can call it from the bus:

    extract_page   — extract schema fields (with provenance) from a URL
    extract_health — service/repo availability

Transport: HTTP to the local service (``ER_SERVICE_URL``, default
http://127.0.0.1:8090) so telemetry and cache stay in one writer. If the service
is unreachable the tool falls back to an in-process run with an ephemeral DB.

No third-party imports on purpose: this file must start even in a bare
interpreter.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

SERVICE_URL = os.environ.get("ER_SERVICE_URL", "http://127.0.0.1:8090").rstrip("/")
REPO_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_SCHEMA = {
    "fields": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "main_text": {"type": "string"},
        "author": {"type": "string", "optional": True},
        "publish_date": {"type": "string", "optional": True},
    }
}

TOOLS = [
    {
        "name": "extract_page",
        "description": (
            "Extract structured fields from a URL with provenance. Returns facts "
            "(field/value/method/confidence), evidence (selector, backend, content hash), "
            "the extraction level (L1_http / L3_browser) and warnings."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Page URL to extract"},
                "preset": {
                    "type": "string",
                    "description": "Schema preset name resolved from the trusted policy (e.g. article, docs, product). Preferred over an inline schema.",
                },
                "schema": {
                    "type": "object",
                    "description": "Optional {fields:{name:{type,optional}}} schema (ignored in strict policy mode)",
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "http", "browser"],
                    "description": "Extraction mode (default auto)",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "extract_health",
        "description": "Report whether the evidence-runtime service and repo are reachable.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _http_extract(url: str, schema: dict, mode: str, *, context: dict | None = None,
                  timeout: float = 90.0) -> dict:
    body = json.dumps({"url": url, "schema": schema, "mode": mode}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if context:
        # Context binding: who asked, under which policy — recorded in the chain.
        headers["X-ER-Context"] = json.dumps(context, ensure_ascii=False)[:2000]
    req = urllib.request.Request(SERVICE_URL + "/extract", data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _load_policy():
    """Trusted policy, or None when unavailable (never fatal on its own)."""
    try:
        if REPO_DIR not in sys.path:
            sys.path.insert(0, REPO_DIR)
        from evidence_runtime import trusted  # noqa: PLC0415

        return trusted.load()
    except Exception:  # noqa: BLE001
        return None


def _inprocess_extract(url: str, schema: dict, mode: str) -> dict:
    """Fallback: run the runtime in this process with an ephemeral DB."""
    if REPO_DIR not in sys.path:
        sys.path.insert(0, REPO_DIR)
    import asyncio

    from evidence_runtime.models import ExtractionRequest  # noqa: PLC0415
    from evidence_runtime.service import extract_async  # noqa: PLC0415

    req = ExtractionRequest(url=url, schema=schema, mode=mode)
    run = asyncio.run(extract_async(req, db_path=":memory:", save_snapshot=False))
    return json.loads(run.model_dump_json())


def call_tool(name: str, args: dict) -> tuple[dict, bool]:
    if name == "extract_health":
        try:
            with urllib.request.urlopen(SERVICE_URL + "/openapi.json", timeout=3) as r:
                ok = r.status == 200
        except Exception:
            ok = False
        return {"service_url": SERVICE_URL, "service_up": ok, "repo_dir": REPO_DIR}, not ok

    if name != "extract_page":
        return {"error": f"unknown tool: {name}"}, True

    url = str(args.get("url") or "").strip()
    if not url:
        return {"error": "url is required"}, True

    policy = _load_policy()
    preset = str(args.get("preset") or "").strip() or None
    schema = args.get("schema") if isinstance(args.get("schema"), dict) else DEFAULT_SCHEMA

    if preset and policy is not None:
        try:
            schema = policy.schema_for(preset)  # trusted source, never the caller
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}, True

    policy_info: dict = {}
    if policy is not None:
        policy_info = {
            "version": policy.version,
            "hash": policy.policy_hash(),
            "preset": preset,
            "strict": policy.strict,
        }
        if policy.strict:
            try:
                policy.check_url(url)
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc), "policy": policy_info}, True

    mode = args.get("mode") if args.get("mode") in ("auto", "http", "browser") else "auto"
    context = {
        "caller": "evidence-runtime-mcp",
        "preset": preset,
        "policy_hash": policy_info.get("hash", ""),
    }

    try:
        result = _http_extract(url, schema, mode, context=context)
        result["_via"] = "service"
        if policy_info:
            result["policy"] = policy_info
        return result, False
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        pass

    try:
        result = _inprocess_extract(url, schema, mode)
        result["_via"] = "in_process"
        return result, False
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}, True


def _write(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(request: dict) -> None:
    method = str(request.get("method") or "")
    rid = request.get("id")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}

    if method.startswith("notifications/"):
        return

    if method == "initialize":
        _write({
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": str(params.get("protocolVersion") or "2024-11-05"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "evidence-runtime", "version": "0.4.1"},
            },
        })
        return

    if method == "ping":
        _write({"jsonrpc": "2.0", "id": rid, "result": {}})
        return

    if method == "tools/list":
        _write({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        return

    if method == "tools/call":
        payload, is_error = call_tool(str(params.get("name") or ""), params.get("arguments") or {})
        _write({
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "isError": is_error,
            },
        })
        return

    _write({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        try:
            handle(request)
        except Exception as exc:  # noqa: BLE001
            _write({"jsonrpc": "2.0", "id": request.get("id"),
                    "error": {"code": -32603, "message": str(exc)}})


if __name__ == "__main__":
    main()
