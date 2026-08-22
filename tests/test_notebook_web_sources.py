"""Notebook web sources (fase 4d): URL-ingest, search-endpoint, url-kolom.

Fixtures mirror tests/test_notebook_flashcards.py (temp sqlite via
make_temp_sqlite, monkeypatched nbr.SessionLocal / nbr.get_current_user,
FakeRagManager); the page fetch and the search provider are always faked —
no network in these tests.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebook-websources")

import uuid

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import routes.notebook_routes as nbr
from src.notebook_ingest import _filename_for_page, ingest_notebook_file, ingest_notebook_url
from tests.helpers.sqlite_db import make_temp_sqlite


class _FakeRagManager:
    def __init__(self):
        self.vector_rag = self

    def add_document(self, text, metadata):
        return True

    def remove_notebook(self, notebook_id, document_id=None):
        pass

    def _split_into_chunks(self, text):
        return [text]


@pytest.fixture()
def ts(monkeypatch):
    test_session_local, engine, tmpfile = make_temp_sqlite(db.Base.metadata)
    monkeypatch.setattr(nbr, "SessionLocal", test_session_local)
    yield test_session_local
    tmpfile.close()


def _client(monkeypatch, user="ed"):
    monkeypatch.setattr(nbr, "get_current_user", lambda request: user)
    app = FastAPI()
    app.include_router(nbr.setup_notebook_routes(rag_manager=_FakeRagManager()))
    return TestClient(app, raise_server_exceptions=False)


def _make_notebook_row(ts, owner="ed", name="NB"):
    s = ts()
    try:
        nb = db.Notebook(id=str(uuid.uuid4()), owner=owner, name=name)
        s.add(nb)
        s.commit()
        return nb.id
    finally:
        s.close()


# ---- filename helper ---------------------------------------------------

def test_filename_for_page_sanitizes_title():
    assert _filename_for_page("SkillsRadar — welk probleem?", "https://x.nl") == "SkillsRadar welk probleem.md"


def test_filename_for_page_falls_back_to_domain():
    assert _filename_for_page("", "https://ceda.nl/pagina").endswith(".md")


def test_filename_for_page_caps_length():
    name = _filename_for_page("x" * 300, "https://x.nl")
    assert len(name) <= 84


# ---- ingest_notebook_url ----------------------------------------------

def _fetch_ok(url):
    return {"success": True, "title": "Testpagina", "url": url,
            "content": "Inhoud over studiesucces en skills."}


def test_ingest_url_success_creates_indexed_source_with_url(ts):
    nb_id = _make_notebook_row(ts)
    s = ts()
    try:
        src = ingest_notebook_url(nb_id, "ed", "https://example.org/artikel",
                                  _FakeRagManager(), s, fetcher=_fetch_ok)
        assert src.status == "indexed"
        assert src.url == "https://example.org/artikel"
        assert src.filename == "Testpagina.md"
        doc = s.get(db.Document, src.document_id)
        assert "Bron: https://example.org/artikel" in doc.current_content
        assert "Inhoud over studiesucces" in doc.current_content
        assert src.to_dict()["url"] == "https://example.org/artikel"
    finally:
        s.close()


def test_ingest_url_fetch_failure_becomes_failed_source(ts):
    nb_id = _make_notebook_row(ts)
    s = ts()
    try:
        src = ingest_notebook_url(
            nb_id, "ed", "https://example.org/kapot", _FakeRagManager(), s,
            fetcher=lambda url: {"success": False, "error": "HTTP 500"})
        assert src.status == "failed"
        assert "HTTP 500" in src.error
        assert src.url == "https://example.org/kapot"
        assert src.document_id is None
    finally:
        s.close()


def test_ingest_url_empty_content_becomes_failed_source(ts):
    nb_id = _make_notebook_row(ts)
    s = ts()
    try:
        src = ingest_notebook_url(
            nb_id, "ed", "https://example.org/leeg", _FakeRagManager(), s,
            fetcher=lambda url: {"success": True, "title": "Leeg", "content": "  "})
        assert src.status == "failed"
        assert "no extractable text" in src.error
    finally:
        s.close()


def test_ingest_url_fetcher_exception_becomes_failed_source(ts):
    nb_id = _make_notebook_row(ts)

    def _boom(url):
        raise ValueError("private address blocked")

    s = ts()
    try:
        src = ingest_notebook_url(nb_id, "ed", "https://10.0.0.1/x",
                                  _FakeRagManager(), s, fetcher=_boom)
        assert src.status == "failed"
        assert "private address blocked" in src.error
    finally:
        s.close()


def test_ingest_file_leaves_url_null(ts):
    nb_id = _make_notebook_row(ts)
    s = ts()
    try:
        src = ingest_notebook_file(nb_id, "ed", "notes.md", b"# Notes\n\ntekst",
                                   _FakeRagManager(), s)
        assert src.status == "indexed"
        assert src.url is None
        assert src.to_dict()["url"] is None
    finally:
        s.close()


# ---- migration ---------------------------------------------------------

def test_migrate_add_notebook_source_url_column(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE notebook_sources (
            id TEXT PRIMARY KEY,
            notebook_id TEXT NOT NULL,
            document_id TEXT,
            filename TEXT NOT NULL,
            status TEXT NOT NULL,
            chunk_count INTEGER NOT NULL,
            error TEXT,
            created_at DATETIME,
            updated_at DATETIME
        );
        INSERT INTO notebook_sources(id, notebook_id, filename, status, chunk_count)
        VALUES ('s1', 'n1', 'a.md', 'indexed', 1);
        """
    )
    conn.close()

    monkeypatch.setattr(db, "DATABASE_URL", f"sqlite:///{db_path}")
    db._migrate_add_notebook_source_url_column()

    conn = sqlite3.connect(db_path)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(notebook_sources)")]
        assert "url" in columns
        assert conn.execute("SELECT url FROM notebook_sources WHERE id='s1'").fetchone() == (None,)
    finally:
        conn.close()

    db._migrate_add_notebook_source_url_column()  # idempotent


