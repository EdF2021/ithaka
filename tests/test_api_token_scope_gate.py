"""Bearer ``ody_`` API tokens must carry the ``chat`` scope to reach
owner-scoped session and upload routes (security finding F3).

The auth middleware (app.py) admits any valid ``ody_`` token and stamps
``request.state.api_token_scopes``, but session_routes/upload_routes resolved
the token straight to its owner via ``effective_user()`` without consulting
those scopes. A token scoped only to e.g. ``todos:read`` could therefore list,
read and delete the owner's entire chat history — and in ``download_file`` it
inherited the owner's ADMIN status through the ``is_admin(effective_user())``
bypass, letting any API token download every user's uploaded files.

The fix mirrors the existing enforcement in routes/codex_routes.py
(``_scope_owner``) and routes/model_routes.py (``GET /api/models``): bearer
tokens need the ``chat`` scope (routes/api_token_routes.py ALLOWED_SCOPES),
and the upload admin bypass keys off the HUMAN cookie identity only.

Fixtures mirror tests/test_session_list_owner_scope.py (real temp sqlite via
tests.helpers.sqlite_db) and tests/test_upload_routes_owner_scope.py (real
UploadHandler + direct endpoint calls).
"""
import asyncio
import json
import sys
import types
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import core.database as cdb
from core.database import Session as DbSession
from tests.helpers.sqlite_db import make_temp_sqlite

_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)


class _AuthManager:
    is_configured = True

    def __init__(self, admins=()):
        self._admins = set(admins)

    def is_admin(self, user):
        return user in self._admins


def _request(*, user=None, api_token=False, token_owner=None, scopes=None,
             auth_manager=None, body=None):
    """Request stub covering both cookie sessions and bearer-token callers.

    For ``api_token=True`` the middleware stamps ``current_user="api"`` and
    attaches owner + scopes — mirror that exactly.
    """
    req = SimpleNamespace(
        state=SimpleNamespace(
            current_user=("api" if api_token else user),
            api_token=api_token,
            api_token_owner=token_owner,
            api_token_scopes=list(scopes or []),
        ),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_manager)),
        client=SimpleNamespace(host="203.0.113.10"),
    )
    return req


def _stub_multipart_if_missing(monkeypatch):
    try:
        import python_multipart  # noqa: F401
        return
    except ImportError:
        pass
    stub = types.ModuleType("python_multipart")
    stub.__version__ = "0.0.20"
    monkeypatch.setitem(sys.modules, "python_multipart", stub)


# --- session routes ---------------------------------------------------------

def _session_endpoint(monkeypatch, session_manager, path, method):
    import routes.session_routes as sr

    _stub_multipart_if_missing(monkeypatch)
    monkeypatch.setattr(sr, "SessionLocal", _TS)
    # The module-level router accumulates routes across setup_session_routes
    # calls: search only the routes this call registered (so each test binds
    # its own session_manager), then truncate them again so other test files
    # that first-match on the shared router don't pick up our registrations.
    before = len(sr.router.routes)
    sr.setup_session_routes(session_manager, {})
    added = list(sr.router.routes[before:])
    del sr.router.routes[before:]
    return next(r.endpoint for r in added
                if getattr(r, "path", "") == path
                and method in getattr(r, "methods", set()))


def _seed_session(owner):
    sid = str(uuid.uuid4())
    db = _TS()
    try:
        db.query(DbSession).delete()
        db.add(DbSession(id=sid, owner=owner, name=f"{owner} session",
                         endpoint_url="http://localhost", model="gpt-4",
                         archived=False, is_important=False))
        db.commit()
    finally:
        db.close()
    return sid


def _session_manager_for(sid, owner):
    session = MagicMock(id=sid, name=f"{owner} session", model="gpt-4",
                        endpoint_url="http://localhost", rag=False,
                        archived=False, history=[])
    sm = MagicMock()
    sm.get_sessions_for_user.return_value = {sid: session}
    sm.get_session.return_value = session
    sm.delete_session.return_value = True
    return sm


