"""Headline economics are computed from telemetry, so they are testable offline."""

from __future__ import annotations

import duckdb

from evidence_runtime.economy import build_report


def _seed(path: str) -> None:
    con = duckdb.connect(path)
    con.execute("CREATE TABLE runs (run_id VARCHAR, level VARCHAR, status VARCHAR, latency_ms DOUBLE, warnings JSON)")
    con.execute("CREATE TABLE facts (run_id VARCHAR, fact_id VARCHAR)")
    con.execute("CREATE TABLE evidence (run_id VARCHAR, evidence_id VARCHAR)")

    con.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?)",
        ("r1", "L1", "success", 1000.0, '["route:L1_http:tactic:docs","missing_fields: []"]'),
    )
    con.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?)",
        ("r2", "L1", "success", 2000.0, '["route:L1_http:mode=http"]'),
    )
    con.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?)",
        ("r3", "L3", "success", 9000.0, '["route:L3_browser:bot_wall","escalated:L3_playwright"]'),
    )
    con.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?)",
        ("r4", "L1", "partial", 3000.0, '["route:L1_http:tactic:docs","l2_llm_fill:+[\'price\']"]'),
    )

    for fact_id, run_id in (("f1", "r1"), ("f2", "r1"), ("f3", "r3")):
        con.execute("INSERT INTO facts VALUES (?,?)", (run_id, fact_id))
    for eid, run_id in (("e1", "r1"), ("e2", "r1")):
        con.execute("INSERT INTO evidence VALUES (?,?)", (run_id, eid))
    con.close()


def test_build_report_counts_avoidance_and_provenance(tmp_path):
    db = tmp_path / "runtime.duckdb"
    _seed(str(db))
    r = build_report(str(db))

    assert r["total_runs"] == 4
    assert r["browser_runs"] == 1
    assert r["browser_avoidance_rate"] == 0.75
    assert r["llm_runs"] == 1
    assert r["llm_avoidance_rate"] == 0.75
    assert r["facts_total"] == 3
    assert r["facts_with_evidence"] == 2
    assert r["provenance_coverage"] == 0.667
    assert r["by_level"] == {"L1": 3, "L3": 1}
    assert r["escalation_reasons"]["route:L3_browser:bot_wall"] == 1
    assert r["escalation_reasons"]["route:L1_http:tactic:docs"] == 2
    assert r["p50_latency_ms"] > 0


def test_build_report_empty_db_is_safe(tmp_path):
    db = tmp_path / "empty.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE runs (run_id VARCHAR, level VARCHAR, status VARCHAR, latency_ms DOUBLE, warnings JSON)")
    con.execute("CREATE TABLE facts (run_id VARCHAR, fact_id VARCHAR)")
    con.execute("CREATE TABLE evidence (run_id VARCHAR, evidence_id VARCHAR)")
    con.close()

    r = build_report(str(db))
    assert r["total_runs"] == 0
    assert r["browser_avoidance_rate"] == 0.0
    assert r["provenance_coverage"] == 0.0
