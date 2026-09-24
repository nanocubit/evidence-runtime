"""Ground-truth matching and honest aggregation (importable, testable).

`scripts/evaluate_gt.py` is a thin CLI over these helpers. Keeping the matcher
and the summary math inside the package means the *measuring device* itself is
covered by tests.
"""

from __future__ import annotations

from typing import Any

STRICT_KINDS = ("equals", "in", "range")
SOFT_KINDS = ("contains", "min_len")


def match_kind(expected: dict[str, Any]) -> str:
    """Classify a ground-truth rule as strict, soft or unknown."""
    if any(k in expected for k in STRICT_KINDS):
        return "strict"
    if any(k in expected for k in SOFT_KINDS):
        return "soft"
    return "unknown"


def rule_matches(expected: dict[str, Any], value: Any) -> bool:
    """True when `value` satisfies the ground-truth rule."""
    if value is None:
        return False
    if "equals" in expected:
        return str(value).strip().lower() == str(expected["equals"]).strip().lower()
    if "contains" in expected:
        s = str(value).lower()
        needles = expected["contains"]
        if isinstance(needles, str):
            needles = [needles]
        return all(n.lower() in s for n in needles)
    if "in" in expected:
        return str(value).strip().lower() in {str(x).lower() for x in expected["in"]}
    if "range" in expected:
        try:
            v = float(value)
            lo, hi = expected["range"]
            return lo <= v <= hi
        except (TypeError, ValueError):
            return False
    if "min_len" in expected:
        return len(str(value).strip()) >= int(expected["min_len"])
    return False


def _ratio(a: int, b: int) -> float:
    return a / b if b else 0.0


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-item results, including the honesty block.

    Each result carries: tp, fp, fn, by_kind{strict|soft|unknown: [tp, fp, fn]},
    extra_fields (fields returned that ground truth never asked for).
    """
    tp = sum(r["tp"] for r in results)
    fp = sum(r["fp"] for r in results)
    fn = sum(r["fn"] for r in results)
    prec = _ratio(tp, tp + fp)
    rec = _ratio(tp, tp + fn)
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0

    by_kind: dict[str, dict[str, Any]] = {}
    for kind in ("strict", "soft", "unknown"):
        k_tp = sum(r.get("by_kind", {}).get(kind, [0, 0, 0])[0] for r in results)
        k_fp = sum(r.get("by_kind", {}).get(kind, [0, 0, 0])[1] for r in results)
        k_fn = sum(r.get("by_kind", {}).get(kind, [0, 0, 0])[2] for r in results)
        k_p = _ratio(k_tp, k_tp + k_fp)
        k_r = _ratio(k_tp, k_tp + k_fn)
        by_kind[kind] = {
            "tp": k_tp, "fp": k_fp, "fn": k_fn,
            "precision": k_p, "recall": k_r,
            "f1": (2 * k_p * k_r / (k_p + k_r)) if (k_p + k_r) else 0.0,
        }

    extra_total = sum(len(r.get("extra_fields") or []) for r in results)
    items_with_extra = sum(1 for r in results if r.get("extra_fields"))
    committed = by_kind["strict"]["tp"] + by_kind["soft"]["tp"] + by_kind["unknown"]["tp"]

    return {
        "n": len(results),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": prec,
        "recall": rec,
        "f1": f1,
        "by_kind": by_kind,
        "soft_share_of_matches": _ratio(by_kind["soft"]["tp"], committed),
        "extra_fields_returned": extra_total,
        "items_with_extra_fields": items_with_extra,
        "effective_fp_including_extras": fp + extra_total,
        "effective_precision": _ratio(tp, tp + fp + extra_total),
    }
