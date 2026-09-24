"""Red-team suite for the fetch layer — the attack set behind our SSRF/MIME claims.

Every case here is an attack the reader is expected to *reject*. Run:

    python -m pytest tests/test_redteam_fetch.py -q
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from evidence_runtime import policies
from evidence_runtime.fetcher import fetch
from evidence_runtime.policies import FetchPolicy, PolicyError, validate_mime, validate_url

METADATA = "http://169.254.169.254/latest/meta-data/"


# --- attack surface: URL validation -----------------------------------------

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://127.0.0.1:6379/_INFO",
    "ftp://example.com/x",
    "data:text/html,<script>alert(1)</script>",
])
def test_non_http_schemes_rejected(url):
    with pytest.raises(PolicyError):
        validate_url(url, FetchPolicy())


@pytest.mark.parametrize("host", [
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    "169.254.169.254", "metadata.google.internal",
])
def test_blocked_hosts_rejected(host):
    with pytest.raises(PolicyError):
        validate_url(f"http://{host}/x", FetchPolicy())


@pytest.mark.parametrize("host", [
    "10.1.2.3", "192.168.0.10", "172.16.5.5", "169.254.1.1", "[::1]", "[fc00::1]",
])
def test_private_ranges_rejected(host):
    with pytest.raises(PolicyError):
        validate_url(f"http://{host}/x", FetchPolicy())


def test_mime_allowlist_rejects_binaries():
    with pytest.raises(PolicyError):
        validate_mime("application/octet-stream", FetchPolicy())
    with pytest.raises(PolicyError):
        validate_mime("text/plain; charset=utf-8", FetchPolicy())


def test_robots_disallowed_is_blocked(monkeypatch):
    class Denied:
        allowed = False
        crawl_delay = None
        sitemaps: list[str] = []

    monkeypatch.setattr(policies, "robots_check", lambda url, ua: Denied())
    # fetcher imports robots_check into its own namespace, so patch there too
    import evidence_runtime.fetcher as fetcher
    monkeypatch.setattr(fetcher, "robots_check", lambda url, ua: Denied())
    with pytest.raises(PolicyError, match="robots_disallowed"):
        fetch("https://example.com/page", FetchPolicy())


# --- attack surface: redirects (the SSRF pivot) ------------------------------

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/redirect-metadata":
            self._redirect(METADATA)
        elif self.path == "/redirect-blocked-host":
            self._redirect("http://localhost/secret")
        elif self.path == "/redirect-loop":
            self._redirect("/redirect-loop")
        elif self.path == "/big":
            body = b"x" * 5000
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b"<html><body>ok</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # silence
        pass


@pytest.fixture(scope="module")
def local_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _local_policy(**over) -> FetchPolicy:
    # The loopback *address* must be reachable to talk to the test server, so private-IP
    # blocking is off — but the metadata/localhost *hosts* stay denied, which is exactly
    # what the redirect-pivot cases must trip on.
    base = {
        "respect_robots": False,
        "block_private_ips": False,
        "blocked_hosts": ("169.254.169.254", "metadata.google.internal", "localhost"),
    }
    base.update(over)
    return FetchPolicy(**base)


def test_redirect_to_metadata_endpoint_is_blocked(local_server):
    """The pivot that matters: a reachable URL 302s to the cloud metadata IP."""
    with pytest.raises(PolicyError):
        fetch(f"{local_server}/redirect-metadata", _local_policy())


def test_redirect_to_blocked_host_is_blocked(local_server):
    with pytest.raises(PolicyError):
        fetch(f"{local_server}/redirect-blocked-host", _local_policy())


def test_redirect_loop_is_blocked(local_server):
    with pytest.raises(PolicyError, match="too many redirects"):
        fetch(f"{local_server}/redirect-loop", _local_policy(max_redirects=3))


def test_oversized_body_is_blocked(local_server):
    with pytest.raises(ValueError, match="max_bytes"):
        fetch(f"{local_server}/big", _local_policy(max_bytes=1000))


def test_plain_fetch_still_works(local_server):
    doc = fetch(f"{local_server}/", _local_policy())
    assert "ok" in doc.text


def test_every_redirect_hop_is_validated(local_server, monkeypatch):
    """Guard against regressions to httpx auto-redirect: each hop must be validated."""
    import evidence_runtime.fetcher as fetcher

    seen: list[str] = []
    original = fetcher.validate_url

    def spy(url, policy):
        seen.append(url)
        return original(url, policy)

    monkeypatch.setattr(fetcher, "validate_url", spy)
    with pytest.raises(PolicyError):
        fetch(f"{local_server}/redirect-metadata", _local_policy())

    assert any("redirect-metadata" in u for u in seen), "initial hop not validated"
    # a second validation call proves the redirect target was checked too
    assert len(seen) >= 2
