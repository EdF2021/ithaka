"""Notebook artifacts routes: CRUD, owner-scoping, kind/source-error mapping.

Pattern copied from tests/test_routes_notebooks.py — same file-backed temp
sqlite rationale (routes.notebook_routes.SessionLocal must be monkeypatched
per-test, since the default in-memory sqlite engine hands out an independent
DB per connection).

``generate_artifact`` is monkeypatched at the route-module level
(``routes.notebook_routes.generate_artifact``) so these tests never touch the
real LLM/endpoint chain. The fakes below still write real Document +
NotebookArtifact rows so the GET-list title-join and DELETE-cascade behavior
can be verified against real data.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ITHAKA_DATA_DIR", "/tmp/ithaka-test-notebook-artifacts-routes")

import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from starlette.testclient import TestClient

import core.database as db
import routes.notebook_routes as nbr
from tests.helpers.sqlite_db import make_temp_sqlite


class _FakeRagManager:
    def __init__(self):
        self.removed = []
        # notebook_ingest.ingest_notebook_file calls
        # rag_manager.vector_rag._split_into_chunks(...) — mirror the
        # convention from tests/test_routes_notebooks.py's _FakeRagManager.
        self.vector_rag = self

    def add_document(self, text, metadata):
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


async def _fake_generate_artifact_ok(notebook_id, owner, kind, db_session, focus=None):
    """Mirror the real generate_artifact's row-writing shape, minus the LLM call."""
    document_id = str(uuid.uuid4())
    db_session.add(db.Document(
        id=document_id, title=f"Gegenereerd — {kind}", owner=owner,
        language="markdown", current_content="# inhoud", session_id=None,
    ))
    artifact = db.NotebookArtifact(
        id=str(uuid.uuid4()), notebook_id=notebook_id, document_id=document_id, kind=kind,
    )
    db_session.add(artifact)
    db_session.commit()
    db_session.refresh(artifact)
    return artifact


async def _fake_generate_artifact_no_sources(notebook_id, owner, kind, db_session, focus=None):
    raise ValueError("Geen geïndexeerde bronnen")


async def _fake_generate_artifact_llm_failure(notebook_id, owner, kind, db_session, focus=None):
    raise RuntimeError("Het model gaf een leeg antwoord terug")


def _make_notebook(c, name="NB"):
    return c.post("/api/notebooks", json={"name": name}).json()["id"]


# ---- POST (generate) ----

def test_generate_artifact_creates_document_and_row(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"})
    assert r.status_code == 200
    body = r.json()
    assert body["notebook_id"] == nb_id
    assert body["kind"] == "faq"
    assert body["document_id"]

    s = ts()
    try:
        doc = s.get(db.Document, body["document_id"])
        assert doc is not None and doc.current_content == "# inhoud"
    finally:
        s.close()


def test_generate_artifact_passes_focus_through(monkeypatch, ts):
    """The route reads `focus` from the body and forwards it as a keyword
    to generate_artifact (added in 8b8cdf0 for the mindmap focus-prompt)."""
    captured = {}

    async def _fake_generate_artifact_captures_focus(notebook_id, owner, kind, db_session, focus=None):
        captured["focus"] = focus
        return await _fake_generate_artifact_ok(notebook_id, owner, kind, db_session, focus=focus)

    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_captures_focus)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "mindmap", "focus": "budgetten"})
    assert r.status_code == 200
    assert captured["focus"] == "budgetten"


def test_generate_artifact_non_string_focus_is_400(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "mindmap", "focus": 123})
    assert r.status_code == 400


def test_generate_artifact_unknown_kind_is_400(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "not-a-kind"})
    assert r.status_code == 400


def test_generate_artifact_non_dict_json_body_is_400(monkeypatch, ts):
    """A syntactically valid JSON body that isn't an object (e.g. a bare
    array) must not 500 on `.get("kind")` - it should fall through to the
    same unknown-kind 400 as a missing/wrong kind."""
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/artifacts", json=[1, 2])
    assert r.status_code == 400


