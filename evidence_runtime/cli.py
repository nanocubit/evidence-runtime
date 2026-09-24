import json

import typer
from rich import print_json

from .models import ExtractionRequest
from .service import extract

app = typer.Typer()


@app.command("extract")
def extract_cmd(
    url: str,
    schema: str,
    db: str = "runtime.duckdb",
    mode: str = "auto",
    locale: str = "en-US",
):
    """Run Phase 0+ HTTP-first deterministic extraction."""
    with open(schema, encoding="utf-8") as f:
        schema_data = json.load(f)
    req = ExtractionRequest(url=url, schema=schema_data, mode=mode, locale=locale)
    result = extract(req, db)
    print_json(data=result.model_dump(mode="json"))


@app.command("query")
def query_cmd(url: str, db: str = "runtime.duckdb", limit: int = 5):
    """Query previous extraction runs for a URL."""
    from .storage import RunStore
    store = RunStore(db_path=db)
    rows = store.query_by_url(url, limit=limit)
    for row in rows:
        print_json(data=row)
