"""Orchestration pipeline: robots → fetch → L0 cache → extract_l1 → store → metrics."""

from __future__ import annotations

import asyncio
import time
from time import perf_counter

from .browser import fetch_browser, playwright_available
from .cache import get_cache
from .extract import extract_l1
from .fetcher import fetch_async
from .llm_fill import fill_missing_fields, llm_fill_available
from .metrics import compute_run_metrics
from .models import Evidence, ExtractionRequest, ExtractionRun, Fact, RunStatus
from .normalize import normalize_html
from .policies import BrowserPolicy, FetchPolicy, PolicyError
from .robots import is_allowed
from .router import AdaptiveRouter
from .snapshot import SnapshotStore
from .storage import RunStore

_snapshot_store: SnapshotStore | None = None
_last_fetch_at: dict[str, float] = {}  # host -> monotonic time
_router: AdaptiveRouter | None = None


def get_router() -> AdaptiveRouter:
    """Process-wide router (lazy). Override with set_router() in tests."""
    global _router
    if _router is None:
        _router = AdaptiveRouter()
    return _router


def set_router(router: AdaptiveRouter | None) -> None:
    global _router
    _router = router


def _get_snapshot_store() -> SnapshotStore:
    global _snapshot_store
    if _snapshot_store is None:
        _snapshot_store = SnapshotStore()
    return _snapshot_store


def extract(req: ExtractionRequest, db_path: str = "runtime.duckdb") -> ExtractionRun:
    try:
        return asyncio.run(extract_async(req, db_path))
    except RuntimeError:
        import nest_asyncio
        nest_asyncio.apply()
        return asyncio.run(extract_async(req, db_path))


def _classify_failure(exc: Exception) -> str:
    name = type(exc).__name__
    msg = str(exc).lower()
    if isinstance(exc, PolicyError):
        if "robots_disallowed" in msg:
            return "robots_disallowed"
        if "mime" in msg:
            return "wrong_mime_type"
        if "private ip" in msg or "blocked host" in msg:
            return "policy_blocked"
        return f"policy_error: {exc}"
    if name in ("ConnectError", "ConnectTimeout", "ReadTimeout", "TimeoutException", "TimeoutError"):
        return "no_html:timeout"
    if name in ("HTTPStatusError",):
        return "no_html:http_error"
    if "max_bytes" in msg:
        return "no_html:too_large"
    return f"{name}: {exc}"


def _host(url: str) -> str:
    try:
        return url.split("/")[2].lower()
    except Exception:
        return url



def should_escalate(
    req: ExtractionRequest,
    *,
    missing: list[str],
    sig: dict | None,
    failure_reason: str | None,
    status: RunStatus | None = None,
) -> bool:
    """Decide whether to run L3 Playwright after L1.

    Thin wrapper over :meth:`AdaptiveRouter.decide` so the escalation policy
    lives in one place and every decision carries a reason.
    """
    decision = get_router().decide(
        req,
        signature=sig,
        missing=missing,
        failure_reason=failure_reason,
        status=status,
    )
    return decision.backend == "L3_browser"


async def _l3_fill_missing(
    url: str,
    req: ExtractionRequest,
    *,
    missing: list[str],
    facts: list,
    evidence: list,
    browser_policy: BrowserPolicy,
    fetch_policy: FetchPolicy,
    trace: list[str],
) -> tuple[list, list, list[str], dict, list[str]]:
    """Fetch via Playwright and extract_l1; keep only fields still missing."""
    if not missing and req.mode != "browser":
        return facts, evidence, missing, {}, trace

    bdoc = await fetch_browser(
        url,
        browser_policy,
        user_agent=fetch_policy.user_agent,
    )
    f3, e3, miss3, sig3, tr3 = extract_l1(
        bdoc.text,
        bdoc.url or url,
        req.schema,
        locale=req.locale,
        timezone_name=req.timezone,
        auth_context_id=req.auth_context_id,
        content_hash=bdoc.content_hash,
        headers=bdoc.headers or None,
        ttfb_ms=bdoc.ttfb_ms,
        body_bytes=len(bdoc.body) if bdoc.body else None,
        retrieved_at=None,
    )
    # Tag backends as L3 for newly added evidence
    have = {f.field for f in facts}
    missing_set = set(missing)
    added = []
    added_eids = set()
    for f in f3:
        if f.field in have:
            continue
        if req.mode != "browser" and f.field not in missing_set:
            continue
        for ev in e3:
            if ev.evidence_id in f.evidence_ids:
                ev.extraction_backend = "L3_playwright"
                added_eids.add(ev.evidence_id)
        f.extraction_method = f"L3_{f.extraction_method}"
        facts.append(f)
        have.add(f.field)
        added.append(f.field)
    for ev in e3:
        if ev.evidence_id in added_eids:
            evidence.append(ev)

    schema_fields = (
        list(req.schema.get("fields", req.schema).keys())
        if isinstance(req.schema, dict) else []
    )
    # respect optional
    fields_spec = req.schema.get("fields", {}) if isinstance(req.schema, dict) else {}
    required = [
        k for k, v in fields_spec.items()
        if not (isinstance(v, dict) and v.get("optional"))
    ] if isinstance(fields_spec, dict) else schema_fields
    missing = [m for m in required if m not in have]
    trace.append(f"L3_playwright: +{added} still_missing={missing}")
    trace.extend(["  | " + x for x in tr3[:4]])
    return facts, evidence, missing, sig3 or {}, trace


