"""Outbound URL safety checks (SSRF hardening).

Run before the server makes a request to a *user-supplied* URL — e.g. the custom
embedding endpoint set via ``POST /api/embeddings/endpoint``, which then triggers
an outbound ``httpx`` call.

Ithaka is local-first: pointing the embedding endpoint at a loopback or LAN
address (a local vLLM / llama.cpp / Ollama server) is a normal, intended setup.
So this guard does **not** blanket-block private addresses by default — that would
break the primary use case. What it *always* rejects:

  - a non-HTTP(S) scheme (``file://``, ``gopher://``, ``ftp://`` …), and
  - the link-local range (``169.254.0.0/16`` / ``fe80::/10``), i.e. the cloud
    instance-metadata SSRF credential-exfil vector — nobody serves embeddings
    there — plus multicast / reserved / unspecified addresses.

For exposed multi-tenant deployments, set ``EMBEDDING_BLOCK_PRIVATE_IPS=true`` to
additionally reject all private and loopback targets (full SSRF lockdown).
"""

import ipaddress
import socket
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx

ALLOWED_SCHEMES = ("http", "https")


class UnsafeOutboundURL(Exception):
    """Raised by the safe fetch helpers when a request — or any redirect hop —
    violates the outbound URL policy."""


def _default_resolver(host: str) -> List[str]:
    """Resolve a hostname to the list of IP strings it maps to (A + AAAA)."""
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _classify(ip: ipaddress._BaseAddress, *, block_private: bool) -> Optional[str]:
    """Return a rejection reason for an IP, or None if it is allowed."""
    # IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254) — judge the embedded v4.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_link_local:
        return f"link-local address blocked (SSRF metadata risk): {ip}"
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return f"disallowed address: {ip}"
    if block_private and (ip.is_private or ip.is_loopback):
        return f"private/loopback address blocked: {ip}"
    return None


def check_outbound_url(
    url: str,
    *,
    block_private: bool = False,
    resolver: Optional[Callable[[str], List[str]]] = None,
) -> Tuple[bool, str]:
    """Validate a user-supplied outbound URL.

    Returns ``(ok, reason)``. ``ok`` is True only when the URL is safe to fetch.
    ``resolver`` is injectable so callers/tests can avoid real DNS.
    """
    if not isinstance(url, str):
        return False, "URL must be a string"
    if not url or not url.strip():
        return False, "URL is required"
    try:
        parsed = urlparse(url.strip())
    except Exception as e:  # pragma: no cover - urlparse is very tolerant
        return False, f"unparseable URL: {e}"

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return False, f"scheme must be http or https, got '{parsed.scheme or '(none)'}'"
    host = parsed.hostname
    if not host:
        return False, "URL has no host"

    resolve = resolver or _default_resolver
    try:
        raw_ips = resolve(host)
    except Exception as e:
        return False, f"host does not resolve: {e}"
    if not raw_ips:
        return False, "host does not resolve"

    saw_ip = False
    for raw in raw_ips:
        if not isinstance(raw, str):
            continue
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])  # strip IPv6 zone id
        except ValueError:
            continue
        saw_ip = True
        reason = _classify(ip, block_private=block_private)
        if reason:
            return False, reason
    if not saw_ip:
        return False, "host does not resolve to an IP"
    return True, "ok"


# ---------------------------------------------------------------------------
# SSRF-safe fetch helpers
#
# ``check_outbound_url`` alone is check-then-fetch: the guard resolves the
# hostname, but the actual httpx call resolves it *again*, so a DNS-rebinding
# name (first resolve public, second resolve 169.254.169.254) slips past the
# metadata block. And nothing stops a caller from following a 302 straight
# into the link-local range. ``safe_httpx_request`` closes both holes:
#
#   - it resolves each hop exactly once, classifies those IPs with the same
#     policy as ``check_outbound_url``, and PINS the vetted IP onto the
#     connection (request goes to the IP, ``Host`` header carries the
#     hostname, and for TLS the ``sni_hostname`` request extension keeps
#     certificate validation against the hostname);
#   - it never lets httpx follow redirects — each ``Location`` is re-vetted
#     and re-pinned, with a hop ceiling (default 5).
# ---------------------------------------------------------------------------

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_DROP_BODY_STATUSES = (301, 302, 303)
_BODY_KWARGS = ("content", "data", "json", "files")


