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


class _Req:
    """Minimal Request stand-in: json() body + state for effective_user."""

    def __init__(self, body):
        self._body = body
        self.state = type("S", (), {"current_user": "ed"})()

    async def json(self):
        return self._body


def _wire_ask(monkeypatch, *, enabled=True, tools=True, answer=None, raises=None):
    import routes.realtime_routes as rr
    monkeypatch.setattr(rr, "effective_user", lambda request: "ed")
    monkeypatch.setattr(
        rr, "get_setting",
        lambda key, default=None, owner=None: {"realtime_enabled": enabled, "realtime_tools_enabled": tools}.get(key, default),
    )
    seen = {}

    async def _answer(question, owner):
        seen["question"], seen["owner"] = question, owner
        if raises:
            raise raises
        return answer
    monkeypatch.setattr(rr, "answer_question", _answer)
    return seen


async def test_ask_route_returns_answer(monkeypatch):
    from routes.realtime_routes import setup_realtime_routes
    seen = _wire_ask(monkeypatch, answer="Het is 18 graden.")
    endpoint = _get_endpoint(setup_realtime_routes(_FakeRealtimeServiceOk()), "/api/realtime/ask")
    out = await endpoint(_Req({"question": "  Wat is het weer?  ", "call_id": "call_1"}))
    assert out == {"answer": "Het is 18 graden."}
    assert seen == {"question": "Wat is het weer?", "owner": "ed"}


async def test_ask_route_400_on_empty_question(monkeypatch):
    from routes.realtime_routes import setup_realtime_routes
    _wire_ask(monkeypatch, answer="x")
    endpoint = _get_endpoint(setup_realtime_routes(_FakeRealtimeServiceOk()), "/api/realtime/ask")
    with pytest.raises(HTTPException) as ei:
        await endpoint(_Req({"question": "   "}))
    assert ei.value.status_code == 400


async def test_ask_route_400_when_tools_disabled(monkeypatch):
    from routes.realtime_routes import setup_realtime_routes
    _wire_ask(monkeypatch, tools=False, answer="x")
    endpoint = _get_endpoint(setup_realtime_routes(_FakeRealtimeServiceOk()), "/api/realtime/ask")
    with pytest.raises(HTTPException) as ei:
        await endpoint(_Req({"question": "hoi"}))
    assert ei.value.status_code == 400
    assert "Realtime-tools staan uit" in ei.value.detail["message"]


async def test_ask_route_400_on_dutch_runtime_error(monkeypatch):
    from routes.realtime_routes import setup_realtime_routes
    _wire_ask(monkeypatch, raises=RuntimeError("Het opzoeken duurde te lang"))
    endpoint = _get_endpoint(setup_realtime_routes(_FakeRealtimeServiceOk()), "/api/realtime/ask")
    with pytest.raises(HTTPException) as ei:
        await endpoint(_Req({"question": "hoi"}))
    assert ei.value.status_code == 400
    assert ei.value.detail == {"message": "Het opzoeken duurde te lang"}


async def test_ask_route_500_generic_on_unexpected(monkeypatch):
    from routes.realtime_routes import setup_realtime_routes
    _wire_ask(monkeypatch, raises=KeyError("boom"))
    endpoint = _get_endpoint(setup_realtime_routes(_FakeRealtimeServiceOk()), "/api/realtime/ask")
    with pytest.raises(HTTPException) as ei:
        await endpoint(_Req({"question": "hoi"}))
    assert ei.value.status_code == 500
    assert "boom" not in str(ei.value.detail)


def test_ask_route_is_exempt_from_hard_timeout():
    # C1: the 45s _RequestTimeoutMiddleware pre-empts answer_question's own
    # 60s ASK_TIMEOUT_S unless this route is exempt — the Dutch timeout
    # message would otherwise never be reachable in production.
    import re
    src = open("app.py", encoding="utf-8").read()
    # Non-greedy up to a ")" that starts its own line: several entries carry
    # a trailing comment with a "(...)" aside (e.g. the /api/image line),
    # so `\)` alone would stop at the first of those instead of the tuple's
    # actual close.
    block = re.search(r"_TIMEOUT_EXEMPT_PREFIXES = \((.*?)\n\)", src, re.S).group(1)
    assert '"/api/realtime/ask"' in block