async def _respect_crawl_delay(url: str, policy: FetchPolicy) -> None:
    """Sleep if robots.txt Crawl-delay requires it."""
    if not policy.respect_robots:
        return
    info = is_allowed(url, policy.user_agent)
    delay = info.crawl_delay or 0.0
    delay = max(delay, policy.min_crawl_delay_s)
    if delay <= 0:
        return
    host = _host(url)
    now = time.monotonic()
    last = _last_fetch_at.get(host, 0.0)
    wait = delay - (now - last)
    if wait > 0:
        await asyncio.sleep(min(wait, 10.0))  # cap so benchmarks don't stall forever
    _last_fetch_at[host] = time.monotonic()


async def extract_async(
    req: ExtractionRequest,
    db_path: str = "runtime.duckdb",
    *,
    save_snapshot: bool = True,
    debug: bool = False,
    use_cache: bool = True,
) -> ExtractionRun:
    t0 = perf_counter()
    url = str(req.url)
    run = ExtractionRun(
        request=req,
        normalized_url=url,
        attempted_level="L1",
    )
    policy = FetchPolicy()

    browser_policy = BrowserPolicy()
    # mode=http → never escalate; mode=browser/auto → may escalate after L1

    try:
        await _respect_crawl_delay(url, policy)

        doc = await asyncio.wait_for(fetch_async(url, policy), timeout=18.0)
        run.content_hash = doc.content_hash
        run.content_signature = doc.content_signature
        run.bytes_downloaded = len(doc.body)
        run.normalized_url = doc.url

        if not doc.text or len(doc.text.strip()) < 20:
            run.status = RunStatus.failed
            run.failure_reason = "no_html:empty_body"
            return run

        # L0 cache hit?
        if use_cache and doc.content_signature:
            cached = get_cache().get(url, req.schema, doc.content_signature)
            if cached:
                run.status = RunStatus(cached["status"])
                run.facts = [Fact.model_validate(f) for f in cached["facts"]]
                run.evidence = [Evidence.model_validate(e) for e in cached["evidence"]]
                run.missing_fields = cached["missing_fields"]
                run.warnings = list(cached["warnings"]) + ["cache_hit:L0"]
                run.failure_reason = cached.get("failure_reason")
                run.content_hash = cached.get("content_hash") or run.content_hash
                schema_fields = list(req.schema.get("fields", req.schema).keys()) if isinstance(req.schema, dict) else []
                run.metrics = compute_run_metrics(run, schema_fields)
                return run

        if save_snapshot and db_path != ":memory:":
            snapshot = normalize_html(doc.text, url)
            _get_snapshot_store().save(doc.content_hash, snapshot, doc.text)

        facts, evidence, missing, sig, trace = extract_l1(
            doc.text,
            url,
            req.schema,
            locale=req.locale,
            timezone_name=req.timezone,
            auth_context_id=req.auth_context_id,
            content_hash=doc.content_hash,
            headers=doc.headers,
            ttfb_ms=getattr(doc, "ttfb_ms", None),
            body_bytes=len(doc.body) if doc.body is not None else None,
            retrieved_at=run.retrieved_at,
        )

        # One-hop product follow: marketing page → first SKU candidate if price still missing
        schema_fields_early = (
            list(req.schema.get("fields", req.schema).keys())
            if isinstance(req.schema, dict) else []
        )
        need_price = "price" in schema_fields_early and "price" not in {f.field for f in facts}
        skus = (sig or {}).get("sku_candidates") or []
        if need_price and skus and (sig.get("tactic") == "product" or sig.get("page_class") == "product"):
            hop = skus[0]
            try:
                await _respect_crawl_delay(hop, policy)
                doc2 = await asyncio.wait_for(fetch_async(hop, policy), timeout=12.0)
                if doc2.text and len(doc2.text.strip()) > 20:
                    f2, e2, miss2, sig2, tr2 = extract_l1(
                        doc2.text, hop, req.schema,
                        locale=req.locale, timezone_name=req.timezone,
                        auth_context_id=req.auth_context_id,
                        content_hash=doc2.content_hash, headers=doc2.headers,
                        ttfb_ms=getattr(doc2, "ttfb_ms", None),
                        body_bytes=len(doc2.body),
                        retrieved_at=run.retrieved_at,
                    )
                    # merge facts for missing fields only
                    have = {f.field for f in facts}
                    for f in f2:
                        if f.field not in have:
                            facts.append(f)
                            have.add(f.field)
                    evidence.extend(e2)
                    missing = [m for m in schema_fields_early if m not in have]
                    trace.append(f"product_hop: {hop[:80]} +fields={sorted(have)}")
                    trace.extend(["  | " + x for x in tr2[:4]])
                    run.warnings.append(f"product_hop:{hop[:120]}")
                    sig = sig2 or sig
            except Exception as hop_exc:
                run.warnings.append(f"product_hop_failed:{type(hop_exc).__name__}")
                trace.append(f"product_hop_failed: {hop_exc}")
        run.facts = facts
        run.evidence = evidence
        run.missing_fields = missing
        if debug:
            run.debug_trace = trace

        page_class = sig.get("page_class", "generic")
        tactic = sig.get("tactic", page_class)
        run.backend_prediction_confidence = 0.3 if (sig.get("likely_spa") or sig.get("browser_hint")) else 0.8
        if sig.get("likely_spa"):
            run.warnings.append("likely_spa: low confidence in L1 extraction")
        if sig.get("has_jsonld"):
            run.warnings.append("jsonld_detected")
        if sig.get("generator"):
            run.warnings.append(f"generator:{sig['generator']}")
        if sig.get("cdn"):
            run.warnings.append(f"cdn:{sig['cdn']}")
        if sig.get("frameworks"):
            run.warnings.append("frameworks:" + ",".join(sig["frameworks"]))
        if sig.get("embedded_blobs"):
            run.warnings.append("embedded:" + ",".join(sig["embedded_blobs"]))
        if sig.get("static_hint"):
            run.warnings.append("static_hint:cache_hit")
        if sig.get("etag"):
            run.warnings.append(f"etag:{sig['etag'][:40]}")
        if sig.get("tracker_count"):
            run.warnings.append(f"trackers:{sig['tracker_count']}")
        if sig.get("spa_score") is not None:
            run.warnings.append(f"spa_score:{sig['spa_score']}")
        if sig.get("canonical"):
            run.warnings.append(f"canonical:{sig['canonical'][:120]}")
        if sig.get("ab_cookies"):
            run.warnings.append("ab_cookies:" + ",".join(sig["ab_cookies"]))
        run.warnings.append(f"page_class:{page_class}")
        run.warnings.append(f"tactic:{tactic}")
        if sig.get("browser_hint"):
            run.warnings.append(f"needs_browser:{tactic}_low_yield")

        schema_fields = (
            list(req.schema.get("fields", req.schema).keys())
            if isinstance(req.schema, dict)
            else []
        )

        # Bot / challenge pages: do not report fake product names as success
        if sig.get("bot_wall"):
            run.warnings.append("bot_wall_detected")
            # Drop challenge-page garbage for name/title
            from .extract import _is_low_quality_title
            facts = [f for f in facts if f.field not in ("name", "title") or not _is_low_quality_title(f.value)]
            missing = [f for f in schema_fields if f not in {x.field for x in facts}]
            run.facts = facts
            run.status = RunStatus.failed
            run.failure_reason = "bot_wall"
        elif not missing and facts:
            run.status = RunStatus.success
        elif facts:
            run.status = RunStatus.partial
            run.warnings.append(f"missing_fields: {missing}")
        else:
            run.status = RunStatus.unresolved
            if sig.get("browser_hint") or tactic in ("spa", "product"):
                run.failure_reason = f"needs_browser:{tactic}_no_facts"
            else:
                run.failure_reason = "no_facts"

        # --- L2 LLM fill (optional; only missing fields) ---
        if missing and llm_fill_available() and not sig.get("bot_wall"):
            try:
                lf, le = await fill_missing_fields(
                    url=url,
                    html_text=doc.text,
                    missing_fields=list(missing),
                    schema=req.schema if isinstance(req.schema, dict) else {},
                    existing_facts=list(facts),
                    content_hash=run.content_hash or doc.content_hash or "",
                    locale=req.locale,
                    timezone_name=req.timezone,
                    auth_context_id=req.auth_context_id,
                )
                if lf:
                    facts.extend(lf)
                    evidence.extend(le)
                    have = {f.field for f in facts}
                    fields_spec = req.schema.get("fields", {}) if isinstance(req.schema, dict) else {}
                    required = [
                        k for k, v in fields_spec.items()
                        if not (isinstance(v, dict) and v.get("optional"))
                    ] if isinstance(fields_spec, dict) else list(fields_spec.keys())
                    missing = [m for m in required if m not in have]
                    run.facts = facts
                    run.evidence = evidence
                    run.missing_fields = missing
                    run.warnings.append(f"l2_llm_fill:+{[f.field for f in lf]}")
                    if not missing and facts:
                        run.status = RunStatus.success
                    elif facts:
                        run.status = RunStatus.partial
                    if debug:
                        run.debug_trace = (run.debug_trace or []) + [f"l2_llm_fill: +{[f.field for f in lf]}"]
            except Exception as l2_exc:
                run.warnings.append(f"l2_failed:{type(l2_exc).__name__}:{l2_exc}")

        # --- L3 Playwright escalate (missing / bot_wall / mode=browser) ---
        decision = get_router().decide(
            req,
            signature=sig,
            missing=missing,
            failure_reason=run.failure_reason,
            status=run.status,
        )
        run.warnings.append(f"route:{decision.backend}:{decision.reason}")
        run.backend_prediction_confidence = decision.confidence

        if decision.backend == "L3_browser":
            if not browser_policy.enabled:
                run.warnings.append("l3_skipped:browser_disabled")
            elif not playwright_available():
                run.warnings.append("l3_skipped:playwright_not_installed")
            else:
                try:
                    run.attempted_level = "L3"
                    facts, evidence, missing, sig3, trace = await _l3_fill_missing(
                        url,
                        req,
                        missing=list(missing),
                        facts=list(facts),
                        evidence=list(evidence),
                        browser_policy=browser_policy,
                        fetch_policy=policy,
                        trace=list(trace) if debug else [],
                    )
                    if sig3:
                        sig = {**(sig or {}), **sig3}
                    run.facts = facts
                    run.evidence = evidence
                    run.missing_fields = missing
                    if any(f.extraction_method.startswith("L3_") for f in facts) or req.mode == "browser":
                        run.level = "L3"
                    run.warnings.append("escalated:L3_playwright")
                    run.fallback_level = "L3"
                    get_router().record_fallback(
                        str(req.url), "L1_http", "L3_browser", decision.reason
                    )
                    if debug:
                        run.debug_trace = (run.debug_trace or []) + trace
                    # If L3 HTML is still a challenge page, do not claim success
                    still_bot = bool((sig3 or {}).get("bot_wall") or (sig or {}).get("bot_wall"))
                    if still_bot and (sig3 or {}).get("bot_wall"):
                        from .extract import _is_low_quality_title
                        facts = [
                            f for f in facts
                            if f.field not in ("name", "title") or not _is_low_quality_title(f.value)
                        ]
                        # drop challenge titles even if method was L3
                        facts = [
                            f for f in facts
                            if not (
                                f.field in ("name", "title")
                                and str(f.value).strip().lower() in {
                                    "robot or human?", "access denied", "attention required", "just a moment..."
                                }
                            )
                        ]
                        run.facts = facts
                        have = {f.field for f in facts}
                        fields_spec = req.schema.get("fields", {}) if isinstance(req.schema, dict) else {}
                        required = [
                            k for k, v in fields_spec.items()
                            if not (isinstance(v, dict) and v.get("optional"))
                        ] if isinstance(fields_spec, dict) else list(fields_spec.keys()) if isinstance(fields_spec, dict) else []
                        missing = [m for m in required if m not in have]
                        run.missing_fields = missing
                        run.failure_reason = "bot_wall"
                        run.status = RunStatus.failed if not facts else RunStatus.partial
                        run.warnings.append("l3_still_bot_wall")
                    else:
                        if run.failure_reason == "bot_wall" and facts:
                            run.failure_reason = None
                        if not missing and facts:
                            run.status = RunStatus.success
                        elif facts:
                            run.status = RunStatus.partial
                            if run.failure_reason == "bot_wall":
                                run.failure_reason = None
                        else:
                            run.status = RunStatus.unresolved
                except Exception as l3_exc:
                    run.warnings.append(f"l3_failed:{type(l3_exc).__name__}:{l3_exc}")
                    trace.append(f"l3_failed: {l3_exc}")

        run.metrics = compute_run_metrics(run, schema_fields)

        # Store in L0 cache
        if use_cache and doc.content_signature and run.status.value in ("success", "partial"):
            get_cache().put(url, req.schema, doc.content_signature, run)

    except PolicyError as exc:
        run.status = RunStatus.failed
        run.failure_reason = _classify_failure(exc)
        run.warnings.append(str(exc))
    except Exception as exc:
        run.status = RunStatus.failed
        run.failure_reason = _classify_failure(exc)
        run.warnings.append(str(exc))

    finally:
        run.latency_ms = (perf_counter() - t0) * 1000
        try:
            RunStore(db_path).save(run)
        except Exception:
            pass

    return run
