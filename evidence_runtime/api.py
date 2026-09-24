from fastapi import FastAPI

from .models import ExtractionRequest, ExtractionRun
from .service import extract_async

app = FastAPI(title="Evidence Runtime", version="0.2.0")


@app.post("/extract", response_model=ExtractionRun)
async def extract_endpoint(req: ExtractionRequest):
    return await extract_async(req)
