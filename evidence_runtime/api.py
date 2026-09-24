import json

from fastapi import FastAPI, Request

from .models import ExtractionRequest, ExtractionRun
from .service import extract_async

app = FastAPI(title="Evidence Runtime", version="0.2.0")


@app.post("/extract", response_model=ExtractionRun)
async def extract_endpoint(req: ExtractionRequest, request: Request) -> ExtractionRun:
    """Extract schema fields from a URL.

    Caller identity travels in the optional `X-ER-Context` header (JSON) and is
    recorded in the provenance chain, so every entry says *who* asked — the
    context binding, not just the payload.
    """
    context: dict = {}
    raw = request.headers.get("x-er-context")
    if raw:
        try:
            decoded = json.loads(raw)
            context = decoded if isinstance(decoded, dict) else {"raw": str(decoded)[:200]}
        except json.JSONDecodeError:
            context = {"raw": raw[:200]}
    context.setdefault("caller", "http")

    return await extract_async(req, context=context)
