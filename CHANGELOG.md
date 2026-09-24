# Changelog

## [0.4.1] — 2026-09-24

### Changed (metrics honesty)
- `scripts/evaluate_gt.py` now prints a **HONESTY BLOCK**: strict (`equals`/`in`/`range`) vs
  soft (`contains`/`min_len`) matcher split, extra fields returned that ground truth never
  asked for, and `effective_precision` counting those extras as false positives.
- `scripts/benchmark_dataset.py` adds `usable_rate`, `completeness_by_status` and
  `failed_but_complete` — a run can be `failed` and still score 100% completeness, and that
  is now visible instead of inflating the headline.
- Matcher + aggregation moved into `evidence_runtime/evalgt.py` so the measuring device is tested.

### Added
- **MCP server** (`mcp_server.py`) — stdio tools `extract_page` (schema fields +
  provenance) and `extract_health`; talks to the HTTP service so telemetry has a single
  writer, with an in-process fallback when the service is down.
- `scripts/avoidance_report.py` + `evidence_runtime/economy.py` — headline economics from
  telemetry: `browser_avoidance_rate`, `llm_avoidance_rate`, `provenance_coverage`,
  latency p50/p95 and recorded escalation reasons (auditable, not asserted).
- **AdaptiveRouter is wired** into `service.extract_async`: each run records
  `route:<backend>:<reason>`, escalation calls `record_fallback` (in-memory + optional JSONL
  training set via `EVIDENCE_ROUTER_TELEMETRY`), and `ExtractionRun.fallback_level` is set.
- `scripts/baseline_compare.py` — first published baseline against the library-only arm
  (plain httpx + trafilatura metadata, same URLs and required fields):
  **L1 100.0% vs 30.5% completeness, 5045 ms vs 1412 ms** on a 6-URL sample.
- Tests: `tests/test_router.py`, `tests/test_metrics_honesty.py` — suite now **51 passed / 3 skipped**.
- A/B harness results (14 URLs, ER vs naive HTTP) informed the reading policy now used by the
  search layer: **HTTP → ER → browser** by default, ER-first only for `evidence=true`
  (ER-first measured ~2.7x slower for the same coverage).

### Fixed
- **False bot-wall on script config.** `_looks_like_bot_wall` matched markers against the raw
  HTML head, so MediaWiki's `wgConfirmEditCaptchaNeededForGenericEdit` (a `<script>` config
  string) made **every Wikipedia page** look like a bot wall: the run was reported `failed`
  while still extracting ~190k chars. Markers are now matched against *visible* text only,
  and challenge titles match by substring (`Attention Required! | Cloudflare`). Coverage on
  the 14-URL A/B set went 9/14 → 10/14. Regression tests: `tests/test_bot_wall.py`.
- `normalize_html` crashed on a valueless `<a href>` (selectolax yields `{'href': None}`,
  and `dict.get("href", "")` still returns `None`) — because snapshotting is on by default
  in the service, **every such page was reported `failed`**. Found by the first HTTP-service
  call; fixed in `evidence_runtime/normalize.py`, regression test added.
- `ruff check .` is clean (4 lint errors in `scripts/verify_cli.py`).

## [Unreleased]

### CI/CD
- GitHub Actions: `.github/workflows/ci.yml` (ruff + pytest on 3.11/3.12)
- Release workflow on tags `v*`: build sdist/wheel (+ optional PyPI)
- Dependabot for pip + actions

## [0.4.0] — 2026-08-17

### Metrics (freeze)
- Completeness `dataset_v2` (40 URLs): **100.0%** field completeness (reachable 37/40; 3 reference fetch fails still schema-filled where measured)
- Ground truth `dataset_gt_v2` (60 URLs): **P 100.0% / R 98.6% / F1 99.3%** (tp=144 fp=0 fn=2)
- product / reference / spa / article / docs required fields: **100%** on reachable set

### Added since 0.3.x
- PR-1 structured layer (`structured.py`, RFC-8288 `links.py`, `@graph` / `mainEntity`, OG fallback)
- Structured vs legacy `json_ld` dedupe (conf ≥ 0.88)
- `publisher_author_fallback`, `docs_lead`, title quality / site-brand filter
- Dataset cleanup (SciAm → CSS-Tricks, k8s hub → release post)
- **L3 Playwright escalate** + `BrowserPool` (Chromium reuse, isolated contexts)
- `sig[bot_wall]` wiring; `l3_still_bot_wall` honesty
- Design freeze: `docs/PRESENTATION_LAYER.md` (not implemented)

### Reports
- `reports/v040_baseline.json`, `reports/v040_gt.json`

## [0.3.1] — 2026-08-10 — PR-1 structured layer (historical notes below)

## [0.3.1] — 2026-08-10 — PR-1 structured layer

### Added
- `structured.py` — path-aware JSON-LD / microdata / OG / RDFa (`@graph`, list offers, scalar projection)
- `links.py` — RFC-8288 Link header parser + HTML `link[rel]` (canonical)
- `StructuredAssertion`, `LinkRelation` models
- `retrieved_at` injected from `ExtractionRun` (no more `datetime.now()` inside extract path)
- Benchmark: `task_completeness` + `evidence_validity` helpers
- Tests: `tests/test_structured_pr1.py`

### Sample metrics (15 live URLs, post-PR-1)
- field completeness: **100%**
- task completeness: **100%**
- structured methods visible: `json-ld`, `og_manual` alongside legacy `json_ld`/`meta_og`

### Notes
- Legacy JSON-LD path still runs for fields structured misses (e.g. sku) — conflict resolver merges
- Next: optional de-dupe of legacy json_ld when structured already high-confidence; full dataset baseline

## [0.3.0] — 2026-08-08 — L1 freeze

### Metrics (dataset v2.1, 40 live URLs)
- Overall completeness: **98.1%**
- Ground-truth (12 URL soft matchers): Precision **100%**, Recall **100%**, F1 **100%**
- product / reference / spa required fields: **100%**
- Zero fetch failures on curated set

### L1 features
- Multi-method extract: JSON-LD, Meta/OG, Trafilatura, CSS, microdata, price_regex, URL heuristics
- Embedded SPA state: `__NEXT_DATA__`, `__NUXT__`, Shopify meta, `application/json` blobs
- Tactic profiles (product / article / docs / reference / spa)
- Signature: headers (CDN, ETag, Age, A/B cookies), script fingerprint, spa_score, trackers
- robots.txt + crawl-delay, SSRF/MIME policy
- L0 content_signature cache
- Conflict resolution with title quality scoring
- Bot-wall detection → `failed` / `bot_wall`
- Wikipedia lead paragraph → description
- Optional schema fields (`"optional": true`) excluded from completeness denominator

### Datasets
- `datasets/dataset_v2.json` (v2.1) — completeness, 40 live URLs
- `datasets/dataset_gt.json` — precision GT
- `datasets/dataset_v3.json` — expanded completeness set

### Explicitly out of scope for 0.3
- L2 LLM fill
- L3/L4 browser
- Ground-truth scale 50–100 (next)

### Post-freeze GT expansion
- `dataset_gt` → **60 URLs**: P **99.3%** R **98.6%** F1 **99.0%**
