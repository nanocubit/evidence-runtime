from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class RunStatus(str, Enum):
    success = "success"
    partial = "partial"
    unresolved = "unresolved"
    failed = "failed"


class ExtractionRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        protected_namespaces=(),
        populate_by_name=True,
        ser_json_by_alias=True,
    )
    url: HttpUrl
    schema_: dict[str, Any] = Field(..., alias="schema")
    mode: Literal["auto", "http", "browser"] = "auto"
    locale: str = "en-US"
    timezone: str = "UTC"
    auth_context_id: str | None = None
    max_cost: float | None = None
    evidence: bool = True
    freshness_seconds: int | None = None  # max age of cached result
    max_latency_ms: float | None = None

    @property
    def schema(self) -> dict[str, Any]:
        """Backward-compatible access to extraction schema."""
        return self.schema_


class Conflict(BaseModel):
    status: Literal["none", "unresolved", "resolved"] = "none"
    fields: list[str] = Field(default_factory=list)
    values: list[dict[str, Any]] = Field(default_factory=list)
    resolution_policy: str | None = None
    resolution: dict[str, Any] | None = None


class Evidence(BaseModel):
    evidence_id: UUID = Field(default_factory=uuid4)
    source_id: str
    source_url: str
    evidence_text: str
    selector: str | None = None
    xpath: str | None = None
    content_hash: str
    content_signature: str
    page_state_hash: str | None = None
    render_state_hash: str | None = None
    schema_version: str = "1"
    locale: str
    timezone: str
    auth_context_id: str | None = None
    retrieved_at: datetime
    start_offset: int | None = None
    end_offset: int | None = None
    extraction_backend: str = "unknown"  # L1, L2, L3, L4


class Fact(BaseModel):
    fact_id: UUID = Field(default_factory=uuid4)
    field: str
    value: Any
    datatype: str
    confidence: float = Field(ge=0, le=1)
    source_id: str
    extraction_method: str
    validation_status: Literal["valid", "invalid", "unverified", "missing"] = "unverified"
    evidence_ids: list[UUID] = Field(default_factory=list)
    conflict: Conflict = Field(default_factory=Conflict)


class RunMetrics(BaseModel):
    deterministic_success_rate: float | None = None
    schema_completeness: float | None = None
    fact_precision: float | None = None
    fact_recall: float | None = None
    evidence_validity: float | None = None
    p50_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    bytes_downloaded: int = 0
    cache_hit_rate: float | None = None
    cost_per_fact: float | None = None
    browser_avoidance_rate: float | None = None
    llm_avoidance_rate: float | None = None


class ExtractionRun(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    request: ExtractionRequest
    normalized_url: str
    level: str = "L1"
    status: RunStatus = RunStatus.failed
    attempted_level: str = "L1"
    fallback_level: str | None = None
    failure_reason: str | None = None
    backend_prediction_confidence: float | None = None
    latency_ms: float | None = None
    bytes_downloaded: int = 0
    content_hash: str | None = None
    content_signature: str | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    facts: list[Fact] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    debug_trace: list[str] = Field(default_factory=list)
    metrics: RunMetrics = Field(default_factory=RunMetrics)


# ---------------------------------------------------------------------------
# Structured extraction intermediate types (PR-1)
# ---------------------------------------------------------------------------

StructuredSyntax = Literal[
    "json-ld",
    "microdata",
    "microformat",
    "rdfa",
    "opengraph",
    "og_manual",
]

LinkSource = Literal["html", "http_header"]


class StructuredAssertion(BaseModel):
    """Intermediate claim from JSON-LD / microdata / OG before Fact adaptation."""

    field: str
    value: Any
    raw_value: str
    datatype: str = "string"
    evidence_locator: str
    structured_syntax: StructuredSyntax
    source_url: str
    snapshot_hash: str
    retrieved_at: datetime | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class LinkRelation(BaseModel):
    """RFC-8288 / HTML link relation (canonical, alternate, next, ...)."""

    url: str
    rel: list[str]
    type: str | None = None
    hreflang: str | None = None
    media: str | None = None
    source: LinkSource
    raw_value: str