def test_generate_artifact_invalid_json_body_is_400(monkeypatch, ts):
    """Malformed JSON (request.json() raises) must not 500 either."""
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(
        f"/api/notebooks/{nb_id}/artifacts",
        content=b"{not valid json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


def test_generate_artifact_no_sources_is_400_with_message(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_no_sources)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"})
    assert r.status_code == 400
    assert "Geen geïndexeerde bronnen" in r.json()["detail"]


def test_generate_artifact_llm_failure_is_502(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_llm_failure)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"})
    assert r.status_code == 502
    assert "leeg antwoord" in r.json()["detail"]


def test_generate_artifact_cross_owner_is_404(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c_ed = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c_ed)
    c_eve = _client(monkeypatch, user="eve")

    r = c_eve.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"})
    assert r.status_code == 404


# ---- GET (list) ----

def test_list_artifacts_newest_first_with_title(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    first = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"}).json()
    second = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "quiz"}).json()

    # Both artifacts can land in the same wall-clock second under
    # utcnow_naive, which would make ordering coincidental rather than
    # verified. Force a real gap so "nieuwste eerst" is actually exercised.
    s = ts()
    try:
        import datetime
        first_row = s.get(db.NotebookArtifact, first["id"])
        second_row = s.get(db.NotebookArtifact, second["id"])
        first_row.created_at = datetime.datetime(2020, 1, 1)
        second_row.created_at = datetime.datetime(2021, 1, 1)
        s.commit()
    finally:
        s.close()

    r = c.get(f"/api/notebooks/{nb_id}/artifacts")
    assert r.status_code == 200
    artifacts = r.json()["artifacts"]
    assert [a["id"] for a in artifacts] == [second["id"], first["id"]]
    for a in artifacts:
        assert a["title"] == "Gegenereerd — " + a["kind"]