def _prepare_hop(
    url: str,
    hop_index: int,
    *,
    block_private: bool,
    resolver: Optional[Callable[[str], List[str]]],
    allowed_hosts: Optional[frozenset],
) -> Tuple[str, Dict[str, str], Dict[str, str]]:
    """Vet one hop of a fetch. Returns ``(request_url, headers, extensions)``.

    The hostname is resolved exactly once; the same vetted IPs are pinned onto
    the request URL so the connect cannot hit a different (rebound) address.
    Raises ``UnsafeOutboundURL`` on any policy violation.
    """
    url = (url or "").strip()
    host = (urlparse(url).hostname or "").lower()
    if allowed_hosts is not None and host not in allowed_hosts:
        if hop_index == 0:
            raise UnsafeOutboundURL(f"host not allowed: {host or 'unknown host'}")
        raise UnsafeOutboundURL(
            f"redirect target host not allowed: {host or 'unknown host'}"
        )

    captured: Dict[str, List[str]] = {}

    def _capturing_resolver(name: str) -> List[str]:
        ips = (resolver or _default_resolver)(name)
        captured["ips"] = ips
        return ips

    ok, reason = check_outbound_url(
        url, block_private=block_private, resolver=_capturing_resolver
    )
    if not ok:
        raise UnsafeOutboundURL(reason)

    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    # IP-literal host: no DNS involved, nothing to pin.
    try:
        ipaddress.ip_address(hostname)
        return url, {}, {}
    except ValueError:
        pass

    pin_ip = None
    for raw in captured.get("ips", []):
        if not isinstance(raw, str):
            continue
        candidate = raw.split("%")[0]
        try:
            ipaddress.ip_address(candidate)
            pin_ip = candidate
            break
        except ValueError:
            continue
    if pin_ip is None:
        # check_outbound_url was overridden without touching the resolver
        # (test stubs) — fall back to an unpinned request.
        return url, {}, {}

    netloc = f"[{pin_ip}]" if ":" in pin_ip else pin_ip
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    pinned_url = parsed._replace(netloc=netloc).geturl()
    host_header = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
    extensions = (
        {"sni_hostname": hostname} if parsed.scheme.lower() == "https" else {}
    )
    return pinned_url, {"Host": host_header}, extensions


def _redirect_target(
    response, current_url: str, hops_done: int, max_redirects: int
) -> Tuple[Optional[str], int]:
    """Return ``(next_url, status)``; ``next_url`` is None when the response is
    final. Raises ``UnsafeOutboundURL`` when the redirect ceiling is hit."""
    status = int(getattr(response, "status_code", 0) or 0)
    if status not in _REDIRECT_STATUSES:
        return None, status
    headers = getattr(response, "headers", None)
    location = ""
    if headers is not None:
        location = headers.get("location") or headers.get("Location") or ""
    if not location:
        return None, status
    if hops_done >= max_redirects:
        raise UnsafeOutboundURL(f"too many redirects (limit {max_redirects})")
    return urljoin(current_url, location), status


def _next_hop_state(
    status: int,
    method: str,
    kwargs: Dict,
    base_headers: Dict[str, str],
    auth,
    current_url: str,
    target_url: str,
):
    """Adjust method/body (301/302/303 become GET) and drop credentials when a
    redirect leaves the current host. Returns ``(method, kwargs, auth)``."""
    if status in _DROP_BODY_STATUSES and method not in ("GET", "HEAD"):
        method = "GET"
        kwargs = {k: v for k, v in kwargs.items() if k not in _BODY_KWARGS}
    prev_host = (urlparse(current_url).hostname or "").lower()
    next_host = (urlparse(target_url).hostname or "").lower()
    if prev_host != next_host:
        auth = None
        for key in list(base_headers):
            if key.lower() == "authorization":
                base_headers.pop(key)
    return method, kwargs, auth


def _attach_final_url(response, final_url: str) -> None:
    try:
        response.extensions["final_url"] = final_url
    except Exception:
        pass