def test_unscoped_token_gets_403_on_list_sessions(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    sid = _seed_session("admin")
    endpoint = _session_endpoint(monkeypatch, _session_manager_for(sid, "admin"),
                                 "/api/sessions", "GET")

    with pytest.raises(HTTPException) as exc:
        endpoint(request=_request(api_token=True, token_owner="admin",
                                  scopes=["todos:read"]))

    assert exc.value.status_code == 403


def test_unscoped_token_gets_403_on_session_history(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    sid = _seed_session("admin")
    endpoint = _session_endpoint(monkeypatch, _session_manager_for(sid, "admin"),
                                 "/api/history/{sid}", "GET")

    with pytest.raises(HTTPException) as exc:
        endpoint(request=_request(api_token=True, token_owner="admin",
                                  scopes=["todos:read"]), sid=sid)

    assert exc.value.status_code == 403


def test_unscoped_token_gets_403_on_session_delete(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    sid = _seed_session("admin")
    sm = _session_manager_for(sid, "admin")
    endpoint = _session_endpoint(monkeypatch, sm, "/api/session/{sid}", "DELETE")

    with pytest.raises(HTTPException) as exc:
        endpoint(request=_request(api_token=True, token_owner="admin",
                                  scopes=["todos:read"]), sid=sid)

    assert exc.value.status_code == 403
    sm.delete_session.assert_not_called()


def test_chat_scoped_token_still_lists_owner_sessions(monkeypatch):
    # Positive control: the companion/pairing flow mints "chat"-scoped tokens
    # (companion/pairing.py COMPANION_SCOPE) — those must keep working.
    monkeypatch.setenv("AUTH_ENABLED", "true")
    sid = _seed_session("alice")
    endpoint = _session_endpoint(monkeypatch, _session_manager_for(sid, "alice"),
                                 "/api/sessions", "GET")

    result = endpoint(request=_request(api_token=True, token_owner="alice",
                                       scopes=["chat"]))

    assert {s["id"] for s in result} == {sid}


def test_chat_scoped_token_still_reads_history(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    sid = _seed_session("alice")
    endpoint = _session_endpoint(monkeypatch, _session_manager_for(sid, "alice"),
                                 "/api/history/{sid}", "GET")

    result = endpoint(request=_request(api_token=True, token_owner="alice",
                                       scopes=["chat"]), sid=sid)

    assert result == {"history": []}


def test_cookie_user_still_lists_sessions(monkeypatch):
    # Positive control: browser/cookie sessions carry no bearer token and are
    # completely unaffected by the scope gate.
    monkeypatch.setenv("AUTH_ENABLED", "true")
    sid = _seed_session("admin")
    endpoint = _session_endpoint(monkeypatch, _session_manager_for(sid, "admin"),
                                 "/api/sessions", "GET")

    result = endpoint(request=_request(user="admin"))

    assert {s["id"] for s in result} == {sid}


def test_cookie_user_still_deletes_session(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    sid = _seed_session("admin")
    sm = _session_manager_for(sid, "admin")
    endpoint = _session_endpoint(monkeypatch, sm, "/api/session/{sid}", "DELETE")

    result = endpoint(request=_request(user="admin"), sid=sid)

    assert result == {"status": "deleted"}
    sm.delete_session.assert_called_once_with(sid)


# --- upload routes ----------------------------------------------------------
# Store fixture mirrors tests/test_upload_routes_owner_scope.py.

def _upload_endpoints(upload_handler, monkeypatch):
    import fastapi.dependencies.utils as dependency_utils
    from routes.upload_routes import router, setup_upload_routes

    monkeypatch.setattr(dependency_utils, "ensure_multipart_is_installed", lambda: None)
    before = len(router.routes)
    setup_upload_routes(upload_handler)
    routes = router.routes[before:]
    return {route.endpoint.__name__: route.endpoint for route in routes}


def _make_upload_store(tmp_path, monkeypatch):
    from src.upload_handler import UploadHandler
    from src import constants

    upload_dir = tmp_path / "uploads"
    dated = upload_dir / "2026" / "06" / "02"
    dated.mkdir(parents=True)

    alice_id = "a" * 32 + ".png"
    bob_id = "b" * 32 + ".png"
    alice_path = dated / alice_id
    bob_path = dated / bob_id
    alice_path.write_bytes(b"alice image bytes")
    bob_path.write_bytes(b"bob image bytes")

    index = {
        "alice:h1": {
            "id": alice_id,
            "path": str(alice_path),
            "mime": "image/png",
            "size": alice_path.stat().st_size,
            "name": "alice.png",
            "original_name": "alice.png",
            "owner": "alice",
        },
        "bob:h2": {
            "id": bob_id,
            "path": str(bob_path),
            "mime": "image/png",
            "size": bob_path.stat().st_size,
            "name": "bob.png",
            "original_name": "bob.png",
            "owner": "bob",
        },
    }
    (upload_dir / "uploads.json").write_text(json.dumps(index), encoding="utf-8")
    monkeypatch.setattr(constants, "UPLOAD_DIR", str(upload_dir))
    return UploadHandler(str(tmp_path), str(upload_dir)), alice_id, bob_id, upload_dir


def test_unscoped_token_cannot_download_any_file(tmp_path, monkeypatch):
    handler, _alice_id, bob_id, _upload_dir = _make_upload_store(tmp_path, monkeypatch)
    download_file = _upload_endpoints(handler, monkeypatch)["download_file"]

    with pytest.raises(HTTPException) as exc:
        asyncio.run(download_file(
            _request(api_token=True, token_owner="admin", scopes=["todos:read"],
                     auth_manager=_AuthManager(admins={"admin"})),
            bob_id,
        ))

    assert exc.value.status_code == 403


def test_chat_token_of_admin_owner_cannot_download_other_users_file(tmp_path, monkeypatch):
    # The core of F3: effective_user() resolves the token to its ADMIN owner,
    # which used to satisfy the is_admin() bypass — any token could download
    # every user's uploads. The bypass must key off the human cookie identity.
    handler, _alice_id, bob_id, _upload_dir = _make_upload_store(tmp_path, monkeypatch)
    download_file = _upload_endpoints(handler, monkeypatch)["download_file"]

    with pytest.raises(HTTPException) as exc:
        asyncio.run(download_file(
            _request(api_token=True, token_owner="admin", scopes=["chat"],
                     auth_manager=_AuthManager(admins={"admin"})),
            bob_id,
        ))

    assert exc.value.status_code == 404


def test_chat_token_still_downloads_token_owners_own_file(tmp_path, monkeypatch):
    # Positive control: a paired client ("chat" scope) still fetches the
    # owner's own attachments.
    handler, alice_id, _bob_id, _upload_dir = _make_upload_store(tmp_path, monkeypatch)
    download_file = _upload_endpoints(handler, monkeypatch)["download_file"]

    response = asyncio.run(download_file(
        _request(api_token=True, token_owner="alice", scopes=["chat"],
                 auth_manager=_AuthManager()),
        alice_id,
    ))

    assert response.path.endswith(alice_id)
    assert response.media_type == "image/png"


def test_cookie_admin_still_downloads_other_owner_upload(tmp_path, monkeypatch):
    # Positive control: the human admin bypass stays intact for cookie logins
    # (mirrors test_upload_routes_owner_scope.py).
    handler, _alice_id, bob_id, _upload_dir = _make_upload_store(tmp_path, monkeypatch)
    download_file = _upload_endpoints(handler, monkeypatch)["download_file"]

    response = asyncio.run(download_file(
        _request(user="admin", auth_manager=_AuthManager(admins={"admin"})),
        bob_id,
    ))

    assert response.path.endswith(bob_id)
    assert response.media_type == "image/png"


def test_chat_token_of_admin_owner_cannot_read_other_users_vision_text(tmp_path, monkeypatch):
    # Same admin-bypass hole existed in the vision cache read.
    handler, _alice_id, bob_id, upload_dir = _make_upload_store(tmp_path, monkeypatch)
    get_vision_text = _upload_endpoints(handler, monkeypatch)["get_vision_text"]
    cache_dir = upload_dir / ".vision"
    cache_dir.mkdir()
    (cache_dir / f"{bob_id}.txt").write_text("bob private cached text", encoding="utf-8")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_vision_text(
            _request(api_token=True, token_owner="admin", scopes=["chat"],
                     auth_manager=_AuthManager(admins={"admin"})),
            bob_id,
        ))

    assert exc.value.status_code == 404
