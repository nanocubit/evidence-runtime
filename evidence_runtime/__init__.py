"""Evidence-First Adaptive Web Runtime.

Deterministic, evidence-grounded web extraction with multi-method
conflict resolution and telemetry.
"""

__version__ = "0.4.0"

from .models import (
    Conflict,
    Evidence,
    ExtractionRequest,
    ExtractionRun,
    Fact,
    RunMetrics,
    RunStatus,
)
from .service import extract, extract_async

__all__ = [
    "ExtractionRequest",
    "ExtractionRun",
    "Fact",
    "Evidence",
    "Conflict",
    "RunStatus",
    "RunMetrics",
    "extract",
    "extract_async",
    "__version__",
]
