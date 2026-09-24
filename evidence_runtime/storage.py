from __future__ import annotations

import json
import threading
from typing import Any

import duckdb

from .models import ExtractionRun


class RunStore:
    """Thread-safe singleton DuckDB store for extraction telemetry."""

    _instances: dict[str, "RunStore"] = {}
    _lock = threading.Lock()

    def __new__(cls, path: str = "runtime.duckdb") -> "RunStore":
        with cls._lock:
            if path not in cls._instances:
                instance = super().__new__(cls)
                instance._path = path
                instance._local = threading.local()
                instance._init_db()
                cls._instances[path] = instance
            return cls._instances[path]

    def _conn(self) -> duckdb.DuckDBPyConnection:
        if not hasattr(self._local, "con") or self._local.con is None:
            self._local.con = duckdb.connect(self._path)
        return self._local.con

    def _init_db(self) -> None:
        con = self._conn()
        con.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id VARCHAR PRIMARY KEY,
                url VARCHAR,
                normalized_url VARCHAR,
                schema_hash VARCHAR,
                level VARCHAR,
                status VARCHAR,
                latency_ms DOUBLE,
                bytes_downloaded BIGINT,
                content_hash VARCHAR,
                content_signature VARCHAR,
                retrieved_at TIMESTAMP,
                missing_fields JSON,
                warnings JSON,
                payload JSON
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                run_id VARCHAR,
                fact_id VARCHAR PRIMARY KEY,
                field VARCHAR,
                value JSON,
                datatype VARCHAR,
                confidence DOUBLE,
                extraction_method VARCHAR,
                validation_status VARCHAR,
                payload JSON
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS evidence (
                run_id VARCHAR,
                evidence_id VARCHAR PRIMARY KEY,
                source_id VARCHAR,
                source_url VARCHAR,
                evidence_text VARCHAR,
                selector VARCHAR,
                content_hash VARCHAR,
                content_signature VARCHAR,
                retrieved_at TIMESTAMP,
                extraction_backend VARCHAR,
                payload JSON
            )
        """)
        # Indexes
        con.execute("CREATE INDEX IF NOT EXISTS idx_runs_url ON runs(url)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_runs_signature ON runs(content_signature)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_runs_hash ON runs(content_hash)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_facts_run ON facts(run_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_facts_field ON facts(field)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence(run_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence(source_id)")

    def save(self, run: ExtractionRun) -> None:
        con = self._conn()
        rid = str(run.run_id)
        con.execute(
            """
            INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status=excluded.status,
                latency_ms=excluded.latency_ms,
                payload=excluded.payload,
                missing_fields=excluded.missing_fields,
                warnings=excluded.warnings
            """,
            [
                rid,
                str(run.request.url),
                run.normalized_url,
                _hash_json(run.request.schema),
                run.level,
                run.status.value,
                run.latency_ms,
                run.bytes_downloaded,
                run.content_hash,
                run.content_signature,
                run.retrieved_at,
                json.dumps(run.missing_fields),
                json.dumps(run.warnings),
                json.dumps(run.model_dump(mode="json")),
            ],
        )
        for f in run.facts:
            con.execute(
                """
                INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fact_id) DO UPDATE SET
                    confidence=excluded.confidence,
                    validation_status=excluded.validation_status,
                    payload=excluded.payload
                """,
                [
                    rid,
                    str(f.fact_id),
                    f.field,
                    json.dumps(f.value),
                    f.datatype,
                    f.confidence,
                    f.extraction_method,
                    f.validation_status,
                    json.dumps(f.model_dump(mode="json")),
                ],
            )
        for e in run.evidence:
            con.execute(
                """
                INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                    evidence_text=excluded.evidence_text,
                    payload=excluded.payload
                """,
                [
                    rid,
                    str(e.evidence_id),
                    e.source_id,
                    e.source_url,
                    e.evidence_text,
                    e.selector,
                    e.content_hash,
                    e.content_signature,
                    e.retrieved_at,
                    e.extraction_backend,
                    json.dumps(e.model_dump(mode="json")),
                ],
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        con = self._conn()
        row = con.execute("SELECT payload FROM runs WHERE run_id = ?", [run_id]).fetchone()
        return json.loads(row[0]) if row else None

    def query_by_url(self, url: str, limit: int = 10) -> list[dict[str, Any]]:
        con = self._conn()
        rows = con.execute(
            "SELECT payload FROM runs WHERE url = ? ORDER BY retrieved_at DESC LIMIT ?",
            [url, limit],
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def query_by_signature(self, signature: str) -> list[dict[str, Any]]:
        con = self._conn()
        rows = con.execute(
            "SELECT payload FROM runs WHERE content_signature = ? ORDER BY retrieved_at DESC",
            [signature],
        ).fetchall()
        return [json.loads(r[0]) for r in rows]


def _hash_json(obj: Any) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]