def test_list_artifacts_title_none_safe_when_document_missing(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    created = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"}).json()

    s = ts()
    try:
        # Simulate a pre-existing data inconsistency (e.g. legacy row) rather
        # than a reachable app code path: the notebook_artifacts.document_id
        # FK is ondelete=CASCADE and SQLite FK enforcement is on (see
        # core/database.py's "connect" event), so deleting the Document
        # through normal app code always takes the artifact row with it.
        # Turning enforcement off for this one connection lets the test
        # produce an orphaned artifact row to exercise the route's
        # None-safe title lookup.
        s.execute(text("PRAGMA foreign_keys=OFF"))
        doc = s.get(db.Document, created["document_id"])
        s.delete(doc)
        s.commit()
    finally:
        s.close()

    r = c.get(f"/api/notebooks/{nb_id}/artifacts")
    assert r.status_code == 200
    artifacts = r.json()["artifacts"]
    assert artifacts[0]["title"] is None


def test_list_artifacts_own_title_wins_over_document_title(monkeypatch, ts):
    """Once an artifact has its own title (set via rename or at creation),
    the list response must use it instead of falling back to the linked
    Document's title."""
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    created = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"}).json()

    s = ts()
    try:
        row = s.get(db.NotebookArtifact, created["id"])
        row.title = "Eigen titel"
        s.commit()
    finally:
        s.close()

    r = c.get(f"/api/notebooks/{nb_id}/artifacts")
    assert r.status_code == 200
    assert r.json()["artifacts"][0]["title"] == "Eigen titel"


def test_list_artifacts_cross_owner_is_404(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c_ed = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c_ed)
    c_eve = _client(monkeypatch, user="eve")

    assert c_eve.get(f"/api/notebooks/{nb_id}/artifacts").status_code == 404


# ---- DELETE (single artifact) ----

def test_delete_artifact_removes_row_and_document(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    created = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"}).json()

    r = c.delete(f"/api/notebooks/{nb_id}/artifacts/{created['id']}")
    assert r.status_code == 200

    assert c.get(f"/api/notebooks/{nb_id}/artifacts").json()["artifacts"] == []

    s = ts()
    try:
        assert s.get(db.Document, created["document_id"]) is None
        assert s.get(db.NotebookArtifact, created["id"]) is None
    finally:
        s.close()


def test_delete_artifact_cross_owner_is_404(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c_ed = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c_ed)
    created = c_ed.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"}).json()
    c_eve = _client(monkeypatch, user="eve")

    r = c_eve.delete(f"/api/notebooks/{nb_id}/artifacts/{created['id']}")
    assert r.status_code == 404

    s = ts()
    try:
        assert s.get(db.NotebookArtifact, created["id"]) is not None
    finally:
        s.close()


def test_delete_artifact_unknown_id_is_404(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    r = c.delete(f"/api/notebooks/{nb_id}/artifacts/does-not-exist")
    assert r.status_code == 404


# ---- PATCH (rename) ----

def test_rename_artifact_happy_path(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    created = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"}).json()

    r = c.patch(f"/api/notebooks/{nb_id}/artifacts/{created['id']}", json={"title": "Nieuwe titel"})
    assert r.status_code == 200
    assert r.json()["title"] == "Nieuwe titel"

    s = ts()
    try:
        row = s.get(db.NotebookArtifact, created["id"])
        assert row.title == "Nieuwe titel"
    finally:
        s.close()

    # And the list reflects it too.
    listed = c.get(f"/api/notebooks/{nb_id}/artifacts").json()["artifacts"]
    assert listed[0]["title"] == "Nieuwe titel"


def test_rename_artifact_strips_whitespace(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    created = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"}).json()

    r = c.patch(f"/api/notebooks/{nb_id}/artifacts/{created['id']}", json={"title": "  Padded  "})
    assert r.status_code == 200
    assert r.json()["title"] == "Padded"


def test_rename_artifact_empty_title_is_400(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    created = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"}).json()

    r = c.patch(f"/api/notebooks/{nb_id}/artifacts/{created['id']}", json={"title": "   "})
    assert r.status_code == 400


def test_rename_artifact_too_long_title_is_400(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    created = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"}).json()

    r = c.patch(f"/api/notebooks/{nb_id}/artifacts/{created['id']}", json={"title": "x" * 201})
    assert r.status_code == 400


def test_rename_artifact_missing_title_key_is_400(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    created = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"}).json()

    r = c.patch(f"/api/notebooks/{nb_id}/artifacts/{created['id']}", json={})
    assert r.status_code == 400


def test_rename_artifact_unknown_id_is_404(monkeypatch, ts):
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)
    r = c.patch(f"/api/notebooks/{nb_id}/artifacts/does-not-exist", json={"title": "X"})
    assert r.status_code == 404


def test_rename_artifact_cross_owner_is_404(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c_ed = _client(monkeypatch, user="ed")
    nb_id = _make_notebook(c_ed)
    created = c_ed.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"}).json()
    c_eve = _client(monkeypatch, user="eve")

    r = c_eve.patch(f"/api/notebooks/{nb_id}/artifacts/{created['id']}", json={"title": "Gekaapt"})
    assert r.status_code == 404

    s = ts()
    try:
        row = s.get(db.NotebookArtifact, created["id"])
        assert row.title is None
    finally:
        s.close()


def test_rename_artifact_foreign_notebook_is_404(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb1_id = _make_notebook(c, name="NB1")
    nb2_id = _make_notebook(c, name="NB2")
    created = c.post(f"/api/notebooks/{nb1_id}/artifacts", json={"kind": "faq"}).json()

    r = c.patch(f"/api/notebooks/{nb2_id}/artifacts/{created['id']}", json={"title": "X"})
    assert r.status_code == 404


# ---- notebook DELETE cleans up artifact Documents, not source Documents ----

def test_notebook_delete_removes_artifact_documents_but_not_source_documents(monkeypatch, ts):
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    files = [("files", ("good.txt", b"plain text content " * 50, "text/plain"))]
    src = c.post(f"/api/notebooks/{nb_id}/sources", files=files).json()["sources"][0]
    source_doc_id = src["document_id"]
    assert source_doc_id

    art = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": "faq"}).json()
    artifact_doc_id = art["document_id"]

    assert c.delete(f"/api/notebooks/{nb_id}").status_code == 200

    s = ts()
    try:
        assert s.get(db.Document, source_doc_id) is not None
        assert s.get(db.Document, artifact_doc_id) is None
        assert s.get(db.NotebookArtifact, art["id"]) is None
    finally:
        s.close()


@pytest.mark.parametrize("kind", [[1, 2, 3], {"a": 1}, 7, None, True])
def test_generate_artifact_unhashable_or_non_string_kind_is_400(monkeypatch, ts, kind):
    """ARTIFACT_KINDS is a dict, so `kind not in ARTIFACT_KINDS` raises
    TypeError: unhashable type on a list/dict kind — a 500 on plain bad
    client input. Every non-str kind must land on the same 400 as an
    unknown string kind."""
    monkeypatch.setattr(nbr, "generate_artifact", _fake_generate_artifact_ok)
    c = _client(monkeypatch)
    nb_id = _make_notebook(c)

    r = c.post(f"/api/notebooks/{nb_id}/artifacts", json={"kind": kind})
    assert r.status_code == 400
