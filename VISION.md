# Vision: making Evidence Runtime stronger

## What is already good

- Clean separation of fetch / extract / store / metrics
- Evidence objects attached to every fact
- Multi-method extraction with explicit priority
- Signature-based SPA detection
- DuckDB telemetry ready for router training
- 50-URL evaluation dataset with class labels (article/product/docs/reference/spa)

## Done since this vision was written (0.4.1)

- **§2 Adaptive router** — `AdaptiveRouter` is wired into `service.extract_async`; every
  escalation records a fallback reason (JSONL training set via `EVIDENCE_ROUTER_TELEMETRY`).
- **§1/§8 Honest measurement** — strict/soft matcher split, extra-field accounting and
  `effective_precision`; reports expose `usable_rate`, `completeness_by_status`,
  `failed_but_complete`.
- **Published baseline** — `scripts/baseline_compare.py` measures L1 against library-only
  extraction (completeness + latency) instead of asserting the gap.
- **Runnable service + MCP** — the runtime runs as an HTTP service (`/extract`) and is
  exposed on the MCP bus as `extract_page` / `extract_health`, so it can sit between a
  search layer and a browser in one agent plan.
- **Headline economics** — `browser_avoidance_rate` / `llm_avoidance_rate` / `provenance_coverage`
  computed from telemetry (`scripts/avoidance_report.py`).
- **Trust primitives (borrowed from Pearl Necklace, applied to reading)** —
  hash-chained provenance with fail-closed verification (`chain.py`), schemas/allowlist from a
  versioned trusted file (`trusted.py` + `policies/trusted_policy.json`), per-call context
  binding (`X-ER-Context`), and a published red-team register (`docs/REDTEAM.md`, 24 cases).
  Also fixed a real **SSRF-via-redirect** hole found by that suite.

Still open: ground-truth *scale*, L2 gap-fill tuning, dynamic confidence (§4), domain
profiles (§6), ML router (§2 Phase 1), cost-per-fact.

## Where to push further

### 1. Ground-truth loop (highest leverage)

You already have the tools:

```
dataset.json
  → auto_ground_truth.py   (cross-source consensus)
  → verify_cli.py          (human-in-the-loop)
  → evaluate.py            (precision vs expected_values)
```

**Next step:** run auto-GT on the 40 non-SPA items, spend 1–2 hours in `verify_cli`,
then measure real precision/recall by class. This turns the project from a scaffold
into a measured system.

### 2. Adaptive router that actually routes

Today `AdaptiveRouter` is unused. Wire it into `service.extract_async`:

```
predict(req) → L1_http
  if status in (unresolved, partial) and signature.likely_spa:
      fallback → L3_browser
  record_fallback(...)  # feeds future ML model
```

Store features per run: domain, signature flags, missing field count, latency.
Train a simple sklearn / XGBoost classifier on “which backend succeeded”.

### 3. L2 = structured LLM fill (not free-form)

For missing fields only, call an LLM with:

- the *already extracted* facts as context
- the specific CSS/JSON-LD candidates that almost matched
- a strict JSON schema (Instructor / outlines)

This keeps LLM cost proportional to *gaps*, not to every page.

### 4. Evidence validity scoring

Today confidence is static per method. Make it dynamic:

```
confidence = method_prior
           * selector_specificity
           * cross_source_agreement
           * value_plausibility (price > 0, currency ISO, date parseable)
```

### 5. Cache layer (L0)

```
key = (normalized_url, schema_hash, content_signature)
```

If content_signature matches a previous run and `freshness_seconds` allows → return cached facts.
DuckDB already stores everything needed.

### 6. Domain profiles

After enough telemetry, learn per-domain:

- preferred selectors for `price` / `author`
- “this site is always SPA → start at L3”
- currency defaults (ozon.ru → RUB)

Store in a `domain_profiles` table.

### 7. Safer defaults

- Real robots.txt cache (respect_robots=True becomes meaningful)
- Redirect host pinning (block redirect to private IP)
- Optional request signing / API key for the FastAPI endpoint

### 8. Evaluation that teaches

Extend `evaluate.py`:

- per-field confusion (title vs name mix-ups)
- latency vs completeness Pareto curve
- “which method would have won if we flipped priority?”

This feeds directly into METHOD_PRIORITY tuning.

## Suggested milestone order

| Milestone | Outcome |
|-----------|---------|
| M1 | P0 bugs fixed (done in 0.3) |
| M2 | Ground truth for 30+ pages, baseline precision report |
| M3 | L0 cache + router wired to L1 |
| M4 | L2 LLM gap-fill with Instructor |
| M5 | L3 Playwright for SPA class |
| M6 | Domain profiles + ML router |

## Why this is interesting research

Most scrapers optimize for “get the data”. This runtime optimizes for:

1. **provable provenance** (evidence chain)
2. **minimal cost** (browser/LLM avoidance rates as first-class metrics)
3. **measurable quality** (ground-truth loop built in)

That combination is rare and publishable.

## Presentation (design freeze)

See `docs/PRESENTATION_LAYER.md`.

Evidence Runtime does not render UI. A future Presentation Compiler turns
Fact/Evidence into EditorialCard → node tree → Takumi (or equivalent) pixels.
Not in scope for v0.3.x; blocked on recorded L1 baseline after PR-1.
