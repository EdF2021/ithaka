# tests/test_security_headers_middleware.py
"""
Focused regression coverage for `SecurityHeadersMiddleware`
(core/middleware.py), added alongside the HSTS + Permissions-Policy
hardening:

  1. HSTS is emitted only for HTTPS requests, including those reaching
     the app over a reverse proxy (`X-Forwarded-Proto: https`).
  2. HSTS is absent on plain HTTP so local/dev deployments are unaffected.
  3. `Permissions-Policy` locks down camera/geolocation but preserves
     same-origin microphone access (`microphone=(self)`), so the app's
     own voice/STT flow (`getUserMedia({ audio: true })`) keeps working.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import SecurityHeadersMiddleware


def _build_app():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/")
    def root():
        return {"ok": True}

    @app.get("/api/notebooks/{notebook_id}/artifacts/{artifact_id}/report")
    def notebook_report(notebook_id: str, artifact_id: str):
        return {"ok": True}

    @app.get("/api/notebooks/{notebook_id}/sources")
    def notebook_sources(notebook_id: str):
        return {"ok": True}

    return app


def _client(base_url="http://testserver"):
    return TestClient(_build_app(), base_url=base_url)


def test_hsts_absent_on_plain_http():
    response = _client().get("/")

    assert "strict-transport-security" not in response.headers


def test_hsts_present_for_direct_https_requests():
    response = _client(base_url="https://testserver").get("/")

    assert response.headers["strict-transport-security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_hsts_present_via_x_forwarded_proto_https():
    response = _client().get("/", headers={"X-Forwarded-Proto": "https"})

    assert response.headers["strict-transport-security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_permissions_policy_locks_camera_and_geolocation_but_allows_self_microphone():
    response = _client().get("/")

    policy = response.headers["permissions-policy"]
    assert policy == "camera=(), microphone=(self), geolocation=()"

    # Explicitly pin the contract the reviewer flagged: an empty allowlist
    # would also block the app's own same-origin voice/STT button.
    assert "microphone=()" not in policy
    assert "microphone=(self)" in policy


def test_notebook_artifact_report_gets_relaxed_report_csp():
    """The notebook-artifact visual report reuses src/visual_report.py's
    self-contained template (inline scripts + toolbar), same as
    /api/research/report/<id>, so it needs the same relaxed CSP rather than
    the app-wide nonce-only default."""
    response = _client().get("/api/notebooks/nb1/artifacts/art1/report")

    csp = response.headers["content-security-policy"]
    assert "script-src 'self' 'unsafe-inline'" in csp
    assert "frame-ancestors 'none'" in csp


def test_notebook_sources_keeps_default_strict_csp():
    """The relaxed CSP is scoped to the one report route, not the whole
    /api/notebooks/ prefix — a sibling route like /sources must keep the
    app-wide nonce-only CSP."""
    response = _client().get("/api/notebooks/nb1/sources")

    csp = response.headers["content-security-policy"]
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "frame-ancestors 'none'" in csp
