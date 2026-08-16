"""MCP connector preset catalog: shape validation + admin-gated route.

Presets seed the "Add MCP Server" form (static/js/settings.js) with known
server configs. Every preset must declare a transport-appropriate set of
fields (stdio needs a command, http/sse need a url), have a unique id, and
never carry a real secret in its env defaults — env values must be empty or
placeholder-shaped (e.g. "<TOKEN>").
"""
from unittest.mock import MagicMock

import pytest

from src.mcp_presets import get_presets


def _is_placeholder(value: str) -> bool:
    """Env values must be empty or an obvious placeholder, never a real secret."""
    if value == "":
        return True
    return value.startswith("<") and value.endswith(">")


def test_presets_have_unique_ids():
    presets = get_presets()
    ids = [p["id"] for p in presets]
    assert len(ids) == len(set(ids))
    assert len(ids) >= 14  # migrated from admin.js MCP_PRESETS + hosted-HTTP additions


def test_presets_have_valid_transport_field_combos():
    presets = get_presets()
    for p in presets:
        transport = p.get("transport")
        assert transport in ("stdio", "sse", "http"), p["id"]
        if transport == "stdio":
            assert p.get("command"), f"{p['id']}: stdio preset needs a command"
        else:
            assert p.get("url"), f"{p['id']}: {transport} preset needs a url"


def test_presets_env_values_are_placeholders_not_secrets():
    presets = get_presets()
    for p in presets:
        env = p.get("env") or {}
        for key, value in env.items():
            assert _is_placeholder(value), (
                f"{p['id']}.env[{key!r}] = {value!r} does not look like a placeholder"
            )


def test_presets_have_required_fields():
    presets = get_presets()
    for p in presets:
        assert p.get("id")
        assert p.get("name")
        assert isinstance(p.get("args"), list)
        assert isinstance(p.get("env"), dict)


def test_get_presets_returns_a_copy_not_shared_state():
    a = get_presets()
    a[0]["name"] = "mutated"
    b = get_presets()
    assert b[0]["name"] != "mutated"


def test_presets_route_is_admin_gated_and_returns_catalog(monkeypatch):
    import routes.mcp_routes as mr

    calls = []
    monkeypatch.setattr(mr, "require_admin", lambda request: calls.append(request))

    router = mr.setup_mcp_routes(MagicMock())
    endpoint = next(
        r.endpoint for r in router.routes
        if getattr(r, "path", "") == "/api/mcp/presets"
        and "GET" in getattr(r, "methods", set())
    )

    request = MagicMock()
    result = endpoint(request=request)

    assert calls == [request]  # require_admin was invoked with the request
    assert isinstance(result, list)
    assert len(result) == len(get_presets())
    assert {p["id"] for p in result} == {p["id"] for p in get_presets()}


def test_presets_route_denies_when_require_admin_raises(monkeypatch):
    from fastapi import HTTPException
    import routes.mcp_routes as mr

    def _deny(request):
        raise HTTPException(403, "not admin")

    monkeypatch.setattr(mr, "require_admin", _deny)

    router = mr.setup_mcp_routes(MagicMock())
    endpoint = next(
        r.endpoint for r in router.routes
        if getattr(r, "path", "") == "/api/mcp/presets"
        and "GET" in getattr(r, "methods", set())
    )

    with pytest.raises(HTTPException) as exc_info:
        endpoint(request=MagicMock())
    assert exc_info.value.status_code == 403
