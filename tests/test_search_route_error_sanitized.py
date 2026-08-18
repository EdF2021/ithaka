"""POST /api/search must not leak raw exception text to the client.

Raw provider exceptions can contain URLs or key fragments; the route logs
the detail and returns an actionable, human-readable error instead.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.search_routes as search_routes
from routes.search_routes import setup_search_routes


def _client(monkeypatch, exc: Exception) -> TestClient:
    def _boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(search_routes, "comprehensive_web_search", _boom)
    app = FastAPI()
    app.include_router(setup_search_routes(config=None))
    return TestClient(app)


def test_search_error_is_sanitized_and_actionable(monkeypatch):
    client = _client(monkeypatch, RuntimeError("401 key sk-secret at https://api.example.com"))
    resp = client.post("/api/search", json={"query": "test"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"] == []
    assert "sk-secret" not in body["error"]
    assert "api.example.com" not in body["error"]
    assert "Settings" in body["error"]


def test_search_empty_query_still_reports_required():
    app = FastAPI()
    app.include_router(setup_search_routes(config=None))
    client = TestClient(app)
    resp = client.post("/api/search", json={"query": ""})
    assert resp.json()["error"] == "query is required"
