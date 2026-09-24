# Red Team — attacks we claim to stop

Every row is an attack the reader is expected to **reject**. This file exists so the
SSRF / MIME / robots claims are auditable instead of asserted: the middle column is
the mechanism, the right column is the test that proves it.

```bash
python -m pytest tests/test_redteam_fetch.py -q     # 24 cases
```

## Network / SSRF

| Attack | Defense | Test |
|---|---|---|
| `file:///etc/passwd`, `gopher://`, `ftp://`, `data:` | scheme allowlist (`http`, `https` only) | `test_non_http_schemes_rejected` |
| `http://127.0.0.1/…`, `http://0.0.0.0/…`, `http://[::1]/…` | blocked-host list | `test_blocked_hosts_rejected` |
| `http://169.254.169.254/latest/meta-data/` (cloud metadata) | blocked-host list | `test_blocked_hosts_rejected` |
| `http://metadata.google.internal/` | blocked-host list | `test_blocked_hosts_rejected` |
| RFC1918 / link-local / ULA addresses (`10.x`, `192.168.x`, `172.16.x`, `169.254.x`, `fc00::/7`) | private-network check, incl. **DNS resolution** of the host | `test_private_ranges_rejected` |
| **Pivot via redirect:** a reachable URL answers `302 Location: http://169.254.169.254/…` | redirects are walked manually and **every hop is re-validated** (`follow_redirects=False`) | `test_redirect_to_metadata_endpoint_is_blocked`, `test_every_redirect_hop_is_validated` |
| Redirect to a blocked host (`http://localhost/…`) | same per-hop validation | `test_redirect_to_blocked_host_is_blocked` |
| Redirect loop / redirect flood | `max_redirects`, then `PolicyError("too many redirects")` | `test_redirect_loop_is_blocked` |
| Oversized response (memory exhaustion) | streaming byte counter vs `max_bytes` | `test_oversized_body_is_blocked` |
| Binary/foreign content (`application/octet-stream`, `text/plain`) | MIME allowlist | `test_mime_allowlist_rejects_binaries` |
| Disallowed path per `robots.txt` | robots check before fetch | `test_robots_disallowed_is_blocked` |

## Provenance / audit

| Attack | Defense | Test |
|---|---|---|
| Edit a past chain entry (change the recorded URL/fact) | entry hash mismatch → `verify()` fails | `test_chain_detects_tampering` |
| Delete a middle entry (truncate history) | `prev_hash` link mismatch | `test_chain_detects_truncation` |
| Swap a fact's value while keeping the run id | `evidence_digest` covers facts + evidence (selector, backend, content hash) | `test_evidence_digest_changes_when_a_fact_changes` |

## Trusted policy

| Attack | Defense | Test |
|---|---|---|
| Caller asks for an arbitrary permissive schema | schemas resolve from the versioned policy, not from the caller | `test_unknown_preset_is_refused` |
| Read a domain outside the allowlist (strict mode) | `ER_REQUIRE_TRUSTED=1` → refuse before fetch | `test_strict_mode_gates_domains` |
| Policy swapped silently | `policy_hash()` pins the revision into responses and chain entries | `test_policy_hash_is_stable_and_versioned` |

## Not covered yet (be honest)

- **Cross-hop robots.txt.** robots is checked for the requested URL, not for each redirect
  target; a redirect to another host is not re-checked against that host's robots.
- **DNS rebinding (TOCTOU).** The hostname is resolved and validated, then httpx resolves it
  again for the connection; a hostile resolver could answer differently the second time.
  Pinning requires resolving once and dialing by IP.
- **L3/browser egress.** Playwright traffic is not subject to `FetchPolicy`.
- **Body-level attacks** (decompression bombs, HTML parser exhaustion) are not exercised.
