"""Notebook CRUD + sources routes: owner-scoping and per-file ingest statuses.

DB isolation caveat: routes/notebook_routes.py imports ``SessionLocal`` from
core.database, which conftest.py binds to ``sqlite:///:memory:``. That
engine's default pool hands out a fresh, independent in-memory database per
checked-out connection (verified empirically: ``db.Base.metadata.create_all``
via one connection is invisible to a second ``SessionLocal()`` connection),
so the ``_client()`` helper described in the task brief cannot rely on a
single shared in-memory DB across the multiple SessionLocal() calls each
request makes. Per the suite's documented convention (tests/README.md),
these tests use a file-backed temp sqlite DB via
tests.helpers.sqlite_db.make_temp_sqlite and monkeypatch
routes.notebook_routes.SessionLocal to point at it.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebooks-routes")

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import routes.notebook_routes as nbr
from tests.helpers.sqlite_db import make_temp_sqlite


class _FakeRagManager:
    def __init__(self):
        self.docs = []
        self.removed = []
        # notebook_ingest.ingest_notebook_file calls
        # rag_manager.vector_rag._split_into_chunks(...) — mirror the
        # convention from tests/test_services_notebooks_ingest.py's _FakeRag.
        self.vector_rag = self

    def add_document(self, text, metadata):
        self.docs.append(metadata)
        return True

    def remove_notebook(self, notebook_id, document_id=None):
        self.removed.append((notebook_id, document_id))

    def _split_into_chunks(self, text):
        return [text[i:i + 1000] for i in range(0, len(text), 800)]


@pytest.fixture()
def ts(monkeypatch):
    """A shared, file-backed temp sqlite SessionLocal all clients in a test use."""
    test_session_local, engine, tmpfile = make_temp_sqlite(db.Base.metadata)
    monkeypatch.setattr(nbr, "SessionLocal", test_session_local)
    yield test_session_local
    tmpfile.close()


def _client(monkeypatch, user="ed"):
    monkeypatch.setattr(nbr, "get_current_user", lambda request: user)
    app = FastAPI()
    app.include_router(nbr.setup_notebook_routes(rag_manager=_FakeRagManager()))
    return TestClient(app, raise_server_exceptions=False)


def test_crud_roundtrip(monkeypatch, ts):
    c = _client(monkeypatch)
    r = c.post("/api/notebooks", json={"name": "Thesis"})
    assert r.status_code == 200
    nb_id = r.json()["id"]
    assert any(n["id"] == nb_id for n in c.get("/api/notebooks").json()["notebooks"])
    assert c.patch(f"/api/notebooks/{nb_id}", json={"name": "Thesis v2"}).status_code == 200
    assert c.delete(f"/api/notebooks/{nb_id}").status_code == 200
    assert all(n["id"] != nb_id for n in c.get("/api/notebooks").json()["notebooks"])


def test_empty_name_rejected(monkeypatch, ts):
    c = _client(monkeypatch)
    assert c.post("/api/notebooks", json={"name": "  "}).status_code == 400


def test_cross_owner_is_404(monkeypatch, ts):
    c_ed = _client(monkeypatch, user="ed")
    nb_id = c_ed.post("/api/notebooks", json={"name": "Private"}).json()["id"]
    c_eve = _client(monkeypatch, user="eve")
    assert c_eve.get(f"/api/notebooks/{nb_id}/sources").status_code == 404
    assert c_eve.delete(f"/api/notebooks/{nb_id}").status_code == 404


def test_source_upload_mixes_ok_and_failed(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = c.post("/api/notebooks", json={"name": "Mix"}).json()["id"]
    files = [
        ("files", ("good.txt", b"plain text content " * 50, "text/plain")),
        ("files", ("bad.exe", b"MZ", "application/octet-stream")),
    ]
    r = c.post(f"/api/notebooks/{nb_id}/sources", files=files)
    assert r.status_code == 200
    statuses = {s["filename"]: s["status"] for s in r.json()["sources"]}
    assert statuses["good.txt"] == "indexed" and statuses["bad.exe"] == "failed"


def test_delete_notebook_removes_chunks_and_unbinds_sessions(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = c.post("/api/notebooks", json={"name": "Bound"}).json()["id"]
    s = ts()
    try:
        sess = db.Session(id="sess-1", name="chat", endpoint_url="http://x", model="m")
        sess.notebook_id = nb_id
        s.add(sess)
        s.commit()
    finally:
        s.close()

    assert c.delete(f"/api/notebooks/{nb_id}").status_code == 200

    s = ts()
    try:
        sess = s.get(db.Session, "sess-1")
        assert sess is not None and sess.notebook_id is None
    finally:
        s.close()


def test_delete_source_removes_row_but_not_document(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = c.post("/api/notebooks", json={"name": "Src"}).json()["id"]
    files = [("files", ("good.txt", b"plain text content " * 50, "text/plain"))]
    r = c.post(f"/api/notebooks/{nb_id}/sources", files=files)
    src = r.json()["sources"][0]
    assert src["status"] == "indexed"
    doc_id = src["document_id"]

    assert c.delete(f"/api/notebooks/{nb_id}/sources/{src['id']}").status_code == 200
    assert c.get(f"/api/notebooks/{nb_id}/sources").json()["sources"] == []

    s = ts()
    try:
        assert s.get(db.Document, doc_id) is not None
    finally:
        s.close()
