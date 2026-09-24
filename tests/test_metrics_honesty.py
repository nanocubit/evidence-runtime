"""The measuring device is tested too: matcher classification and honest summary."""

from __future__ import annotations

from evidence_runtime.evalgt import match_kind, rule_matches, summarize


def test_match_kind_classification():
    assert match_kind({"equals": "x"}) == "strict"
    assert match_kind({"in": ["a", "b"]}) == "strict"
    assert match_kind({"range": [1, 2]}) == "strict"
    assert match_kind({"contains": "x"}) == "soft"
    assert match_kind({"min_len": 5}) == "soft"
    assert match_kind({"weird": 1}) == "unknown"


def test_rule_matches_semantics():
    assert rule_matches({"equals": "PHP"}, "  php ")
    assert rule_matches({"contains": ["php", "8.4"]}, "PHP 8.4 release")
    assert not rule_matches({"contains": ["php", "9.0"]}, "PHP 8.4 release")
    assert rule_matches({"in": ["a", "B"]}, "b")
    assert rule_matches({"range": [1.0, 2.0]}, "1.5")
    assert not rule_matches({"range": [1.0, 2.0]}, "nan-ish")
    assert rule_matches({"min_len": 3}, "abcd")
    assert not rule_matches({"min_len": 3}, "ab")
    assert not rule_matches({"equals": "x"}, None)


def _item(tp=0, fp=0, fn=0, strict=(0, 0, 0), soft=(0, 0, 0), extras=None):
    return {
        "tp": tp, "fp": fp, "fn": fn,
        "by_kind": {"strict": list(strict), "soft": list(soft), "unknown": [0, 0, 0]},
        "extra_fields": extras or [],
    }


def test_summarize_keeps_legacy_precision():
    s = summarize([_item(tp=9, fp=1, fn=0, strict=(9, 1, 0))])
    assert s["precision"] == 0.9
    assert s["recall"] == 1.0


def test_summarize_reports_soft_share():
    s = summarize([_item(tp=10, strict=(6, 0, 0), soft=(4, 0, 0))])
    assert s["soft_share_of_matches"] == 0.4
    assert s["by_kind"]["soft"]["tp"] == 4


def test_summarize_effective_precision_counts_extra_fields_as_fp():
    # 10 correct, 0 wrong, but the extractor also returned 10 unexpected fields
    items = [_item(tp=10, extras=["sku", "brand"]) for _ in range(5)]
    s = summarize(items)
    assert s["precision"] == 1.0                 # legacy view
    assert s["extra_fields_returned"] == 10
    assert s["items_with_extra_fields"] == 5
    assert s["effective_fp_including_extras"] == 10
    assert s["effective_precision"] < 1.0        # honest view


def test_summarize_empty_is_safe():
    s = summarize([])
    assert s["precision"] == 0.0 and s["n"] == 0
