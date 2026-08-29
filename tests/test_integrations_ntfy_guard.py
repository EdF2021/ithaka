"""Guards against misconfigured ntfy base URLs (see 2026-08-28 incident:
the Integrations UI accepted a tailnet URL the server can't reach and a
public ntfy.sh URL with the topic pasted into the base URL)."""
import json
from types import SimpleNamespace

import pytest

from src import integrations

fastapi = pytest.importorskip("fastapi")
from fastapi import HTTPException


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(integrations, "DATA_FILE", str(tmp_path / "integrations.json"))


# --- path rejection in the store layer ---

def test_add_ntfy_integration_rejects_base_url_with_path(store):
    with pytest.raises(HTTPException) as exc:
        integrations.add_integration(
            {"preset": "ntfy", "name": "ntfy", "base_url": "http://ntfy/ithaka"}
        )
    assert exc.value.status_code == 400
    assert "path" in str(exc.value.detail).lower()
    assert integrations.load_integrations() == []


def test_add_ntfy_integration_accepts_bare_host(store):
    item = integrations.add_integration(
        {"preset": "ntfy", "name": "ntfy", "base_url": "http://ntfy"}
    )
    assert item["base_url"] == "http://ntfy"


def test_update_ntfy_integration_rejects_base_url_with_path(store):
    item = integrations.add_integration(
        {"preset": "ntfy", "name": "ntfy", "base_url": "http://ntfy"}
    )
    with pytest.raises(HTTPException) as exc:
        integrations.update_integration(item["id"], {"base_url": "https://ntfy.sh/ithaka"})
    assert exc.value.status_code == 400
    assert integrations.get_integration(item["id"])["base_url"] == "http://ntfy"


def test_non_ntfy_preset_still_allows_base_url_with_path(store):
    item = integrations.add_integration(
        {
            "preset": "discord_webhook",
            "name": "discord",
            "base_url": "https://discord.com/api/webhooks/123/abc",
        }
    )
    assert item["base_url"] == "https://discord.com/api/webhooks/123/abc"


# --- reachability probe helper ---

async def test_check_ntfy_reachable_reports_unreachable_host():
    err = await integrations.check_ntfy_reachable("http://127.0.0.1:1")
    assert err is not None
    assert "not reachable" in err


async def test_check_ntfy_reachable_ok_against_live_server():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"healthy":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        err = await integrations.check_ntfy_reachable(f"http://127.0.0.1:{port}")
        assert err is None
    finally:
        server.shutdown()
        thread.join(timeout=2)


# --- route wiring (save is refused when the probe fails) ---

@pytest.fixture
def integrations_routes(tmp_path, monkeypatch):
    from routes import auth_routes

    monkeypatch.setattr(integrations, "DATA_FILE", str(tmp_path / "integrations.json"))
    monkeypatch.setattr(auth_routes, "migrate_from_settings", lambda: None)

    class _AuthManager:
        def get_username_for_token(self, token):
            return "admin" if token == "session-token" else None

        def is_admin(self, user):
            return user == "admin"

    router = auth_routes.setup_auth_routes(_AuthManager())

    def endpoint(path, method):
        for route in router.routes:
            if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
                return route.endpoint
        raise AssertionError(f"{method} {path} route not registered")

    return endpoint, auth_routes


class _JsonRequest(SimpleNamespace):
    def __init__(self, body, session_cookie):
        super().__init__(
            cookies={session_cookie: "session-token"},
            client=SimpleNamespace(host="127.0.0.1"),
            _body=body,
        )

    async def json(self):
        return self._body


async def test_create_ntfy_route_rejects_unreachable_host(integrations_routes, monkeypatch):
    endpoint, auth_routes = integrations_routes

    async def fake_probe(base_url, timeout=4.0):
        return f"ntfy server at {base_url} is not reachable from the app"

    monkeypatch.setattr(auth_routes, "check_ntfy_reachable", fake_probe)
    create = endpoint("/api/auth/integrations", "POST")
    request = _JsonRequest(
        {"preset": "ntfy", "name": "ntfy", "base_url": "https://ithaka.example.ts.net:8443"},
        auth_routes.SESSION_COOKIE,
    )
    with pytest.raises(HTTPException) as exc:
        await create(request)
    assert exc.value.status_code == 400
    assert "not reachable" in str(exc.value.detail)
    assert integrations.load_integrations() == []


async def test_update_ntfy_route_accepts_reachable_host(integrations_routes, monkeypatch):
    endpoint, auth_routes = integrations_routes
    item = integrations.add_integration(
        {"preset": "ntfy", "name": "ntfy", "base_url": "http://ntfy"}
    )

    async def fake_probe(base_url, timeout=4.0):
        return None

    monkeypatch.setattr(auth_routes, "check_ntfy_reachable", fake_probe)
    update = endpoint("/api/auth/integrations/{integration_id}", "PUT")
    request = _JsonRequest({"base_url": "http://ntfy2"}, auth_routes.SESSION_COOKIE)
    result = await update(item["id"], request)
    assert result["ok"] is True
    assert integrations.get_integration(item["id"])["base_url"] == "http://ntfy2"


async def test_update_ntfy_route_rejects_unreachable_host(integrations_routes, monkeypatch):
    endpoint, auth_routes = integrations_routes
    item = integrations.add_integration(
        {"preset": "ntfy", "name": "ntfy", "base_url": "http://ntfy"}
    )

    async def fake_probe(base_url, timeout=4.0):
        return "ntfy server is not reachable from the app"

    monkeypatch.setattr(auth_routes, "check_ntfy_reachable", fake_probe)
    update = endpoint("/api/auth/integrations/{integration_id}", "PUT")
    request = _JsonRequest({"base_url": "http://elders"}, auth_routes.SESSION_COOKIE)
    with pytest.raises(HTTPException) as exc:
        await update(item["id"], request)
    assert exc.value.status_code == 400
    assert integrations.get_integration(item["id"])["base_url"] == "http://ntfy"
