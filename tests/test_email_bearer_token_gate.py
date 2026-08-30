"""Bearer API tokens must never reach the direct /api/email/* routes.

Middleware (app.py ~line 414-462) resolves any valid bearer ``ody_`` token to
the shared "api" pseudo-user (``request.state.current_user = "api"``,
``request.state.api_token = True``); its scopes aren't visible to
``routes/email_helpers.py``'s ``_require_auth`` (used by ``require_owner`` /
``require_user``). Scope enforcement belongs in the scope-gated
``/api/codex/*`` proxy (see ``routes/codex_routes.py`` ``_scope_owner``),
so these direct routes must reject bearer-token callers outright -- mirrors
the same gate already enforced by ``src.auth_helpers.require_user`` for the
equivalent "general" user routes.
"""

import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.params import Depends

from routes import email_helpers


def _request(*, current_user="api", api_token=True, auth_configured=True, host="203.0.113.10"):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=current_user, api_token=api_token),
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_manager=SimpleNamespace(is_configured=auth_configured),
            ),
        ),
        client=SimpleNamespace(host=host),
    )


def _route_endpoint(router, path: str, method: str):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


# ── Core gate: _require_auth / require_owner / require_user ────────────────

def test_require_auth_rejects_bearer_token(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    req = _request()
    with pytest.raises(HTTPException) as exc:
        email_helpers._require_auth(req)
    assert exc.value.status_code == 403


def test_require_owner_rejects_bearer_token(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    req = _request()
    with pytest.raises(HTTPException) as exc:
        email_helpers.require_owner(req)
    assert exc.value.status_code == 403


def test_require_user_rejects_bearer_token(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    req = _request()
    with pytest.raises(HTTPException) as exc:
        email_helpers.require_user(req)
    assert exc.value.status_code == 403


def test_require_auth_rejects_bearer_token_even_when_auth_disabled(monkeypatch):
    """The gate fires before the AUTH_ENABLED=false fallback -- a bearer
    token is never a legitimate direct-email-route caller regardless of the
    operator's auth mode."""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    req = _request()
    with pytest.raises(HTTPException) as exc:
        email_helpers._require_auth(req)
    assert exc.value.status_code == 403


def test_require_auth_rejects_bearer_token_in_unconfigured_loopback_mode(monkeypatch):
    """Nor does the first-run/loopback fallback rescue a bearer-token caller."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    req = _request(auth_configured=False, host="127.0.0.1")
    with pytest.raises(HTTPException) as exc:
        email_helpers._require_auth(req)
    assert exc.value.status_code == 403


# ── Route-level: POST /api/email/accounts is wired to the gate ─────────────

@pytest.mark.asyncio
async def test_create_email_account_dependency_rejects_bearer_token(monkeypatch):
    """The endpoint's own Depends(require_owner) -- the thing FastAPI would
    invoke before the handler body ever runs -- rejects a bearer-token
    request state."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    import routes.email_routes as email_routes

    router = email_routes.setup_email_routes()
    create = _route_endpoint(router, "/api/email/accounts", "POST")

    owner_param = inspect.signature(create).parameters["owner"]
    assert isinstance(owner_param.default, Depends)
    assert owner_param.default.dependency is email_helpers.require_owner

    req = _request()
    with pytest.raises(HTTPException) as exc:
        email_helpers.require_owner(req, account_id=None)
    assert exc.value.status_code == 403


# ── Codex proxy path stays intact ───────────────────────────────────────────

def test_codex_as_owner_restores_owner_resolution_for_email_helpers(monkeypatch):
    """`_as_owner` clears request.state.api_token before delegating, so the
    scope-gated /api/codex/* proxy keeps working through this same gate."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    req = _request()

    # Simulate what _as_owner does: impersonate the resolved token owner and
    # clear the bearer-token flag for the duration of the borrowed call.
    req.state.current_user = "alice"
    req.state.api_token = False

    assert email_helpers.require_owner(req, account_id=None) == "alice"
    assert email_helpers.require_user(req) == "alice"


# ── Cookie-session behavior is unchanged ────────────────────────────────────

def test_require_auth_allows_normal_cookie_session(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    req = _request(current_user="bob", api_token=False)
    assert email_helpers._require_auth(req) == "bob"


def test_require_owner_allows_normal_cookie_session(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    req = _request(current_user="bob", api_token=False)
    assert email_helpers.require_owner(req, account_id=None) == "bob"
