"""Tests for outbound URL safety / SSRF hardening (src/url_safety.py).

A stub resolver is injected so the tests never touch real DNS, and the fetch
helpers run against httpx.MockTransport so no real network calls happen.
"""

import httpx
import pytest

from src.url_safety import (
    UnsafeOutboundURL,
    check_outbound_url,
    safe_httpx_request,
    safe_httpx_request_async,
)


def _resolver(mapping):
    def resolve(host):
        if host in mapping:
            return mapping[host]
        raise OSError(f"unresolvable: {host}")
    return resolve


PUBLIC = _resolver({"example.com": ["93.184.216.34"]})
LOOPBACK = _resolver({"localhost": ["127.0.0.1"]})
LAN = _resolver({"nas.local": ["192.168.1.50"]})
METADATA = _resolver({"evil.example": ["169.254.169.254"]})
MAPPED_METADATA = _resolver({"evil6.example": ["::ffff:169.254.169.254"]})


def test_non_http_scheme_blocked():
    for url in ("file:///etc/passwd", "ftp://x/y", "gopher://h", "redis://h:6379"):
        ok, reason = check_outbound_url(url, resolver=PUBLIC)
        assert ok is False, url
        assert "scheme" in reason


def test_missing_host_or_empty_blocked():
    assert check_outbound_url("", resolver=PUBLIC)[0] is False
    assert check_outbound_url("http://", resolver=PUBLIC)[0] is False


def test_public_url_allowed():
    ok, reason = check_outbound_url("https://example.com/v1/embeddings", resolver=PUBLIC)
    assert ok is True, reason


def test_cloud_metadata_blocked_even_when_private_allowed():
    # The headline SSRF vector must be blocked regardless of block_private.
    ok, reason = check_outbound_url("http://evil.example/latest/meta-data/", resolver=METADATA)
    assert ok is False
    assert "link-local" in reason


def test_ipv4_mapped_metadata_blocked():
    ok, reason = check_outbound_url("http://evil6.example/", resolver=MAPPED_METADATA)
    assert ok is False
    assert "link-local" in reason


def test_loopback_and_lan_allowed_by_default_local_first():
    # Local-first: a localhost / LAN embedding server is a legitimate target.
    assert check_outbound_url("http://localhost:8080/v1", resolver=LOOPBACK)[0] is True
    assert check_outbound_url("http://nas.local:1234/v1", resolver=LAN)[0] is True


def test_strict_mode_blocks_private_and_loopback():
    ok, reason = check_outbound_url("http://localhost:8080", block_private=True, resolver=LOOPBACK)
    assert ok is False and "private" in reason
    ok, reason = check_outbound_url("http://nas.local", block_private=True, resolver=LAN)
    assert ok is False and "private" in reason


def test_unresolvable_host_blocked():
    ok, reason = check_outbound_url("http://does-not-resolve.invalid", resolver=PUBLIC)
    assert ok is False
    assert "resolve" in reason


def test_resolver_values_must_include_a_parseable_ip():
    ok, reason = check_outbound_url(
        "https://example.test",
        resolver=lambda _host: [None, 123, "not-an-ip"],
    )

    assert ok is False
    assert "does not resolve to an IP" in reason


def test_resolver_skips_invalid_values_but_accepts_public_ip():
    ok, reason = check_outbound_url(
        "https://example.test",
        resolver=lambda _host: [None, "not-an-ip", "93.184.216.34"],
    )

    assert ok is True
    assert reason == "ok"


# ---------------------------------------------------------------------------
# safe_httpx_request — DNS-pinned fetch with per-hop redirect revalidation
# ---------------------------------------------------------------------------


def _recording_transport(responder):
    """MockTransport that records every request it sees."""
    seen = []

    def handler(request):
        seen.append(request)
        return responder(request)

    return httpx.MockTransport(handler), seen


def test_fetch_pins_vetted_ip_and_sets_host_header():
    transport, seen = _recording_transport(lambda req: httpx.Response(200, text="ok"))

    resp = safe_httpx_request(
        "GET",
        "http://example.com:8080/v1/x?q=1",
        resolver=_resolver({"example.com": ["93.184.216.34"]}),
        transport=transport,
    )

    assert resp.status_code == 200
    assert len(seen) == 1
    # The connection went to the vetted IP, not to the hostname...
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].url.port == 8080
    assert seen[0].url.path == "/v1/x"
    assert seen[0].url.params["q"] == "1"
    # ...while the Host header still carries the hostname (+ non-default port).
    assert seen[0].headers["host"] == "example.com:8080"
    assert resp.extensions["final_url"] == "http://example.com:8080/v1/x?q=1"


def test_fetch_https_pins_ip_but_keeps_sni_hostname():
    transport, seen = _recording_transport(lambda req: httpx.Response(200))

    safe_httpx_request(
        "GET",
        "https://secure.example/path",
        resolver=_resolver({"secure.example": ["93.184.216.34"]}),
        transport=transport,
    )

    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "secure.example"
    # TLS certificate validation stays keyed to the hostname via SNI.
    assert seen[0].extensions.get("sni_hostname") == "secure.example"