def test_migrate_url_column_missing_db_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_URL", f"sqlite:///{tmp_path / 'missing.db'}")
    db._migrate_add_notebook_source_url_column()


# ---- routes ------------------------------------------------------------

def _make_notebook(c, name="NB"):
    return c.post("/api/notebooks", json={"name": name}).json()["id"]


def test_route_add_url_rejects_non_http_schemes(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    for bad in ("ftp://x.nl/a", "file:///etc/passwd", "javascript:alert(1)", ""):
        r = c.post(f"/api/notebooks/{nb_id}/sources/url", json={"url": bad})
        assert r.status_code == 400, bad


def test_route_add_url_ingests_via_fetcher(monkeypatch, ts):
    import services.search.content as content_mod
    monkeypatch.setattr(content_mod, "fetch_webpage_content", _fetch_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/sources/url",
               json={"url": "https://example.org/artikel"})
    assert r.status_code == 200
    src = r.json()["source"]
    assert src["status"] == "indexed"
    assert src["url"] == "https://example.org/artikel"

    listing = c.get(f"/api/notebooks/{nb_id}/sources").json()["sources"]
    assert any(s["url"] == "https://example.org/artikel" for s in listing)


def test_route_add_url_foreign_notebook_404(monkeypatch, ts):
    c_ed = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c_ed)
    c_eve = _client(monkeypatch, user="eve")
    r = c_eve.post(f"/api/notebooks/{nb_id}/sources/url",
                   json={"url": "https://example.org"})
    assert r.status_code == 404


def test_route_source_search_returns_slim_results(monkeypatch, ts):
    import services.search.core as core_mod
    monkeypatch.setattr(core_mod, "searxng_search_results", lambda q: [
        {"title": "Res 1", "url": "https://a.nl/1", "snippet": "x" * 500},
        {"title": None, "url": "https://b.nl/2", "snippet": "kort"},
        {"title": "geen url", "url": None},
    ])
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/source-search", json={"query": "skills"})
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 2  # url-less result dropped
    assert results[0]["title"] == "Res 1"
    assert len(results[0]["snippet"]) <= 300
    assert results[1]["title"] == "https://b.nl/2"  # title falls back to url


def test_route_source_search_empty_query_400(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    r = c.post(f"/api/notebooks/{nb_id}/source-search", json={"query": "  "})
    assert r.status_code == 400


def test_route_source_search_provider_failure_502(monkeypatch, ts):
    import services.search.core as core_mod

    def _boom(q):
        raise RuntimeError("searxng down")

    monkeypatch.setattr(core_mod, "searxng_search_results", _boom)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    r = c.post(f"/api/notebooks/{nb_id}/source-search", json={"query": "skills"})
    assert r.status_code == 502
