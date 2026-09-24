# Evidence-First Adaptive Web Runtime

[![CI](https://github.com/nanocubit/evidence-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/nanocubit/evidence-runtime/actions/workflows/ci.yml)

**v0.4.0 — L1 + L3 escalate** · cost-aware, evidence-grounded HTTP extraction.

Every extracted fact ships with provenance (selector, content hash, backend, confidence).
Conflicts between JSON-LD / Meta / Trafilatura / CSS are resolved deterministically.
Telemetry lives in DuckDB for router training and audit.

## Benchmarks

### How to read these numbers (honest framing)

Completeness is scored **only on required fields**, and a run can be `failed`
while still scoring 100% completeness (e.g. a page flagged `bot_wall` whose
fields were extracted anyway). Read the headline together with:

* `usable_rate` — share of runs that are `success`/`partial`, regardless of completeness;
* `completeness_by_status` — completeness split per status, so failures cannot inflate it;
* `failed_but_complete` — URLs that scored 100% while marked failed (must be explained, not hidden).

For precision, the legacy number counts only *expected* fields. `evaluate_gt.py`
now also prints a **HONESTY BLOCK**: strict (`equals`/`in`/`range`) vs soft
(`contains`/`min_len`) matches, extra fields returned that ground truth never
asked for, and `effective_precision` that counts those extras as false positives.

| Suite | n | Metric | Result |
|-------|---|--------|--------|
| Completeness `dataset_v2` | 40 URLs | overall completeness | **100.0%** (see honesty block) |
| Ground truth `dataset_gt_v2` | **60 URLs** | precision / recall / F1 | **100% / 98.6% / 99.3%** |
| product (required fields) | 8 | completeness | **100%** |
| reference | 8 | completeness | **100%** |
| spa (required fields) | 8 | completeness | **100%** |

### Baseline: L1 vs library-only (v0.4.1)

First published comparison against the obvious alternative (plain `httpx` +
`trafilatura` metadata) on the same URLs and required fields — 6-URL sample:

| Arm | mean completeness | mean latency |
|-----|-------------------|--------------|
| evidence-runtime L1 | **100.0%** | 5045 ms |
| trafilatura only | 30.5% | **1412 ms** |

The honest reading: L1 buys ~+70 pp completeness for ~3.6× the latency.
Cost-per-fact is still not measured — that remains open work.

```bash
PYTHONPATH=. python scripts/baseline_compare.py --limit 6
```

```bash
# Completeness (+ usable_rate, completeness_by_status, failed_but_complete)
PYTHONPATH=. python scripts/benchmark_dataset.py \
  --dataset datasets/dataset_v2.json --output benchmark_v21.json

# Precision / recall (+ strict/soft split, extra fields, effective precision)
PYTHONPATH=. python scripts/evaluate_gt.py \
  --dataset datasets/dataset_gt_v2.json --output benchmark_gt2.json

# Baseline: L1 vs trafilatura-only
PYTHONPATH=. python scripts/baseline_compare.py --limit 6
```

### Run as a service + MCP (v0.4.1)

```bash
# HTTP service (FastAPI) — schema extraction with provenance
python -m uvicorn evidence_runtime.api:app --host 127.0.0.1 --port 8090
curl -X POST http://127.0.0.1:8090/extract \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://docs.python.org/3/library/asyncio.html","schema":{"fields":{"title":{"type":"string"},"main_text":{"type":"string"}}}}'

# MCP (stdio) — extract_page / extract_health, for agent buses
ER_SERVICE_URL=http://127.0.0.1:8090 python mcp_server.py
```

Headline economics, computed from telemetry instead of asserted:

```bash
python scripts/avoidance_report.py --output reports/avoidance_report.json
# browser_avoidance_rate, llm_avoidance_rate, provenance_coverage, latency p50/p95
```

### Position in a chain

```
search layer (find URLs) → evidence-runtime L1 (extract + provenance) → L3 (render) → browser (interact)
```

The point of the first arrow: a plain text fetch returns prose, this runtime returns
facts with selector / backend / content hash, and reports how often the browser and the
LLM were avoided. See `reports/avoidance_report.json` for the current numbers.

### Trust primitives (0.4.1)

Three mechanisms make the provenance claim auditable rather than rhetorical:

```bash
# 1. tamper-evident chain over every run (fails closed)
python scripts/verify_chain.py

# 2. schemas + allowlist from a versioned, trusted file (not from the page)
cat policies/trusted_policy.json
ER_REQUIRE_TRUSTED=1 python -m uvicorn evidence_runtime.api:app --port 8090

# 3. the attack register we claim to stop
python -m pytest tests/test_redteam_fetch.py -q   # 24 cases
cat docs/REDTEAM.md
```

Every entry also records the caller context (`X-ER-Context`: caller, tool, policy hash),
so a run says *who asked and under which policy*, not just what URL was read.

## Architecture

```
L0  Cache (content_signature)           ✅
L1  HTTP + deterministic extraction     ✅  ← v0.3 freeze
L2  HTTP + deep DOM / LLM fill          ☐ planned (only on missing_fields)
L3  Lightweight browser (Playwright)    ✅ escalate after L1 (optional dep)
L4  Full Chromium + interaction           ☐ planned
```

**L1 pipeline**

```
robots + fetch (httpx, SSRF-safe, ttfb)
  → signature (headers, frameworks, spa_score, bot_wall)
  → JSON-LD → embedded state → Meta/OG → Trafilatura
  → CSS + microdata → price_regex → URL heuristics
  → aliases + conflict resolve (title quality)
  → RunStore (DuckDB) + metrics
```

## Schema: required vs optional

Completeness is scored **only on required fields**. Mark known L1 ceilings as optional:

```json
{
  "fields": {
    "title": { "type": "string" },
    "description": { "type": "string" },
    "version": { "type": "string", "optional": true },
    "author": { "type": "string", "optional": true }
  }
}
```

Use `optional: true` when the field is often absent from first HTML
(docs without version segment, blog hubs without a single author, HN without description).

## Quick start

```bash
pip install -r requirements.txt
# or: pip install -e ".[dev]"

PYTHONPATH=. python scripts/example.py

# CLI
PYTHONPATH=. python -m evidence_runtime.cli extract URL --schema examples/product_schema.json

# API
uvicorn evidence_runtime.api:app --reload
```

## Project layout

```
evidence_runtime/     # package
  extract.py          # L1 multi-method + conflict
  embedded.py         # __NEXT_DATA__ / Shopify / JSON blobs
  signature.py        # headers, scripts, spa_score
  fetcher.py          # HTTP + ttfb + robots
  tactics.py          # tactic profiles
  service.py          # orchestration
  router.py           # AdaptiveRouter (wired into service; records fallbacks)
  evalgt.py           # ground-truth matcher + honest aggregation (tested)
datasets/
  dataset_v2.json     # completeness (40 live)
  dataset_gt.json     # precision GT
scripts/
  benchmark_dataset.py
  evaluate_gt.py
tests/
VERSION               # 0.4.0
CHANGELOG.md
```

## Design principles

1. **Evidence first** — no fact without selector, hash, backend.
2. **Deterministic before probabilistic** — exhaust L1 before LLM/browser.
3. **Conflict is data** — losers retained in `Conflict.values`.
4. **Cost awareness** — track browser/LLM avoidance.
5. **Fail closed** — SSRF, bad MIME, bot_wall → explicit failure, not fake facts.

## Roadmap after v0.4

1. Expand ground truth to 50–100 URLs (precision confidence).
2. L2: LLM fill **only** for `missing_fields` after L1.
3. L3: Playwright for `bot_wall` / `needs_browser` product pages.


## L2 LLM fill (optional)

```bash
pip install openai   # or: pip install -e ".[llm]"
export LLM_FILL_ENABLED=1
export OPENAI_API_KEY=...
# optional: LLM_FILL_MODEL=gpt-4o-mini
```

Runs **after L1**, only for `missing_fields`, confidence 0.55, method `llm_fill`.
Skipped on `bot_wall`. L3 still handles browser escalate after L2.