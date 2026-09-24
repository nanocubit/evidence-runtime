"""Trusted-source policy: schemas and allowlists come from a versioned file.

The principle (borrowed from Pearl Necklace's "read the manifest from the base
SHA, not from the agent branch"): the description under which a page is read must
come from a trusted, reviewable source — never from the page itself, and not
ad-hoc from whoever calls the API.

* `schema_for(preset)` resolves a named schema from the policy file.
* `domain_allowed(url)` checks an allowlist.
* `ER_REQUIRE_TRUSTED=1` makes both mandatory: unknown presets and off-allowlist
  domains are refused instead of silently accepted.
* `policy_hash()` pins the exact policy revision into every response/chain entry.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class TrustedPolicyError(RuntimeError):
    """Raised when a request violates the trusted policy."""


DEFAULT_POLICY_PATH = "policies/trusted_policy.json"


@dataclass(frozen=True)
class TrustedPolicy:
    version: str
    allow_domains: tuple[str, ...]
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    strict: bool = False

    # -- hashing -----------------------------------------------------------

    def policy_hash(self) -> str:
        core = {
            "version": self.version,
            "allow_domains": sorted(self.allow_domains),
            "schemas": {k: self.schemas[k] for k in sorted(self.schemas)},
        }
        blob = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return "sha256:" + hashlib.sha256(blob).hexdigest()

    # -- lookups -----------------------------------------------------------

    def schema_for(self, preset: str) -> dict[str, Any]:
        schema = self.schemas.get(preset)
        if schema is None:
            raise TrustedPolicyError(
                f"schema preset {preset!r} is not in trusted policy {self.version} "
                f"(known: {sorted(self.schemas)})"
            )
        return schema

    def domain_allowed(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        for pattern in self.allow_domains:
            pattern = pattern.lower()
            if pattern == "*":
                return True
            if pattern.startswith("*.") and host.endswith(pattern[1:]):
                return True
            if host == pattern:
                return True
        return False

    def check_url(self, url: str) -> None:
        if self.strict and not self.domain_allowed(url):
            raise TrustedPolicyError(f"domain not in trusted allowlist: {url}")


def default_policy_path() -> Path:
    """Repo-relative by default: the policy must not depend on the caller's cwd."""
    env = os.environ.get("ER_TRUSTED_POLICY")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / DEFAULT_POLICY_PATH


def load(path: str | os.PathLike[str] | None = None) -> TrustedPolicy:
    p = Path(path) if path is not None else default_policy_path()
    if not p.exists():
        # No policy file → permissive non-strict policy, still hashable.
        return TrustedPolicy(version="none", allow_domains=("*",), schemas={}, strict=False)
    data = json.loads(p.read_text(encoding="utf-8"))
    return TrustedPolicy(
        version=str(data.get("version", "unversioned")),
        allow_domains=tuple(data.get("allow_domains") or ("*",)),
        schemas=dict(data.get("schemas") or {}),
        strict=bool(data.get("strict", False)) or os.environ.get("ER_REQUIRE_TRUSTED") == "1",
    )