def test_fetch_pins_ipv6_with_brackets():
    transport, seen = _recording_transport(lambda req: httpx.Response(200))

    safe_httpx_request(
        "GET",
        "http://v6.example:9000/x",
        resolver=_resolver({"v6.example": ["2606:2800:220:1::1"]}),
        transport=transport,
    )

    assert seen[0].url.host == "2606:2800:220:1::1"
    assert seen[0].url.port == 9000
    assert seen[0].headers["host"] == "v6.example:9000"


def test_fetch_rebinding_dns_cannot_reach_metadata_ip():
    """DNS-rebinding TOCTOU: first resolve is public, second resolve would be
    the cloud metadata address. The helper resolves exactly once and pins that
    vetted IP on the connection, so the second (malicious) answer never wins."""
    calls = []

    def rebinding_resolver(host):
        calls.append(host)
        return ["93.184.216.34"] if len(calls) == 1 else ["169.254.169.254"]

    transport, seen = _recording_transport(lambda req: httpx.Response(200, text="ok"))

    resp = safe_httpx_request(
        "GET",
        "http://rebind.example/steal",
        resolver=rebinding_resolver,
        transport=transport,
    )

    assert resp.status_code == 200
    assert calls == ["rebind.example"], "must resolve exactly once (classify + pin share it)"
    assert len(seen) == 1
    assert seen[0].url.host == "93.184.216.34", "request must go to the vetted IP"


def test_fetch_redirect_to_metadata_ip_blocked():
    resolver = _resolver({
        "start.example": ["93.184.216.34"],
        "evil.example": ["169.254.169.254"],
    })

    def responder(req):
        if req.headers.get("host") == "start.example":
            return httpx.Response(302, headers={"location": "http://evil.example/latest/meta-data/"})
        raise AssertionError("the metadata redirect target must never be fetched")

    transport, seen = _recording_transport(responder)

    with pytest.raises(UnsafeOutboundURL, match="link-local"):
        safe_httpx_request(
            "GET", "http://start.example/", resolver=resolver, transport=transport
        )

    assert len(seen) == 1, "only the first hop may be fetched"


def test_fetch_redirect_ceiling():
    resolver = _resolver({"loop.example": ["93.184.216.34"]})

    transport, seen = _recording_transport(
        lambda req: httpx.Response(302, headers={"location": "http://loop.example/again"})
    )

    with pytest.raises(UnsafeOutboundURL, match="too many redirects"):
        safe_httpx_request(
            "GET", "http://loop.example/", resolver=resolver,
            transport=transport, max_redirects=5,
        )

    # initial request + 5 followed redirects; the 6th redirect is refused
    assert len(seen) == 6


def test_fetch_allowed_hosts_confines_redirects():
    resolver = _resolver({
        "good.example": ["93.184.216.34"],
        "other.example": ["93.184.216.35"],
    })

    def responder(req):
        if req.headers.get("host") == "good.example":
            return httpx.Response(302, headers={"location": "https://other.example/x"})
        raise AssertionError("off-allowlist redirect target must never be fetched")

    transport, _seen = _recording_transport(responder)

    with pytest.raises(UnsafeOutboundURL, match="redirect target"):
        safe_httpx_request(
            "GET", "http://good.example/", resolver=resolver,
            transport=transport, allowed_hosts={"good.example"},
        )


def test_fetch_ip_literal_passes_through_unpinned():
    transport, seen = _recording_transport(lambda req: httpx.Response(200))

    resp = safe_httpx_request("GET", "http://127.0.0.1:9000/x", transport=transport)

    assert resp.status_code == 200
    assert seen[0].url.host == "127.0.0.1"


def test_fetch_metadata_ip_literal_blocked_before_any_request():
    transport, seen = _recording_transport(lambda req: httpx.Response(200))

    with pytest.raises(UnsafeOutboundURL, match="link-local"):
        safe_httpx_request("GET", "http://169.254.169.254/", transport=transport)

    assert seen == []


async def test_fetch_async_rebinding_and_redirect_hardening():
    """Async twin: single pinned resolve, and a redirect into the metadata
    range is refused."""
    calls = []

    def rebinding_resolver(host):
        calls.append(host)
        if host == "evil.example":
            return ["169.254.169.254"]
        return ["93.184.216.34"] if len(calls) == 1 else ["169.254.169.254"]

    seen = []

    def handler(request):
        seen.append(request)
        if request.headers.get("host") == "api.example":
            return httpx.Response(302, headers={"location": "http://evil.example/"})
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)

    with pytest.raises(UnsafeOutboundURL, match="link-local"):
        await safe_httpx_request_async(
            "GET", "http://api.example/", resolver=rebinding_resolver, transport=transport
        )

    assert calls == ["api.example", "evil.example"]
    assert len(seen) == 1
    assert seen[0].url.host == "93.184.216.34"
