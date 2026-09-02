"""POST /api/auth/settings must probe a newly-selected `endpoint:<id>` STT
provider before persisting it (see services/stt/stt_service.probe_endpoint
and the 2026-09-02 incident this guards against: an STT endpoint with no
/audio/transcriptions route saved silently and 500'd every voice-mode turn).
"""
from types import SimpleNamespace

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import HTTPException

from src import settings as settings_module


@pytest.fixture
def settings_routes(tmp_path, monkeypatch):
    from routes import auth_routes

    monkeypatch.setattr(settings_module, "SETTINGS_FILE", str(tmp_path / "settings.json"))
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


async def test_new_endpoint_provider_rejected_when_probe_fails(settings_routes, monkeypatch):
    endpoint, auth_routes = settings_routes

    async def fake_probe(endpoint_id, model, timeout=10.0):
        return False, "endpoint returned 404 — no /audio/transcriptions route (not a transcription-capable API)"

    monkeypatch.setattr(auth_routes, "probe_stt_endpoint", fake_probe)
    set_settings = endpoint("/api/auth/settings", "POST")
    request = _JsonRequest(
        {"stt_provider": "endpoint:gemini", "stt_model": "whisper-1"},
        auth_routes.SESSION_COOKIE,
    )

    with pytest.raises(HTTPException) as exc:
        await set_settings(request)

    assert exc.value.status_code == 400
    assert "404" in str(exc.value.detail)
    saved = settings_module.load_settings()
    assert saved["stt_provider"] == "disabled"  # unchanged (default)


async def test_new_endpoint_provider_saved_when_probe_succeeds(settings_routes, monkeypatch):
    endpoint, auth_routes = settings_routes

    calls = []

    async def fake_probe(endpoint_id, model, timeout=10.0):
        calls.append((endpoint_id, model))
        return True, ""

    monkeypatch.setattr(auth_routes, "probe_stt_endpoint", fake_probe)
    set_settings = endpoint("/api/auth/settings", "POST")
    request = _JsonRequest(
        {"stt_provider": "endpoint:openai", "stt_model": "whisper-1", "stt_enabled": True},
        auth_routes.SESSION_COOKIE,
    )

    result = await set_settings(request)

    assert result["stt_provider"] == "endpoint:openai"
    assert calls == [("openai", "whisper-1")]
    saved = settings_module.load_settings()
    assert saved["stt_provider"] == "endpoint:openai"


async def test_probe_timeout_rejects_save(settings_routes, monkeypatch):
    endpoint, auth_routes = settings_routes

    async def fake_probe(endpoint_id, model, timeout=10.0):
        return False, f"endpoint timed out after {timeout:.0f}s — check base_url and network reachability"

    monkeypatch.setattr(auth_routes, "probe_stt_endpoint", fake_probe)
    set_settings = endpoint("/api/auth/settings", "POST")
    request = _JsonRequest(
        {"stt_provider": "endpoint:slow", "stt_model": "whisper-1"},
        auth_routes.SESSION_COOKIE,
    )

    with pytest.raises(HTTPException) as exc:
        await set_settings(request)

    assert exc.value.status_code == 400
    assert "timed out" in str(exc.value.detail)


async def test_stt_skip_probe_bypasses_network_call(settings_routes, monkeypatch):
    endpoint, auth_routes = settings_routes

    called = False

    async def fake_probe(endpoint_id, model, timeout=10.0):
        nonlocal called
        called = True
        return False, "should not be called"

    monkeypatch.setattr(auth_routes, "probe_stt_endpoint", fake_probe)
    set_settings = endpoint("/api/auth/settings", "POST")
    request = _JsonRequest(
        {"stt_provider": "endpoint:offline", "stt_model": "whisper-1", "stt_skip_probe": True},
        auth_routes.SESSION_COOKIE,
    )

    result = await set_settings(request)

    assert called is False
    assert result["stt_provider"] == "endpoint:offline"
    # stt_skip_probe is a one-off request flag, not a persisted setting
    assert "stt_skip_probe" not in result


async def test_unchanged_provider_is_not_reprobed(settings_routes, monkeypatch):
    endpoint, auth_routes = settings_routes
    # Seed an already-saved endpoint provider directly on disk.
    current = settings_module.load_settings()
    current["stt_provider"] = "endpoint:openai"
    current["stt_enabled"] = True
    settings_module.save_settings(current)

    called = False

    async def fake_probe(endpoint_id, model, timeout=10.0):
        nonlocal called
        called = True
        return True, ""

    monkeypatch.setattr(auth_routes, "probe_stt_endpoint", fake_probe)
    set_settings = endpoint("/api/auth/settings", "POST")
    # Re-saving the same provider, only tweaking an unrelated setting.
    request = _JsonRequest(
        {"stt_provider": "endpoint:openai", "stt_model": "whisper-1", "theme": "dark"},
        auth_routes.SESSION_COOKIE,
    )

    result = await set_settings(request)

    assert called is False
    assert result["stt_provider"] == "endpoint:openai"


async def test_non_endpoint_provider_is_never_probed(settings_routes, monkeypatch):
    endpoint, auth_routes = settings_routes

    called = False

    async def fake_probe(endpoint_id, model, timeout=10.0):
        nonlocal called
        called = True
        return True, ""

    monkeypatch.setattr(auth_routes, "probe_stt_endpoint", fake_probe)
    set_settings = endpoint("/api/auth/settings", "POST")
    request = _JsonRequest(
        {"stt_provider": "local", "stt_model": "base"},
        auth_routes.SESSION_COOKIE,
    )

    result = await set_settings(request)

    assert called is False
    assert result["stt_provider"] == "local"
