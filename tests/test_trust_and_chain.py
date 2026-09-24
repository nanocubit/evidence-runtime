"""Tests for the two trust primitives: the provenance chain and the trusted policy."""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass
from dataclasses import field as dc_field

import pytest

from evidence_runtime import chain, trusted


# --- provenance chain --------------------------------------------------------

@dataclass
class _Ev:
    evidence_id: str = "e1"
    extraction_backend: str = "L1_trafilatura"
    selector: str = "main"
    content_hash: str = "sha256:aaa"


@dataclass
class _Fact:
    # NB: the attribute is literally named `field` (chain.evidence_digest reads it),
    # so dataclasses.field is imported under an alias above.
    field: str = "title"
    value: str = "Hello"
    evidence_ids: list = dc_field(default_factory=lambda: ["e1"])


class _Status:
    value = "success"


@dataclass
class _Run:
    run_id: str = "r1"
    normalized_url: str = "https://example.com/a"
    level: str = "L1"
    status: _Status = dc_field(default_factory=_Status)
    facts: list = dc_field(default_factory=lambda: [_Fact()])
    evidence: list = dc_field(default_factory=lambda: [_Ev()])


def test_chain_appends_and_links(tmp_path):
    path = tmp_path / "chain.jsonl"
    e1 = chain.append(_Run(run_id="r1"), context={"caller": "test"}, path=path)
    e2 = chain.append(_Run(run_id="r2", normalized_url="https://example.com/b"), path=path)

    assert e1["prev_hash"] == chain.GENESIS
    assert e2["prev_hash"] == e1["hash"]
    ok, count, err = chain.verify(path)
    assert (ok, count, err) == (True, 2, None)
    assert e1["context"] == {"caller": "test"}


def test_chain_detects_tampering(tmp_path):
    path = tmp_path / "chain.jsonl"
    chain.append(_Run(run_id="r1"), path=path)
    chain.append(_Run(run_id="r2"), path=path)

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["url"] = "https://evil.example/"      # change the recorded fact
    lines[0] = json.dumps(tampered, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, count, err = chain.verify(path)
    assert ok is False and err and "hash mismatch" in err


def test_chain_detects_truncation(tmp_path):
    path = tmp_path / "chain.jsonl"
    chain.append(_Run(run_id="r1"), path=path)
    chain.append(_Run(run_id="r2"), path=path)
    chain.append(_Run(run_id="r3"), path=path)

    lines = path.read_text(encoding="utf-8").splitlines()
    # drop the middle entry: the next entry's prev_hash no longer matches
    path.write_text(lines[0] + "\n" + lines[2] + "\n", encoding="utf-8")

    ok, _count, err = chain.verify(path)
    assert ok is False and err and "broken link" in err


def test_evidence_digest_changes_when_a_fact_changes():
    base = chain.evidence_digest(_Run())
    changed = _Run(facts=[_Fact(value="Tampered")])
    assert chain.evidence_digest(changed) != base


def test_chain_verifies_empty_or_missing(tmp_path):
    assert chain.verify(tmp_path / "nope.jsonl") == (True, 0, None)


# --- trusted policy ----------------------------------------------------------

def _policy_file(tmp_path, strict: bool = False):
    p = tmp_path / "trusted_policy.json"
    p.write_text(json.dumps({
        "version": "test.1",
        "strict": strict,
        "allow_domains": ["docs.python.org", "*.wikipedia.org"],
        "schemas": {"article": {"fields": {"title": {"type": "string"}}}},
    }), encoding="utf-8")
    return p


def test_policy_hash_is_stable_and_versioned(tmp_path):
    policy = trusted.load(_policy_file(tmp_path))
    assert policy.policy_hash() == trusted.load(_policy_file(tmp_path)).policy_hash()
    assert policy.version == "test.1"


def test_schema_preset_resolves_from_policy(tmp_path):
    policy = trusted.load(_policy_file(tmp_path))
    assert policy.schema_for("article")["fields"]["title"]["type"] == "string"


def test_unknown_preset_is_refused(tmp_path):
    policy = trusted.load(_policy_file(tmp_path))
    with pytest.raises(trusted.TrustedPolicyError):
        policy.schema_for("whatever-the-caller-wants")


def test_allowlist_matching(tmp_path):
    policy = trusted.load(_policy_file(tmp_path))
    assert policy.domain_allowed("https://docs.python.org/3/library/asyncio.html")
    assert policy.domain_allowed("https://en.wikipedia.org/wiki/Python")
    assert not policy.domain_allowed("https://evil.example/x")


def test_strict_mode_gates_domains(tmp_path):
    permissive = trusted.load(_policy_file(tmp_path, strict=False))
    permissive.check_url("https://evil.example/x")     # no raise

    strict = trusted.load(_policy_file(tmp_path, strict=True))
    with pytest.raises(trusted.TrustedPolicyError):
        strict.check_url("https://evil.example/x")
    strict.check_url("https://docs.python.org/3/")     # allowed


def test_missing_policy_is_permissive_but_hashable(tmp_path):
    policy = trusted.load(tmp_path / "absent.json")
    assert policy.version == "none"
    assert policy.policy_hash().startswith("sha256:")


def test_default_policy_is_repo_relative():
    """Regression: the MCP server runs with an arbitrary cwd, so the default path must
    resolve inside the repo, not relative to the process working directory."""
    repo = Path(__file__).resolve().parents[1]
    assert trusted.default_policy_path() == repo / "policies" / "trusted_policy.json"
    policy = trusted.load()
    assert policy.version != "none", "shipped policy file was not found"
    assert "article" in policy.schemas