def safe_httpx_request(
    method: str,
    url: str,
    *,
    block_private: bool = False,
    resolver: Optional[Callable[[str], List[str]]] = None,
    max_redirects: int = 5,
    allowed_hosts: Optional[Iterable[str]] = None,
    timeout: float = 30.0,
    verify: bool = True,
    transport=None,
    headers: Optional[Dict[str, str]] = None,
    auth=None,
    **request_kwargs,
):
    """SSRF-safe sync fetch of a user-influenced URL (see module block comment).

    Every hop is validated with the ``check_outbound_url`` policy and the
    vetted IP is pinned onto the connection; redirects are followed manually
    (``follow_redirects`` is never delegated to httpx) up to ``max_redirects``.
    ``allowed_hosts`` optionally confines every hop to a hostname allowlist.
    Raises ``UnsafeOutboundURL`` on any violation; otherwise returns the
    ``httpx.Response``, with the final logical (hostname) URL available as
    ``response.extensions["final_url"]``. Userinfo in the URL is not preserved
    — pass ``auth`` instead.
    """
    allowed = (
        frozenset(h.lower() for h in allowed_hosts)
        if allowed_hosts is not None
        else None
    )
    current_url = (url or "").strip()
    current_method = (method or "GET").upper()
    base_headers = dict(headers or {})
    kwargs = dict(request_kwargs)
    kwargs.pop("follow_redirects", None)  # always handled here
    client_kwargs = {"timeout": timeout, "verify": verify, "follow_redirects": False}
    if transport is not None:
        client_kwargs["transport"] = transport

    hops = 0
    with httpx.Client(**client_kwargs) as client:
        while True:
            request_url, pin_headers, extensions = _prepare_hop(
                current_url,
                hops,
                block_private=block_private,
                resolver=resolver,
                allowed_hosts=allowed,
            )
            response = client.request(
                current_method,
                request_url,
                headers={**base_headers, **pin_headers},
                auth=auth,
                extensions=extensions or None,
                **kwargs,
            )
            target, status = _redirect_target(
                response, current_url, hops, max_redirects
            )
            if target is None:
                _attach_final_url(response, current_url)
                return response
            hops += 1
            current_method, kwargs, auth = _next_hop_state(
                status, current_method, kwargs, base_headers, auth,
                current_url, target,
            )
            current_url = target


async def safe_httpx_request_async(
    method: str,
    url: str,
    *,
    block_private: bool = False,
    resolver: Optional[Callable[[str], List[str]]] = None,
    max_redirects: int = 5,
    allowed_hosts: Optional[Iterable[str]] = None,
    timeout: float = 30.0,
    verify: bool = True,
    transport=None,
    headers: Optional[Dict[str, str]] = None,
    auth=None,
    **request_kwargs,
):
    """Async twin of ``safe_httpx_request`` — same policy, per-hop pinning and
    manual redirect handling, using ``httpx.AsyncClient``."""
    allowed = (
        frozenset(h.lower() for h in allowed_hosts)
        if allowed_hosts is not None
        else None
    )
    current_url = (url or "").strip()
    current_method = (method or "GET").upper()
    base_headers = dict(headers or {})
    kwargs = dict(request_kwargs)
    kwargs.pop("follow_redirects", None)  # always handled here
    client_kwargs = {"timeout": timeout, "verify": verify, "follow_redirects": False}
    if transport is not None:
        client_kwargs["transport"] = transport

    hops = 0
    async with httpx.AsyncClient(**client_kwargs) as client:
        while True:
            request_url, pin_headers, extensions = _prepare_hop(
                current_url,
                hops,
                block_private=block_private,
                resolver=resolver,
                allowed_hosts=allowed,
            )
            response = await client.request(
                current_method,
                request_url,
                headers={**base_headers, **pin_headers},
                auth=auth,
                extensions=extensions or None,
                **kwargs,
            )
            target, status = _redirect_target(
                response, current_url, hops, max_redirects
            )
            if target is None:
                _attach_final_url(response, current_url)
                return response
            hops += 1
            current_method, kwargs, auth = _next_hop_state(
                status, current_method, kwargs, base_headers, auth,
                current_url, target,
            )
            current_url = target
