from evidence_runtime.models import ExtractionRequest, RunMetrics


def test_request_contract():
    r = ExtractionRequest(
        url="https://example.com",
        schema={"fields": {"title": {"type": "string"}}},
    )
    assert r.mode == "auto"
    assert r.locale == "en-US"
    assert r.evidence is True


def test_metrics_defaults():
    m = RunMetrics()
    assert m.bytes_downloaded == 0
