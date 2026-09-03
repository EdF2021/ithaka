# tests/test_routes_realtime.py
"""routes/realtime_routes.py — POST /api/realtime/session. See
docs/superpowers/plans/2026-09-03-realtime-voice-mode.md, Task 3."""

import pytest
from fastapi import HTTPException


def _get_endpoint(router, path, method="POST"):
    return next(
        r.endpoint for r in router.routes
        if getattr(r, "path", "") == path and method in getattr(r, "methods", set())
    )


class _FakeRealtimeServiceOk:
    def create_session(self):
        return {
            "client_secret": "ek_abc123",
            "expires_at": 1234567890,
            "max_minutes": 10,
            "model": "gpt-realtime-2.1-mini",
        }


class _FakeRealtimeServiceDisabled:
    def create_session(self):
        raise ValueError("Realtime-gesprek staat uit")


class _FakeRealtimeServiceBoom:
    def create_session(self):
        raise RuntimeError("unexpected crash")


async def test_session_route_returns_client_secret_never_raw_key():
    from routes.realtime_routes import setup_realtime_routes

    router = setup_realtime_routes(_FakeRealtimeServiceOk())
    endpoint = _get_endpoint(router, "/api/realtime/session")

    result = await endpoint()

    assert result["client_secret"] == "ek_abc123"
    assert result["max_minutes"] == 10
    assert "api_key" not in result


async def test_session_route_maps_value_error_to_400():
    from routes.realtime_routes import setup_realtime_routes

    router = setup_realtime_routes(_FakeRealtimeServiceDisabled())
    endpoint = _get_endpoint(router, "/api/realtime/session")

    with pytest.raises(HTTPException) as exc:
        await endpoint()

    assert exc.value.status_code == 400
    assert exc.value.detail == {"message": "Realtime-gesprek staat uit"}


async def test_session_route_maps_unexpected_error_to_500():
    from routes.realtime_routes import setup_realtime_routes

    router = setup_realtime_routes(_FakeRealtimeServiceBoom())
    endpoint = _get_endpoint(router, "/api/realtime/session")

    with pytest.raises(HTTPException) as exc:
        await endpoint()

    assert exc.value.status_code == 500
